"""Throwaway spike: is SQLite FTS5 a better lexical core than in-process BM25F?

Compares the two *cores* on the same documents and the same query terms:
build cost, on-disk/in-memory footprint, warm latency, and ranking quality on the
relevance cases. The lexicon layer (synonym and intent expansion, spelling
correction, attribute anchoring, abstention) is disabled on both sides so the
comparison is about retrieval, not about the layer above it.

    .venv/bin/python scripts/spike_fts5.py

This script exists as evidence for the decision recorded in PROGRESS.md. It is not a
production path: only one retrieval implementation ships.
"""
from __future__ import annotations

import argparse
import json
import math
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any, Sequence

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'scripts'))

import evalkit  # noqa: E402
from evalkit import Grader, load_cases, load_judgments, mean, ndcg  # noqa: E402
from retrieval import GROUPS, GROUP_SOURCES, Lexicon, RankingParams, tokenize  # noqa: E402
from search import Catalog  # noqa: E402

COLUMNS = ('ptype', 'cat', 'attr', 'name', 'descr')
RELEVANCE_AREAS = ('ranking', 'unusual')


def document_columns(product: dict[str, Any]) -> tuple[str, ...]:
    return tuple(' '.join(str(product.get(source) or '') for source in
                          GROUP_SOURCES[group]) for group in GROUPS)


def build_fts5(products: Sequence[dict[str, Any]], path: Path) -> dict[str, Any]:
    if path.exists():
        path.unlink()
    started = time.perf_counter()
    connection = sqlite3.connect(path)
    connection.execute(
        f"CREATE VIRTUAL TABLE docs USING fts5({', '.join(COLUMNS)}, "
        f"tokenize='unicode61')")
    connection.executemany(
        f"INSERT INTO docs(rowid, {', '.join(COLUMNS)}) "
        f"VALUES (?, ?, ?, ?, ?, ?)",
        ((position, *document_columns(product))
         for position, product in enumerate(products)))
    connection.commit()
    connection.execute("INSERT INTO docs(docs) VALUES('optimize')")
    connection.commit()
    build_seconds = time.perf_counter() - started
    return {'connection': connection, 'build_seconds': round(build_seconds, 2),
            'bytes': path.stat().st_size}


def fts5_search(connection: sqlite3.Connection, terms: Sequence[str],
                weights: Sequence[float], limit: int) -> list[int]:
    if not terms:
        return []
    expression = ' OR '.join(f'"{term}"' for term in terms)
    weight_sql = ', '.join(str(float(w)) for w in weights)
    try:
        rows = connection.execute(
            f'SELECT rowid FROM docs WHERE docs MATCH ? '
            f'ORDER BY bm25(docs, {weight_sql}) LIMIT ?',
            (expression, limit)).fetchall()
    except sqlite3.OperationalError:
        return []
    return [row[0] for row in rows]


def query_terms(text: str, lexicon: Lexicon) -> list[str]:
    return [t for t in tokenize(text) if t not in lexicon.stopwords]


def bm25f_core_search(catalog: Catalog, terms: Sequence[str], limit: int) -> list[int]:
    """BM25F term scoring only: no expansion, no anchoring, no abstention."""
    from retrieval import PlannedQuery, Requirement
    plan = PlannedQuery(
        requirements=tuple(Requirement(source=t, terms=((t, 1.0),)) for t in terms),
        has_anchor=True)
    scores = catalog.retriever.score_plan(plan)
    return [position for position, _ in
            sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))[:limit]]


def grade_page(case, positions: Sequence[int], products: Sequence[dict[str, Any]],
               grader: Grader) -> float:
    grades = []
    for position in positions[:10]:
        grade, _ = grader.grade(case.id, products[position]['id'], case.grades,
                                case.constraints)
        grades.append(grade)
    return ndcg(grades, 10)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--catalog', type=Path, default=ROOT / 'data/products.json')
    parser.add_argument('--db', type=Path, default=ROOT / 'analysis/spike_fts5.sqlite')
    parser.add_argument('--repeats', type=int, default=5)
    parser.add_argument('--keep-db', action='store_true')
    parser.add_argument('--out', type=Path, default=ROOT / 'analysis/spike_fts5.json')
    args = parser.parse_args(argv)

    print(f'loading catalog from {args.catalog} ...')
    products = json.loads(args.catalog.read_text(encoding='utf-8'))
    products_by_id = {p['id']: p for p in products}
    grader = Grader(products_by_id, load_judgments())
    cases = [c for c in load_cases() if c.area in RELEVANCE_AREAS and c.grades]
    lexicon = Lexicon.load()
    params = RankingParams()

    started = time.perf_counter()
    catalog = Catalog(products)
    bm25f_build = time.perf_counter() - started
    print(f'in-process index built in {bm25f_build:.1f}s')

    fts5 = build_fts5(products, args.db)
    print(f"FTS5 index built in {fts5['build_seconds']}s, "
          f"{fts5['bytes'] / 1e6:.0f} MB on disk")

    weights = [params.weight(group) for group in GROUPS]
    bm25f_ndcg, fts5_ndcg = [], []
    bm25f_times, fts5_times = [], []
    for case in cases:
        terms = query_terms(case.arguments.get('query', ''), lexicon)

        catalog._score_cache.clear()
        start = time.perf_counter()
        bm25f_page = bm25f_core_search(catalog, terms, 10)
        bm25f_times.append((time.perf_counter() - start) * 1000)

        start = time.perf_counter()
        fts5_page = fts5_search(fts5['connection'], terms, weights, 10)
        fts5_times.append((time.perf_counter() - start) * 1000)

        bm25f_ndcg.append(grade_page(case, bm25f_page, products, grader))
        fts5_ndcg.append(grade_page(case, fts5_page, products, grader))

    for _ in range(args.repeats - 1):
        for case in cases:
            terms = query_terms(case.arguments.get('query', ''), lexicon)
            start = time.perf_counter()
            fts5_search(fts5['connection'], terms, weights, 10)
            fts5_times.append((time.perf_counter() - start) * 1000)
            catalog._score_cache.clear()
            start = time.perf_counter()
            bm25f_core_search(catalog, terms, 10)
            bm25f_times.append((time.perf_counter() - start) * 1000)

    report = {
        'cases': len(cases),
        'in_process_bm25f': {
            'build_seconds': round(bm25f_build, 2),
            'mean_ndcg_at_10': round(mean(bm25f_ndcg), 4),
            'p50_ms': round(evalkit.percentile(bm25f_times, 50), 2),
            'p95_ms': round(evalkit.percentile(bm25f_times, 95), 2),
        },
        'sqlite_fts5': {
            'build_seconds': fts5['build_seconds'],
            'db_megabytes': round(fts5['bytes'] / 1e6, 1),
            'mean_ndcg_at_10': round(mean(fts5_ndcg), 4),
            'p50_ms': round(evalkit.percentile(fts5_times, 50), 2),
            'p95_ms': round(evalkit.percentile(fts5_times, 95), 2),
        },
        'note': ('cores only: synonym and intent expansion, spelling correction, '
                 'attribute anchoring and abstention are disabled on both sides'),
    }
    fts5['connection'].close()
    if not args.keep_db:
        args.db.unlink(missing_ok=True)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2), encoding='utf-8')

    print()
    print(f"{'core':<22}{'build s':>9}{'nDCG@10':>10}{'p50 ms':>9}{'p95 ms':>9}")
    for label, key in (('in-process BM25F', 'in_process_bm25f'),
                       ('SQLite FTS5', 'sqlite_fts5')):
        block = report[key]
        print(f"{label:<22}{block['build_seconds']:>9}{block['mean_ndcg_at_10']:>10.4f}"
              f"{block['p50_ms']:>9.2f}{block['p95_ms']:>9.2f}")
    print(f"\nFTS5 database: {report['sqlite_fts5']['db_megabytes']} MB on disk")
    print(f"report -> {args.out}")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
