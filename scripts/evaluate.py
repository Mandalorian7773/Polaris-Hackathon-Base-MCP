"""Run eval/cases.jsonl through a live /mcp endpoint and score it honestly.

Reports nDCG@10, abstention, mixed-choice coverage, recommendation quality, the
four contract checks, latency, startup, and RSS, with dev and held-out split apart
and a weighted estimate using the official 25/30/15/15/10/5 mix.

    .venv/bin/python scripts/evaluate.py --manage
    .venv/bin/python scripts/evaluate.py --url http://127.0.0.1:8000/mcp

The weighted total is a LOCAL ESTIMATE, not an official score: relevance grades come
from the reviewable rubric in scripts/build_cases.py plus any human grades in
eval/judgments.jsonl, not from the organizer's Bedrock judge or hidden suite.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'scripts'))

from jsonschema import Draft202012Validator  # noqa: E402

from mcp import ClientSession  # noqa: E402
from mcp.client.streamable_http import streamable_http_client  # noqa: E402

import bench as bench_module  # noqa: E402
import evalkit  # noqa: E402
from evalkit import (AREA_POINTS, Case, Grader, load_cases, load_judgments,  # noqa: E402
                     mean, ndcg, selector_matches)
from search import OUTPUT_SCHEMA  # noqa: E402

OUTPUT_VALIDATOR = Draft202012Validator(OUTPUT_SCHEMA)
RUNS_DIR = ROOT / 'eval/runs'
PRIMARY_SLOTS = 10
RECOMMENDATION_SLOTS = 5
#: Functional points available before performance; the brief's gate is 85.5 of 95.
FUNCTIONAL_POINTS = 95.0
PERFORMANCE_GATE = 85.5


# --------------------------------------------------------------------------- #
# Transport
# --------------------------------------------------------------------------- #

@dataclass
class Response:
    ok: bool
    is_error: bool
    structured: dict[str, Any] | None
    text_matches: bool
    elapsed_ms: float
    failure: str = ''

    @property
    def results(self) -> list[dict[str, Any]]:
        return (self.structured or {}).get('results', [])

    @property
    def recommendations(self) -> list[dict[str, Any]]:
        return (self.structured or {}).get('recommendations', [])

    @property
    def ids(self) -> list[str]:
        return [row.get('id') for row in self.results]


async def _call_all(url: str, requests: Sequence[dict[str, Any]],
                    deadline_s: float) -> list[Response]:
    out: list[Response] = []
    async with streamable_http_client(url) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            for arguments in requests:
                started = time.perf_counter()
                try:
                    result = await asyncio.wait_for(
                        session.call_tool('search_products', arguments),
                        timeout=deadline_s)
                except asyncio.TimeoutError:
                    out.append(Response(False, False, None, False,
                                        (time.perf_counter() - started) * 1000,
                                        'deadline exceeded'))
                    continue
                except Exception as exc:
                    out.append(Response(False, False, None, False,
                                        (time.perf_counter() - started) * 1000,
                                        f'{type(exc).__name__}: {exc}'[:160]))
                    continue
                elapsed = (time.perf_counter() - started) * 1000
                structured = result.structuredContent
                text_matches = bool(result.content) and not result.isError and (
                    json.loads(result.content[0].text) == structured)
                out.append(Response(True, bool(result.isError), structured,
                                    text_matches, elapsed))
    return out


def call_all(url: str, requests: Sequence[dict[str, Any]],
             deadline_s: float) -> list[Response]:
    if not requests:
        return []
    return asyncio.run(_call_all(url, requests, deadline_s))


# --------------------------------------------------------------------------- #
# Independent reference page for exact empty-query filtering
# --------------------------------------------------------------------------- #

def reference_page(products: Sequence[dict[str, Any]],
                   arguments: dict[str, Any]) -> list[str]:
    """Brute-force expected page, written straight from the brief.

    Deliberately independent of search.py so it cross-checks the server rather
    than agreeing with it by construction. Only valid for an empty query.
    """
    filters = arguments.get('filters', {})
    rows = [p for p in products if _passes(p, filters)]
    sort_by = arguments.get('sort_by', 'relevance')
    descending = arguments.get('sort_order', 'desc') == 'desc'
    sign = -1 if descending else 1
    if sort_by == 'relevance':
        rows.sort(key=lambda p: p['id'])
    elif sort_by == 'id':
        rows.sort(key=lambda p: p['id'], reverse=descending)
    elif sort_by == 'price':
        rows.sort(key=lambda p: (sign * p['price']['amount'], p['id']))
    elif sort_by == 'review_count':
        rows.sort(key=lambda p: (sign * p['rating']['count'], p['id']))
    elif sort_by == 'rating':
        rows.sort(key=lambda p: (p['rating']['average'] is None,
                                 sign * (p['rating']['average'] or 0.0), p['id']))
    offset = int(arguments.get('offset', 0))
    limit = int(arguments.get('limit', 10))
    return [p['id'] for p in rows[offset:offset + limit]]


def _passes(product: dict[str, Any], filters: dict[str, Any]) -> bool:
    for key in ('category', 'product_type', 'color', 'audience', 'section', 'pattern'):
        if key in filters and evalkit.norm(product.get(key)) != evalkit.norm(filters[key]):
            return False
    price = product['price']['amount']
    average = product['rating']['average']
    if price < filters.get('min_price', 0) or price > filters.get('max_price', math.inf):
        return False
    if 'min_rating' in filters and (average is None or average < filters['min_rating']):
        return False
    return product['rating']['count'] >= filters.get('min_review_count', 0)


# --------------------------------------------------------------------------- #
# Per-case scoring
# --------------------------------------------------------------------------- #

@dataclass
class CaseResult:
    case_id: str
    category: str
    split: str
    area: str
    elapsed_ms: float
    checks: dict[str, bool | None] = field(default_factory=dict)
    validity: float | None = None
    ndcg: float | None = None
    coverage: float | None = None
    recommendation_score: float | None = None
    score: float | None = None
    empty_correct: bool | None = None
    grades: list[int] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items() if v not in (None, [], {})}


def _ordering_ok(case: Case, response: Response,
                 products_by_id: dict[str, dict[str, Any]]) -> bool:
    """Check ordering among the returned products for a field sort."""
    sort_by = case.arguments.get('sort_by', 'relevance')
    if sort_by == 'relevance':
        return True  # no organizer numeric relevance order is imposed
    descending = case.arguments.get('sort_order', 'desc') == 'desc'
    rows: list[tuple[bool, Any, str]] = []
    for product_id in response.ids:
        product = products_by_id.get(product_id)
        if product is None:
            return False
        if sort_by == 'id':
            rows.append((False, product_id, product_id))
        elif sort_by == 'price':
            rows.append((False, product['price']['amount'], product_id))
        elif sort_by == 'review_count':
            rows.append((False, product['rating']['count'], product_id))
        else:
            average = product['rating']['average']
            rows.append((average is None, average, product_id))

    for (null_a, value_a, id_a), (null_b, value_b, id_b) in zip(rows, rows[1:]):
        if null_a and not null_b:
            return False           # a null rating must never precede a rated product
        if null_a and null_b:
            if id_a > id_b:
                return False       # among nulls, ID ascending
            continue
        if not null_a and null_b:
            continue               # rated before null is exactly what we want
        if value_a == value_b:
            if id_a > id_b:
                return False       # ties break by ID ascending in both directions
        elif descending and value_a < value_b:
            return False
        elif not descending and value_a > value_b:
            return False
    return True


def score_case(case: Case, response: Response, probes: dict[str, Response],
               grader: Grader, products: Sequence[dict[str, Any]],
               products_by_id: dict[str, dict[str, Any]]) -> CaseResult:
    result = CaseResult(case_id=case.id, category=case.category, split=case.split,
                        area=case.area, elapsed_ms=round(response.elapsed_ms, 1))

    if not response.ok:
        result.failures.append(response.failure or 'transport failure')
        result.validity = 0.0
        result.score = 0.0
        return result

    if case.expect_error:
        result.validity = 1.0 if response.is_error else 0.0
        result.score = result.validity
        if not response.is_error:
            result.failures.append('invalid call succeeded')
        return result

    if response.is_error:
        result.failures.append('valid call returned an error')
        result.validity = 0.0
        result.score = 0.0
        return result

    primary_ids = response.ids
    # Check 1: schema, length, uniqueness, known IDs, recommendation references.
    structural = True
    if not response.structured or list(OUTPUT_VALIDATOR.iter_errors(response.structured)):
        structural = False
        result.failures.append('output schema violation')
    if not response.text_matches:
        structural = False
        result.failures.append('text content does not match structuredContent')
    if len(primary_ids) > case.arguments.get('limit', 10):
        structural = False
        result.failures.append('more results than limit')
    if len(set(primary_ids)) != len(primary_ids):
        structural = False
        result.failures.append('duplicate result ids')
    unknown = [i for i in primary_ids if i not in products_by_id]
    if unknown:
        structural = False
        result.failures.append(f'unknown result ids: {unknown[:3]}')
    structural = structural and _recommendation_structure_ok(case, response,
                                                             products_by_id, result)

    # Check 2: hard constraints on primary and recommendation products.
    constraints_ok = True
    explicit = case.arguments.get('filters', {})
    for product_id in primary_ids:
        product = products_by_id.get(product_id)
        if product is None or not _passes(product, explicit):
            constraints_ok = False
            result.failures.append(f'explicit filter violated by {product_id}')
            break
    if constraints_ok and case.constraints:
        for product_id in primary_ids:
            product = products_by_id.get(product_id)
            if product is None or not selector_matches(product, case.constraints):
                constraints_ok = False
                result.failures.append(f'query constraint violated by {product_id}')
                break
    complement_constraints = (case.complement or {}).get('constraints', {})
    if constraints_ok and complement_constraints:
        for row in response.recommendations:
            product = products_by_id.get(row.get('id'))
            if product is None or not selector_matches(product, complement_constraints):
                constraints_ok = False
                result.failures.append('recommendation constraint violated')
                break

    # Check 3: requested sorting and deterministic pagination.
    ordering_ok = _ordering_ok(case, response, products_by_id)
    if not ordering_ok:
        result.failures.append('ordering violates the requested sort')
    pagination_ok: bool | None = None
    repeat = probes.get('repeat')
    prefix = probes.get('prefix')
    if repeat is not None:
        pagination_ok = repeat.ok and not repeat.is_error and repeat.ids == primary_ids
        if not pagination_ok:
            result.failures.append('repeated identical request returned a different page')
    if prefix is not None:
        offset = int(case.arguments.get('offset', 0))
        limit = int(case.arguments.get('limit', 10))
        expected = prefix.ids[offset:offset + limit] if prefix.ok else None
        prefix_ok = prefix.ok and not prefix.is_error and expected == primary_ids
        if not prefix_ok:
            result.failures.append('page is not a prefix slice of the larger request')
        pagination_ok = prefix_ok if pagination_ok is None else (
            pagination_ok and prefix_ok)
    sorting_ok = ordering_ok and (pagination_ok is not False)

    # Check 4: expected presence or absence.
    if case.expect_empty:
        presence_ok = primary_ids == []
        result.empty_correct = presence_ok
        if not presence_ok:
            result.failures.append(f'expected an empty page, got {len(primary_ids)} rows')
    elif case.exact_page:
        expected = reference_page(products, case.arguments)
        presence_ok = primary_ids == expected
        if not presence_ok:
            result.failures.append('page differs from the exact catalog-filtered page')
    else:
        presence_ok = bool(primary_ids)
        if not presence_ok:
            result.failures.append('expected results, got an empty page')

    result.checks = {'structural': structural, 'constraints': constraints_ok,
                     'sorting_pagination': sorting_ok, 'presence': presence_ok}
    result.validity = mean(1.0 if v else 0.0 for v in result.checks.values())

    # Relevance, coverage, and recommendation quality.
    if case.grades:
        graded = []
        for product_id in primary_ids[:PRIMARY_SLOTS]:
            grade, _ = grader.grade(case.id, product_id, case.grades, case.constraints)
            graded.append(grade)
        result.grades = graded
        result.ndcg = round(ndcg(graded, PRIMARY_SLOTS), 6)
    if case.choices:
        result.coverage = round(_coverage(case, primary_ids, grader), 6)
    if case.category == 'complement':
        result.recommendation_score = round(
            _recommendation_quality(case, response, grader, products_by_id), 6)

    result.score = _area_score(case, result)
    return result


def _recommendation_structure_ok(case: Case, response: Response,
                                 products_by_id: dict[str, dict[str, Any]],
                                 result: CaseResult) -> bool:
    enabled = case.arguments.get('include_recommendations', False)
    recommendations = response.recommendations
    primary = set(response.ids)
    if not enabled:
        if recommendations:
            result.failures.append('recommendations returned while disabled')
            return False
        return True
    if recommendations and not response.ids:
        result.failures.append('recommendations returned for an empty result list')
        return False
    seen: set[str] = set()
    for row in recommendations:
        product_id = row.get('id')
        if product_id not in products_by_id:
            result.failures.append(f'unknown recommendation id: {product_id}')
            return False
        if product_id in primary:
            result.failures.append('recommendation duplicates a primary result')
            return False
        if product_id in seen:
            result.failures.append('duplicate recommendation id')
            return False
        if row.get('for_product_id') not in primary:
            result.failures.append('for_product_id is not on the current page')
            return False
        if not 1 <= len(row.get('reason', '')) <= 300:
            result.failures.append('reason length out of bounds')
            return False
        seen.add(product_id)
    return True


def _coverage(case: Case, primary_ids: Sequence[str], grader: Grader) -> float:
    covered = 0
    for choice in case.choices:
        for product_id in primary_ids[:PRIMARY_SLOTS]:
            product = grader.product(product_id)
            if product is None or not selector_matches(product, choice['match']):
                continue
            grade, _ = grader.grade(case.id, product_id, case.grades, case.constraints)
            if grade >= 2:
                covered += 1
                break
    return covered / len(case.choices) if case.choices else 0.0


def _recommendation_quality(case: Case, response: Response, grader: Grader,
                            products_by_id: dict[str, dict[str, Any]]) -> float:
    rubric = (case.complement or {}).get('grades', [])
    constraints = (case.complement or {}).get('constraints', {})
    recommendations = response.recommendations
    if not rubric:
        # The case declares no available complement: an empty list earns full credit.
        return 1.0 if not recommendations else 0.0
    primary_grade = {}
    for product_id in response.ids:
        primary_grade[product_id] = grader.grade(case.id, product_id, case.grades,
                                                 case.constraints)[0]
    graded: list[int] = []
    for row in recommendations[:RECOMMENDATION_SLOTS]:
        anchor = row.get('for_product_id')
        if primary_grade.get(anchor, 0) < 2:
            graded.append(0)       # must reference a primary with grade >= 2
            continue
        grade, _ = grader.grade(case.id, row.get('id'), rubric, constraints)
        graded.append(grade)
    return ndcg(graded, RECOMMENDATION_SLOTS)


def _area_score(case: Case, result: CaseResult) -> float:
    if case.area == 'validity':
        return result.validity or 0.0
    if case.area == 'irrelevant':
        return 1.0 if result.empty_correct else 0.0
    if case.area == 'mixed':
        return 0.5 * (result.coverage or 0.0) + 0.5 * (result.ndcg or 0.0)
    if case.area == 'complement':
        return 0.5 * (result.ndcg or 0.0) + 0.5 * (result.recommendation_score or 0.0)
    return result.ndcg or 0.0


# --------------------------------------------------------------------------- #
# Run
# --------------------------------------------------------------------------- #

def build_requests(case: Case) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    """The case call plus its pagination probes, which share the case deadline."""
    probes: dict[str, dict[str, Any]] = {}
    if case.pagination_probe and not case.expect_error:
        probes['repeat'] = dict(case.arguments)
        offset = int(case.arguments.get('offset', 0))
        limit = int(case.arguments.get('limit', 10))
        if offset + limit <= 100:
            probes['prefix'] = {**case.arguments, 'offset': 0, 'limit': offset + limit}
    return dict(case.arguments), probes


def run_cases(url: str, cases: Sequence[Case], grader: Grader,
              products: Sequence[dict[str, Any]],
              products_by_id: dict[str, dict[str, Any]],
              deadline_s: float) -> list[CaseResult]:
    requests: list[dict[str, Any]] = []
    layout: list[tuple[Case, int, dict[str, int]]] = []
    for case in cases:
        arguments, probes = build_requests(case)
        index = len(requests)
        requests.append(arguments)
        probe_index = {}
        for name, probe_arguments in probes.items():
            probe_index[name] = len(requests)
            requests.append(probe_arguments)
        layout.append((case, index, probe_index))

    responses = call_all(url, requests, deadline_s)
    results = []
    for case, index, probe_index in layout:
        probes = {name: responses[position] for name, position in probe_index.items()}
        results.append(score_case(case, responses[index], probes, grader, products,
                                  products_by_id))
    return results


def summarise(results: Sequence[CaseResult], split: str | None = None) -> dict[str, Any]:
    rows = [r for r in results if split is None or r.split == split]
    by_area: dict[str, list[CaseResult]] = {}
    for row in rows:
        by_area.setdefault(row.area, []).append(row)

    areas: dict[str, Any] = {}
    for area, items in sorted(by_area.items()):
        areas[area] = {
            'cases': len(items),
            'mean_score': round(mean(i.score or 0.0 for i in items), 6),
            'points': round(AREA_POINTS[area] * mean(i.score or 0.0 for i in items), 3),
            'max_points': AREA_POINTS[area],
        }
    relevance = [r for r in rows if r.ndcg is not None]
    irrelevant = [r for r in rows if r.area == 'irrelevant']
    mixed = [r for r in rows if r.area == 'mixed']
    complements = [r for r in rows if r.area == 'complement']
    validity = [r for r in rows if r.validity is not None]

    return {
        'cases': len(rows),
        'areas': areas,
        'functional_points': round(sum(a['points'] for a in areas.values()), 3),
        'functional_max': round(sum(a['max_points'] for a in areas.values()), 3),
        'mean_ndcg_at_10': round(mean(r.ndcg for r in relevance), 6) if relevance else None,
        'abstention_rate': round(mean(1.0 if r.empty_correct else 0.0
                                      for r in irrelevant), 6) if irrelevant else None,
        'false_positive_rate': round(mean(0.0 if r.empty_correct else 1.0
                                          for r in irrelevant), 6) if irrelevant else None,
        'mixed_coverage': round(mean(r.coverage or 0.0 for r in mixed), 6) if mixed else None,
        'recommendation_quality': round(mean(r.recommendation_score or 0.0
                                             for r in complements), 6)
                                  if complements else None,
        'contract_pass_rate': round(mean(1.0 if (r.validity or 0.0) == 1.0 else 0.0
                                         for r in validity), 6) if validity else None,
        'mean_validity': round(mean(r.validity or 0.0 for r in validity), 6)
                         if validity else None,
        'cases_with_failures': sorted(r.case_id for r in rows if r.failures),
        'p50_case_ms': round(evalkit.percentile([r.elapsed_ms for r in rows], 50), 1)
                       if rows else None,
        'p95_case_ms': round(evalkit.percentile([r.elapsed_ms for r in rows], 95), 1)
                       if rows else None,
    }


def performance_points(functional_points: float, bench_report: dict[str, Any] | None
                       ) -> dict[str, Any]:
    if not bench_report:
        return {'eligible': False, 'points': 0.0, 'reason': 'no bench measured'}
    pooled = bench_report['pooled']
    p95 = pooled['p95_ms']
    correct = pooled['correct_response_rate']
    scale = max(0.0, min(1.0, (2000 - p95) / 1800))
    eligible = functional_points >= PERFORMANCE_GATE and correct >= 0.95
    return {
        'eligible': eligible,
        'gate': f'>= {PERFORMANCE_GATE} of {FUNCTIONAL_POINTS} functional points '
                f'and >= 95% correct calls',
        'p95_ms': p95,
        'correct_response_rate': correct,
        'latency_scale': round(scale, 6),
        'points': round(AREA_POINTS['performance'] * correct * scale, 3) if eligible else 0.0,
    }


def git_sha() -> str:
    try:
        return subprocess.run(['git', 'rev-parse', '--short', 'HEAD'], cwd=str(ROOT),
                              capture_output=True, text=True, check=True).stdout.strip()
    except (subprocess.CalledProcessError, OSError):
        return 'nogit'


def print_report(report: dict[str, Any]) -> None:
    print('=' * 78)
    print(f"cases {report['overall']['cases']}   endpoint {report['url']}   "
          f"git {report['git_sha']}")
    judged = report['judgments']
    print(f"grades: {judged['human']} human, rubric for the rest "
          f"({judged['path']})")
    server = report.get('server') or {}
    if server.get('startup_seconds') is not None:
        print(f"startup {server['startup_seconds']}s   "
              f"RSS {server.get('rss_mb_after_startup', '?')} MB   "
              f"peak {server.get('peak_rss_mb', '?')} MB")
    print('=' * 78)

    header = f"{'area':<14}{'cases':>6}{'mean':>9}{'points':>10}{'max':>7}"
    for split in ('dev', 'heldout', 'overall'):
        block = report[split]
        print(f'\n--- {split} ---')
        print(header)
        for area, stats in block['areas'].items():
            print(f"{area:<14}{stats['cases']:>6}{stats['mean_score']:>9.4f}"
                  f"{stats['points']:>10.3f}{stats['max_points']:>7}")
        print(f"{'functional':<14}{block['cases']:>6}{'':>9}"
              f"{block['functional_points']:>10.3f}{block['functional_max']:>7}")
        for label, key in (('nDCG@10', 'mean_ndcg_at_10'),
                           ('abstention', 'abstention_rate'),
                           ('false positives', 'false_positive_rate'),
                           ('mixed coverage', 'mixed_coverage'),
                           ('rec quality@5', 'recommendation_quality'),
                           ('mean validity', 'mean_validity'),
                           ('contract pass', 'contract_pass_rate')):
            if block.get(key) is not None:
                print(f'  {label:<18} {block[key]:.4f}')

    performance = report['performance']
    print(f"\n--- performance ---")
    print(f"  p95 {performance.get('p95_ms')} ms   correct "
          f"{performance.get('correct_response_rate')}   eligible "
          f"{performance['eligible']}   points {performance['points']}")
    print(f"\nweighted local estimate: {report['estimate']['total']:.2f} / 100 "
          f"(functional {report['estimate']['functional']:.2f} / 95, "
          f"performance {report['estimate']['performance']:.2f} / 5)")
    print('NOT an official score: grades come from the local rubric, not Bedrock.')

    failures = report['overall']['cases_with_failures']
    if failures:
        print(f"\ncases with failures ({len(failures)}):")
        for row in report['per_case']:
            if row.get('failures'):
                print(f"  {row['case_id']:<14} {row['category']:<12} "
                      f"{'; '.join(row['failures'])[:110]}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', default=None)
    parser.add_argument('--manage', action='store_true',
                        help='start and stop server.py, measuring startup and RSS')
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8000)
    parser.add_argument('--catalog', type=Path, default=ROOT / 'data/products.json')
    parser.add_argument('--cases', type=Path, default=evalkit.CASES_PATH)
    parser.add_argument('--deadline', type=float, default=10.0)
    parser.add_argument('--category', action='append', default=None,
                        help='only run these case categories')
    parser.add_argument('--split', choices=('dev', 'heldout'), default=None)
    parser.add_argument('--no-bench', action='store_true',
                        help='skip the latency pool')
    parser.add_argument('--bench-calls', type=int, default=100)
    parser.add_argument('--out', type=Path, default=None)
    args = parser.parse_args(argv)

    cases = load_cases(args.cases)
    if args.category:
        cases = [c for c in cases if c.category in set(args.category)]
    if args.split:
        cases = [c for c in cases if c.split == args.split]
    if not cases:
        raise SystemExit('no cases selected')

    print(f'loading catalog from {args.catalog} ...')
    products = json.loads(args.catalog.read_text(encoding='utf-8'))
    products_by_id = {p['id']: p for p in products}
    judgments = load_judgments()
    grader = Grader(products_by_id, judgments)

    base = args.url.rsplit('/mcp', 1)[0] if args.url else f'http://{args.host}:{args.port}'
    mcp_url = args.url or f'{base}/mcp'
    health_url = f'{base}/healthz'

    server_info: dict[str, Any] = {'managed': args.manage}
    process = None
    bench_report = None
    try:
        if args.manage:
            process = bench_module.start_server(args.host, args.port,
                                                ROOT / 'analysis/eval_server.log')
        startup, health = bench_module.wait_for_health(health_url, 300.0, process)
        server_info['startup_seconds'] = round(startup, 2)
        server_info['products'] = health.get('products')
        if process:
            server_info['rss_mb_after_startup'] = round(
                (bench_module.read_rss_kb(process.pid) or 0) / 1024, 1)

        print(f'running {len(cases)} cases against {mcp_url} ...')
        results = run_cases(mcp_url, cases, grader, products, products_by_id,
                            args.deadline)

        if not args.no_bench:
            print('running the latency pool ...')
            bench_report = asyncio.run(bench_module.bench(mcp_url, args.bench_calls,
                                                          20, args.deadline))
        if process:
            server_info['rss_mb_after_run'] = round(
                (bench_module.read_rss_kb(process.pid) or 0) / 1024, 1)
            server_info['peak_rss_mb'] = round(
                (bench_module.peak_rss_kb(process.pid) or 0) / 1024, 1)
    finally:
        if process is not None:
            bench_module.stop_server(process)

    overall = summarise(results)
    performance = performance_points(overall['functional_points'], bench_report)
    report = {
        'generated_at': time.strftime('%Y-%m-%dT%H:%M:%S%z'),
        'git_sha': git_sha(),
        'url': mcp_url,
        'cases_path': str(args.cases),
        'judgments': {'human': len(judgments), 'path': str(evalkit.JUDGMENTS_PATH)},
        'server': server_info,
        'dev': summarise(results, 'dev'),
        'heldout': summarise(results, 'heldout'),
        'overall': overall,
        'performance': performance,
        'bench': bench_report,
        'estimate': {
            'functional': overall['functional_points'],
            'performance': performance['points'],
            'total': round(overall['functional_points'] + performance['points'], 2),
            'disclaimer': 'local rubric-based estimate, not an official score',
        },
        'per_case': [r.as_dict() for r in results],
    }

    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime('%Y%m%dT%H%M%S')
    out = args.out or RUNS_DIR / f'{stamp}_{report["git_sha"]}.json'
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2), encoding='utf-8')

    print_report(report)
    print(f'\nrun -> {out}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
