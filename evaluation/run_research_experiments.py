"""Controlled retrieval ablations on query families absent from the main suite.

The cases use catalog-fact grading rules, not organizer or independent human
judgments. Development is the default. Use --split heldout only for a final
promotion check after selecting a candidate.
"""
from __future__ import annotations

import argparse
from difflib import get_close_matches
from functools import lru_cache
import json
import math
from pathlib import Path
import sqlite3
import statistics
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from search import (  # noqa: E402
    Catalog, FIELD_WEIGHTS, normalized_text,
)
from evaluation.run_benchmark import (  # noqa: E402
    CATEGORY, TYPE, grade, rule,
)


def case(case_id, split, family, query, rules):
    return {
        'id': case_id, 'split': split, 'family': family, 'query': query,
        'rules': rules,
    }


CASES = [
    case('typo-snikers', 'development', 'typo', 'snikers', [TYPE(3, 'Sneakers')]),
    case('typo-blaser', 'development', 'typo', 'blaser', [TYPE(3, 'Blazer')]),
    case('typo-trouzers', 'development', 'typo', 'trouzers', [TYPE(3, 'Trousers')]),
    case('typo-earings', 'development', 'typo', 'earings',
         [rule(3, product_type=['Earring', 'Earrings'])]),
    case('morph-costume', 'development', 'morphology', 'blue dragon costume',
         [TYPE(3, 'Costumes', color='Blue'), TYPE(2, 'Costumes')]),
    case('morph-dresses', 'development', 'morphology', 'casual dresses',
         [TYPE(3, 'Dress')]),
    case('morph-boot', 'development', 'morphology', 'winter boot',
         [TYPE(3, 'Boots'), CATEGORY(1, 'Shoes')]),
    case('task-warm-commute', 'development', 'task',
         'warm layer for a cold commute',
         [rule(3, product_type=['Coat', 'Jacket', 'Sweater', 'Cardigan', 'Hoodie'])]),
    case('task-hands-free', 'development', 'task',
         'hands free bag for city travel',
         [rule(3, product_type=['Cross-body bag', 'Bumbag', 'Backpack']),
          CATEGORY(1, 'Bags')]),
    case('task-interview', 'development', 'task',
         'smart outfit for a job interview',
         [rule(3, product_type=['Blazer', 'Shirt', 'Trousers', 'Blouse'])]),
    case('task-runner-gift', 'development', 'task', 'gift for a runner',
         [rule(3, product_type=['Sneakers', 'Waterbottle', 'Watch',
                                'Weekend/Gym bag'])]),
    case('task-sun', 'development', 'task', 'sun protection for the beach',
         [rule(3, product_type=['Sunglasses', 'Straw hat', 'Cap', 'Cap/peaked',
                                'Hat/brim'])]),
    case('task-sleep', 'development', 'task', 'sleepwear for a cold night',
         [rule(3, product_type=['Pyjama set', 'Pyjama bottom', 'Robe',
                                'Night gown', 'Pyjama jumpsuit/playsuit']),
          CATEGORY(2, 'Nightwear')]),
    case('task-flight', 'development', 'task',
         'comfortable clothes for a long flight',
         [rule(3, product_type=['Trousers', 'Sweater', 'Hoodie',
                                'Leggings/Tights', 'Cardigan'])]),
    case('task-water-bottle', 'development', 'task', 'water bottle for gym',
         [TYPE(3, 'Waterbottle')]),
    case('ood-dresser', 'development', 'irrelevant', 'dresser', []),
    case('ood-sinkers', 'development', 'irrelevant', 'fishing sinkers', []),
    case('ood-dressage', 'development', 'irrelevant', 'dressage saddle', []),
    case('ood-bootloader', 'development', 'irrelevant', 'bootloader software', []),
    case('ood-ringtone', 'development', 'irrelevant', 'ringtone app', []),

    case('typo-sneekers', 'heldout', 'typo', 'sneekers', [TYPE(3, 'Sneakers')]),
    case('typo-blazzer', 'heldout', 'typo', 'blazzer', [TYPE(3, 'Blazer')]),
    case('typo-trousrs', 'heldout', 'typo', 'trousrs', [TYPE(3, 'Trousers')]),
    case('typo-earringz', 'heldout', 'typo', 'earringz',
         [rule(3, product_type=['Earring', 'Earrings'])]),
    case('morph-costumes', 'heldout', 'morphology', 'party costume',
         [TYPE(3, 'Costumes')]),
    case('morph-dress', 'heldout', 'morphology', 'red dresses',
         [TYPE(3, 'Dress', color='Red'), TYPE(2, 'Dress')]),
    case('morph-boots', 'heldout', 'morphology', 'black boot',
         [TYPE(3, 'Boots', color='Black'), TYPE(2, 'Boots')]),
    case('task-winter', 'heldout', 'task', 'keep me warm on the winter commute',
         [rule(3, product_type=['Coat', 'Jacket', 'Sweater', 'Cardigan', 'Hoodie'])]),
    case('task-crossbody', 'heldout', 'task',
         'a bag I can wear across my body',
         [rule(3, product_type=['Cross-body bag', 'Bumbag', 'Backpack']),
          CATEGORY(1, 'Bags')]),
    case('task-job', 'heldout', 'task', 'what to wear for an interview',
         [rule(3, product_type=['Blazer', 'Shirt', 'Trousers', 'Blouse'])]),
    case('task-jogger', 'heldout', 'task', 'present for someone who jogs',
         [rule(3, product_type=['Sneakers', 'Waterbottle', 'Watch',
                                'Weekend/Gym bag'])]),
    case('task-sunny', 'heldout', 'task', 'hat or glasses for a sunny beach',
         [rule(3, product_type=['Sunglasses', 'Straw hat', 'Cap', 'Cap/peaked',
                                'Hat/brim'])]),
    case('task-bed', 'heldout', 'task', 'clothes to sleep in',
         [rule(3, product_type=['Pyjama set', 'Pyjama bottom', 'Robe',
                                'Night gown', 'Pyjama jumpsuit/playsuit']),
          CATEGORY(2, 'Nightwear')]),
    case('task-travel', 'heldout', 'task', 'comfy travel outfit',
         [rule(3, product_type=['Trousers', 'Sweater', 'Hoodie',
                                'Leggings/Tights', 'Cardigan'])]),
    case('task-bottle', 'heldout', 'task', 'gym hydration bottle',
         [TYPE(3, 'Waterbottle')]),
    case('ood-dressmaker', 'heldout', 'irrelevant', 'find a dressmaker', []),
    case('ood-sneaker-bot', 'heldout', 'irrelevant', 'sneaker bot software', []),
    case('ood-blast', 'heldout', 'irrelevant', 'laser blaster', []),
    case('ood-trouser-press', 'heldout', 'irrelevant', 'trouser press machine', []),
]


ORIGINAL_MANUAL_ALIASES = {
    'accessory', 'accessories', 'bag', 'bags', 'denim', 'denims', 'footwear',
    'gym bag', 'handbag', 'handbags', 'jeans', 'jewellery', 'jewelry', 'pants',
    'purse', 'purses', 'running shoes', 'shoe', 'shoes', 'slacks', 'tees',
    'tshirt', 'tshrit', 'tshirts', 'top', 'tops', 'trainers', 'swimwear',
}


class PrePromotionCatalog(Catalog):
    """The implementation immediately before this research pass."""

    def _build_query_vocabulary(self):
        super()._build_query_vocabulary()
        manual = {
            alias: self.type_aliases[alias]
            for alias in ORIGINAL_MANUAL_ALIASES
        }
        aliases = {}
        product_types = sorted(
            {product['product_type'] for product in self.products},
            key=lambda value: (-len(value), value),
        )
        for value in product_types:
            phrase = normalized_text(value)
            aliases[phrase] = ((value,), (), ())
            if phrase and not phrase.endswith('s'):
                aliases[f'{phrase}s'] = ((value,), (), ())
        aliases.update(manual)
        self.type_aliases = aliases
        for cue in ('cold', 'warm', 'interview'):
            self.soft_intents.pop(cue, None)

    @lru_cache(maxsize=4096)
    def _correct_term(self, term):
        if term in self.vocabulary or len(term) < 4:
            return term
        matches = get_close_matches(term, self.vocabulary, n=1, cutoff=0.88)
        return matches[0] if matches else term


class TaxonomyCatalog(Catalog):
    """Promoted morphology/correction without the new intent cues."""

    def _build_query_vocabulary(self):
        super()._build_query_vocabulary()
        for cue in ('cold', 'warm', 'interview'):
            self.soft_intents.pop(cue, None)


class ExpandedIntentCatalog(TaxonomyCatalog):
    """Development-only broad intent expansion."""

    def _build_query_vocabulary(self):
        super()._build_query_vocabulary()
        self.type_aliases['water bottle'] = (('Waterbottle',), (), ())
        self.soft_intents.update({
            'cold': ('Coat', 'Jacket', 'Sweater', 'Cardigan', 'Hoodie'),
            'warm': ('Coat', 'Jacket', 'Sweater', 'Cardigan', 'Hoodie'),
            'hands free': ('Cross-body bag', 'Bumbag', 'Backpack'),
            'interview': ('Blazer', 'Shirt', 'Trousers', 'Blouse'),
            'runner': ('Sneakers', 'Waterbottle', 'Watch', 'Weekend/Gym bag'),
            'sun protection': ('Sunglasses', 'Straw hat', 'Cap', 'Cap/peaked',
                               'Hat/brim'),
            'sleepwear': ('Pyjama set', 'Pyjama bottom', 'Robe', 'Night gown',
                          'Pyjama jumpsuit/playsuit'),
            'flight': ('Trousers', 'Sweater', 'Hoodie', 'Leggings/Tights',
                       'Cardigan'),
        })


class FTS5Catalog(Catalog):
    """Ranking-only FTS5 ablation over the current candidate set."""

    def __init__(self, products):
        super().__init__(products)
        self.fts = sqlite3.connect(':memory:')
        self.fts.execute(
            "CREATE VIRTUAL TABLE docs USING fts5("
            "name, taxonomy, attributes, description, tokenize='unicode61')")
        rows = (
            (
                product['name'],
                f"{product['product_type']} {product['category']}",
                ' '.join(str(product[field]) for field in (
                    'color', 'pattern', 'audience', 'section')),
                product.get('description') or '',
            )
            for product in self.products
        )
        self.fts.executemany('INSERT INTO docs VALUES (?, ?, ?, ?)', rows)
        self.fts.commit()

    def _retrieve(self, query, allowed):
        baseline = super()._retrieve(query, allowed)
        if not baseline:
            return baseline
        query_terms = self.query_terms(query)
        expression = ' OR '.join(f'"{term.replace(chr(34), chr(34) * 2)}"'
                                 for term in query_terms)
        rows = self.fts.execute(
            'SELECT rowid, bm25(docs, ?, ?, ?, ?) FROM docs '
            'WHERE docs MATCH ?',
            (
                FIELD_WEIGHTS['name'], FIELD_WEIGHTS['taxonomy'],
                FIELD_WEIGHTS['attributes'], FIELD_WEIGHTS['description'],
                expression,
            ),
        )
        fts_scores = {rowid - 1: -score for rowid, score in rows}
        phrase = normalized_text(query)
        scores = {}
        for index in baseline:
            product = self.products[index]
            score = fts_scores.get(index, -100.0)
            name = normalized_text(product['name'])
            taxonomy = normalized_text(
                f"{product['product_type']} {product['category']}")
            if phrase == name:
                score += 20
            elif phrase and phrase in name:
                score += 8
            if phrase and phrase in taxonomy:
                score += 10
            scores[index] = score
        return scores


def standard_ndcg(grades, ideal_grades):
    dcg = sum((2 ** value - 1) / math.log2(rank + 2)
              for rank, value in enumerate(grades[:10]))
    ideal = sum((2 ** value - 1) / math.log2(rank + 2)
                for rank, value in enumerate(ideal_grades[:10]))
    return dcg / ideal if ideal else float(not grades)


def evaluate(catalog, products, split):
    selected = [item for item in CASES if item['split'] == split]
    details = []
    latencies = []
    for item in selected:
        relevant = {
            index: grade(product, item['rules'])
            for index, product in enumerate(products)
            if grade(product, item['rules']) >= 2
        }
        ideal = sorted(relevant.values(), reverse=True)
        started = time.perf_counter()
        output = catalog.search({'query': item['query'], 'limit': 100})
        latencies.append((time.perf_counter() - started) * 1000)
        result_indices = [catalog.id_to_index[result['id']]
                          for result in output['results']]
        grades = [relevant.get(index, 0) for index in result_indices]
        top_ten = grades[:10]
        first_relevant = next(
            (rank for rank, value in enumerate(top_ten, 1) if value >= 2), None)
        if item['family'] == 'irrelevant':
            score = float(not result_indices)
        else:
            score = standard_ndcg(top_ten, ideal)
        details.append({
            'id': item['id'],
            'family': item['family'],
            'query': item['query'],
            'score': score,
            'success_at_10': float(any(value >= 2 for value in top_ten)),
            'success_at_100': float(any(value >= 2 for value in grades)),
            **{
                f'recall_at_{depth}': (
                    sum(value >= 2 for value in grades[:depth]) / len(relevant)
                    if relevant else None
                )
                for depth in (20, 50, 100)
            },
            'mrr_at_10': 1 / first_relevant if first_relevant else 0.0,
            'result_ids': [result['id'] for result in output['results'][:10]],
            'grades': top_ten,
        })
    families = {}
    for family in sorted({item['family'] for item in selected}):
        members = [detail for detail in details if detail['family'] == family]
        families[family] = {
            'score': statistics.mean(member['score'] for member in members),
            'success_at_10': statistics.mean(
                member['success_at_10'] for member in members),
            'success_at_100': statistics.mean(
                member['success_at_100'] for member in members),
            'mrr_at_10': statistics.mean(
                member['mrr_at_10'] for member in members),
            'recall_at_100': (
                statistics.mean(
                    member['recall_at_100'] for member in members
                    if member['recall_at_100'] is not None)
                if any(member['recall_at_100'] is not None for member in members)
                else None
            ),
        }
    return {
        'split': split,
        'case_count': len(selected),
        'score': statistics.mean(item['score'] for item in details),
        'success_at_10': statistics.mean(
            item['success_at_10'] for item in details),
        'recall_at_20': statistics.mean(
            item['recall_at_20'] for item in details
            if item['recall_at_20'] is not None),
        'recall_at_50': statistics.mean(
            item['recall_at_50'] for item in details
            if item['recall_at_50'] is not None),
        'recall_at_100': statistics.mean(
            item['recall_at_100'] for item in details
            if item['recall_at_100'] is not None),
        'false_positive_rate': statistics.mean(
            bool(item['result_ids']) for item in details
            if item['family'] == 'irrelevant'),
        'p50_ms': statistics.median(latencies),
        'p95_ms': sorted(latencies)[math.ceil(0.95 * len(latencies)) - 1],
        'families': families,
        'details': details,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--split', choices=('development', 'heldout'),
                        default='development')
    parser.add_argument('--output')
    args = parser.parse_args()
    products = json.loads((ROOT / 'data/products.json').read_text(encoding='utf-8'))
    configurations = {
        'pre_promotion': PrePromotionCatalog,
        'taxonomy_correction': TaxonomyCatalog,
        'expanded_intents': ExpandedIntentCatalog,
        'promoted': Catalog,
        'fts5_promoted_ranking': FTS5Catalog,
    }
    reports = {}
    for name, catalog_type in configurations.items():
        started = time.perf_counter()
        catalog = catalog_type(products)
        startup_ms = (time.perf_counter() - started) * 1000
        report = evaluate(catalog, products, args.split)
        report['startup_ms'] = startup_ms
        reports[name] = report
        if isinstance(catalog, FTS5Catalog):
            catalog.fts.close()
    summary = {
        name: {key: report[key] for key in (
            'split', 'case_count', 'score', 'success_at_10',
            'recall_at_20', 'recall_at_50', 'recall_at_100',
            'false_positive_rate', 'startup_ms', 'p50_ms', 'p95_ms',
            'families')}
        for name, report in reports.items()
    }
    print(json.dumps(summary, indent=2))
    if args.output:
        Path(args.output).write_text(
            json.dumps(reports, indent=2) + '\n', encoding='utf-8')


if __name__ == '__main__':
    main()
