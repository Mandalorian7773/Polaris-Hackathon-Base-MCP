"""Shopping-intent planning: budgets, mixed choices, and complement cues.

The planner only needs to know whether a fragment names a kind of product, so these
tests inject a small vocabulary instead of building a catalog index.
"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from query_planner import (Budget, ShoppingPlanner, parse_amount,  # noqa: E402
                           parse_budget)

#: Words that name a kind of product in the catalog, for the injected decider.
PRODUCT_WORDS = {
    'dress', 'dresses', 'bag', 'bags', 'jeans', 'trousers', 'shirt', 'shirts',
    't-shirt', 't-shirts', 'tshirt', 'tee', 'sneakers', 'socks', 'gloves', 'scarf',
    'belt', 'tie', 'hat', 'beanie', 'heels', 'sandals', 'shorts', 'blazer', 'coat',
    'sarong', 'swimsuit', 'bikini', 'bra', 'necklace', 'earrings', 'skirt',
    'cardigan', 'sweater', 'hoodie', 'boots', 'sunglasses', 'wallet', 'gown',
    'pyjama', 'pyjamas', 'bottoms', 'bottom', 'top', 'shoes', 'umbrella',
}


def names_product(text):
    return any(word in PRODUCT_WORDS for word in text.casefold().split())


class AmountTests(unittest.TestCase):
    def test_plain_and_grouped_numbers(self):
        self.assertEqual(parse_amount('2000', None), 2000.0)
        self.assertEqual(parse_amount('2,000', None), 2000.0)
        self.assertEqual(parse_amount('1999.5', None), 1999.5)

    def test_multipliers(self):
        self.assertEqual(parse_amount('1', 'k'), 1000.0)
        self.assertEqual(parse_amount('1.5', 'k'), 1500.0)
        self.assertEqual(parse_amount('2', 'lakh'), 200000.0)

    def test_unparseable_amount(self):
        self.assertIsNone(parse_amount('', None))


class BudgetParsingTests(unittest.TestCase):
    def assert_budget(self, text, minimum, maximum):
        budget, _ = parse_budget(text)
        with self.subTest(text=text):
            self.assertEqual((budget.min_price, budget.max_price), (minimum, maximum))

    def test_upper_bound_phrasings(self):
        for text in ('a dress under 2000', 'a dress below INR 2000',
                     'a dress less than rs 2000', 'a dress within ₹2000',
                     'a dress up to 2000', 'a dress upto 2000',
                     'a dress at most 2000', 'a dress no more than 2000',
                     'a dress max 2000', 'a dress cheaper than 2000',
                     'a dress < 2000', 'a dress <= 2000',
                     'a dress 2000 or less', 'a dress 2000 and under'):
            self.assert_budget(text, None, 2000.0)

    def test_lower_bound_phrasings(self):
        for text in ('trousers over 1000', 'trousers above INR 1000',
                     'trousers more than 1000', 'trousers at least 1000',
                     'trousers minimum 1000', 'trousers starting at 1000',
                     'trousers > 1000', 'trousers 1000 or more',
                     'trousers 1000 and above'):
            self.assert_budget(text, 1000.0, None)

    def test_ranges(self):
        self.assert_budget('jeans between 1000 and 2000', 1000.0, 2000.0)
        self.assert_budget('jeans from 500 to 1500', 500.0, 1500.0)
        self.assert_budget('jeans between ₹1k and ₹2k', 1000.0, 2000.0)

    def test_abbreviated_and_marked_amounts(self):
        self.assert_budget('a coat below Rs 1.5k', None, 1500.0)
        self.assert_budget('a coat under ₹3,000', None, 3000.0)
        self.assert_budget('a coat under INR 899', None, 899.0)

    def test_no_budget_is_empty(self):
        budget, remainder = parse_budget('a black dress for a party')
        self.assertTrue(budget.is_empty)
        self.assertEqual(remainder, 'a black dress for a party')

    def test_budget_wording_is_removed_from_the_text(self):
        _, remainder = parse_budget('a sequin dress up to ₹3,000')
        self.assertEqual(remainder, 'a sequin dress')

    def test_contradictory_bounds_keep_the_upper_one(self):
        self.assert_budget('a dress over 5000 under 1000', None, 1000.0)

    def test_bare_numbers_are_not_budgets(self):
        self.assert_budget('socks 3 pack', None, None)
        self.assert_budget('car tyres 205/55 R16', None, None)

    def test_merge_only_fills_missing_bounds(self):
        self.assertEqual(Budget(None, 1000.0).merge(Budget(500.0, 2000.0)),
                         Budget(500.0, 1000.0))
        self.assertEqual(Budget().merge(Budget(1.0, 2.0)), Budget(1.0, 2.0))


class PlanTests(unittest.TestCase):
    def setUp(self):
        self.planner = ShoppingPlanner(names_product=names_product)

    def plan(self, text):
        return self.planner.plan(text)

    def primary(self, text):
        return [i.text for i in self.plan(text).primary]

    def accessories(self, text):
        return [i.text for i in self.plan(text).accessories]

    # -- single intent ----------------------------------------------------- #

    def test_empty_query_plans_nothing(self):
        for text in ('', '   ', None):
            self.assertTrue(self.plan(text).is_empty)

    def test_single_item_stays_single(self):
        self.assertEqual(self.primary('a black dress'), ['a black dress'])
        self.assertEqual(self.primary('sneakers'), ['sneakers'])

    # -- mixed choices ----------------------------------------------------- #

    def test_and_splits_named_choices(self):
        self.assertEqual(self.primary('black jeans and white t-shirts'),
                         ['black jeans', 'white t-shirts'])
        self.assertEqual(self.primary('a dress and a bag'), ['a dress', 'a bag'])

    def test_or_and_commas_split_too(self):
        self.assertEqual(self.primary('sunglasses or a hat'), ['sunglasses', 'a hat'])
        self.assertEqual(self.primary('a scarf, a beanie and gloves'),
                         ['a scarf', 'a beanie', 'gloves'])

    def test_three_choices_share_one_plan(self):
        plan = self.plan('a scarf, a beanie and gloves')
        self.assertTrue(plan.is_mixed)
        self.assertEqual(len(plan.primary), 3)

    def test_a_fragment_naming_no_product_is_not_its_own_choice(self):
        """"black and white dress" is one item, not "black" plus "white dress"."""
        self.assertEqual(self.primary('black and white dress'),
                         ['black and white dress'])
        self.assertEqual(self.primary('a red or blue dress'), ['a red or blue dress'])

    def test_trailing_qualifier_attaches_to_its_choice(self):
        self.assertEqual(self.primary('socks and gloves for winter'),
                         ['socks', 'gloves for winter'])

    # -- complement cues --------------------------------------------------- #

    def test_matching_cue_makes_an_accessory(self):
        plan = self.plan('a black dress with a matching bag')
        self.assertEqual([i.text for i in plan.primary], ['a black dress'])
        self.assertEqual([i.text for i in plan.accessories], ['bag'])
        self.assertTrue(plan.complement_requested)

    def test_trailing_cues_make_an_accessory(self):
        for text, primary, accessory in (
                ('denim trousers with a belt to go with them',
                 'denim trousers', 'a belt'),
                ('a swimsuit with a sarong to match', 'a swimsuit', 'a sarong'),
                ('sneakers with socks to go with them', 'sneakers', 'socks'),
                ('a shirt with a tie that matches', 'a shirt', 'a tie'),
                ('a dress and a bag to complete the look', 'a dress', 'a bag')):
            with self.subTest(text=text):
                self.assertEqual(self.primary(text), [primary])
                self.assertEqual(self.accessories(text), [accessory])

    def test_plain_with_is_an_accessory_only_when_both_sides_name_products(self):
        self.assertEqual(self.accessories('a dress with a bag'), ['a bag'])
        self.assertEqual(self.accessories('a dress with a floral pattern'), [])
        self.assertEqual(self.primary('a dress with a floral pattern'),
                         ['a dress with a floral pattern'])

    def test_several_accessories_are_each_planned(self):
        self.assertEqual(self.accessories('a coat with gloves and a scarf to match'),
                         ['gloves', 'a scarf'])

    def test_accessory_first_questions_reverse_the_surface_order(self):
        for text, primary, accessory in (
                ('what bag goes with a dress?', 'a dress', 'bag'),
                ('which scarf would go best with a coat?', 'a coat', 'scarf'),
                ('which shoes should I pair with a dress?', 'a dress', 'shoes')):
            with self.subTest(text=text):
                self.assertEqual(self.primary(text), [primary])
                self.assertEqual(self.accessories(text), [accessory])

    def test_generic_relation_question_keeps_the_named_anchor(self):
        plan = self.plan('what goes with a dress?')
        self.assertEqual([intent.text for intent in plan.primary], ['a dress'])
        self.assertEqual(plan.accessories, ())
        self.assertTrue(plan.complement_requested)

    def test_relational_imperatives_have_explicit_orientation(self):
        for text, primary, accessory in (
                ('recommend a scarf for a coat', 'a coat', 'a scarf'),
                ('recommend a tie to go with a shirt', 'a shirt', 'a tie'),
                ('pair a dress with shoes', 'a dress', 'shoes'),
                ('a dress to pair with a bag', 'a dress', 'a bag'),
                ('accessorize a dress with a necklace', 'a dress', 'a necklace'),
                ('complete the look with a bag for a dress', 'a dress', 'a bag')):
            with self.subTest(text=text):
                self.assertEqual(self.primary(text), [primary])
                self.assertEqual(self.accessories(text), [accessory])

    def test_repeated_inline_cues_keep_all_accessories_out_of_primary(self):
        plan = self.plan(
            'a dress with a matching bag and coordinating shoes')
        self.assertEqual([intent.text for intent in plan.primary], ['a dress'])
        self.assertEqual([intent.text for intent in plan.accessories],
                         ['bag', 'coordinating shoes'])

    def test_plus_and_and_can_introduce_trailing_accessories(self):
        for text in ('a dress plus a bag to match',
                     'a dress and a bag to complete the look'):
            with self.subTest(text=text):
                self.assertEqual(self.primary(text), ['a dress'])
                self.assertEqual(self.accessories(text), ['a bag'])

    def test_a_lone_accessory_phrase_is_the_primary_ask(self):
        """Nothing to match it against, so the bag is what the shopper wants."""
        plan = self.plan('a matching bag')
        self.assertEqual([i.text for i in plan.primary], ['a matching bag'])
        self.assertEqual(plan.accessories, ())
        self.assertFalse(plan.complement_requested)

    def test_a_cue_with_an_empty_left_side_still_yields_a_primary_intent(self):
        plan = self.plan('with a matching bag')
        self.assertTrue(plan.primary)
        self.assertEqual(plan.accessories, ())
        self.assertFalse(plan.complement_requested)

    # -- budgets across intents -------------------------------------------- #

    def test_budget_attaches_to_its_own_item(self):
        plan = self.plan('a dress under 2000 with a bag under 1000')
        self.assertEqual(plan.primary[0].budget, Budget(None, 2000.0))
        self.assertEqual(plan.accessories[0].budget, Budget(None, 1000.0))

    def test_each_repeats_one_budget_across_choices(self):
        plan = self.plan('a red dress and black heels under 3000 each')
        self.assertTrue(plan.per_item_budget)
        self.assertEqual([i.budget for i in plan.primary],
                         [Budget(None, 3000.0), Budget(None, 3000.0)])

    def test_an_unqualified_budget_reaches_the_accessory(self):
        plan = self.plan('a dress under 2000 with a matching bag')
        self.assertEqual(plan.primary[0].budget, Budget(None, 2000.0))
        self.assertEqual(plan.accessories[0].budget, Budget(None, 2000.0))

    def test_impossible_budget_is_still_a_budget(self):
        plan = self.plan('a dress for under INR 50')
        self.assertEqual(plan.primary[0].budget, Budget(None, 50.0))

    def test_budget_filters_use_the_published_filter_names(self):
        plan = self.plan('jeans between 1000 and 2000')
        self.assertEqual(plan.primary[0].budget.as_filters(),
                         {'min_price': 1000.0, 'max_price': 2000.0})

    def test_planning_is_deterministic(self):
        text = 'a red dress and black heels under 3000 each with a bag to match'
        first = self.planner.plan(text)
        for _ in range(3):
            self.assertEqual(self.planner.plan(text), first)


class RoleTests(unittest.TestCase):
    def setUp(self):
        self.planner = ShoppingPlanner(names_product=names_product)

    def test_roles_are_labelled(self):
        plan = self.planner.plan('a dress with a matching bag')
        self.assertEqual(plan.primary[0].role, 'primary')
        self.assertFalse(plan.primary[0].is_accessory)
        self.assertEqual(plan.accessories[0].role, 'accessory')
        self.assertTrue(plan.accessories[0].is_accessory)


if __name__ == '__main__':
    unittest.main()
