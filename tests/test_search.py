import unittest
from search import Catalog


def product(id, price, rating, count, color='Black', name='Cotton top', description='Soft jersey'):
    return dict(id=id, name=name, description=description, category='Garment Upper body',
                product_type='Vest top', audience='Ladieswear', section='Basics',
                color=color, pattern='Solid', price={'amount':price,'currency':'INR'},
                rating={'average':rating,'count':count})


class SearchTests(unittest.TestCase):
    def setUp(self):
        self.catalog = Catalog([product('001',999,4,2),product('002',499,None,0),
                                product('003',999,5,3,'White'),
                                product('004',1499,4,2,name='Cotton skirt',description=None)])
        self.catalog.products[-1]['product_type'] = 'Skirt'
        self.catalog = Catalog(self.catalog.products)

    def ids(self, **args):
        return [p['id'] for p in self.catalog.search(args)['results']]

    def test_defaults_and_pagination(self):
        self.assertEqual(self.ids(), ['001','002','003','004'])
        self.assertEqual(self.ids(offset=1,limit=2), ['002','003'])

    def test_filters(self):
        self.assertEqual(self.ids(filters={'color':' black ','min_price':999,'max_price':999}), ['001'])
        self.assertEqual(self.ids(filters={'min_rating':4,'min_review_count':3}), ['003'])
        self.assertEqual(self.ids(filters={'color':'unknown'}), [])

    def test_sort_nulls_and_ties(self):
        self.assertEqual(self.ids(sort_by='rating',sort_order='asc'), ['001','004','003','002'])
        self.assertEqual(self.ids(sort_by='price',sort_order='desc'), ['004','001','003','002'])
        self.assertEqual(self.ids(sort_by='id'), ['004','003','002','001'])

    def test_search_and_no_matches(self):
        self.assertEqual(self.ids(query='COTTON top'), ['001','002','003'])
        self.assertEqual(self.ids(query='spaceship engine'), [])
        self.assertEqual(self.ids(query='   '), ['001','002','003','004'])
        self.assertEqual(self.catalog.search({'query':'skirt','include_recommendations':True})['recommendations'], [])

    def test_invalid_arguments(self):
        for args in [{'limit':0},{'limit':True},{'offset':-1},{'sort_by':'unknown'},
                     {'unexpected':1},{'filters':{'color':' '}},
                     {'filters':{'min_price':2,'max_price':1}}, {'query':None},
                     {'filters':{'min_price':float('nan')}}, {'filters':{'min_price':float('inf')}}]:
            with self.subTest(args=args), self.assertRaises(ValueError):
                self.catalog.search(args)


# ----------------------------------------------------------------------------- #
# Added in phase 2. The cases above are the original starter fixtures and stay
# untouched; everything below exercises the staged, indexed implementation.
# ----------------------------------------------------------------------------- #

import math  # noqa: E402
import search as search_module  # noqa: E402
from search import (Request, build_index, fold, parse_request,  # noqa: E402
                    plan_query, validate_arguments)

CATEGORIES = ('Garment Upper body', 'Garment Lower body', 'Accessories', 'Shoes')
TYPES = ('Vest top', 'Dress', 'Trousers', 'Bag', 'Dog Wear', 'Dog wear')
COLORS = ('Black', 'White', 'Dark Blue', 'Light Pink')
AUDIENCES = ('Ladieswear', 'Menswear', 'Divided')
SECTIONS = ('Basics', 'Divided Collection', 'Womens Everyday Collection')
PATTERNS = ('Solid', 'Melange', 'All over pattern')
PRICES = (199.0, 499.0, 999.0, 1499.0, 1999.0, 2999.0, 4499.0)
RATINGS = (None, 1.0, 2.5, 3.0, 3.0, 4.0, 4.5, 5.0)


def synthetic_catalog(count=240):
    """A deterministic fixture spanning every facet, price, and rating shape."""
    products = []
    for i in range(count):
        rating = RATINGS[i % len(RATINGS)]
        products.append(dict(
            id=f'{i:010d}',
            name=f'{COLORS[i % len(COLORS)]} {TYPES[i % len(TYPES)]} number {i}',
            # Some descriptions name a colour the product itself is not, so a query
            # can conflict with an explicit colour filter and still match text.
            description=None if i % 17 == 0 else (
                f'Soft jersey item {i} for everyday wear'
                + (' pairs well with white sneakers' if i % 3 == 0 else '')),
            category=CATEGORIES[i % len(CATEGORIES)],
            product_type=TYPES[i % len(TYPES)],
            audience=AUDIENCES[i % len(AUDIENCES)],
            section=SECTIONS[i % len(SECTIONS)],
            color=COLORS[i % len(COLORS)],
            pattern=PATTERNS[i % len(PATTERNS)],
            image_url=f'https://example.invalid/{i}.jpg',
            price={'amount': PRICES[i % len(PRICES)], 'currency': 'INR', 'synthetic': True},
            rating={'average': rating, 'count': 0 if rating is None else (i % 8) + 1,
                    'synthetic': True},
            reviews=[],
        ))
    return products


def reference_page(catalog, arguments):
    """Brute-force page straight from the brief, independent of the index."""
    request = parse_request(arguments)
    plan = plan_query(request.query)
    rows = [p for p in catalog.products if Catalog.matches(p, request.filters)]
    scores = {}
    if not plan.is_empty:
        scores = {p['id']: catalog.relevance(p, plan.terms) for p in rows}
        rows = [p for p in rows if scores[p['id']] > 0]
    sign = -1 if request.descending else 1
    if request.sort_by == 'relevance':
        if plan.is_empty:
            rows.sort(key=lambda p: p['id'])
        else:
            rows.sort(key=lambda p: (sign * scores[p['id']], p['id']))
    elif request.sort_by == 'id':
        rows.sort(key=lambda p: p['id'], reverse=request.descending)
    elif request.sort_by == 'price':
        rows.sort(key=lambda p: (sign * p['price']['amount'], p['id']))
    elif request.sort_by == 'review_count':
        rows.sort(key=lambda p: (sign * p['rating']['count'], p['id']))
    else:
        rows.sort(key=lambda p: (p['rating']['average'] is None,
                                 sign * (p['rating']['average'] or 0.0), p['id']))
    return [p['id'] for p in rows[request.offset:request.offset + request.limit]]


class ValidationTests(unittest.TestCase):
    def setUp(self):
        self.catalog = Catalog(synthetic_catalog(40))

    def assert_invalid(self, arguments):
        with self.subTest(arguments=arguments), self.assertRaises(ValueError):
            self.catalog.search(arguments)

    def test_wrong_types_and_nulls_are_invalid(self):
        for arguments in [{'query': 5}, {'query': None}, {'query': ['a']},
                          {'filters': None}, {'filters': []}, {'sort_by': None},
                          {'sort_order': None}, {'offset': None}, {'limit': None},
                          {'include_recommendations': None},
                          {'include_recommendations': 'true'},
                          {'offset': 1.5}, {'limit': 10.5}, {'limit': '10'},
                          {'filters': {'color': 1}}, {'filters': {'color': None}},
                          {'filters': {'min_price': '5'}},
                          {'filters': {'min_review_count': 1.5}},
                          {'filters': {'min_review_count': None}}]:
            self.assert_invalid(arguments)

    def test_booleans_are_not_integers(self):
        for arguments in [{'limit': True}, {'offset': True}, {'offset': False},
                          {'filters': {'min_review_count': True}},
                          {'filters': {'min_price': True}},
                          {'filters': {'min_rating': False}}]:
            self.assert_invalid(arguments)

    def test_non_json_floats_are_invalid(self):
        for value in (float('nan'), float('inf'), float('-inf')):
            for key in ('min_price', 'max_price', 'min_rating'):
                self.assert_invalid({'filters': {key: value}})

    def test_out_of_range_values_are_invalid(self):
        for arguments in [{'limit': 0}, {'limit': -1}, {'limit': 101},
                          {'offset': -1}, {'offset': 10001},
                          {'filters': {'min_rating': 0.9}},
                          {'filters': {'min_rating': 5.1}},
                          {'filters': {'min_price': -1}},
                          {'filters': {'max_price': -0.01}},
                          {'filters': {'min_review_count': -1}}]:
            self.assert_invalid(arguments)

    def test_boundary_values_are_valid(self):
        for arguments in [{'limit': 1}, {'limit': 100}, {'offset': 0}, {'offset': 10000},
                          {'filters': {'min_rating': 1}}, {'filters': {'min_rating': 5}},
                          {'filters': {'min_price': 0}}, {'filters': {'max_price': 0}},
                          {'filters': {'min_review_count': 0}},
                          {'filters': {'min_price': 5, 'max_price': 5}},
                          {'query': 'x' * 1000}]:
            with self.subTest(arguments=arguments):
                self.catalog.search(arguments)

    def test_query_over_one_thousand_characters_is_invalid(self):
        self.assert_invalid({'query': 'x' * 1001})

    def test_integer_valued_floats_are_accepted_and_coerced(self):
        """JSON Schema 2020-12 counts 10.0 as an integer, so it must not crash."""
        self.assertEqual(parse_request({'limit': 10.0, 'offset': 2.0}).limit, 10)
        self.assertIsInstance(parse_request({'offset': 2.0}).offset, int)
        self.assertEqual(
            parse_request({'filters': {'min_review_count': 3.0}}).filters,
            {'min_review_count': 3})
        self.assertEqual([r['id'] for r in self.catalog.search({'limit': 3.0})['results']],
                         [r['id'] for r in self.catalog.search({'limit': 3})['results']])

    def test_unknown_properties_are_invalid(self):
        for arguments in [{'unexpected': 1}, {'Query': 'a'}, {'filters': {'brand': 'x'}},
                          {'filters': {'min_Price': 5}}, {'sortBy': 'price'}]:
            self.assert_invalid(arguments)

    def test_blank_string_filters_are_invalid_but_unknown_values_are_not(self):
        for facet in ('category', 'product_type', 'color', 'audience', 'section', 'pattern'):
            self.assert_invalid({'filters': {facet: ''}})
            self.assert_invalid({'filters': {facet: '   '}})
            self.assert_invalid({'filters': {facet: '\t\n'}})
            with self.subTest(facet=facet):
                self.assertEqual(
                    self.catalog.search({'filters': {facet: 'no such value'}}),
                    {'results': [], 'recommendations': []})

    def test_price_bound_conflict_is_invalid(self):
        self.assert_invalid({'filters': {'min_price': 2, 'max_price': 1}})
        self.catalog.search({'filters': {'min_price': 1, 'max_price': 1}})

    def test_defaults_are_applied_by_us(self):
        self.assertEqual(
            parse_request({}),
            Request(query='', filters={}, sort_by='relevance', sort_order='desc',
                    offset=0, limit=10, include_recommendations=False))
        self.assertEqual(parse_request({'limit': 3}).limit, 3)
        self.assertEqual(parse_request({'limit': 3}).offset, 0)

    def test_validate_arguments_does_not_mutate_input(self):
        arguments = {'filters': {'color': ' Black '}}
        validate_arguments(arguments)
        self.assertEqual(arguments, {'filters': {'color': ' Black '}})


class FilterSemanticsTests(unittest.TestCase):
    def setUp(self):
        self.catalog = Catalog(synthetic_catalog())

    def ids(self, **arguments):
        return [r['id'] for r in self.catalog.search(arguments)['results']]

    def test_string_filters_are_trimmed_and_case_insensitive(self):
        expected = self.ids(filters={'color': 'Dark Blue'}, limit=100)
        self.assertTrue(expected)
        for variant in ('dark blue', 'DARK BLUE', '  Dark Blue  ', 'dArK bLuE'):
            with self.subTest(variant=variant):
                self.assertEqual(self.ids(filters={'color': variant}, limit=100), expected)

    def test_case_variant_catalog_values_fold_together(self):
        """The real catalog holds both 'Dog Wear' and 'Dog wear'."""
        folded = {fold('Dog Wear'), fold('Dog wear')}
        self.assertEqual(len(folded), 1)
        matched = self.ids(filters={'product_type': 'Dog Wear'}, limit=100)
        self.assertEqual(self.ids(filters={'product_type': 'Dog wear'}, limit=100), matched)
        raw = {p['product_type'] for p in self.catalog.products if p['id'] in set(matched)}
        self.assertEqual(raw, {'Dog Wear', 'Dog wear'})

    def test_numeric_bounds_are_inclusive(self):
        at_bound = self.ids(filters={'min_price': 999, 'max_price': 999}, limit=100)
        self.assertTrue(at_bound)
        for pid in at_bound:
            self.assertEqual(self.product(pid)['price']['amount'], 999.0)

    def test_null_rating_fails_min_rating_and_is_never_zero(self):
        unrated = {p['id'] for p in self.catalog.products
                   if p['rating']['average'] is None}
        self.assertTrue(unrated)
        for minimum in (1, 2.5, 5):
            with self.subTest(minimum=minimum):
                returned = set(self.ids(filters={'min_rating': minimum}, limit=100))
                self.assertFalse(returned & unrated)

    def test_zero_review_products_satisfy_min_review_count_zero(self):
        zero = {p['id'] for p in self.catalog.products if p['rating']['count'] == 0}
        self.assertTrue(zero)
        returned = set(self.ids(filters={'min_review_count': 0}, limit=100, offset=0))
        all_ids = set(self.ids(limit=100))
        self.assertEqual(returned, all_ids)

    def test_filters_combine_with_and(self):
        arguments = {'filters': {'color': 'Black', 'audience': 'Ladieswear',
                                 'min_price': 500, 'max_price': 2000, 'min_rating': 3,
                                 'min_review_count': 2}, 'limit': 100}
        for pid in self.ids(**arguments):
            row = self.product(pid)
            self.assertEqual(fold(row['color']), 'black')
            self.assertEqual(fold(row['audience']), 'ladieswear')
            self.assertTrue(500 <= row['price']['amount'] <= 2000)
            self.assertGreaterEqual(row['rating']['average'], 3)
            self.assertGreaterEqual(row['rating']['count'], 2)

    def test_explicit_filter_beats_conflicting_query_wording(self):
        """"white" in the query must never widen an explicit Black colour filter."""
        unfiltered = set(self.ids(query='white sneakers', limit=100))
        self.assertTrue(unfiltered)
        ids = self.ids(query='white sneakers', filters={'color': 'Black'}, limit=100)
        self.assertTrue(ids)
        self.assertLess(set(ids), unfiltered)
        for pid in ids:
            self.assertEqual(self.product(pid)['color'], 'Black')

    def test_explicit_filter_wins_even_when_it_empties_the_page(self):
        for filters in ({'color': 'White'}, {'product_type': 'Trousers'},
                        {'max_price': 199}):
            with self.subTest(filters=filters):
                for row in self.catalog.search({'query': 'black dress',
                                                'filters': dict(filters),
                                                'limit': 100})['results']:
                    self.assertTrue(Catalog.matches(self.product(row['id']), filters))

    def test_impossible_filter_combination_is_an_empty_success(self):
        self.assertEqual(
            self.catalog.search({'filters': {'min_price': 5000, 'max_price': 6000,
                                             'category': 'Shoes'}}),
            {'results': [], 'recommendations': []})

    def product(self, pid):
        return self.catalog.products[self.catalog.position_of[pid]]


class OrderingTests(unittest.TestCase):
    def setUp(self):
        # Small enough that limit=100 covers the whole catalog, so the null-rating
        # tail is actually visible on one page.
        self.catalog = Catalog(synthetic_catalog(90))

    def ids(self, **arguments):
        return [r['id'] for r in self.catalog.search(arguments)['results']]

    def row(self, pid):
        return self.catalog.products[self.catalog.position_of[pid]]

    def test_empty_query_relevance_is_id_ascending_in_both_directions(self):
        ascending = sorted(p['id'] for p in self.catalog.products)[:25]
        self.assertEqual(self.ids(limit=25), ascending)
        self.assertEqual(self.ids(limit=25, sort_order='asc'), ascending)
        self.assertEqual(self.ids(limit=25, sort_order='desc'), ascending)

    def test_ties_break_by_id_ascending_in_both_directions(self):
        for sort_by in ('price', 'rating', 'review_count'):
            for sort_order in ('asc', 'desc'):
                with self.subTest(sort_by=sort_by, sort_order=sort_order):
                    ids = self.ids(sort_by=sort_by, sort_order=sort_order, limit=100)
                    keys = [self.sort_value(sort_by, pid) for pid in ids]
                    for i in range(1, len(ids)):
                        if keys[i] == keys[i - 1]:
                            self.assertLess(ids[i - 1], ids[i])

    def sort_value(self, sort_by, pid):
        row = self.row(pid)
        return {'price': row['price']['amount'],
                'rating': row['rating']['average'],
                'review_count': row['rating']['count']}[sort_by]

    def test_null_ratings_are_last_in_both_directions(self):
        for sort_order in ('asc', 'desc'):
            with self.subTest(sort_order=sort_order):
                ids = self.ids(sort_by='rating', sort_order=sort_order, limit=100)
                nulls = [i for i, pid in enumerate(ids)
                         if self.row(pid)['rating']['average'] is None]
                rated = [i for i, pid in enumerate(ids)
                         if self.row(pid)['rating']['average'] is not None]
                self.assertTrue(nulls and rated)
                self.assertGreater(min(nulls), max(rated))

    def test_field_sorting_applies_after_relevance_selection(self):
        relevant = set(self.ids(query='dress', limit=100))
        self.assertTrue(relevant)
        ids = self.ids(query='dress', sort_by='price', sort_order='asc', limit=100)
        self.assertEqual(set(ids), relevant)
        prices = [self.row(pid)['price']['amount'] for pid in ids]
        self.assertEqual(prices, sorted(prices))

    def test_relevance_ascending_reverses_the_relevance_order(self):
        descending = self.ids(query='black dog wear', limit=100)
        ascending = self.ids(query='black dog wear', sort_order='asc', limit=100)
        self.assertEqual(set(descending), set(ascending))
        scores_desc = [self.catalog.relevance(self.row(pid), ('black', 'dog', 'wear'))
                       for pid in descending]
        scores_asc = [self.catalog.relevance(self.row(pid), ('black', 'dog', 'wear'))
                      for pid in ascending]
        self.assertEqual(scores_desc, sorted(scores_desc, reverse=True))
        self.assertEqual(scores_asc, sorted(scores_asc))


class PaginationTests(unittest.TestCase):
    def setUp(self):
        self.catalog = Catalog(synthetic_catalog())

    def ids(self, **arguments):
        return [r['id'] for r in self.catalog.search(arguments)['results']]

    def test_repeated_identical_request_returns_the_same_page(self):
        arguments = {'filters': {'color': 'Black'}, 'sort_by': 'price',
                     'sort_order': 'desc', 'offset': 7, 'limit': 5}
        first = self.ids(**arguments)
        for _ in range(3):
            self.assertEqual(self.ids(**arguments), first)

    def test_pages_are_prefixes_of_a_larger_request(self):
        for sort_by in ('relevance', 'id', 'price', 'rating', 'review_count'):
            for sort_order in ('asc', 'desc'):
                with self.subTest(sort_by=sort_by, sort_order=sort_order):
                    whole = self.ids(sort_by=sort_by, sort_order=sort_order, limit=100)
                    for offset in (0, 1, 13, 40):
                        page = self.ids(sort_by=sort_by, sort_order=sort_order,
                                        offset=offset, limit=7)
                        self.assertEqual(page, whole[offset:offset + 7])

    def test_concatenated_pages_have_no_duplicates_or_gaps(self):
        whole = self.ids(sort_by='price', sort_order='asc', limit=100)
        walked = []
        for offset in range(0, 100, 10):
            walked.extend(self.ids(sort_by='price', sort_order='asc',
                                   offset=offset, limit=10))
        self.assertEqual(walked, whole)
        self.assertEqual(len(set(walked)), len(walked))

    def test_offset_past_the_end_is_an_empty_success(self):
        self.assertEqual(self.catalog.search({'offset': 10000}),
                         {'results': [], 'recommendations': []})
        self.assertEqual(self.catalog.search({'query': 'dress', 'offset': 9999}),
                         {'results': [], 'recommendations': []})

    def test_fewer_than_limit_when_fewer_products_match(self):
        ids = self.ids(filters={'category': 'Shoes', 'color': 'Light Pink'}, limit=100)
        self.assertLess(len(ids), 100)
        self.assertEqual(len(ids), len(set(ids)))


class ContractInvariantTests(unittest.TestCase):
    def setUp(self):
        self.catalog = Catalog(synthetic_catalog())
        self.known = {p['id'] for p in self.catalog.products}

    def test_every_returned_id_is_a_known_catalog_string_id(self):
        for arguments in [{}, {'limit': 100}, {'query': 'dress'},
                          {'query': 'black bag', 'limit': 100},
                          {'filters': {'color': 'Black'}, 'sort_by': 'rating'},
                          {'sort_by': 'review_count', 'sort_order': 'asc', 'limit': 100}]:
            with self.subTest(arguments=arguments):
                for row in self.catalog.search(arguments)['results']:
                    self.assertIsInstance(row['id'], str)
                    self.assertIn(row['id'], self.known)
                    self.assertEqual(set(row), {'id'})

    def test_leading_zeros_are_preserved(self):
        ids = [r['id'] for r in self.catalog.search({'limit': 5})['results']]
        self.assertTrue(all(pid.startswith('0') for pid in ids), ids)

    def test_both_keys_are_always_present(self):
        for arguments in [{}, {'query': 'nothing matches this at all'},
                          {'filters': {'color': 'no such value'}}]:
            with self.subTest(arguments=arguments):
                self.assertEqual(set(self.catalog.search(arguments)),
                                 {'results', 'recommendations'})

    def test_recommendations_empty_when_disabled_or_without_results(self):
        self.assertEqual(self.catalog.search({'query': 'dress'})['recommendations'], [])
        self.assertEqual(
            self.catalog.search({'query': 'dress',
                                 'include_recommendations': False})['recommendations'], [])
        self.assertEqual(
            self.catalog.search({'query': 'spaceship engine parts',
                                 'include_recommendations': True})['recommendations'], [])

    def test_recommendation_invariants_hold_for_whatever_recommend_returns(self):
        response = self.catalog.search({'query': 'dress', 'include_recommendations': True,
                                        'limit': 10})
        primary = [r['id'] for r in response['results']]
        recommendations = response['recommendations']
        self.assertLessEqual(len(recommendations), 5)
        seen = set()
        for row in recommendations:
            self.assertEqual(set(row), {'id', 'for_product_id', 'reason'})
            self.assertIn(row['id'], self.known)
            self.assertNotIn(row['id'], primary)
            self.assertIn(row['for_product_id'], primary)
            self.assertNotIn(row['id'], seen)
            self.assertTrue(1 <= len(row['reason']) <= 300)
            seen.add(row['id'])


class CandidatePathEquivalenceTests(unittest.TestCase):
    """The selective-source path and the ordered-walk path must agree exactly."""

    FILTER_SETS = (
        {}, {'color': 'Black'}, {'product_type': 'Dress'}, {'category': 'Shoes'},
        {'audience': 'Menswear'}, {'section': 'Basics'}, {'pattern': 'Melange'},
        {'product_type': 'dog wear'}, {'color': 'no such value'},
        {'max_price': 499}, {'min_price': 4499}, {'min_price': 999, 'max_price': 999},
        {'min_rating': 4.5}, {'min_rating': 1}, {'min_review_count': 7},
        {'min_review_count': 0},
        {'color': 'Black', 'min_rating': 3, 'max_price': 1999},
        {'category': 'Accessories', 'pattern': 'Solid', 'min_price': 400,
         'max_price': 3000, 'min_rating': 2.5, 'min_review_count': 1},
    )
    SORTS = (('relevance', 'desc'), ('relevance', 'asc'), ('id', 'asc'), ('id', 'desc'),
             ('price', 'asc'), ('price', 'desc'), ('rating', 'asc'), ('rating', 'desc'),
             ('review_count', 'asc'), ('review_count', 'desc'))
    QUERIES = ('', '  ', 'dress', 'black dog wear', 'spaceship engine')
    PAGES = ((0, 10), (0, 1), (3, 5), (30, 25), (0, 100))

    def setUp(self):
        self.catalog = Catalog(synthetic_catalog())
        self.original_ratio = search_module.MATERIALIZE_RATIO
        self.addCleanup(setattr, search_module, 'MATERIALIZE_RATIO', self.original_ratio)

    def pages(self, arguments):
        search_module.MATERIALIZE_RATIO = 0  # always take the selective-source path
        materialized = [r['id'] for r in self.catalog.search(arguments)['results']]
        search_module.MATERIALIZE_RATIO = 10 ** 9  # always take the ordered-walk path
        walked = [r['id'] for r in self.catalog.search(arguments)['results']]
        return materialized, walked

    def test_both_paths_match_the_brute_force_reference(self):
        checked = 0
        for filters in self.FILTER_SETS:
            for sort_by, sort_order in self.SORTS:
                for query in self.QUERIES:
                    for offset, limit in self.PAGES:
                        arguments = {'query': query, 'filters': dict(filters),
                                     'sort_by': sort_by, 'sort_order': sort_order,
                                     'offset': offset, 'limit': limit}
                        expected = reference_page(self.catalog, arguments)
                        materialized, walked = self.pages(arguments)
                        with self.subTest(arguments=arguments):
                            self.assertEqual(materialized, expected)
                            self.assertEqual(walked, expected)
                        checked += 1
        self.assertGreater(checked, 1000)


class IndexTests(unittest.TestCase):
    def setUp(self):
        self.products = synthetic_catalog()
        self.catalog = Catalog(self.products)
        self.index = self.catalog.index

    def test_products_are_stored_id_ascending(self):
        ids = [p['id'] for p in self.catalog.products]
        self.assertEqual(ids, sorted(ids))
        self.assertEqual(self.index.ids, tuple(ids))

    def test_orders_cover_every_field_and_direction(self):
        for field in ('id', 'price', 'rating', 'review_count'):
            for direction in ('asc', 'desc'):
                order = self.index.orders[(field, direction)]
                self.assertEqual(len(order), self.index.size)
                self.assertEqual(sorted(order), list(range(self.index.size)))

    def test_postings_partition_the_catalog_per_facet(self):
        for facet, buckets in self.index.postings.items():
            positions = sorted(p for bucket in buckets.values() for p in bucket)
            self.assertEqual(positions, list(range(self.index.size)), facet)
            for value, bucket in buckets.items():
                self.assertEqual(list(bucket), sorted(bucket))
                for position in bucket:
                    self.assertEqual(fold(self.products[position][facet]), value)

    def test_matches_agrees_with_the_acceptor_for_every_position(self):
        for filters in CandidatePathEquivalenceTests.FILTER_SETS:
            accept = search_module._acceptor(self.index, filters)
            with self.subTest(filters=filters):
                for position, row in enumerate(self.catalog.products):
                    expected = Catalog.matches(row, filters)
                    actual = True if accept is None else accept(position)
                    self.assertEqual(actual, expected, (position, filters))

    def test_null_description_and_missing_optional_fields_are_tolerated(self):
        rows = [{'id': '0000000001', 'name': 'Plain tee', 'description': None,
                 'category': 'Garment Upper body', 'product_type': 'Vest top',
                 'audience': 'Ladieswear', 'section': 'Basics', 'color': 'Black',
                 'pattern': 'Solid', 'price': {'amount': 499, 'currency': 'INR'},
                 'rating': {'average': None, 'count': 0}}]
        catalog = Catalog(rows)
        self.assertEqual(catalog.search({'query': 'plain tee'})['results'],
                         [{'id': '0000000001'}])
        self.assertEqual(catalog.search({'filters': {'min_rating': 1}})['results'], [])


class QueryPlanTests(unittest.TestCase):
    def test_whitespace_only_query_is_empty(self):
        for text in ('', '   ', '\t', '\n  \n'):
            self.assertTrue(plan_query(text).is_empty, repr(text))

    def test_tokens_are_case_folded(self):
        self.assertEqual(plan_query(' Black DRESS ').terms, ('black', 'dress'))

    def test_unicode_query_does_not_crash(self):
        catalog = Catalog(synthetic_catalog(40))
        self.assertEqual(catalog.search({'query': 'robe noire 👗 für Damen'}),
                         {'results': [], 'recommendations': []})


if __name__ == '__main__': unittest.main()
