import unittest

from jsonschema import Draft202012Validator

from search import Catalog
from search import OUTPUT_SCHEMA


def product(id, price, rating, count, color='Black', name='Cotton top',
            description='Soft jersey', product_type='Vest top',
            category='Garment Upper body', pattern='Solid', audience='Ladieswear'):
    return dict(id=id, name=name, description=description, category=category,
                product_type=product_type, audience=audience, section='Basics',
                color=color, pattern=pattern, price={'amount':price,'currency':'INR'},
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
        self.assertEqual(self.ids(filters={'min_review_count':0}), ['001','002','003','004'])
        self.assertEqual(self.ids(filters={'min_price':1499}), ['004'])
        self.assertEqual(self.ids(filters={'max_price':499}), ['002'])
        self.assertEqual(self.ids(filters={'color':'unknown'}), [])

    def test_sort_nulls_and_ties(self):
        self.assertEqual(self.ids(sort_by='rating',sort_order='asc'), ['001','004','003','002'])
        self.assertEqual(self.ids(sort_by='rating',sort_order='desc'), ['003','001','004','002'])
        self.assertEqual(self.ids(sort_by='price',sort_order='desc'), ['004','001','003','002'])
        self.assertEqual(self.ids(sort_by='id'), ['004','003','002','001'])

    def test_empty_relevance_is_always_id_ascending(self):
        self.assertEqual(self.ids(query='  ', sort_by='relevance', sort_order='desc'),
                         ['001','002','003','004'])
        self.assertEqual(self.ids(query='', sort_by='relevance', sort_order='asc'),
                         ['001','002','003','004'])

    def test_field_sort_only_orders_relevant_products(self):
        self.assertEqual(self.ids(query='top', sort_by='price', sort_order='desc'),
                         ['001','003','002'])
        self.assertEqual(self.ids(query='top', sort_by='rating', sort_order='asc'),
                         ['001','003','002'])

    def test_pagination_is_repeatable_and_matches_prefix(self):
        page = self.ids(offset=1, limit=2, sort_by='price', sort_order='desc')
        self.assertEqual(page, self.ids(offset=1, limit=2, sort_by='price', sort_order='desc'))
        self.assertEqual(page, self.ids(limit=3, sort_by='price', sort_order='desc')[1:3])

    def test_output_schema_and_unique_real_ids(self):
        result = self.catalog.search({'limit': 4})
        Draft202012Validator(OUTPUT_SCHEMA).validate(result)
        ids = [item['id'] for item in result['results']]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertLessEqual(set(ids), {p['id'] for p in self.catalog.products})
        self.assertEqual(result['recommendations'], [])

    def test_search_and_no_matches(self):
        self.assertEqual(self.ids(query='COTTON top'), ['001','002','003'])
        self.assertEqual(self.ids(query='spaceship engine'), [])
        self.assertEqual(self.ids(query='   '), ['001','002','003','004'])
        self.assertEqual(self.catalog.search({'query':'skirt','include_recommendations':True})['recommendations'], [])

    def test_invalid_arguments(self):
        for args in [{'limit':0},{'limit':True},{'offset':-1},{'sort_by':'unknown'},
                     {'unexpected':1},{'filters':{'color':' '}},
                     {'filters':{'min_price':2,'max_price':1}}, {'query':None},
                     {'filters':{'min_price':float('nan')}}, {'filters':{'min_price':float('inf')}},
                     None, [], {'filters':None}, {'filters':{'min_review_count':True}},
                     {'include_recommendations':None}]:
            with self.subTest(args=args), self.assertRaises(ValueError):
                self.catalog.search(args)


class IntentSearchTests(unittest.TestCase):
    def setUp(self):
        self.catalog = Catalog([
            product('101', 1500, 4, 2, name='Black denim jeans',
                    product_type='Trousers', category='Garment Lower body', pattern='Denim'),
            product('102', 900, 4, 2, color='White', name='White basic tee',
                    product_type='T-shirt'),
            product('103', 2500, 4, 2, name='Premium denim jeans',
                    product_type='Trousers', category='Garment Lower body', pattern='Denim'),
            product('104', 800, 4, 2, name='Black basic tee', product_type='T-shirt'),
            product('105', 1800, 4, 2, color='Red', name='Dinner dress',
                    product_type='Dress', category='Garment Full body'),
            product('106', 700, 4, 2, color='Red', name='Shoulder bag',
                    product_type='Shoulder bag', category='Bags'),
        ])

    def ids(self, **args):
        return [p['id'] for p in self.catalog.search(args)['results']]

    def test_mixed_choices_are_balanced_and_budgeted(self):
        ids = self.ids(query='black jeans and white t-shirts under INR 2000 each', limit=4)
        self.assertEqual(ids[:2], ['101', '102'])
        self.assertNotIn('103', ids)

    def test_complement_clause_is_not_a_primary_choice(self):
        self.assertEqual(
            self.ids(query='dress under INR 2000 with a matching bag under INR 1000'),
            ['105'])
        self.assertEqual(self.ids(query='dress with bag')[:2], ['105', '106'])
        self.assertEqual(self.ids(query='dress plus bag')[:2], ['105', '106'])

    def test_complement_recommendation_respects_scope_and_budget(self):
        arguments = {
            'query': 'dress under INR 2000 with a matching bag under INR 1000',
            'include_recommendations': True,
        }
        result = self.catalog.search(arguments)
        self.assertEqual([item['id'] for item in result['results']], ['105'])
        self.assertEqual([item['id'] for item in result['recommendations']], ['106'])
        recommendation = result['recommendations'][0]
        self.assertEqual(recommendation['for_product_id'], '105')
        self.assertNotIn(recommendation['id'],
                         {item['id'] for item in result['results']})
        Draft202012Validator(OUTPUT_SCHEMA).validate(result)

    def test_recommendations_disabled_or_without_primary(self):
        self.assertEqual(
            self.catalog.search({'query': 'dress with a matching bag'})
            ['recommendations'], [])
        self.assertEqual(
            self.catalog.search({
                'query': 'spaceship with a matching bag',
                'include_recommendations': True,
            })['recommendations'], [])

    def test_explicit_filter_overrides_query_color(self):
        self.assertEqual(self.ids(query='black t-shirt', filters={'color': 'White'}), ['102'])
        self.assertEqual(
            self.ids(query='jeans', filters={'product_type': 'T-shirt'}), ['102', '104'])
        self.assertEqual(
            self.ids(query='jeans', filters={'category': 'Garment Upper body'}),
            ['102', '104'])
        self.assertEqual(
            self.ids(query='white t-shirt',
                     filters={'category': 'Garment Upper body'}), ['102'])

    def test_field_sort_uses_full_mixed_relevant_set(self):
        self.assertEqual(
            self.ids(query='jeans and t-shirts', sort_by='price', sort_order='asc'),
            ['104', '102', '101', '103'])

    def test_catalog_grounded_typo_and_irrelevant_abstention(self):
        self.assertEqual(self.ids(query='tshrit')[:2], ['102', '104'])
        self.assertEqual(self.ids(query='spaceship engine'), [])


class TaxonomyNormalizationTests(unittest.TestCase):
    def setUp(self):
        self.catalog = Catalog([
            product('201', 1000, 4, 2, name='Everyday sneaker',
                    product_type='Sneakers', category='Shoes'),
            product('202', 1200, 4, 2, name='Tailored blazer',
                    product_type='Blazer'),
            product('203', 900, 4, 2, name='Straight trousers',
                    product_type='Trousers', category='Garment Lower body'),
            product('204', 500, 4, 2, name='Small earring',
                    product_type='Earring', category='Accessories'),
            product('205', 700, 4, 2, name='Dragon outfit',
                    product_type='Costumes', category='Garment Full body'),
            product('206', 1500, 4, 2, name='Casual midi',
                    product_type='Dress', category='Garment Full body'),
            product('207', 2000, 4, 2, name='Winter layer',
                    product_type='Coat'),
            product('208', 800, 4, 2, name='Formal button-up',
                    product_type='Shirt'),
        ])

    def ids(self, query):
        return [item['id'] for item in self.catalog.search(
            {'query': query, 'limit': 10})['results']]

    def test_taxonomy_only_typo_correction(self):
        self.assertEqual(self.ids('snikers'), ['201'])
        self.assertEqual(self.ids('blaser'), ['202'])
        self.assertEqual(self.ids('trouzers'), ['203'])
        self.assertEqual(self.ids('earings'), ['204'])

    def test_catalog_inflections(self):
        self.assertEqual(self.ids('costume'), ['205'])
        self.assertEqual(self.ids('dresses'), ['206'])

    def test_typo_correction_does_not_expand_unrelated_terms(self):
        self.assertEqual(self.ids('dresser'), [])
        self.assertEqual(self.ids('fishing sinkers'), [])
        self.assertEqual(self.ids('dressage saddle'), [])

    def test_generalized_shopping_intents(self):
        self.assertEqual(self.ids('warm for a cold commute'), ['207'])
        self.assertEqual(self.ids('outfit for an interview'), ['202', '208', '203'])


if __name__ == '__main__': unittest.main()
