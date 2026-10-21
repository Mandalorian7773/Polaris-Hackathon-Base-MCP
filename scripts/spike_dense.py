"""Phase 7 experiment: does dense retrieval beat the lexical system we have?

Compares four methods on the same cases, the same hard-constraint masks and the same
rubric, reporting dev and held out separately:

    bm25    the production BM25F selection, unchanged
    dense   cosine over precomputed bge-small-en-v1.5 vectors
    rrf     reciprocal rank fusion of the two
    fusion  min-max normalised weighted sum of the two

Abstention is re-calibrated per method rather than inherited, because a similarity
threshold is model-dependent and the whole risk of dense retrieval is that it returns
neighbours for nonsense.

    .venv-embed/bin/python scripts/spike_dense.py

This is an experiment, not a production path. The decision it feeds is recorded in
PROGRESS.md; if the gate fails, the dependency never enters requirements.txt.
"""
from __future__ import annotations

import argparse
import json
import resource
import sys
import time
from pathlib import Path
from typing import Any, Sequence

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'scripts'))

import evalkit  # noqa: E402
from evalkit import Case, Grader, load_cases, load_judgments, mean, ndcg  # noqa: E402
from search import Catalog, parse_request, tokens  # noqa: E402

EMBED_DIR = ROOT / 'analysis/embeddings'
SLUG = 'bge-small-en-v1.5'
RELEVANCE_AREAS = ('ranking', 'unusual')
PRIMARY_SLOTS = 10
RRF_K = 60


def load_vectors(slug: str = SLUG):
    import numpy as np
    meta = json.loads((EMBED_DIR / f'{slug}.meta.json').read_text(encoding='utf-8'))
    ids = json.loads((EMBED_DIR / f'{slug}.ids.json').read_text(encoding='utf-8'))
    matrix = np.load(EMBED_DIR / f'{slug}.f16.npy')
    return matrix, ids, meta


def allowed_mask(catalog: Catalog, case: Case):
    """Positions satisfying every hard constraint the case implies."""
    import numpy as np
    request = parse_request(case.arguments)
    plan = catalog.plan_shopping(request)
    filters = dict(request.filters)
    for intent in plan.primary:
        filters = {**intent.budget.as_filters(), **filters}
    mask = np.ones(len(catalog.products), dtype=bool)
    if filters:
        for position, product in enumerate(catalog.products):
            if not catalog.matches(product, filters):
                mask[position] = False
    return mask


def bm25_scores(catalog: Catalog, case: Case):
    """Production selection, as a dense score vector over positions."""
    import numpy as np
    request = parse_request(case.arguments)
    plan = catalog.plan_shopping(request)
    scores = np.zeros(len(catalog.products), dtype='float32')
    for intent in plan.primary:
        for position, value in catalog.score_all(tokens(intent.text)).items():
            scores[position] = max(scores[position], value)
    return scores


def dense_scores(matrix, model, query: str):
    import numpy as np
    vector = np.asarray(next(iter(model.query_embed([query]))), dtype='float32')
    norm = np.linalg.norm(vector) or 1.0
    return (matrix.astype('float32') @ (vector / norm)).astype('float32')


def normalise(scores, mask):
    import numpy as np
    values = scores[mask & (scores > 0)]
    if values.size == 0:
        return np.zeros_like(scores)
    low, high = float(values.min()), float(values.max())
    if high <= low:
        return (scores > 0).astype('float32')
    out = (scores - low) / (high - low)
    out[scores <= 0] = 0.0
    return out.clip(0.0, 1.0)


def rank_positions(scores, mask, top_k: int):
    import numpy as np
    candidates = np.where(mask & (scores > 0))[0]
    if candidates.size == 0:
        return []
    order = np.lexsort((candidates, -scores[candidates]))
    return [int(candidates[i]) for i in order[:top_k]]


def rrf_positions(left: Sequence[int], right: Sequence[int], top_k: int) -> list[int]:
    combined: dict[int, float] = {}
    for ranking in (left, right):
        for rank, position in enumerate(ranking, 1):
            combined[position] = combined.get(position, 0.0) + 1.0 / (RRF_K + rank)
    return [p for p, _ in sorted(combined.items(), key=lambda kv: (-kv[1], kv[0]))
            ][:top_k]


def grade_page(case: Case, positions: Sequence[int], catalog: Catalog,
               grader: Grader) -> float:
    grades = []
    for position in positions[:PRIMARY_SLOTS]:
        grade, _ = grader.grade(case.id, catalog.index.ids[position], case.grades,
                                case.constraints)
        grades.append(grade)
    return ndcg(grades, PRIMARY_SLOTS)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--catalog', type=Path, default=ROOT / 'data/products.json')
    parser.add_argument('--out', type=Path, default=ROOT / 'analysis/spike_dense.json')
    parser.add_argument('--top-k', type=int, default=200)
    parser.add_argument('--weights', default='0.3,0.5,0.7')
    parser.add_argument('--thresholds', default='0.60,0.65,0.70,0.75,0.80,0.85')
    args = parser.parse_args(argv)

    try:
        import numpy as np
        from fastembed import TextEmbedding
    except ImportError as exc:  # pragma: no cover
        raise SystemExit(f'{exc}; run this with .venv-embed/bin/python') from exc

    matrix, ids, meta = load_vectors()
    print(f"vectors: {meta['products']} x {meta['dimension']} float16, "
          f"{meta['megabytes']} MB, built in {meta['embed_seconds']}s")

    products = json.loads(args.catalog.read_text(encoding='utf-8'))
    products_by_id = {p['id']: p for p in products}
    grader = Grader(products_by_id, load_judgments())

    started = time.perf_counter()
    catalog = Catalog(products)
    lexical_startup = time.perf_counter() - started
    assert list(catalog.index.ids) == ids, 'vector order does not match the catalog'

    started = time.perf_counter()
    model = TextEmbedding(meta['model'])
    model_load = time.perf_counter() - started
    rss_mb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024

    cases = load_cases()
    relevance = [c for c in cases if c.area in RELEVANCE_AREAS and c.grades]
    irrelevant = [c for c in cases if c.area == 'irrelevant']
    weights = [float(w) for w in args.weights.split(',')]
    thresholds = [float(t) for t in args.thresholds.split(',')]

    per_case: dict[str, dict[str, Any]] = {}
    dense_latency: list[float] = []
    dense_best_for_irrelevant: list[float] = []

    for case in relevance + irrelevant:
        query = case.arguments.get('query', '')
        mask = allowed_mask(catalog, case)
        lexical = bm25_scores(catalog, case)
        started = time.perf_counter()
        dense = dense_scores(matrix, model, query)
        dense_latency.append((time.perf_counter() - started) * 1000)

        lexical_rank = rank_positions(lexical, mask, args.top_k)
        dense_rank = rank_positions(dense, mask, args.top_k)
        normal_lexical = normalise(lexical, mask)
        normal_dense = normalise(dense, mask)

        entry: dict[str, Any] = {
            'bm25': lexical_rank,
            'dense': dense_rank,
            'rrf': rrf_positions(lexical_rank, dense_rank, args.top_k),
            'dense_top_similarity': float(dense[mask].max()) if mask.any() else 0.0,
        }
        for weight in weights:
            blended = weight * normal_lexical + (1.0 - weight) * normal_dense
            entry[f'fusion@{weight}'] = rank_positions(blended, mask, args.top_k)
        per_case[case.id] = entry
        if case.area == 'irrelevant':
            dense_best_for_irrelevant.append(entry['dense_top_similarity'])

    methods = ['bm25', 'dense', 'rrf'] + [f'fusion@{w}' for w in weights]
    report: dict[str, Any] = {
        'vectors': meta,
        'query_latency_ms': {
            'p50': round(evalkit.percentile(dense_latency, 50), 2),
            'p95': round(evalkit.percentile(dense_latency, 95), 2),
        },
        'cost': {
            'lexical_index_seconds': round(lexical_startup, 2),
            'embedding_model_load_seconds': round(model_load, 2),
            'process_peak_rss_mb': round(rss_mb, 1),
            'vector_megabytes': meta['megabytes'],
        },
        'methods': {},
        'abstention_sweep': {},
    }

    for method in methods:
        rows = []
        for case in relevance:
            rows.append((case.split, grade_page(case, per_case[case.id][method],
                                                catalog, grader)))
        report['methods'][method] = {
            'ndcg_dev': round(mean(v for s, v in rows if s == 'dev'), 4),
            'ndcg_heldout': round(mean(v for s, v in rows if s == 'heldout'), 4),
            'ndcg_all': round(mean(v for _, v in rows), 4),
            # Without a threshold a similarity search always returns neighbours.
            'irrelevant_cases_answered': sum(
                1 for case in irrelevant if per_case[case.id][method]),
            'irrelevant_cases': len(irrelevant),
        }

    for threshold in thresholds:
        kept = [case for case in irrelevant
                if per_case[case.id]['dense_top_similarity'] >= threshold]
        answered_relevance = [case for case in relevance
                              if per_case[case.id]['dense_top_similarity'] >= threshold]
        report['abstention_sweep'][f'{threshold}'] = {
            'irrelevant_wrongly_answered': len(kept),
            'relevance_cases_still_answered': len(answered_relevance),
            'relevance_cases': len(relevance),
        }

    report['dense_similarity_on_irrelevant'] = {
        'min': round(min(dense_best_for_irrelevant), 4),
        'max': round(max(dense_best_for_irrelevant), 4),
        'mean': round(mean(dense_best_for_irrelevant), 4),
    }
    report['dense_similarity_on_relevance'] = {
        'min': round(min(per_case[c.id]['dense_top_similarity'] for c in relevance), 4),
        'max': round(max(per_case[c.id]['dense_top_similarity'] for c in relevance), 4),
        'mean': round(mean(per_case[c.id]['dense_top_similarity']
                           for c in relevance), 4),
    }

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2), encoding='utf-8')

    print()
    print(f"{'method':<14}{'nDCG dev':>10}{'nDCG held':>11}{'nDCG all':>10}"
          f"{'irrelevant answered':>22}")
    for method in methods:
        block = report['methods'][method]
        print(f"{method:<14}{block['ndcg_dev']:>10.4f}{block['ndcg_heldout']:>11.4f}"
              f"{block['ndcg_all']:>10.4f}"
              f"{block['irrelevant_cases_answered']:>14}/"
              f"{block['irrelevant_cases']}")
    print()
    print('dense top similarity: '
          f"relevance {report['dense_similarity_on_relevance']}")
    print(f"                      irrelevant {report['dense_similarity_on_irrelevant']}")
    print()
    print(f"{'threshold':<12}{'irrelevant wrongly answered':>30}"
          f"{'relevance kept':>18}")
    for threshold, block in report['abstention_sweep'].items():
        print(f"{threshold:<12}{block['irrelevant_wrongly_answered']:>22}/"
              f"{len(irrelevant)}{block['relevance_cases_still_answered']:>13}/"
              f"{block['relevance_cases']}")
    print()
    print(f"dense query latency p50 {report['query_latency_ms']['p50']} ms, "
          f"p95 {report['query_latency_ms']['p95']} ms")
    print(f"cost: model load {model_load:.1f}s, vectors "
          f"{meta['megabytes']} MB, process peak RSS {rss_mb:.0f} MB")
    print(f"report -> {args.out}")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
