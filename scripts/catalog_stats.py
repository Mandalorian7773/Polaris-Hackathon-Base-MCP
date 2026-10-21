"""Summarise the frozen catalog without ever exposing raw product rows.

Loads data/products.json once, aggregates field coverage, facet vocabularies, and
numeric distributions, writes analysis/catalog_stats.json, and prints a short summary.

    .venv/bin/python scripts/catalog_stats.py [--catalog PATH] [--out PATH]
"""
from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any, Iterable, Sequence

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CATALOG = ROOT / 'data/products.json'
DEFAULT_OUT = ROOT / 'analysis/catalog_stats.json'

FACETS: tuple[str, ...] = ('category', 'product_type', 'color', 'audience', 'section', 'pattern')
TEXT_FIELDS: tuple[str, ...] = ('id', 'name', 'description', 'image_url')
MAX_FACET_VALUES = 2000
PERCENTILES: tuple[float, ...] = (1, 5, 10, 25, 50, 75, 90, 95, 99)


def load_products(path: Path) -> list[dict[str, Any]]:
    """Parse the catalog array once. Callers must not print whole products."""
    with path.open(encoding='utf-8') as handle:
        products = json.load(handle)
    if not isinstance(products, list):
        raise ValueError(f'{path} is not a JSON array')
    return products


def percentile(values: Sequence[float], pct: float) -> float:
    """Nearest-rank percentile over an already sorted sequence."""
    if not values:
        return math.nan
    rank = max(1, math.ceil(pct / 100 * len(values)))
    return float(values[rank - 1])


def describe_numbers(values: Iterable[float], present: int, total: int) -> dict[str, Any]:
    ordered = sorted(float(v) for v in values)
    summary: dict[str, Any] = {
        'present': present,
        'missing': total - present,
        'count': len(ordered),
    }
    if ordered:
        summary.update(
            min=ordered[0],
            max=ordered[-1],
            mean=round(statistics.fmean(ordered), 4),
            stdev=round(statistics.pstdev(ordered), 4) if len(ordered) > 1 else 0.0,
            percentiles={f'p{int(p)}': round(percentile(ordered, p), 4) for p in PERCENTILES},
            distinct=len(set(ordered)),
        )
    return summary


def histogram(values: Sequence[float], edges: Sequence[float]) -> dict[str, int]:
    """Count values into [edge, next_edge) buckets plus a final open bucket."""
    buckets: dict[str, int] = {}
    for low, high in zip(edges, edges[1:]):
        label = f'{low:g}-{high:g}'
        buckets[label] = sum(1 for v in values if low <= v < high)
    buckets[f'{edges[-1]:g}+'] = sum(1 for v in values if v >= edges[-1])
    return buckets


def facet_summary(counts: Counter[str], total: int, nulls: int) -> dict[str, Any]:
    ordered = counts.most_common()
    summary: dict[str, Any] = {
        'distinct': len(counts),
        'null_or_blank': nulls,
        'coverage': round((total - nulls) / total, 6) if total else 0.0,
        'truncated': len(ordered) > MAX_FACET_VALUES,
        'values': dict(ordered[:MAX_FACET_VALUES]),
    }
    return summary


def collect(products: list[dict[str, Any]]) -> dict[str, Any]:
    total = len(products)
    field_counts: Counter[str] = Counter()
    null_counts: Counter[str] = Counter()
    facet_counts: dict[str, Counter[str]] = {f: Counter() for f in FACETS}
    facet_nulls: Counter[str] = Counter()
    facet_case_variants: dict[str, dict[str, set[str]]] = {f: {} for f in FACETS}

    prices: list[float] = []
    currencies: Counter[str] = Counter()
    ratings: list[float] = []
    review_counts: list[int] = []
    reviews_len: Counter[int] = Counter()
    id_lengths: Counter[int] = Counter()
    ids_with_leading_zero = 0
    name_lengths: list[int] = []
    description_lengths: list[int] = []
    unique_ids: set[str] = set()
    unique_names: set[str] = set()
    rating_present_but_zero_count = 0
    count_zero_with_average = 0

    for product in products:
        for key, value in product.items():
            field_counts[key] += 1
            if value is None:
                null_counts[key] += 1

        for facet in FACETS:
            raw = product.get(facet)
            if raw is None or not str(raw).strip():
                facet_nulls[facet] += 1
                continue
            value = str(raw)
            facet_counts[facet][value] += 1
            facet_case_variants[facet].setdefault(value.strip().casefold(), set()).add(value)

        pid = product.get('id')
        if isinstance(pid, str):
            unique_ids.add(pid)
            id_lengths[len(pid)] += 1
            if pid.startswith('0'):
                ids_with_leading_zero += 1

        name = product.get('name')
        if isinstance(name, str):
            unique_names.add(name)
            name_lengths.append(len(name))
        description = product.get('description')
        if isinstance(description, str):
            description_lengths.append(len(description))
        else:
            null_counts['description.null_or_missing'] += 1

        price = product.get('price') or {}
        amount = price.get('amount')
        if isinstance(amount, (int, float)):
            prices.append(float(amount))
        if price.get('currency'):
            currencies[str(price['currency'])] += 1

        rating = product.get('rating') or {}
        average = rating.get('average')
        count = rating.get('count')
        if isinstance(average, (int, float)):
            ratings.append(float(average))
            if count == 0:
                count_zero_with_average += 1
        else:
            null_counts['rating.average.null'] += 1
            if isinstance(count, int) and count > 0:
                rating_present_but_zero_count += 1
        if isinstance(count, int):
            review_counts.append(count)

        reviews = product.get('reviews')
        reviews_len[len(reviews) if isinstance(reviews, list) else -1] += 1

    stats: dict[str, Any] = {
        'catalog': {
            'products': total,
            'unique_ids': len(unique_ids),
            'duplicate_ids': total - len(unique_ids),
            'unique_names': len(unique_names),
            'ids_with_leading_zero': ids_with_leading_zero,
            'id_lengths': dict(sorted(id_lengths.items())),
        },
        'fields': {
            'present': dict(field_counts.most_common()),
            'null': dict(null_counts.most_common()),
            'always_present': sorted(k for k, v in field_counts.items() if v == total),
            'sometimes_missing': sorted(k for k, v in field_counts.items() if v < total),
        },
        'facets': {
            facet: facet_summary(facet_counts[facet], total, facet_nulls[facet])
            for facet in FACETS
        },
        'facet_case_collisions': {
            facet: {
                folded: sorted(variants)
                for folded, variants in facet_case_variants[facet].items()
                if len(variants) > 1
            }
            for facet in FACETS
        },
        'text': {
            'name_length': describe_numbers(name_lengths, len(name_lengths), total),
            'description_length': describe_numbers(
                description_lengths, len(description_lengths), total
            ),
        },
        'price': {
            **describe_numbers(prices, len(prices), total),
            'currencies': dict(currencies.most_common()),
            'histogram': histogram(prices, (0, 250, 500, 750, 1000, 1500, 2000, 3000, 5000, 10000)),
        },
        'rating': {
            **describe_numbers(ratings, len(ratings), total),
            'null_average': total - len(ratings),
            'histogram': histogram(ratings, (1, 2, 2.5, 3, 3.5, 4, 4.5, 5)),
            'null_average_with_positive_count': rating_present_but_zero_count,
            'average_present_with_zero_count': count_zero_with_average,
        },
        'review_count': {
            **describe_numbers(review_counts, len(review_counts), total),
            'histogram': histogram(
                [float(c) for c in review_counts], (0, 1, 2, 3, 4, 5, 6, 7, 8)
            ),
            'exact_counts': dict(sorted(Counter(review_counts).items())),
        },
        'reviews_array_length': dict(sorted(reviews_len.items())),
    }
    return stats


def print_summary(stats: dict[str, Any]) -> None:
    catalog = stats['catalog']
    print(f"products            {catalog['products']:,}  "
          f"unique ids {catalog['unique_ids']:,}  "
          f"leading-zero ids {catalog['ids_with_leading_zero']:,}")
    print(f"always present      {', '.join(stats['fields']['always_present'])}")
    missing = stats['fields']['sometimes_missing']
    print(f"sometimes missing   {', '.join(missing) if missing else '(none)'}")
    print()
    print('facet                distinct  null/blank  top values')
    for facet, summary in stats['facets'].items():
        top = ', '.join(f'{v} ({c:,})' for v, c in list(summary['values'].items())[:3])
        print(f"{facet:<20} {summary['distinct']:>8}  {summary['null_or_blank']:>10}  {top}")
    print()
    price, rating, reviews = stats['price'], stats['rating'], stats['review_count']
    print(f"price INR           min {price['min']:g}  p50 {price['percentiles']['p50']:g}  "
          f"p95 {price['percentiles']['p95']:g}  max {price['max']:g}  "
          f"distinct {price['distinct']:,}")
    print(f"rating.average      present {rating['count']:,}  null {rating['null_average']:,}  "
          f"min {rating['min']:g}  p50 {rating['percentiles']['p50']:g}  max {rating['max']:g}")
    print(f"rating.count        p50 {reviews['percentiles']['p50']:g}  "
          f"max {reviews['max']:g}  zero-review products "
          f"{reviews['exact_counts'].get(0, 0):,}")
    collisions = {f: c for f, c in stats['facet_case_collisions'].items() if c}
    print(f"case collisions     {collisions if collisions else '(none)'}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--catalog', type=Path, default=DEFAULT_CATALOG)
    parser.add_argument('--out', type=Path, default=DEFAULT_OUT)
    args = parser.parse_args(argv)

    started = time.perf_counter()
    products = load_products(args.catalog)
    loaded = time.perf_counter()
    stats = collect(products)
    done = time.perf_counter()

    stats['meta'] = {
        'catalog_path': str(args.catalog),
        'catalog_bytes': args.catalog.stat().st_size,
        'load_seconds': round(loaded - started, 3),
        'aggregate_seconds': round(done - loaded, 3),
        'generated_by': 'scripts/catalog_stats.py',
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(stats, indent=2, sort_keys=False, ensure_ascii=False),
                        encoding='utf-8')

    print_summary(stats)
    print()
    print(f"load {stats['meta']['load_seconds']:.1f}s  "
          f"aggregate {stats['meta']['aggregate_seconds']:.1f}s  -> {args.out}")
    return 0


if __name__ == '__main__':
    sys.exit(main())
