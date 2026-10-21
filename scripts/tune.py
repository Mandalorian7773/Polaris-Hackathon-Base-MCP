"""Sweep ranking parameters on DEV cases only, then report held-out separately.

Runs in process (no server) and swaps parameters without rebuilding the index, so a
sweep costs seconds per configuration instead of a minute. Semantic areas only:
validity and exact-page cases do not depend on ranking parameters.

    .venv/bin/python scripts/tune.py --baseline
    .venv/bin/python scripts/tune.py --sweep no_anchor_coverage=0.5,0.6,0.75,0.9
    .venv/bin/python scripts/tune.py --descend          # coordinate descent, default axes

Tuning on dev and reporting held out is the point: a gain that does not survive the
held-out split is parameter fitting, not retrieval.
"""
from __future__ import annotations

import argparse
import copy
import json
import sys
import time
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any, Sequence

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'scripts'))

import evalkit  # noqa: E402
from evalkit import AREA_POINTS, Case, Grader, load_cases, load_judgments, mean  # noqa: E402
from evaluate import Response, score_case  # noqa: E402
from retrieval import RankingParams  # noqa: E402
from search import Catalog  # noqa: E402

#: Areas whose scores move when ranking parameters change.
SEMANTIC_AREAS = ('ranking', 'unusual', 'irrelevant', 'mixed', 'complement')

#: Default coordinate-descent axes, cheapest and most decisive first.
DEFAULT_AXES: tuple[tuple[str, tuple[Any, ...]], ...] = (
    ('no_anchor_coverage', (0.4, 0.5, 0.6, 0.75, 0.9)),
    ('min_coverage', (0.2, 0.34, 0.45, 0.6)),
    ('coverage_slack', (0, 1, 2)),
    ('coverage_exponent', (1.0, 1.5, 2.0, 3.0)),
    ('score_floor', (0.02, 0.05, 0.08, 0.15, 0.25)),
    ('synonym_boost', (4.0, 7.0, 10.0, 14.0)),
    ('intent_boost', (1.0, 2.5, 4.0, 6.0)),
    ('typo_penalty', (0.4, 0.6, 0.8, 1.0)),
    ('typo_min_df', (1, 8, 30)),
    ('desc_df_share', (0.04, 0.12, 0.3, 1.0)),
    ('type_weight', (5.0, 9.0, 14.0, 20.0)),
    ('attr_weight', (2.0, 3.5, 6.0)),
    ('name_weight', (1.0, 2.0, 4.0)),
    ('desc_weight', (0.5, 1.0, 2.0)),
    ('k1', (0.6, 1.2, 2.0)),
)

WEIGHT_AXES = {'type_weight': 0, 'cat_weight': 1, 'attr_weight': 2,
               'name_weight': 3, 'desc_weight': 4}


def with_axis(params: RankingParams, axis: str, value: Any) -> RankingParams:
    """Set one axis, including the per-group weights that live inside a tuple."""
    if axis in WEIGHT_AXES:
        weights = list(params.weights)
        weights[WEIGHT_AXES[axis]] = float(value)
        return replace(params, weights=tuple(weights))
    return replace(params, **{axis: value})


def local_response(catalog: Catalog, arguments: dict[str, Any]) -> Response:
    started = time.perf_counter()
    try:
        structured = catalog.search(copy.deepcopy(arguments))
    except ValueError:
        return Response(True, True, None, False, (time.perf_counter() - started) * 1000)
    return Response(True, False, structured, True,
                    (time.perf_counter() - started) * 1000)


def evaluate_params(catalog: Catalog, params: RankingParams, cases: Sequence[Case],
                    grader: Grader, products: Sequence[dict[str, Any]],
                    products_by_id: dict[str, dict[str, Any]]) -> dict[str, Any]:
    catalog.set_params(params)
    results = [score_case(case, local_response(catalog, case.arguments), {}, grader,
                          products, products_by_id) for case in cases]

    by_split: dict[str, Any] = {}
    for split in ('dev', 'heldout'):
        rows = [r for r in results if r.split == split]
        areas = {}
        for area in SEMANTIC_AREAS:
            items = [r for r in rows if r.area == area]
            if items:
                areas[area] = round(mean(i.score or 0.0 for i in items), 4)
        by_split[split] = {
            'areas': areas,
            'points': round(sum(AREA_POINTS[a] * v for a, v in areas.items()), 3),
            'ndcg': round(mean(r.ndcg for r in rows if r.ndcg is not None), 4),
            'abstention': round(mean(1.0 if r.empty_correct else 0.0
                                     for r in rows if r.area == 'irrelevant'), 4),
            'coverage': round(mean(r.coverage or 0.0
                                   for r in rows if r.coverage is not None), 4),
            'empty_valid_cases': sorted(
                r.case_id for r in rows
                if r.area in ('ranking', 'unusual', 'mixed', 'complement')
                and any('empty page' in f for f in r.failures)),
        }
    by_split['latency_p95_ms'] = round(
        evalkit.percentile([r.elapsed_ms for r in results], 95), 1)
    return by_split


def objective(report: dict[str, Any]) -> float:
    return report['dev']['points']


def print_row(label: str, report: dict[str, Any]) -> None:
    dev, held = report['dev'], report['heldout']
    print(f"{label:<34} dev {dev['points']:>7.3f}  held {held['points']:>7.3f}   "
          f"ndcg {dev['ndcg']:.3f}/{held['ndcg']:.3f}  "
          f"abst {dev['abstention']:.2f}/{held['abstention']:.2f}  "
          f"cov {dev['coverage']:.2f}/{held['coverage']:.2f}  "
          f"p95 {report['latency_p95_ms']:.0f}ms")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--catalog', type=Path, default=ROOT / 'data/products.json')
    parser.add_argument('--baseline', action='store_true',
                        help='score the current defaults only')
    parser.add_argument('--sweep', action='append', default=None,
                        metavar='AXIS=v1,v2,...', help='sweep one axis independently')
    parser.add_argument('--descend', action='store_true',
                        help='coordinate descent over the default axes')
    parser.add_argument('--rounds', type=int, default=2)
    parser.add_argument('--out', type=Path, default=ROOT / 'analysis/tuning.json')
    args = parser.parse_args(argv)

    cases = [c for c in load_cases() if c.area in SEMANTIC_AREAS]
    print(f'loading catalog from {args.catalog} ...')
    products = json.loads(args.catalog.read_text(encoding='utf-8'))
    products_by_id = {p['id']: p for p in products}
    grader = Grader(products_by_id, load_judgments())

    started = time.perf_counter()
    catalog = Catalog(products)
    print(f'index built in {time.perf_counter() - started:.1f}s; '
          f'{len(cases)} semantic cases '
          f'({sum(1 for c in cases if c.split == "dev")} dev)')

    def run(params: RankingParams) -> dict[str, Any]:
        return evaluate_params(catalog, params, cases, grader, products, products_by_id)

    best = RankingParams()
    base_report = run(best)
    print_row('baseline', base_report)
    history = [{'label': 'baseline', 'params': asdict(best), 'report': base_report}]

    if args.sweep:
        for spec in args.sweep:
            axis, _, raw = spec.partition('=')
            for text in raw.split(','):
                value: Any = json.loads(text) if text[0].isdigit() or text[0] == '-' \
                    else text
                report = run(with_axis(best, axis.strip(), value))
                print_row(f'{axis}={value}', report)
                history.append({'label': f'{axis}={value}', 'report': report})

    if args.descend:
        best_score = objective(base_report)
        for round_number in range(1, args.rounds + 1):
            print(f'\n=== coordinate descent round {round_number} ===')
            improved = False
            for axis, values in DEFAULT_AXES:
                local_best, local_report = best, None
                for value in values:
                    candidate = with_axis(best, axis, value)
                    if candidate == best:
                        continue
                    report = run(candidate)
                    if objective(report) > best_score + 1e-9:
                        best_score, local_best, local_report = (
                            objective(report), candidate, report)
                if local_report is not None:
                    best = local_best
                    improved = True
                    print_row(f'  -> {axis} improved', local_report)
                    history.append({'label': f'descent {axis}',
                                    'params': asdict(best), 'report': local_report})
            if not improved:
                print('  no axis improved; stopping')
                break
        final = run(best)
        print('\n=== final ===')
        print_row('tuned', final)
        print(json.dumps(asdict(best), indent=2))
        history.append({'label': 'tuned', 'params': asdict(best), 'report': final})

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(history, indent=2), encoding='utf-8')
    print(f'\ntuning log -> {args.out}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
