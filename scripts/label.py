"""Grade pooled candidates by hand, 0-3, into eval/judgments.jsonl.

Human grades override the rubric in scripts/build_cases.py, so this is where the
evaluation stops being a proxy and starts reflecting what a shopper would accept.

    .venv/bin/python scripts/label.py --manage                # every relevance case
    .venv/bin/python scripts/label.py --case vague-01 --manage
    .venv/bin/python scripts/label.py --category typo --split dev --url URL

For each case the pool is the server's own top results plus, optionally, products the
rubric rates highly, so both hits and misses get seen. Enter accepts the suggested
grade, 0-3 overrides it, and the grade scale is:

    3  directly what was asked for
    2  useful but less direct
    1  weak connection
    0  irrelevant or unsupported
"""
from __future__ import annotations

import argparse
import json
import random
import sys
import time
from pathlib import Path
from typing import Any, Sequence

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'scripts'))

import bench as bench_module  # noqa: E402
import evalkit  # noqa: E402
from evalkit import (Case, append_judgments, load_cases, load_judgments,  # noqa: E402
                     rubric_grade, selector_matches)
from evaluate import call_all  # noqa: E402

HELP = ('  [Enter] accept suggestion   0-3 grade   s skip   '
        'a accept rest of case   n next case   q quit and save')


def describe(product: dict[str, Any]) -> str:
    rating = product['rating']['average']
    stars = f"{rating:.2f}" if rating is not None else ' null'
    return (f"{product['product_type'][:22]:<22} {product['color'][:14]:<14} "
            f"{product['pattern'][:12]:<12} INR {product['price']['amount']:>6.0f} "
            f"{stars:>5}/{product['rating']['count']:<2} {product['name'][:34]}")


def pool_for_case(case: Case, products_by_id: dict[str, Any],
                  products: Sequence[dict[str, Any]], url: str, size: int,
                  rubric_extra: int, seed: int) -> list[str]:
    """Server results first, then a sample of rubric-positive products not returned."""
    arguments = {**case.arguments, 'limit': min(100, max(size, 20)), 'offset': 0}
    arguments.pop('include_recommendations', None)
    responses = call_all(url, [arguments], 20.0)
    returned = [i for i in responses[0].ids if i in products_by_id] if responses else []

    pool = list(dict.fromkeys(returned))[:size]
    if rubric_extra and case.grades:
        positives = [p['id'] for p in products
                     if rubric_grade(p, case.grades) >= 2 and p['id'] not in set(pool)]
        random.Random(seed).shuffle(positives)
        pool.extend(positives[:rubric_extra])
    return pool


def label_case(case: Case, pool: Sequence[str], products_by_id: dict[str, Any],
               existing: dict[tuple[str, str], int]) -> tuple[list[dict[str, Any]], str]:
    """Walk the pool. Returns new judgment rows and a control verdict."""
    print(f"\n{'=' * 92}")
    print(f"{case.id}  [{case.category}/{case.split}]  "
          f"{json.dumps(case.arguments, ensure_ascii=False)}")
    if case.notes:
        print(f"  note: {case.notes}")
    if case.constraints:
        print(f"  hard constraints: {json.dumps(case.constraints)}")
    for tier in case.grades:
        print(f"  rubric {tier['grade']}: {json.dumps(tier['match'])}")
    print(HELP)
    print('=' * 92)

    rows: list[dict[str, Any]] = []
    accept_rest = False
    for position, product_id in enumerate(pool, 1):
        product = products_by_id[product_id]
        suggested = rubric_grade(product, case.grades)
        if case.constraints and not selector_matches(product, case.constraints):
            suggested = 0
        already = existing.get((case.id, product_id))
        marker = f' [human:{already}]' if already is not None else ''
        line = f'{position:>3}. {product_id}  {describe(product)}'
        if accept_rest:
            rows.append(_row(case.id, product_id, suggested, 'rubric-accepted'))
            continue
        print(f'{line}\n     suggested {suggested}{marker} > ', end='')
        try:
            answer = input().strip().lower()
        except EOFError:
            return rows, 'quit'
        if answer in ('q', 'quit'):
            return rows, 'quit'
        if answer in ('n', 'next'):
            return rows, 'next'
        if answer in ('s', 'skip'):
            continue
        if answer in ('a', 'all'):
            accept_rest = True
            rows.append(_row(case.id, product_id, suggested, 'rubric-accepted'))
            continue
        if answer == '':
            rows.append(_row(case.id, product_id, suggested, 'rubric-accepted'))
            continue
        if answer in ('0', '1', '2', '3'):
            rows.append(_row(case.id, product_id, int(answer), 'human'))
            continue
        print('     ? expected Enter, 0-3, s, a, n, or q')
    return rows, 'done'


def _row(case_id: str, product_id: str, grade: int, by: str) -> dict[str, Any]:
    return {'case': case_id, 'id': product_id, 'grade': grade, 'by': by,
            'at': time.strftime('%Y-%m-%dT%H:%M:%S%z')}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', default=None)
    parser.add_argument('--manage', action='store_true')
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8000)
    parser.add_argument('--catalog', type=Path, default=ROOT / 'data/products.json')
    parser.add_argument('--case', action='append', default=None)
    parser.add_argument('--category', action='append', default=None)
    parser.add_argument('--split', choices=('dev', 'heldout'), default=None)
    parser.add_argument('--pool', type=int, default=20,
                        help='server results to show per case (default 20)')
    parser.add_argument('--rubric-extra', type=int, default=5,
                        help='extra rubric-positive products the server missed')
    parser.add_argument('--seed', type=int, default=11)
    parser.add_argument('--skip-labelled', action='store_true',
                        help='skip cases that already have human grades')
    args = parser.parse_args(argv)

    cases = [c for c in load_cases() if c.grades]
    if args.case:
        cases = [c for c in cases if c.id in set(args.case)]
    if args.category:
        cases = [c for c in cases if c.category in set(args.category)]
    if args.split:
        cases = [c for c in cases if c.split == args.split]
    if not cases:
        raise SystemExit('no relevance cases selected')

    existing = load_judgments()
    if args.skip_labelled:
        labelled = {case_id for case_id, _ in existing}
        cases = [c for c in cases if c.id not in labelled]
        if not cases:
            print('every selected case already has human grades')
            return 0

    print(f'loading catalog from {args.catalog} ...')
    products = json.loads(args.catalog.read_text(encoding='utf-8'))
    products_by_id = {p['id']: p for p in products}

    base = args.url.rsplit('/mcp', 1)[0] if args.url else f'http://{args.host}:{args.port}'
    mcp_url = args.url or f'{base}/mcp'

    process = None
    written = 0
    try:
        if args.manage:
            process = bench_module.start_server(args.host, args.port,
                                                ROOT / 'analysis/label_server.log')
        bench_module.wait_for_health(f'{base}/healthz', 300.0, process)
        print(f'{len(cases)} cases to label against {mcp_url}')
        for case in cases:
            pool = pool_for_case(case, products_by_id, products, mcp_url, args.pool,
                                 args.rubric_extra, args.seed)
            if not pool:
                print(f'\n{case.id}: no candidates returned and no rubric positives; '
                      f'nothing to grade')
                continue
            rows, verdict = label_case(case, pool, products_by_id, existing)
            if rows:
                written += append_judgments(rows)
                for row in rows:
                    existing[(row['case'], row['id'])] = row['grade']
            if verdict == 'quit':
                break
    finally:
        if process is not None:
            bench_module.stop_server(process)

    print(f'\nwrote {written} judgments to {evalkit.JUDGMENTS_PATH}')
    print('re-run scripts/evaluate.py to score against them')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
