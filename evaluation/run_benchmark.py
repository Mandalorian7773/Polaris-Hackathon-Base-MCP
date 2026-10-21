"""Independent catalog-grounded development benchmark.

The cases are intentionally local and public. They are not organizer cases and
the resulting scores are not estimates of the official hidden score.
"""
from __future__ import annotations

import argparse
import gc
import json
import math
import os
import resource
import statistics
import subprocess
import sys
import time
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from search import Catalog, STRING_FILTERS, tokens, validate_arguments  # noqa: E402


def rule(grade, **facts):
    return {'grade': grade, **facts}


def semantic_case(case_id, category, query, rules, **arguments):
    return {
        'id': case_id, 'category': category, 'arguments': {'query': query, **arguments},
        'rules': rules,
    }


def mixed_case(case_id, query, choices, **arguments):
    return {
        'id': case_id, 'category': 'mixed', 'arguments': {'query': query, **arguments},
        'choices': choices,
    }


def recommendation_case(case_id, query, primary, recommendation):
    return {
        'id': case_id, 'category': 'recommendation',
        'arguments': {'query': query, 'limit': 10, 'include_recommendations': True},
        'rules': primary, 'recommendation_rules': recommendation,
    }


TYPE = lambda grade, value, **extra: rule(grade, product_type=value, **extra)
CATEGORY = lambda grade, value, **extra: rule(grade, category=value, **extra)

CASES = [
    semantic_case('rank-red-dress', 'ranking', 'red dresses',
                  [TYPE(3, 'Dress', color='Red'), TYPE(2, 'Dress'),
                   CATEGORY(1, 'Garment Full body')]),
    semantic_case('rank-black-tee', 'ranking', 'black t-shirts',
                  [TYPE(3, 'T-shirt', color='Black'), TYPE(2, 'T-shirt'),
                   CATEGORY(1, 'Garment Upper body')]),
    semantic_case('rank-denim-jeans', 'ranking', 'denim jeans',
                  [TYPE(3, 'Trousers', pattern='Denim'),
                   TYPE(2, 'Trousers'), CATEGORY(1, 'Garment Lower body')]),
    semantic_case('rank-white-sneakers', 'ranking', 'white sneakers',
                  [TYPE(3, 'Sneakers', color='White'), TYPE(2, 'Sneakers'),
                   CATEGORY(1, 'Shoes')]),
    semantic_case('rank-shoulder-bag', 'ranking', 'shoulder bag',
                  [TYPE(3, 'Shoulder bag'), CATEGORY(2, 'Bags'),
                   CATEGORY(1, 'Accessories')]),
    semantic_case('rank-blue-blazer', 'ranking', 'dark blue blazer',
                  [TYPE(3, 'Blazer', color='Dark Blue'), TYPE(2, 'Blazer'),
                   CATEGORY(1, 'Garment Upper body')]),
    semantic_case('rank-striped-blouse', 'ranking', 'striped blouse',
                  [TYPE(3, 'Blouse', pattern='Stripe'), TYPE(2, 'Blouse'),
                   CATEGORY(1, 'Garment Upper body')]),
    semantic_case('rank-gold-earrings', 'ranking', 'gold earrings',
                  [rule(3, product_type=['Earring', 'Earrings'], color='Gold'),
                   rule(2, product_type=['Earring', 'Earrings']),
                   CATEGORY(1, 'Accessories')]),
    semantic_case('rank-kids-hoodie', 'ranking', 'hoodie for kids',
                  [TYPE(3, 'Hoodie', audience_family='children'), TYPE(2, 'Hoodie'),
                   CATEGORY(1, 'Garment Upper body')]),
    semantic_case('rank-mens-trousers', 'ranking', 'trousers for men',
                  [TYPE(3, 'Trousers', audience='Menswear'), TYPE(2, 'Trousers'),
                   CATEGORY(1, 'Garment Lower body')]),
    semantic_case('rank-pattern-dress', 'ranking', 'all over pattern dress',
                  [TYPE(3, 'Dress', pattern='All over pattern'), TYPE(2, 'Dress'),
                   CATEGORY(1, 'Garment Full body')]),
    semantic_case('rank-summer-dress', 'ranking', 'summer dress',
                  [TYPE(3, 'Dress'), CATEGORY(1, 'Garment Full body')]),
    semantic_case('rank-running-shoes', 'ranking', 'running shoes',
                  [TYPE(3, 'Sneakers'), CATEGORY(2, 'Shoes')]),
    semantic_case('rank-cotton-top', 'ranking', 'cotton top',
                  [rule(3, product_type=['Top', 'Vest top'], text='cotton'),
                   rule(2, product_type=['Top', 'Vest top']),
                   CATEGORY(1, 'Garment Upper body')]),
    semantic_case('rank-office-blazer', 'ranking', 'formal office blazer',
                  [TYPE(3, 'Blazer'), rule(2, product_type=['Shirt', 'Blouse', 'Trousers']),
                   CATEGORY(1, 'Garment Upper body')]),
    semantic_case('rank-swimsuit', 'ranking', 'swimsuit',
                  [TYPE(3, 'Swimsuit'), CATEGORY(2, 'Swimwear')]),
    semantic_case('rank-pyjamas', 'ranking', 'pyjama set',
                  [TYPE(3, 'Pyjama set'), CATEGORY(2, 'Nightwear')]),
    semantic_case('rank-black-skirt-budget', 'ranking', 'black skirt under INR 1500',
                  [TYPE(3, 'Skirt', color='Black', max_price=1500),
                   TYPE(2, 'Skirt', max_price=1500), CATEGORY(1, 'Garment Lower body')]),

    semantic_case('unusual-lazy-sunday', 'unusual', 'comfortable for a lazy Sunday',
                  [rule(3, product_type=['Sweater', 'Hoodie', 'Pyjama set', 'Robe']),
                   rule(2, product_type=['Pyjama bottom', 'Leggings/Tights']),
                   CATEGORY(1, 'Nightwear')]),
    semantic_case('unusual-rain', 'unusual', 'something useful for a rainy day',
                  [rule(3, product_type=['Umbrella', 'Jacket', 'Coat', 'Boots']),
                   CATEGORY(1, 'Garment Upper body')]),
    semantic_case('unusual-workout', 'unusual', 'an outfit for a gym workout',
                  [rule(3, product_type=['T-shirt', 'Shorts', 'Leggings/Tights', 'Sneakers']),
                   rule(2, audience='Sport')]),
    semantic_case('unusual-beach', 'unusual', 'essentials for a beach vacation',
                  [rule(3, product_type=['Swimsuit', 'Swimwear top', 'Swimwear bottom',
                                         'Swimwear set', 'Sarong', 'Sandals']),
                   CATEGORY(2, 'Swimwear')]),
    semantic_case('unusual-dinner', 'unusual', 'something smart for a dinner party',
                  [rule(3, product_type=['Dress', 'Blazer', 'Blouse', 'Shirt']),
                   rule(2, product_type=['Heels', 'Trousers'])]),
    semantic_case('unusual-cozy', 'unusual', 'cozy loungewear',
                  [rule(3, product_type=['Pyjama set', 'Robe', 'Hoodie', 'Sweater']),
                   CATEGORY(2, 'Nightwear')]),
    semantic_case('unusual-navy-trainers', 'unusual', 'navy trainers for men under 2k',
                  [TYPE(3, 'Sneakers', color='Dark Blue', audience='Menswear',
                        max_price=2000), TYPE(2, 'Sneakers', max_price=2000),
                   CATEGORY(1, 'Shoes')]),
    semantic_case('unusual-typo-shirt', 'unusual', 'tshrit',
                  [TYPE(3, 'T-shirt'), CATEGORY(1, 'Garment Upper body')]),

    semantic_case('none-spaceship', 'irrelevant', 'spaceship engine', []),
    semantic_case('none-laptop', 'irrelevant', 'laptop charger', []),
    semantic_case('none-groceries', 'irrelevant', 'fresh groceries', []),
    semantic_case('none-car', 'irrelevant', 'car battery', []),
    semantic_case('none-microscope', 'irrelevant', 'quantum microscope', []),
    semantic_case('none-pet-food', 'irrelevant', 'organic pet food', []),
    semantic_case('none-hotel', 'irrelevant', 'book a hotel room', []),
    semantic_case('none-impossible-budget', 'irrelevant', 'dress under INR 1', []),

    mixed_case('mixed-jeans-tees',
               'black jeans and white t-shirts under INR 2000 each',
               [[TYPE(3, 'Trousers', color='Black', pattern='Denim', max_price=2000)],
                [TYPE(3, 'T-shirt', color='White', max_price=2000)]], limit=10),
    mixed_case('mixed-dress-bag', 'red dress and black bag',
               [[TYPE(3, 'Dress', color='Red')],
                [CATEGORY(3, 'Bags', color='Black')]], limit=10),
    mixed_case('mixed-sneakers-hoodie', 'sneakers and hoodie',
               [[TYPE(3, 'Sneakers')], [TYPE(3, 'Hoodie')]], limit=10),
    mixed_case('mixed-skirt-blouse', 'skirt and blouse',
               [[TYPE(3, 'Skirt')], [TYPE(3, 'Blouse')]], limit=10),
    mixed_case('mixed-swim-sunglasses', 'swimwear and sunglasses',
               [[CATEGORY(3, 'Swimwear')], [TYPE(3, 'Sunglasses')]], limit=10),
    mixed_case('mixed-mens-set', 'shirt and trousers for men',
               [[TYPE(3, 'Shirt', audience='Menswear')],
                [TYPE(3, 'Trousers', audience='Menswear')]], limit=10),
    mixed_case('mixed-kids-set', 'sweater and sneakers for kids',
               [[TYPE(3, 'Sweater', audience_family='children')],
                [TYPE(3, 'Sneakers', audience_family='children')]], limit=10),
    mixed_case('mixed-jewellery', 'earrings and necklace',
               [[rule(3, product_type=['Earring', 'Earrings'])],
                [TYPE(3, 'Necklace')]], limit=10),

    recommendation_case('rec-dress-bag',
                        'dress under INR 2000 with a matching bag under INR 1000',
                        [TYPE(3, 'Dress', max_price=2000)],
                        [CATEGORY(3, 'Bags', max_price=1000)]),
    recommendation_case('rec-jeans-sneakers',
                        'black jeans with matching sneakers under INR 2000',
                        [TYPE(3, 'Trousers', color='Black', pattern='Denim')],
                        [TYPE(3, 'Sneakers', max_price=2000)]),
    recommendation_case('rec-swimsuit-sunglasses',
                        'swimsuit with matching sunglasses',
                        [TYPE(3, 'Swimsuit')], [TYPE(3, 'Sunglasses')]),
    recommendation_case('rec-blazer-tie', 'blazer with a coordinating tie',
                        [TYPE(3, 'Blazer')], [TYPE(3, 'Tie')]),
    recommendation_case('rec-dress-heels', 'party dress with matching heels',
                        [TYPE(3, 'Dress')], [TYPE(3, 'Heels')]),
    recommendation_case('rec-workout-bag',
                        'workout clothes with a matching gym bag under 1500',
                        [rule(3, product_type=['T-shirt', 'Shorts', 'Leggings/Tights'])],
                        [TYPE(3, 'Weekend/Gym bag', max_price=1500)]),

    {'id': 'contract-default', 'category': 'contract', 'arguments': {}},
    {'id': 'contract-black-price', 'category': 'contract',
     'arguments': {'filters': {'color': 'Black', 'max_price': 2000},
                   'sort_by': 'price', 'sort_order': 'asc', 'limit': 10}},
    {'id': 'contract-null-rating', 'category': 'contract',
     'arguments': {'sort_by': 'rating', 'sort_order': 'desc', 'limit': 20}},
    {'id': 'contract-review-filter', 'category': 'contract',
     'arguments': {'filters': {'min_review_count': 8},
                   'sort_by': 'review_count', 'sort_order': 'desc', 'limit': 10}},
    {'id': 'contract-deep-page', 'category': 'contract',
     'arguments': {'filters': {'category': 'Shoes'}, 'sort_by': 'id',
                   'sort_order': 'asc', 'offset': 100, 'limit': 20}},
    {'id': 'invalid-limit', 'category': 'invalid', 'arguments': {'limit': 0}},
    {'id': 'invalid-price', 'category': 'invalid',
     'arguments': {'filters': {'min_price': 1000, 'max_price': 500}}},
    {'id': 'invalid-property', 'category': 'invalid', 'arguments': {'extra': True}},
]

for index, case in enumerate(CASES):
    case['split'] = 'heldout' if index % 5 == 4 else 'development'


def audience_family(value):
    if value in {'Ladieswear', 'Ladies Accessories', 'Divided', 'Lingeries/Tights'}:
        return 'women'
    if value == 'Menswear':
        return 'men'
    if value.startswith('Children'):
        return 'children'
    if value.startswith('Baby'):
        return 'baby'
    return value.casefold()


def matches_rule(product, criterion):
    for key, expected in criterion.items():
        if key == 'grade':
            continue
        if key == 'max_price' and product['price']['amount'] > expected:
            return False
        if key == 'min_price' and product['price']['amount'] < expected:
            return False
        if key == 'text':
            haystack = f"{product['name']} {product.get('description') or ''}".casefold()
            if expected.casefold() not in haystack:
                return False
        elif key == 'audience_family':
            if audience_family(product['audience']) != expected:
                return False
        elif key not in {'max_price', 'min_price', 'text'}:
            actual = product[key]
            if isinstance(expected, list):
                if actual not in expected:
                    return False
            elif actual != expected:
                return False
    return True


def grade(product, rules):
    return max((criterion['grade'] for criterion in rules
                if matches_rule(product, criterion)), default=0)


def ndcg(grades, slots):
    dcg = sum((2 ** value - 1) / math.log2(rank + 2)
              for rank, value in enumerate(grades[:slots]))
    ideal = sum(7 / math.log2(rank + 2) for rank in range(slots))
    return dcg / ideal


class BaselineCatalog:
    """Exact copy of the starter's search behavior for before/after comparison."""
    def __init__(self, products):
        self.products = sorted(products, key=lambda product: product['id'])
        self.words = {
            product['id']: set(tokens(' '.join(
                str(product.get(key) or '')
                for key in ('name', 'description', *STRING_FILTERS))))
            for product in self.products
        }

    @staticmethod
    def matches(product, filters):
        return Catalog.matches(product, filters)

    def relevance(self, product, query_tokens):
        words = self.words[product['id']]
        if not all(word in words for word in query_tokens):
            return 0
        name_words = set(tokens(product['name']))
        return 1 + sum(word in name_words for word in query_tokens)

    def search(self, arguments):
        validate_arguments(arguments)
        query = tokens(arguments.get('query', '').strip())
        filters = arguments.get('filters', {})
        candidates = [product for product in self.products if self.matches(product, filters)]
        scores = {}
        if query:
            scores = {product['id']: self.relevance(product, query) for product in candidates}
            candidates = [product for product in candidates if scores[product['id']] > 0]
        sort_by = arguments.get('sort_by', 'relevance')
        reverse = arguments.get('sort_order', 'desc') == 'desc'
        if sort_by == 'relevance' and query:
            candidates.sort(key=lambda product: scores[product['id']], reverse=reverse)
        elif sort_by == 'rating':
            rated = [p for p in candidates if p['rating']['average'] is not None]
            unrated = [p for p in candidates if p['rating']['average'] is None]
            rated.sort(key=lambda p: p['rating']['average'], reverse=reverse)
            candidates = rated + unrated
        elif sort_by != 'relevance':
            key = {
                'id': lambda p: p['id'],
                'price': lambda p: p['price']['amount'],
                'review_count': lambda p: p['rating']['count'],
            }[sort_by]
            candidates.sort(key=key, reverse=reverse)
        offset, limit = arguments.get('offset', 0), arguments.get('limit', 10)
        return {
            'results': [{'id': p['id']} for p in candidates[offset:offset + limit]],
            'recommendations': [],
        }


def expected_contract(products, arguments):
    filters = arguments.get('filters', {})
    candidates = [product for product in products if Catalog.matches(product, filters)]
    sort_by = arguments.get('sort_by', 'relevance')
    order = arguments.get('sort_order', 'desc')
    if sort_by == 'relevance':
        candidates.sort(key=lambda product: product['id'])
    elif sort_by == 'id':
        candidates.sort(key=lambda product: product['id'], reverse=order == 'desc')
    elif sort_by == 'rating':
        rated = [p for p in candidates if p['rating']['average'] is not None]
        unrated = [p for p in candidates if p['rating']['average'] is None]
        direction = -1 if order == 'desc' else 1
        rated.sort(key=lambda p: (direction * p['rating']['average'], p['id']))
        unrated.sort(key=lambda p: p['id'])
        candidates = rated + unrated
    else:
        field = 'amount' if sort_by == 'price' else 'count'
        direction = -1 if order == 'desc' else 1
        candidates.sort(key=lambda p: (
            direction * (p['price'][field] if sort_by == 'price' else p['rating'][field]),
            p['id']))
    offset, limit = arguments.get('offset', 0), arguments.get('limit', 10)
    return [product['id'] for product in candidates[offset:offset + limit]]


def rss_mb():
    value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return value / (1024 * 1024) if sys.platform == 'darwin' else value / 1024


def run(system):
    started = time.perf_counter()
    products = json.loads((ROOT / 'data/products.json').read_text(encoding='utf-8'))
    catalog = BaselineCatalog(products) if system == 'baseline' else Catalog(products)
    startup_ms = (time.perf_counter() - started) * 1000
    by_id = {product['id']: product for product in products}
    details = []
    latencies = []
    for case in CASES:
        arguments = case['arguments']
        began = time.perf_counter()
        error = False
        try:
            output = catalog.search(arguments)
        except ValueError:
            error = True
            output = {'results': [], 'recommendations': []}
        latencies.append((time.perf_counter() - began) * 1000)
        result_ids = [item['id'] for item in output['results']]
        recommendation_ids = [item['id'] for item in output['recommendations']]
        item = {
            'id': case['id'], 'split': case['split'], 'category': case['category'],
            'result_ids': result_ids, 'recommendation_ids': recommendation_ids,
            'error': error,
        }
        if case['category'] in {'ranking', 'unusual'}:
            grades = [grade(by_id[product_id], case['rules']) for product_id in result_ids]
            item.update(grades=grades, score=ndcg(grades, 10))
        elif case['category'] == 'irrelevant':
            item['score'] = float(not result_ids and not recommendation_ids)
        elif case['category'] == 'mixed':
            choice_grades = [
                [grade(by_id[product_id], rules) for product_id in result_ids]
                for rules in case['choices']
            ]
            primary_grades = [max(values) for values in zip(*choice_grades)] if result_ids else []
            coverage = sum(any(value >= 2 for value in values)
                           for values in choice_grades) / len(choice_grades)
            item.update(grades=primary_grades, coverage=coverage,
                        score=(coverage + ndcg(primary_grades, 10)) / 2)
        elif case['category'] == 'recommendation':
            primary_grades = [grade(by_id[product_id], case['rules'])
                              for product_id in result_ids]
            recommendation_grades = [
                grade(by_id[product_id], case['recommendation_rules'])
                for product_id in recommendation_ids
            ]
            valid_refs = all(
                recommendation['for_product_id'] in result_ids and
                recommendation['id'] not in result_ids
                for recommendation in output['recommendations'])
            item.update(
                grades=primary_grades,
                recommendation_grades=recommendation_grades,
                valid_references=valid_refs,
                score=(ndcg(primary_grades, 10) + ndcg(recommendation_grades, 5)) / 2
                if valid_refs else 0,
            )
        elif case['category'] == 'contract':
            item['score'] = float(result_ids == expected_contract(products, arguments))
        else:
            item['score'] = float(error)
        details.append(item)
    categories = {}
    for category in {case['category'] for case in CASES}:
        values = [item['score'] for item in details if item['category'] == category]
        categories[category] = statistics.mean(values)
    splits = {}
    for split in ('development', 'heldout'):
        values = [item['score'] for item in details if item['split'] == split]
        splits[split] = statistics.mean(values)
    return {
        'system': system,
        'case_count': len(CASES),
        'startup_ms': round(startup_ms, 2),
        'peak_rss_mb': round(rss_mb(), 2),
        'p50_ms': round(statistics.median(latencies), 3),
        'p95_ms': round(sorted(latencies)[math.ceil(0.95 * len(latencies)) - 1], 3),
        'categories': categories,
        'splits': splits,
        'details': details,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--system', choices=('baseline', 'current'))
    parser.add_argument('--compare', action='store_true')
    args = parser.parse_args()
    if args.compare:
        reports = {}
        for system in ('baseline', 'current'):
            completed = subprocess.run(
                [sys.executable, __file__, '--system', system],
                cwd=ROOT, text=True, capture_output=True, check=True)
            reports[system] = json.loads(completed.stdout)
        artifact = ROOT / 'evaluation/latest_results.json'
        artifact.write_text(json.dumps(reports, indent=2) + '\n', encoding='utf-8')
        summary = {
            system: {key: report[key] for key in (
                'case_count', 'startup_ms', 'peak_rss_mb', 'p50_ms', 'p95_ms',
                'categories', 'splits')}
            for system, report in reports.items()
        }
        print(json.dumps(summary, indent=2))
        print(f'Wrote catalog-ID judgments to {artifact.relative_to(ROOT)}')
    else:
        print(json.dumps(run(args.system or 'current')))


if __name__ == '__main__':
    main()
