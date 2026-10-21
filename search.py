"""Deterministic, catalog-grounded product retrieval for the MCP tool."""
from collections import defaultdict
from dataclasses import dataclass
from difflib import SequenceMatcher
import json
import math
import re
from functools import lru_cache
from pathlib import Path

from jsonschema import Draft202012Validator, ValidationError

ROOT = Path(__file__).resolve().parent
INPUT_SCHEMA = json.loads((ROOT / 'schemas/input.schema.json').read_text())
OUTPUT_SCHEMA = json.loads((ROOT / 'schemas/output.schema.json').read_text())
VALIDATOR = Draft202012Validator(INPUT_SCHEMA)
STRING_FILTERS = ('category', 'product_type', 'color', 'audience', 'section', 'pattern')
SEARCH_FIELDS = {
    'name': ('name',),
    'taxonomy': ('product_type', 'category'),
    'attributes': ('color', 'pattern', 'audience', 'section'),
    'description': ('description',),
}
FIELD_WEIGHTS = {'name': 6.0, 'taxonomy': 3.5, 'attributes': 2.0, 'description': 1.0}
STOP_WORDS = frozenset({
    'a', 'an', 'and', 'any', 'are', 'at', 'be', 'by', 'each', 'find', 'for',
    'from', 'give', 'i', 'in', 'is', 'it', 'looking', 'me', 'my', 'of', 'on',
    'or', 'please', 'show', 'some', 'something', 'that', 'the', 'to', 'want',
    'with', 'would',
})
QUERY_NOISE = frozenset({
    'below', 'budget', 'costing', 'inr', 'less', 'maximum', 'price', 'rupee',
    'rupees', 'under', 'upto', 'within',
})
AMOUNT_PATTERN = re.compile(
    r'(?:under|below|within|up\s*to|upto|less\s+than|maximum|max)\s*'
    r'(?:inr|rs\.?|₹)?\s*([\d,]+(?:\.\d+)?)\s*(k)?',
    flags=re.IGNORECASE)
MIN_AMOUNT_PATTERN = re.compile(
    r'(?:over|above|at\s+least|minimum|min)\s*'
    r'(?:inr|rs\.?|₹)?\s*([\d,]+(?:\.\d+)?)\s*(k)?',
    flags=re.IGNORECASE)
COMPLEMENT_PATTERN = re.compile(
    r'\b(?:with\s+(?:a\s+|an\s+)?'
    r'(?:matching|complementary|coordinating|accompanying)\s+|'
    r'paired\s+with\s+|accompanied\s+by\s+|to\s+go\s+with\s+)',
    flags=re.IGNORECASE)


@dataclass(frozen=True)
class QueryIntent:
    raw: str
    retrieval_text: str
    product_types: tuple = ()
    categories: tuple = ()
    colors: tuple = ()
    patterns: tuple = ()
    audiences: tuple = ()
    min_price: float | None = None
    max_price: float | None = None
    soft_types: tuple = ()


@dataclass(frozen=True)
class QueryPlan:
    primary: tuple
    complement: QueryIntent | None = None


def validate_arguments(arguments):
    # Reject non-JSON floating point values even when called directly from Python.
    json.dumps(arguments, allow_nan=False)
    try:
        VALIDATOR.validate(arguments)
    except ValidationError as exc:
        raise ValueError(exc.message) from exc
    filters = arguments.get('filters', {})
    if any(not filters[k].strip() for k in STRING_FILTERS if k in filters):
        raise ValueError('String filters cannot be blank')
    if filters.get('min_price', 0) > filters.get('max_price', math.inf):
        raise ValueError('min_price cannot exceed max_price')


@lru_cache(maxsize=2048)
def tokens(text):
    return tuple(re.findall(r'[^\W_]+', text.casefold(), flags=re.UNICODE))


def normalized_text(text):
    return ' '.join(tokens(str(text or '')))


def inflection_variants(phrase):
    """Return one conservative singular/plural variant of a catalog phrase."""
    words = phrase.split()
    if not words:
        return ()
    word = words[-1]
    if word.endswith('ies') and len(word) > 3:
        variant = f'{word[:-3]}y'
    elif word.endswith('sses'):
        variant = word[:-2]
    elif word.endswith('s') and not word.endswith('ss'):
        variant = word[:-1]
    elif word.endswith(('ss', 'sh', 'ch', 'x', 'z')):
        variant = f'{word}es'
    elif len(word) > 1 and word.endswith('y') and word[-2] not in 'aeiou':
        variant = f'{word[:-1]}ies'
    else:
        variant = f'{word}s'
    return (' '.join((*words[:-1], variant)),)


def edit_distance(left, right, stop=2):
    """Levenshtein distance with an early cutoff for query correction."""
    if abs(len(left) - len(right)) > stop:
        return stop + 1
    previous = list(range(len(right) + 1))
    for row_index, left_char in enumerate(left, 1):
        current = [row_index]
        row_minimum = row_index
        for column_index, right_char in enumerate(right, 1):
            value = min(
                current[-1] + 1,
                previous[column_index] + 1,
                previous[column_index - 1] + (left_char != right_char),
            )
            current.append(value)
            row_minimum = min(row_minimum, value)
        if row_minimum > stop:
            return stop + 1
        previous = current
    return previous[-1]


class Catalog:
    def __init__(self, products):
        self.products = sorted(products, key=lambda p: p['id'])
        self.all_indices = frozenset(range(len(self.products)))
        self.id_to_index = {p['id']: i for i, p in enumerate(self.products)}
        self.postings = {field: defaultdict(list) for field in SEARCH_FIELDS}
        self.facets = {field: defaultdict(set) for field in STRING_FILTERS}
        self.taxonomy_phrases = {field: {} for field in STRING_FILTERS}
        for index, product in enumerate(self.products):
            for group, fields in SEARCH_FIELDS.items():
                text = ' '.join(str(product.get(field) or '') for field in fields)
                for term in set(tokens(text)):
                    self.postings[group][term].append(index)
            for field in STRING_FILTERS:
                value = str(product.get(field) or '').strip()
                self.facets[field][value.casefold()].add(index)
                self.taxonomy_phrases[field][normalized_text(value)] = value
        self.vocabulary = frozenset(
            term for field_postings in self.postings.values() for term in field_postings
        )
        self._build_query_vocabulary()
        self.sort_orders = {
            ('id', 'asc'): tuple(range(len(self.products))),
            ('id', 'desc'): tuple(reversed(range(len(self.products)))),
        }
        self._build_numeric_sort_orders()

    @classmethod
    def load(cls, path):
        return cls(json.loads(Path(path).read_text(encoding='utf-8')))

    def _build_numeric_sort_orders(self):
        ids = lambda index: self.products[index]['id']
        self.sort_orders[('price', 'asc')] = tuple(sorted(
            range(len(self.products)),
            key=lambda i: (self.products[i]['price']['amount'], ids(i))))
        self.sort_orders[('price', 'desc')] = tuple(sorted(
            range(len(self.products)),
            key=lambda i: (-self.products[i]['price']['amount'], ids(i))))
        self.sort_orders[('review_count', 'asc')] = tuple(sorted(
            range(len(self.products)),
            key=lambda i: (self.products[i]['rating']['count'], ids(i))))
        self.sort_orders[('review_count', 'desc')] = tuple(sorted(
            range(len(self.products)),
            key=lambda i: (-self.products[i]['rating']['count'], ids(i))))
        rated = [i for i, p in enumerate(self.products) if p['rating']['average'] is not None]
        unrated = [i for i, p in enumerate(self.products) if p['rating']['average'] is None]
        self.sort_orders[('rating', 'asc')] = tuple(
            sorted(rated, key=lambda i: (self.products[i]['rating']['average'], ids(i))) +
            sorted(unrated, key=ids))
        self.sort_orders[('rating', 'desc')] = tuple(
            sorted(rated, key=lambda i: (-self.products[i]['rating']['average'], ids(i))) +
            sorted(unrated, key=ids))

    def _build_query_vocabulary(self):
        product_types = sorted(
            {p['product_type'] for p in self.products}, key=lambda value: (-len(value), value))
        self.type_aliases = {}

        def merge_alias(alias, values):
            existing = self.type_aliases.get(alias)
            if existing is None:
                self.type_aliases[alias] = values
                return
            self.type_aliases[alias] = tuple(
                tuple(dict.fromkeys((*old, *new)))
                for old, new in zip(existing, values)
            )

        for value in product_types:
            phrase = normalized_text(value)
            merge_alias(phrase, ((value,), (), ()))
            for variant in inflection_variants(phrase):
                merge_alias(variant, ((value,), (), ()))
        self.type_aliases.update({
            'accessory': ((), ('Accessories',), ()),
            'accessories': ((), ('Accessories',), ()),
            'bag': ((), ('Bags',), ()),
            'bags': ((), ('Bags',), ()),
            'denim': (('Trousers',), (), ('Denim',)),
            'denims': (('Trousers',), (), ('Denim',)),
            'footwear': ((), ('Shoes',), ()),
            'gym bag': (('Weekend/Gym bag',), (), ()),
            'handbag': ((), ('Bags',), ()),
            'handbags': ((), ('Bags',), ()),
            'jeans': (('Trousers',), (), ('Denim',)),
            'jewellery': ((), ('Accessories',), ()),
            'jewelry': ((), ('Accessories',), ()),
            'pants': (('Trousers',), (), ()),
            'purse': ((), ('Bags',), ()),
            'purses': ((), ('Bags',), ()),
            'running shoes': (('Sneakers',), (), ()),
            'shoe': ((), ('Shoes',), ()),
            'shoes': ((), ('Shoes',), ()),
            'slacks': (('Trousers',), (), ()),
            'tees': (('T-shirt',), (), ()),
            'tshirt': (('T-shirt',), (), ()),
            'tshrit': (('T-shirt',), (), ()),
            'tshirts': (('T-shirt',), (), ()),
            'top': (('Top', 'Vest top'), (), ()),
            'tops': (('Top', 'Vest top'), (), ()),
            'trainers': (('Sneakers',), (), ()),
            'swimwear': ((), ('Swimwear',), ()),
        })
        self.color_aliases = {
            normalized_text(value): (value,)
            for value in {p['color'] for p in self.products}
            if value not in {'Other', 'Unknown'}
        }
        self.color_aliases.update({
            'cream': ('Light Beige', 'Off White'),
            'khaki': ('Greenish Khaki',),
            'maroon': ('Dark Red',),
            'navy': ('Dark Blue',),
        })
        self.pattern_aliases = {
            normalized_text(value): (value,)
            for value in {p['pattern'] for p in self.products}
            if value not in {'Other pattern', 'Unknown'}
        }
        self.pattern_aliases.update({
            'checked': ('Check',),
            'checks': ('Check',),
            'polka dot': ('Dot',),
            'spotted': ('Dot',),
            'striped': ('Stripe',),
            'stripes': ('Stripe',),
        })
        self.audience_aliases = {
            'baby': ('Baby Sizes 50-98',),
            'boy': ('Children Sizes 92-140', 'Children Sizes 134-170'),
            'boys': ('Children Sizes 92-140', 'Children Sizes 134-170'),
            'girl': ('Children Sizes 92-140', 'Children Sizes 134-170'),
            'girls': ('Children Sizes 92-140', 'Children Sizes 134-170'),
            'kid': ('Children Sizes 92-140', 'Children Sizes 134-170',
                    'Children Accessories, Swimwear'),
            'kids': ('Children Sizes 92-140', 'Children Sizes 134-170',
                     'Children Accessories, Swimwear'),
            'men': ('Menswear',),
            'mens': ('Menswear',),
            'man': ('Menswear',),
            'women': ('Ladieswear', 'Ladies Accessories', 'Divided', 'Lingeries/Tights'),
            'womens': ('Ladieswear', 'Ladies Accessories', 'Divided', 'Lingeries/Tights'),
            'woman': ('Ladieswear', 'Ladies Accessories', 'Divided', 'Lingeries/Tights'),
        }
        self.soft_intents = {
            'beach': ('Swimsuit', 'Swimwear top', 'Swimwear bottom', 'Swimwear set',
                      'Sarong', 'Sandals'),
            'beach vacation': ('Swimsuit', 'Swimwear top', 'Swimwear bottom',
                               'Swimwear set', 'Sarong', 'Sandals', 'Sunglasses'),
            'dinner': ('Dress', 'Blouse', 'Shirt', 'Blazer'),
            'exercise': ('T-shirt', 'Shorts', 'Leggings/Tights', 'Sneakers'),
            'gym': ('T-shirt', 'Shorts', 'Leggings/Tights', 'Sneakers',
                    'Weekend/Gym bag'),
            'lazy sunday': ('Sweater', 'Hoodie', 'Pyjama set', 'Pyjama bottom',
                            'Robe', 'Leggings/Tights'),
            'lounge': ('Sweater', 'Hoodie', 'Pyjama set', 'Pyjama bottom', 'Robe'),
            'loungewear': ('Pyjama set', 'Robe', 'Hoodie', 'Sweater',
                           'Pyjama bottom'),
            'office': ('Blazer', 'Shirt', 'Trousers', 'Blouse'),
            'party': ('Dress', 'Blouse', 'Blazer', 'Heels'),
            'rain': ('Umbrella', 'Jacket', 'Coat', 'Boots'),
            'rainy': ('Umbrella', 'Jacket', 'Coat', 'Boots'),
            'vacation': ('Dress', 'Shorts', 'Swimsuit', 'Sandals', 'Sunglasses'),
            'workout': ('T-shirt', 'Shorts', 'Leggings/Tights', 'Sneakers'),
        }
        self.soft_intents.update({
            'cold': ('Coat', 'Jacket', 'Sweater', 'Cardigan', 'Hoodie'),
            'warm': ('Coat', 'Jacket', 'Sweater', 'Cardigan', 'Hoodie'),
            'interview': ('Blazer', 'Shirt', 'Trousers', 'Blouse'),
        })
        canonical_phrases = []
        for field in ('product_type', 'category', 'color', 'pattern'):
            canonical_phrases.extend(self.taxonomy_phrases[field])
        self.correction_vocabulary = frozenset(
            term for phrase in canonical_phrases for term in tokens(phrase)
            if len(term) >= 5
        )

    @lru_cache(maxsize=4096)
    def _correct_term(self, term):
        if term in self.vocabulary or len(term) < 5:
            return term
        candidates = []
        for candidate in self.correction_vocabulary:
            if abs(len(term) - len(candidate)) > 1:
                continue
            distance = edit_distance(term, candidate)
            if distance <= 2:
                similarity = SequenceMatcher(None, term, candidate).ratio()
                candidates.append((distance, -similarity, candidate))
        return min(candidates)[2] if candidates else term

    def query_terms(self, query):
        terms = []
        for term in tokens(query):
            if term in STOP_WORDS or term in QUERY_NOISE or term.isdigit():
                continue
            corrected = self._correct_term(term)
            if corrected not in terms:
                terms.append(corrected)
        return tuple(terms)

    @staticmethod
    def _contains_phrase(text, phrase):
        return f' {phrase} ' in f' {text} '

    @staticmethod
    def _amount(match):
        if not match:
            return None
        amount = float(match.group(1).replace(',', ''))
        return amount * 1000 if match.group(2) else amount

    def _parse_intent(self, text):
        normalized = normalized_text(text)
        consumed = set()
        product_types, categories, patterns = (), (), ()
        for phrase, values in sorted(self.type_aliases.items(), key=lambda item: -len(item[0])):
            if self._contains_phrase(normalized, phrase):
                product_types, categories, patterns = values
                consumed.update(tokens(phrase))
                break
        colors = ()
        for phrase, values in sorted(self.color_aliases.items(), key=lambda item: -len(item[0])):
            if self._contains_phrase(normalized, phrase):
                colors = values
                consumed.update(tokens(phrase))
                break
        if not patterns:
            for phrase, values in sorted(
                    self.pattern_aliases.items(), key=lambda item: -len(item[0])):
                if self._contains_phrase(normalized, phrase):
                    patterns = values
                    consumed.update(tokens(phrase))
                    break
        audiences = ()
        for phrase, values in sorted(
                self.audience_aliases.items(), key=lambda item: -len(item[0])):
            if self._contains_phrase(normalized, phrase):
                audiences = values
                consumed.update(tokens(phrase))
                break
        soft_types = ()
        for phrase, values in sorted(self.soft_intents.items(), key=lambda item: -len(item[0])):
            if self._contains_phrase(normalized, phrase):
                soft_types = values
                consumed.update(tokens(phrase))
                break
        max_price = self._amount(AMOUNT_PATTERN.search(text))
        min_price = self._amount(MIN_AMOUNT_PATTERN.search(text))
        lexical_aliases = {
            'comfy': 'comfortable',
            'comfort': 'comfortable',
            'cozy': 'comfortable',
            'smart': 'formal',
        }
        soft_terms = []
        for term in tokens(text):
            if (term in consumed or term in STOP_WORDS or term in QUERY_NOISE or
                    re.fullmatch(r'\d+(?:k)?', term) or term in {'k', 'matching', 'complementary',
                                               'coordinating', 'accompanying'}):
                continue
            term = lexical_aliases.get(term, term)
            if term not in soft_terms:
                soft_terms.append(term)
        retrieval_parts = []
        if product_types:
            retrieval_parts.append(normalized_text(product_types[0]))
        elif categories:
            retrieval_parts.append(normalized_text(categories[0]))
        if colors:
            retrieval_parts.append(normalized_text(colors[0]))
        if patterns:
            retrieval_parts.append(normalized_text(patterns[0]))
        retrieval_parts.extend(soft_terms)
        return QueryIntent(
            raw=text.strip(),
            retrieval_text=' '.join(retrieval_parts),
            product_types=tuple(product_types),
            categories=tuple(categories),
            colors=tuple(colors),
            patterns=tuple(patterns),
            audiences=tuple(audiences),
            min_price=min_price,
            max_price=max_price,
            soft_types=tuple(soft_types),
        )

    def plan_query(self, query):
        query = query.strip()
        if not query:
            return QueryPlan(())
        primary_text = query
        complement = None
        complement_match = COMPLEMENT_PATTERN.search(query)
        if complement_match:
            possible = self._parse_intent(query[complement_match.end():])
            if possible.product_types or possible.categories:
                primary_text = query[:complement_match.start()]
                complement = possible
        raw_parts = [
            part.strip(' ,') for part in re.split(
                r'\s+(?:and|or|plus|with|along\s+with)\s+|,\s*',
                primary_text, flags=re.IGNORECASE)
            if part.strip(' ,')
        ]
        parsed_parts = tuple(self._parse_intent(part) for part in raw_parts)
        if (len(parsed_parts) < 2 or
                not all(part.product_types or part.categories for part in parsed_parts)):
            parsed_parts = (self._parse_intent(primary_text),)
        if len(parsed_parts) > 1:
            max_prices = [part.max_price for part in parsed_parts if part.max_price is not None]
            min_prices = [part.min_price for part in parsed_parts if part.min_price is not None]
            audiences = [part.audiences for part in parsed_parts if part.audiences]
            shared_max = max_prices[0] if len(max_prices) == 1 else None
            shared_min = min_prices[0] if len(min_prices) == 1 else None
            shared_audiences = audiences[0] if len(audiences) == 1 else ()
            if shared_max is not None or shared_min is not None or shared_audiences:
                parsed_parts = tuple(QueryIntent(
                    raw=part.raw,
                    retrieval_text=part.retrieval_text,
                    product_types=part.product_types,
                    categories=part.categories,
                    colors=part.colors,
                    patterns=part.patterns,
                    audiences=part.audiences or shared_audiences,
                    min_price=part.min_price if part.min_price is not None else shared_min,
                    max_price=part.max_price if part.max_price is not None else shared_max,
                    soft_types=part.soft_types,
                ) for part in parsed_parts)
        if complement is not None and not complement.audiences:
            primary_audiences = {part.audiences for part in parsed_parts if part.audiences}
            if len(primary_audiences) == 1:
                complement = QueryIntent(
                    raw=complement.raw,
                    retrieval_text=complement.retrieval_text,
                    product_types=complement.product_types,
                    categories=complement.categories,
                    colors=complement.colors,
                    patterns=complement.patterns,
                    audiences=next(iter(primary_audiences)),
                    min_price=complement.min_price,
                    max_price=complement.max_price,
                    soft_types=complement.soft_types,
                )
        return QueryPlan(parsed_parts, complement)

    def _facet_union(self, field, values):
        result = set()
        for value in values:
            result.update(self.facets[field].get(value.casefold(), ()))
        return result

    def _intent_allowed(self, intent, allowed, explicit_filters):
        result = set(allowed)
        ignored = set()
        constraints = (
            ('product_type', intent.product_types),
            ('category', intent.categories),
            ('color', intent.colors),
            ('pattern', intent.patterns),
            ('audience', intent.audiences),
        )
        for field, values in constraints:
            if not values:
                continue
            matching = self._facet_union(field, values)
            narrowed = result.intersection(matching)
            explicit_taxonomy = {'product_type', 'category'}.intersection(explicit_filters)
            can_override = field in explicit_filters or (
                field in {'product_type', 'category'} and explicit_taxonomy)
            if not narrowed and can_override:
                ignored.add(field)
                if field in {'product_type', 'category'} and intent.product_types:
                    ignored.add('pattern')
                continue
            if field == 'pattern' and 'pattern' in ignored:
                continue
            result = narrowed
            if not result:
                return result, ignored
        if intent.min_price is not None and 'min_price' not in explicit_filters:
            result = {i for i in result
                      if self.products[i]['price']['amount'] >= intent.min_price}
        if intent.max_price is not None and 'max_price' not in explicit_filters:
            result = {i for i in result
                      if self.products[i]['price']['amount'] <= intent.max_price}
        return result, ignored

    @staticmethod
    def _effective_retrieval_text(intent, ignored):
        removed = set()
        for field, values in (
                ('product_type', intent.product_types), ('category', intent.categories),
                ('color', intent.colors), ('pattern', intent.patterns),
                ('audience', intent.audiences)):
            if field in ignored:
                for value in values:
                    removed.update(tokens(value))
        return ' '.join(term for term in tokens(intent.retrieval_text) if term not in removed)

    def _intent_scores(self, intent, allowed, explicit_filters):
        intent_allowed, ignored = self._intent_allowed(intent, allowed, explicit_filters)
        preferred = set()
        if intent.soft_types:
            preferred = self._facet_union('product_type', intent.soft_types).intersection(
                intent_allowed)
            if not intent.product_types and not intent.categories:
                intent_allowed = preferred
        retrieval_text = self._effective_retrieval_text(intent, ignored)
        scores = self._retrieve(retrieval_text, intent_allowed)
        if not retrieval_text and (
                intent.product_types or intent.categories or intent.colors or intent.patterns or
                intent.audiences or intent.min_price is not None or intent.max_price is not None):
            scores = {index: 1.0 for index in intent_allowed}
        if preferred:
            for index in preferred:
                type_rank = intent.soft_types.index(self.products[index]['product_type'])
                scores[index] = scores.get(index, 0.0) + max(4.0, 12.0 - type_rank)
        return scores

    def _relevance_order(self, score_sets, sort_order):
        direction = -1 if sort_order == 'desc' else 1
        ranked = [
            sorted(scores, key=lambda i: (direction * scores[i], self.products[i]['id']))
            for scores in score_sets if scores
        ]
        if len(ranked) <= 1:
            return ranked[0] if ranked else []
        positions = [0] * len(ranked)
        seen = set()
        merged = []
        while True:
            added = False
            for choice, choices in enumerate(ranked):
                while positions[choice] < len(choices):
                    index = choices[positions[choice]]
                    positions[choice] += 1
                    if index in seen:
                        continue
                    seen.add(index)
                    merged.append(index)
                    added = True
                    break
            if not added:
                return merged

    def _matching_indices(self, filters):
        if not filters:
            return self.all_indices
        candidates = None
        for field in STRING_FILTERS:
            if field not in filters:
                continue
            matching = self.facets[field].get(filters[field].strip().casefold(), set())
            candidates = set(matching) if candidates is None else candidates.intersection(matching)
            if not candidates:
                return set()
        numeric_filters = {'min_price', 'max_price', 'min_rating', 'min_review_count'}
        if candidates is not None and not numeric_filters.intersection(filters):
            return candidates
        source = candidates if candidates is not None else self.all_indices
        return {index for index in source if self.matches(self.products[index], filters)}

    def _retrieve(self, query, allowed):
        query_terms = self.query_terms(query)
        if not query_terms:
            return {}
        scores = defaultdict(float)
        coverage = defaultdict(int)
        total = len(self.products)
        for term in query_terms:
            matching = set()
            for field_postings in self.postings.values():
                matching.update(field_postings.get(term, ()))
            if not matching:
                continue
            document_frequency = len(matching)
            inverse_frequency = math.log(
                1 + (total - document_frequency + 0.5) / (document_frequency + 0.5))
            for index in matching:
                if index in allowed:
                    coverage[index] += 1
            for field, field_postings in self.postings.items():
                contribution = FIELD_WEIGHTS[field] * inverse_frequency
                for index in field_postings.get(term, ()):
                    if index in allowed:
                        scores[index] += contribution
        required = len(query_terms) if len(query_terms) <= 2 else math.ceil(len(query_terms) * 0.6)
        phrase = normalized_text(query)
        for index in tuple(scores):
            if coverage[index] < required:
                del scores[index]
                continue
            product = self.products[index]
            name = normalized_text(product['name'])
            taxonomy = normalized_text(f"{product['product_type']} {product['category']}")
            if phrase and phrase == name:
                scores[index] += 20
            elif phrase and phrase in name:
                scores[index] += 8
            if phrase and phrase in taxonomy:
                scores[index] += 10
            scores[index] += coverage[index] / len(query_terms)
        return scores

    def relevance(self, product, query_tokens):
        """Compatibility helper used by fixture tests and local experimentation."""
        index = self.id_to_index[product['id']]
        scores = self._retrieve(' '.join(query_tokens), {index})
        return scores.get(index, 0)

    @staticmethod
    def _audience_family(audience):
        if audience in {'Ladieswear', 'Ladies Accessories', 'Divided', 'Lingeries/Tights'}:
            return 'women'
        if audience == 'Menswear':
            return 'men'
        if audience == 'Sport':
            return 'sport'
        if audience.startswith('Baby'):
            return 'baby'
        if audience.startswith('Children'):
            return 'children'
        return audience

    def _inferred_complements(self, primary):
        category_pairs = {
            'Garment Full body': ('Bags', 'Shoes', 'Accessories'),
            'Garment Upper body': ('Garment Lower body', 'Bags', 'Accessories'),
            'Garment Lower body': ('Garment Upper body', 'Shoes', 'Bags', 'Accessories'),
            'Shoes': ('Bags', 'Accessories', 'Garment Lower body'),
            'Swimwear': ('Bags', 'Shoes', 'Accessories'),
            'Bags': ('Accessories', 'Garment Full body', 'Garment Upper body'),
            'Nightwear': ('Socks & Tights', 'Accessories'),
        }
        categories = category_pairs.get(primary['category'], ())
        return self._facet_union('category', categories)

    def _complement_score(self, primary, candidate, retrieval_score):
        score = retrieval_score
        neutral = {
            'Black', 'White', 'Off White', 'Beige', 'Light Beige', 'Grey',
            'Dark Grey', 'Light Grey', 'Silver', 'Gold',
        }
        if candidate['color'] == primary['color']:
            score += 4
        elif candidate['color'] in neutral:
            score += 2
        if self._audience_family(candidate['audience']) == self._audience_family(
                primary['audience']):
            score += 8
        if primary['product_type'] in {'Dress', 'Jumpsuit/Playsuit', 'Skirt'}:
            score += {
                'Shoulder bag': 5, 'Cross-body bag': 5, 'Bag': 4, 'Tote bag': 4,
                'Heels': 4, 'Sandals': 3, 'Necklace': 3, 'Earring': 3,
                'Earrings': 3,
            }.get(candidate['product_type'], 0)
        average = candidate['rating']['average']
        if average is not None:
            score += average * 0.05
        return score

    def recommend(self, results, arguments):
        """Return unique complements grounded in the query and current result page."""
        primary_indices = [self.id_to_index[item['id']] for item in results]
        primary_ids = {item['id'] for item in results}
        plan = self.plan_query(arguments.get('query', ''))
        intent = plan.complement
        explicit_scores = None
        if intent is not None:
            # API filters constrain primaries only; query constraints on the complement
            # are represented by its own intent.
            explicit_scores = self._intent_scores(intent, self.all_indices, {})
        per_primary = []
        for primary_index in primary_indices:
            primary = self.products[primary_index]
            if explicit_scores is not None:
                pool = set(explicit_scores)
            else:
                pool = self._inferred_complements(primary)
            if explicit_scores is not None and intent is not None and not intent.audiences:
                same_audience = {
                    index for index in pool
                    if self._audience_family(self.products[index]['audience']) ==
                    self._audience_family(primary['audience'])
                }
                if same_audience:
                    pool = same_audience
            ranked = []
            for candidate_index in pool:
                candidate = self.products[candidate_index]
                if candidate['id'] in primary_ids:
                    continue
                if candidate['product_type'] == primary['product_type']:
                    continue
                if explicit_scores is None and (
                        self._audience_family(candidate['audience']) !=
                        self._audience_family(primary['audience'])):
                    continue
                score = self._complement_score(
                    primary, candidate,
                    explicit_scores.get(candidate_index, 0.0) if explicit_scores else 0.0)
                ranked.append((candidate_index, score))
            ranked.sort(key=lambda item: (-item[1], self.products[item[0]]['id']))
            per_primary.append(ranked)
        recommendations = []
        used = set()
        positions = [0] * len(per_primary)
        while len(recommendations) < 5:
            added = False
            for primary_position, ranked in enumerate(per_primary):
                while positions[primary_position] < len(ranked):
                    candidate_index = ranked[positions[primary_position]][0]
                    positions[primary_position] += 1
                    candidate = self.products[candidate_index]
                    if candidate['id'] in used:
                        continue
                    primary = self.products[primary_indices[primary_position]]
                    used.add(candidate['id'])
                    recommendations.append({
                        'id': candidate['id'],
                        'for_product_id': primary['id'],
                        'reason': (
                            f"{candidate['product_type']} in {candidate['color']} to accompany "
                            f"the selected {primary['product_type']}."
                        ),
                    })
                    added = True
                    break
                if len(recommendations) == 5:
                    break
            if not added:
                break
        return recommendations

    @staticmethod
    def matches(product, filters):
        for key in STRING_FILTERS:
            if key in filters and str(product.get(key) or '').strip().casefold() != filters[key].strip().casefold():
                return False
        price = product['price']['amount']
        average = product['rating']['average']
        return (price >= filters.get('min_price', 0)
                and price <= filters.get('max_price', math.inf)
                and ('min_rating' not in filters or
                     (average is not None and average >= filters['min_rating']))
                and product['rating']['count'] >= filters.get('min_review_count', 0))

    def search(self, arguments):
        validate_arguments(arguments)
        query = arguments.get('query', '').strip()
        filters = arguments.get('filters', {})
        allowed = self._matching_indices(filters)
        score_sets = []
        if query:
            plan = self.plan_query(query)
            score_sets = [
                self._intent_scores(intent, allowed, filters) for intent in plan.primary
            ]
            candidate_indices = set().union(*(set(scores) for scores in score_sets))
        else:
            candidate_indices = allowed
        sort_by = arguments.get('sort_by', 'relevance')
        sort_order = arguments.get('sort_order', 'desc')
        if sort_by == 'relevance':
            if query:
                ordered = self._relevance_order(score_sets, sort_order)
            else:
                ordered = self.sort_orders[('id', 'asc')]
        else:
            ordered = self.sort_orders[(sort_by, sort_order)]
        offset, limit = arguments.get('offset', 0), arguments.get('limit', 10)
        page = []
        for index in ordered:
            if index not in candidate_indices:
                continue
            # Every final row is rechecked against the original catalog.
            if not self.matches(self.products[index], filters):
                continue
            if offset:
                offset -= 1
                continue
            page.append(index)
            if len(page) == limit:
                break
        results = [{'id': self.products[index]['id']} for index in page]
        return {'results': results, 'recommendations':
                self.recommend(results, arguments) if results and arguments.get('include_recommendations', False) else []}
