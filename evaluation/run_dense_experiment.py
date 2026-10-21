"""Optional compact dense/fusion ablation.

This utility requires the unretained experimental dependencies `fastembed` and
`numpy`. It does not change production requirements or persist model artifacts.
"""
from __future__ import annotations

from dataclasses import replace
import json
import math
from pathlib import Path
import statistics
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

try:
    import numpy as np
    from fastembed import TextEmbedding
except ImportError as exc:
    raise SystemExit(
        'Dense trial only: install fastembed and numpy in an experimental venv') from exc

from search import Catalog  # noqa: E402
from evaluation.run_benchmark import CASES as MAIN_CASES, grade  # noqa: E402
from evaluation.run_research_experiments import (  # noqa: E402
    CASES as RESEARCH_CASES, standard_ndcg,
)


MODEL = 'minishlab/potion-base-8M'
CACHE = Path('/tmp/polaris-embedding-trial')


def top_indices(scores, allowed, limit):
    allowed_array = np.fromiter(allowed, dtype=np.int64)
    if not len(allowed_array):
        return []
    allowed_scores = scores[allowed_array]
    count = min(limit, len(allowed_array))
    selected = np.argpartition(-allowed_scores, count - 1)[:count]
    selected = selected[np.lexsort((
        allowed_array[selected], -allowed_scores[selected]))]
    return allowed_array[selected].tolist()


def lexical_lane(catalog, query, depth):
    plan = catalog.plan_query(query)
    if len(plan.primary) != 1:
        return [], {}
    scores = catalog._intent_scores(plan.primary[0], catalog.all_indices, {})
    ordered = sorted(
        scores, key=lambda index: (-scores[index], catalog.products[index]['id']))
    return ordered[:depth], scores


def dense_allowed(catalog, query):
    plan = catalog.plan_query(query)
    if len(plan.primary) != 1:
        return catalog.all_indices
    hard_intent = replace(plan.primary[0], soft_types=())
    allowed, _ = catalog._intent_allowed(hard_intent, catalog.all_indices, {})
    return allowed


def rrf(left, right, constant=60):
    scores = {}
    for lane in (left, right):
        for rank, index in enumerate(lane, 1):
            scores[index] = scores.get(index, 0.0) + 1 / (constant + rank)
    return sorted(scores, key=lambda index: (-scores[index], index))


def minmax_fusion(left, right, left_scores, dense_scores, lexical_weight):
    union = set(left).union(right)
    if not union:
        return []

    def normalize(values):
        if not values:
            return {}
        minimum, maximum = min(values.values()), max(values.values())
        if maximum == minimum:
            return {key: 1.0 for key in values}
        return {
            key: (value - minimum) / (maximum - minimum)
            for key, value in values.items()
        }

    lexical = normalize({index: left_scores[index] for index in left})
    dense = normalize({index: float(dense_scores[index]) for index in right})
    scores = {
        index: (
            lexical_weight * lexical.get(index, 0.0) +
            (1 - lexical_weight) * dense.get(index, 0.0)
        )
        for index in union
    }
    return sorted(scores, key=lambda index: (-scores[index], index))


def ideal_for(products, rules):
    return sorted(
        (grade(product, rules) for product in products
         if grade(product, rules) > 0),
        reverse=True,
    )


def evaluate_case(products, rules, ranking):
    grades = [grade(products[index], rules) for index in ranking[:10]]
    ideal = ideal_for(products, rules)
    return {
        'ndcg_at_10': standard_ndcg(grades, ideal),
        'success_at_10': float(any(value >= 2 for value in grades)),
        'grades': grades,
        'ids': [products[index]['id'] for index in ranking[:10]],
    }


def main():
    products = json.loads((ROOT / 'data/products.json').read_text(encoding='utf-8'))
    catalog = Catalog(products)
    products = catalog.products
    documents = [
        '. '.join((
            product['name'], product['product_type'], product['category'],
            product['color'], product['pattern'], product['audience'],
            product['section'], product.get('description') or '',
        ))
        for product in products
    ]
    started = time.perf_counter()
    model = TextEmbedding(model_name=MODEL, cache_dir=str(CACHE))
    vectors = np.asarray(list(model.embed(documents, batch_size=128)), dtype=np.float32)
    vectors /= np.linalg.norm(vectors, axis=1, keepdims=True).clip(min=1e-9)
    build_seconds = time.perf_counter() - started

    main_cases = [
        {
            'id': item['id'], 'set': 'main', 'query': item['arguments']['query'],
            'rules': item['rules'],
        }
        for item in MAIN_CASES
        if item['category'] in {'ranking', 'unusual'}
    ]
    research_cases = [
        {
            'id': item['id'], 'set': 'research-development',
            'query': item['query'], 'rules': item['rules'],
            'family': item['family'],
        }
        for item in RESEARCH_CASES
        if item['split'] == 'development'
    ]
    cases = main_cases + research_cases
    configurations = {
        'lexical': [], 'dense': [], 'rrf60': [],
        'minmax_lexical_70': [], 'minmax_dense_70': [],
    }
    query_latencies = []
    details = []
    for item in cases:
        started = time.perf_counter()
        query_vector = np.asarray(
            next(model.query_embed(item['query'])), dtype=np.float32)
        dense_scores = vectors @ query_vector
        dense = top_indices(
            dense_scores, dense_allowed(catalog, item['query']), 100)
        lexical, lexical_scores = lexical_lane(catalog, item['query'], 100)
        rankings = {
            'lexical': lexical,
            'dense': dense,
            'rrf60': rrf(lexical, dense),
            'minmax_lexical_70': minmax_fusion(
                lexical, dense, lexical_scores, dense_scores, 0.7),
            'minmax_dense_70': minmax_fusion(
                lexical, dense, lexical_scores, dense_scores, 0.3),
        }
        query_latencies.append((time.perf_counter() - started) * 1000)
        row = {
            'id': item['id'], 'set': item['set'], 'query': item['query'],
            'family': item.get('family'),
            'dense_top_score': float(dense_scores[dense[0]]) if dense else None,
        }
        for name, ranking in rankings.items():
            result = evaluate_case(products, item['rules'], ranking)
            configurations[name].append(result['ndcg_at_10'])
            row[name] = result
        details.append(row)
    summary = {
        name: {
            'main_ndcg_at_10': statistics.mean(
                value for value, item in zip(values, cases) if item['set'] == 'main'),
            'research_development_ndcg_at_10': statistics.mean(
                value for value, item in zip(values, cases)
                if item['set'] == 'research-development'),
        }
        for name, values in configurations.items()
    }
    report = {
        'model': MODEL,
        'document_count': len(products),
        'dimension': int(vectors.shape[1]),
        'build_seconds': build_seconds,
        'vector_mib_float32': vectors.nbytes / 1024 / 1024,
        'query_p50_ms': statistics.median(query_latencies),
        'query_p95_ms': sorted(query_latencies)[
            math.ceil(0.95 * len(query_latencies)) - 1],
        'summary': summary,
        'details': details,
    }
    output = ROOT / 'evaluation/dense_experiment_results.json'
    output.write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({key: report[key] for key in (
        'model', 'document_count', 'dimension', 'build_seconds',
        'vector_mib_float32', 'query_p50_ms', 'query_p95_ms', 'summary')}, indent=2))


if __name__ == '__main__':
    main()
