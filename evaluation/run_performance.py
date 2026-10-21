"""Mirror the organizer's empty-query performance workload in-process."""
import concurrent.futures
import json
import math
import statistics
import time

from run_benchmark import ROOT, expected_contract

import sys
sys.path.insert(0, str(ROOT))
from search import Catalog  # noqa: E402


def main():
    products = json.loads((ROOT / 'data/products.json').read_text(encoding='utf-8'))
    catalog = Catalog(products)
    fields = ['category', 'product_type', 'color', 'audience', 'section', 'pattern']
    sorts = ['price', 'rating', 'review_count', 'id']
    workload = []
    for index in range(50):
        product = products[(index * 2027) % len(products)]
        filters = {fields[index % len(fields)]: product[fields[index % len(fields)]]}
        if index % 3 == 0:
            filters['max_price'] = product['price']['amount']
        arguments = {
            'filters': filters,
            'sort_by': sorts[index % len(sorts)],
            'sort_order': 'asc' if index % 2 else 'desc',
            'limit': 10,
        }
        workload.append((arguments, expected_contract(products, arguments)))
    for arguments, _ in workload[:20]:
        catalog.search(arguments)

    def invoke(item):
        arguments, expected = item
        started = time.perf_counter()
        output = catalog.search(arguments)
        elapsed = (time.perf_counter() - started) * 1000
        actual = [result['id'] for result in output['results']]
        return elapsed, actual == expected

    latencies = []
    correct = 0
    for _ in range(3):
        for concurrency in (1, 5):
            with concurrent.futures.ThreadPoolExecutor(
                    max_workers=concurrency) as executor:
                for elapsed, is_correct in executor.map(invoke, workload):
                    latencies.append(elapsed)
                    correct += is_correct
    ordered = sorted(latencies)
    print(json.dumps({
        'calls': len(ordered),
        'correct_rate': correct / len(ordered),
        'p50_ms': round(statistics.median(ordered), 3),
        'p95_ms': round(ordered[math.ceil(0.95 * len(ordered)) - 1], 3),
        'max_ms': round(max(ordered), 3),
    }, indent=2))


if __name__ == '__main__':
    main()
