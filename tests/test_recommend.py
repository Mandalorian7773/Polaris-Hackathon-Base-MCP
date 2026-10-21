"""Complementary recommendations: contract invariants and complement quality."""
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tests'))

from recommend import (Complements, GLOBAL_FILTERS, KIND_FILTERS,  # noqa: E402
                       RecommendationParams, Recommender)
from search import Catalog  # noqa: E402
from test_retrieval import fixture_products  # noqa: E402

#: Extra accessories so complement tests have more than one candidate per type.
#: (product_type, category, color, pattern, audience, section, name, description)
ACCESSORY_ROWS = [
    ('Bag', 'Accessories', 'Black', 'Solid', 'Ladies Accessories',
     'Womens Big accessories', 'Nora shopper', 'Shopper in imitation leather.'),
    ('Bag', 'Accessories', 'Beige', 'Solid', 'Ladies Accessories',
     'Womens Big accessories', 'Suki tote', 'Tote bag in woven straw.'),
    ('Bag', 'Accessories', 'Dark Red', 'Solid', 'Ladieswear',
     'Womens Small accessories', 'Mila clutch', 'Small clutch bag with a chain.'),
    ('Tie', 'Accessories', 'Dark Blue', 'Stripe', 'Menswear', 'Men Accessories',
     'Oscar tie', 'Tie in a woven silk blend.'),
    ('Tie', 'Accessories', 'Black', 'Solid', 'Menswear', 'Men Accessories',
     'Felix tie', 'Narrow tie in a woven fabric.'),
    ('Socks', 'Socks & Tights', 'Grey', 'Melange', 'Ladieswear',
     'Womens Nightwear, Socks & Tigh', 'Ankle 2pk', 'Socks in a fine-knit blend.'),
    ('Socks', 'Socks & Tights', 'Black', 'Solid', 'Menswear', 'Men Underwear',
     'Trainer 5pk', 'Trainer socks in a cotton blend.'),
    ('Belt', 'Accessories', 'Black', 'Solid', 'Ladies Accessories',
     'Womens Small accessories', 'Vera belt', 'Narrow belt with a metal buckle.'),
    ('Scarf', 'Accessories', 'Light Grey', 'Melange', 'Ladieswear',
     'Womens Big accessories', 'Luna scarf', 'Scarf in a soft knitted blend.'),
    ('Gloves', 'Accessories', 'Dark Beige', 'Solid', 'Ladieswear',
     'Womens Big accessories', 'Iris gloves', 'Gloves in a knitted wool blend.'),
    ('Heeled sandals', 'Shoes', 'Black', 'Solid', 'Ladieswear', 'Womens Shoes',
     'Vega sandal', 'Heeled sandals in imitation leather.'),
    ('Necklace', 'Accessories', 'Gold', 'Solid', 'Ladies Accessories',
     'Womens Small accessories', 'Halo chain', 'Necklace with a small pendant.'),
]


def recommend_fixture():
    """The shared fixture plus enough accessories to choose between."""
    products = fixture_products()
    start = len(products)
    for offset, row in enumerate(ACCESSORY_ROWS):
        index = start + offset
        product_type, category, color, pattern, audience, section, name, desc = row
        average = None if index % 11 == 0 else 3.0 + (index % 5) * 0.5
        products.append({
            'id': f'{index:010d}',
            'name': name, 'description': desc, 'product_type': product_type,
            'category': category, 'color': color, 'pattern': pattern,
            'audience': audience, 'section': section,
            'image_url': f'https://example.invalid/{index}.jpg',
            'price': {'amount': 399.0 + 150 * offset, 'currency': 'INR',
                      'synthetic': True},
            'rating': {'average': average,
                       'count': 0 if average is None else (index % 7) + 1,
                       'synthetic': True},
            'reviews': [],
        })
    return products


class ComplementMapTests(unittest.TestCase):
    def setUp(self):
        self.complements = Complements.load()

    def test_same_role_detects_substitutes(self):
        self.assertTrue(self.complements.same_role('dress', 'dress'))
        self.assertTrue(self.complements.same_role('dress', 'jumpsuit/playsuit'))
        self.assertTrue(self.complements.same_role('sneakers', 'boots'))
        self.assertTrue(self.complements.same_role('bag', 'backpack'))
        self.assertTrue(self.complements.same_role('watch', 'bracelet'))

    def test_different_body_slots_are_not_substitutes(self):
        """A necklace with earrings, a cardigan over a tee, tights under a skirt."""
        for left, right in (('necklace', 'earring'), ('cardigan', 't-shirt'),
                            ('skirt', 'leggings/tights'), ('robe', 'pyjama set'),
                            ('dress', 'bag'), ('trousers', 'belt')):
            with self.subTest(pair=(left, right)):
                self.assertFalse(self.complements.same_role(left, right))

    def test_no_list_recommends_a_substitute(self):
        for key, values in self.complements.complements.items():
            for value in values:
                with self.subTest(key=key, value=value):
                    self.assertFalse(
                        self.complements.same_role(key, value.strip().casefold()))

    def test_types_without_a_useful_complement_return_nothing(self):
        for product_type in ('dog wear', 'zipper head', 'washing bag', 'sewing kit'):
            with self.subTest(product_type=product_type):
                self.assertEqual(
                    self.complements.ranked_for(product_type, 'items'), ())

    def test_category_fallback_covers_an_unmapped_type(self):
        ranked = self.complements.ranked_for('a type that does not exist',
                                             'garment upper body')
        self.assertTrue(ranked)

    def test_audience_compatibility_blocks_cross_audience_pairs(self):
        self.assertFalse(self.complements.audience_ok('ladieswear', 'menswear'))
        self.assertFalse(self.complements.audience_ok('menswear', 'ladieswear'))
        self.assertFalse(self.complements.audience_ok('ladieswear',
                                                      'baby sizes 50-98'))
        self.assertTrue(self.complements.audience_ok('ladieswear', 'ladieswear'))
        self.assertTrue(self.complements.audience_ok('ladieswear', 'sport'))
        self.assertTrue(self.complements.audience_ok('baby sizes 50-98',
                                                     'children sizes 92-140'))

    def test_every_target_exists_in_the_real_catalog_vocabulary(self):
        stats_path = ROOT / 'analysis/catalog_stats.json'
        if not stats_path.exists():
            self.skipTest('analysis/catalog_stats.json not generated')
        stats = json.loads(stats_path.read_text(encoding='utf-8'))

        def folded(field):
            return {v.strip().casefold() for v in stats['facets'][field]['values']}

        self.assertEqual(
            self.complements.unknown_values(folded('product_type'),
                                            folded('category'), folded('color'),
                                            folded('audience')), [])


class RecommendationContractTests(unittest.TestCase):
    def setUp(self):
        self.catalog = Catalog(recommend_fixture())
        self.known = {p['id'] for p in self.catalog.products}

    def response(self, query, **rest):
        return self.catalog.search({'query': query, 'include_recommendations': True,
                                    'limit': 10, **rest})

    def assert_invariants(self, response):
        primary = [r['id'] for r in response['results']]
        recommendations = response['recommendations']
        self.assertLessEqual(len(recommendations), 5)
        seen = set()
        for row in recommendations:
            self.assertEqual(set(row), {'id', 'for_product_id', 'reason'})
            self.assertIn(row['id'], self.known)
            self.assertNotIn(row['id'], primary, 'a primary result is not a complement')
            self.assertNotIn(row['id'], seen, 'recommendation ids must be unique')
            self.assertIn(row['for_product_id'], primary,
                          'for_product_id must be on the current page')
            self.assertTrue(1 <= len(row['reason']) <= 300)
            seen.add(row['id'])
        return recommendations

    def test_invariants_hold_across_many_queries(self):
        for query in ('a black dress with a matching bag', 'sneakers', 'a dress',
                      'denim trousers with a belt to go with them', 'a swimsuit',
                      'a coat for winter', 'sunglasses', 'socks', 'a scarf',
                      'gloves and a scarf', 'dog wear'):
            with self.subTest(query=query):
                self.assert_invariants(self.response(query))

    def test_empty_when_disabled(self):
        response = self.catalog.search({'query': 'a black dress with a matching bag',
                                        'include_recommendations': False})
        self.assertEqual(response['recommendations'], [])

    def test_empty_when_there_are_no_results(self):
        response = self.response('spaceship engine parts')
        self.assertEqual(response['results'], [])
        self.assertEqual(response['recommendations'], [])

    def test_empty_when_the_primary_has_no_useful_complement(self):
        response = self.response('dog wear')
        self.assertTrue(response['results'])
        self.assertEqual(response['recommendations'], [])

    def test_recommendations_are_never_substitutes(self):
        response = self.response('a black dress with a matching bag')
        primary_types = {self.row(r['id'])['product_type'].casefold()
                         for r in response['results']}
        for row in response['recommendations']:
            candidate = self.row(row['id'])['product_type'].casefold()
            anchor = self.row(row['for_product_id'])['product_type'].casefold()
            self.assertFalse(self.catalog.complements.same_role(anchor, candidate))
            self.assertNotIn(candidate, primary_types)

    def test_requested_accessory_type_is_what_is_returned(self):
        response = self.response('a black dress with a matching bag')
        self.assertTrue(response['recommendations'])
        for row in response['recommendations']:
            self.assertEqual(self.row(row['id'])['product_type'], 'Bag')

    def test_reason_only_uses_catalog_attributes(self):
        response = self.response('a black dress with a matching bag')
        for row in response['recommendations']:
            candidate = self.row(row['id'])
            self.assertIn(candidate['color'].casefold(), row['reason'].casefold())
            self.assertIn(candidate['product_type'].casefold(),
                          row['reason'].casefold())

    def test_determinism(self):
        arguments = {'query': 'a black dress with a matching bag',
                     'include_recommendations': True, 'limit': 5}
        first = self.catalog.search(dict(arguments))
        for _ in range(3):
            self.assertEqual(self.catalog.search(dict(arguments)), first)

    def row(self, product_id):
        return self.catalog.products[self.catalog.position_of[product_id]]


class RecommendationConstraintTests(unittest.TestCase):
    def setUp(self):
        self.catalog = Catalog(recommend_fixture())
        self.recommender = self.catalog.recommender

    def row(self, product_id):
        return self.catalog.products[self.catalog.position_of[product_id]]

    def test_kind_filters_do_not_carry_over_to_recommendations(self):
        """A complement is a different kind of product by definition."""
        filters = self.recommender.recommendation_filters(
            {'category': 'Garment Full body', 'color': 'Black',
             'product_type': 'Dress', 'pattern': 'Solid', 'section': 'Womens Casual'},
            None)
        self.assertEqual(filters, {})
        for key in KIND_FILTERS:
            self.assertNotIn(key, filters)

    def test_global_requirements_do_carry_over(self):
        filters = self.recommender.recommendation_filters(
            {'audience': 'Ladieswear', 'min_rating': 4, 'min_review_count': 2,
             'max_price': 1500, 'color': 'Black'}, None)
        self.assertEqual(filters, {'audience': 'Ladieswear', 'min_rating': 4,
                                   'min_review_count': 2, 'max_price': 1500})
        for key in filters:
            self.assertIn(key, GLOBAL_FILTERS)

    def test_accessory_budget_replaces_an_inherited_one(self):
        filters = self.recommender.recommendation_filters(
            {'min_price': 2000, 'max_price': 5000, 'audience': 'Ladieswear'},
            {'max_price': 1000})
        self.assertEqual(filters, {'audience': 'Ladieswear', 'max_price': 1000})

    def test_explicit_audience_filter_constrains_recommendations_by_family(self):
        """A ladies' handbag is filed under Ladies Accessories, not Ladieswear."""
        response = self.catalog.search(
            {'query': 'a dress with a matching bag', 'include_recommendations': True,
             'filters': {'audience': 'Ladieswear'}, 'limit': 5})
        self.assertTrue(response['recommendations'])
        for row in response['recommendations']:
            self.assertTrue(self.catalog.complements.audience_ok(
                'ladieswear', self.row(row['id'])['audience'].casefold()))

    def test_an_explicit_audience_still_excludes_the_wrong_family(self):
        response = self.catalog.search(
            {'query': 'a shirt with a matching tie', 'include_recommendations': True,
             'filters': {'audience': 'Menswear'}, 'limit': 5})
        for row in response['recommendations']:
            self.assertFalse(self.catalog.complements.audience_ok(
                'menswear', 'baby sizes 50-98'))
            self.assertTrue(self.catalog.complements.audience_ok(
                'menswear', self.row(row['id'])['audience'].casefold()))

    def test_explicit_colour_filter_does_not_constrain_recommendations(self):
        """The brief allows a complement in a different colour."""
        response = self.catalog.search(
            {'query': 'a black dress with a matching bag',
             'include_recommendations': True, 'filters': {'color': 'Black'},
             'limit': 5})
        self.assertTrue(response['recommendations'])
        for row in response['results']:
            self.assertEqual(self.row(row['id'])['color'], 'Black')

    def test_accessory_budget_from_the_query_is_enforced(self):
        response = self.catalog.search(
            {'query': 'a dress with a bag under 1500', 'limit': 5,
             'include_recommendations': True})
        self.assertTrue(response['recommendations'])
        for row in response['recommendations']:
            self.assertLessEqual(self.row(row['id'])['price']['amount'], 1500)

    def test_cross_audience_complements_are_excluded(self):
        response = self.catalog.search(
            {'query': 'a dress with a matching bag', 'include_recommendations': True,
             'limit': 10})
        for row in response['recommendations']:
            anchor = self.row(row['for_product_id'])
            candidate = self.row(row['id'])
            self.assertTrue(self.catalog.complements.audience_ok(
                anchor['audience'].casefold(), candidate['audience'].casefold()))


class RecommenderInternalTests(unittest.TestCase):
    def setUp(self):
        self.catalog = Catalog(recommend_fixture())
        self.recommender = self.catalog.recommender

    def test_quality_order_puts_rated_products_first(self):
        for product_type, positions in self.recommender.quality_order.items():
            ratings = [self.catalog.index.ratings[p] for p in positions]
            rated = [r for r in ratings if r is not None]
            with self.subTest(product_type=product_type):
                self.assertEqual(rated, sorted(rated, reverse=True))
                if None in ratings:
                    self.assertEqual(ratings.index(None), len(rated))

    def test_category_expansion_returns_types_in_that_category(self):
        types = self.recommender.expand_category('accessories')
        self.assertTrue(types)
        for product_type in types:
            positions = self.catalog.index.postings['product_type'][product_type]
            self.assertEqual(
                {self.catalog.index.folded['category'][p] for p in positions},
                {'accessories'})

    def test_build_returns_nothing_without_primaries(self):
        self.assertEqual(self.recommender.build([], {}), [])

    def test_limit_is_respected_even_with_many_anchors(self):
        positions = list(range(len(self.catalog.products)))[:10]
        self.assertLessEqual(len(self.recommender.build(positions, {})), 5)

    def test_accessory_targets_resolve_from_the_query(self):
        from search import parse_request
        request = parse_request({'query': 'a dress with a matching bag',
                                 'include_recommendations': True})
        plan = self.catalog.plan_shopping(request)
        types, budget = self.catalog.accessory_targets(plan)
        self.assertIn('bag', types)
        self.assertEqual(budget, {})

    def test_accessory_targets_carry_their_own_budget(self):
        from search import parse_request
        request = parse_request({'query': 'a dress with a bag under 900',
                                 'include_recommendations': True})
        plan = self.catalog.plan_shopping(request)
        types, budget = self.catalog.accessory_targets(plan)
        self.assertIn('bag', types)
        self.assertEqual(budget, {'max_price': 900.0})

    def test_custom_params_are_honoured(self):
        catalog = Catalog(recommend_fixture(),
                          recommendation_params=RecommendationParams(limit=2))
        response = catalog.search({'query': 'a dress with a matching bag',
                                   'include_recommendations': True, 'limit': 5})
        self.assertLessEqual(len(response['recommendations']), 2)


if __name__ == '__main__':
    unittest.main()
