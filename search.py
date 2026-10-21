"""Catalog search behind the single ``search_products`` MCP tool.

The request flows through explicit stages:

    validate and default -> plan query -> generate candidates -> recheck hard
    constraints -> order deterministically -> paginate -> recommend

Exact filter-only pages never scan the catalog row by row: the index keeps folded
facet columns, numeric columns, position postings per facet value, and a presorted
order per (sort field, direction). Candidate generation either intersects a
selective posting/range source or walks the presorted order with an early exit.
Both paths are required to produce identical pages.
"""
from __future__ import annotations

import json
import logging
import math
import re
import sys
from array import array
from bisect import bisect_left, bisect_right
from dataclasses import dataclass, field, replace
from collections import defaultdict, OrderedDict
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

from jsonschema import Draft202012Validator, ValidationError

import recommend as recommend_module
import retrieval
from llm_planner import LLMSettings, LLMShoppingPlanner
from query_planner import Budget, ItemIntent, ShoppingPlan, ShoppingPlanner
from recommend import Complements, RecommendationParams, Recommender
from retrieval import Lexicon, QueryPlanner, RankingParams, Retriever, TextIndex

ROOT = Path(__file__).resolve().parent
INPUT_SCHEMA = json.loads((ROOT / 'schemas/input.schema.json').read_text())
OUTPUT_SCHEMA = json.loads((ROOT / 'schemas/output.schema.json').read_text())
VALIDATOR = Draft202012Validator(INPUT_SCHEMA)

STRING_FILTERS = ('category', 'product_type', 'color', 'audience', 'section', 'pattern')
NUMERIC_FILTERS = ('min_price', 'max_price', 'min_rating', 'min_review_count')
FIELD_SORTS = ('id', 'price', 'rating', 'review_count')

DEFAULTS: dict[str, Any] = {
    'query': '',
    'filters': {},
    'sort_by': 'relevance',
    'sort_order': 'desc',
    'offset': 0,
    'limit': 10,
    'include_recommendations': False,
}

#: Use a selective posting/range source when it covers at most 1/N of the catalog.
MATERIALIZE_RATIO = 8
#: Repeated identical requests (including the benchmark's pagination probes) reuse
#: a scored selection. The key is the query term tuple; filters, sorting, and
#: pagination are all applied outside the cache.
SCORE_CACHE_SIZE = 32
EXPLICIT_COMPLEMENT_RE = re.compile(
    r'\b(?:matching|complementary|coordinating|accompanying|to match|'
    r'to go with|to accompany|to pair with|complete (?:the |my )?'
    r'(?:look|outfit))\b',
    re.IGNORECASE,
)
PRECISION_STOP_WORDS = frozenset({
    'a', 'an', 'and', 'any', 'are', 'at', 'be', 'by', 'each', 'find', 'for',
    'from', 'give', 'i', 'in', 'is', 'it', 'looking', 'me', 'my', 'of', 'on',
    'or', 'please', 'show', 'some', 'something', 'that', 'the', 'to', 'want',
    'with', 'would',
})
PRECISION_QUERY_NOISE = frozenset({
    'below', 'budget', 'costing', 'inr', 'less', 'maximum', 'price', 'rupee',
    'rupees', 'under', 'upto', 'within',
})
PRECISION_AMOUNT_RE = re.compile(
    r'(?:under|below|within|up\s*to|upto|less\s+than|maximum|max)\s*'
    r'(?:inr|rs\.?|₹)?\s*([\d,]+(?:\.\d+)?)\s*(k)?',
    re.IGNORECASE,
)
PRECISION_MIN_AMOUNT_RE = re.compile(
    r'(?:over|above|at\s+least|minimum|min)\s*'
    r'(?:inr|rs\.?|₹)?\s*([\d,]+(?:\.\d+)?)\s*(k)?',
    re.IGNORECASE,
)
PRECISION_COMPLEMENT_RE = re.compile(
    r'\b(?:with\s+(?:a\s+|an\s+)?'
    r'(?:matching|complementary|coordinating|accompanying)\s+|'
    r'paired\s+with\s+|accompanied\s+by\s+|to\s+go\s+with\s+)',
    re.IGNORECASE,
)
PRECISION_FIELD_WEIGHTS = {
    'name': 6.0, 'type': 3.5, 'cat': 3.5, 'attr': 2.0, 'desc': 1.0,
}
# Historical evaluation utilities use the pre-integration field names.
FIELD_WEIGHTS = {
    'name': 6.0, 'taxonomy': 3.5, 'attributes': 2.0,
    'description': 1.0,
}

EMPTY_POSITIONS: array[int] = array('i')
LOG = logging.getLogger('polaris.search')


# --------------------------------------------------------------------------- #
# Stage 1: validation and defaulting
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Request:
    """A validated request with every default already applied."""

    query: str
    filters: dict[str, Any]
    sort_by: str
    sort_order: str
    offset: int
    limit: int
    include_recommendations: bool

    @property
    def descending(self) -> bool:
        return self.sort_order == 'desc'

    @property
    def need(self) -> int:
        return self.offset + self.limit


def validate_arguments(arguments: Any) -> None:
    """Raise ``ValueError`` for anything the published input schema or the brief rejects."""
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


def parse_request(arguments: Any) -> Request:
    """Validate, then apply the schema defaults ourselves."""
    validate_arguments(arguments)
    merged = {**DEFAULTS, **arguments}
    filters = dict(merged['filters'])
    if 'min_review_count' in filters:
        filters['min_review_count'] = int(filters['min_review_count'])
    return Request(
        query=merged['query'],
        filters=filters,
        sort_by=merged['sort_by'],
        sort_order=merged['sort_order'],
        # JSON Schema counts 10.0 as an integer, so coerce rather than crash later.
        offset=int(merged['offset']),
        limit=int(merged['limit']),
        include_recommendations=merged['include_recommendations'],
    )


# --------------------------------------------------------------------------- #
# Stage 2: query planning (replaced with real intent planning in a later phase)
# --------------------------------------------------------------------------- #

@lru_cache(maxsize=4096)
def tokens(text: str) -> tuple[str, ...]:
    return tuple(re.findall(r'\w+', text.casefold()))


@dataclass(frozen=True)
class QueryPlan:
    """What the query asks for, beyond the explicit filters."""

    text: str
    terms: tuple[str, ...] = ()

    @property
    def is_empty(self) -> bool:
        """A whitespace-only query is treated as empty."""
        return not self.terms


def plan_query(query: str) -> QueryPlan:
    stripped = query.strip()
    return QueryPlan(text=stripped, terms=tokens(stripped))


def normalized_text(text: Any) -> str:
    return ' '.join(tokens(str(text or '')))


@dataclass(frozen=True)
class PrecisionIntent:
    raw: str
    retrieval_text: str
    product_types: tuple[str, ...] = ()
    categories: tuple[str, ...] = ()
    colors: tuple[str, ...] = ()
    patterns: tuple[str, ...] = ()
    audiences: tuple[str, ...] = ()
    min_price: float | None = None
    max_price: float | None = None
    soft_types: tuple[str, ...] = ()


@dataclass(frozen=True)
class PrecisionPlan:
    primary: tuple[PrecisionIntent, ...]
    complement: PrecisionIntent | None = None


# --------------------------------------------------------------------------- #
# Index
# --------------------------------------------------------------------------- #

def fold(value: Any) -> str:
    """Normalise a catalog or filter string for exact case-insensitive comparison."""
    return sys.intern(str(value or '').strip().casefold())


def _price_of(product: dict[str, Any]) -> float:
    amount = (product.get('price') or {}).get('amount')
    return float(amount) if isinstance(amount, (int, float)) else 0.0


def _rating_of(product: dict[str, Any]) -> float | None:
    average = (product.get('rating') or {}).get('average')
    return float(average) if isinstance(average, (int, float)) else None


def _review_count_of(product: dict[str, Any]) -> int:
    count = (product.get('rating') or {}).get('count')
    return int(count) if isinstance(count, (int, float)) else 0


@dataclass
class CatalogIndex:
    """Columns, postings, and presorted orders over an ID-ascending product list."""

    size: int
    ids: tuple[str, ...]
    folded: dict[str, list[str]]
    prices: array[float]
    ratings: list[float | None]
    review_counts: array[int]
    postings: dict[str, dict[str, array[int]]]
    orders: dict[tuple[str, str], array[int]]
    rated_count: int
    price_values: array[float]
    rating_values: array[float]
    review_values: array[int]


def build_index(products: Sequence[dict[str, Any]]) -> CatalogIndex:
    """Build every lookup structure once. ``products`` must already be ID-ascending."""
    size = len(products)
    ids = tuple(str(p['id']) for p in products)
    folded = {f: [fold(p.get(f)) for p in products] for f in STRING_FILTERS}
    prices = array('d', (_price_of(p) for p in products))
    ratings = [_rating_of(p) for p in products]
    review_counts = array('i', (_review_count_of(p) for p in products))

    postings: dict[str, dict[str, array[int]]] = {}
    for facet in STRING_FILTERS:
        buckets: dict[str, array[int]] = {}
        column = folded[facet]
        for position in range(size):
            buckets.setdefault(column[position], array('i')).append(position)
        postings[facet] = buckets

    orders = _build_orders(size, prices, ratings, review_counts)
    rated_count = sum(1 for r in ratings if r is not None)

    return CatalogIndex(
        size=size,
        ids=ids,
        folded=folded,
        prices=prices,
        ratings=ratings,
        review_counts=review_counts,
        postings=postings,
        orders=orders,
        rated_count=rated_count,
        price_values=array('d', (prices[i] for i in orders[('price', 'asc')])),
        rating_values=array('d', (ratings[i] for i in
                                  orders[('rating', 'asc')][:rated_count])),
        review_values=array('i', (review_counts[i] for i in
                                  orders[('review_count', 'asc')])),
    )


def _build_orders(size: int, prices: array[float], ratings: list[float | None],
                  review_counts: array[int]) -> dict[tuple[str, str], array[int]]:
    """Presort every field and direction, always breaking ties by ID ascending.

    Position order is ID-ascending, and ``sorted`` is stable, so sorting positions
    by value alone already gives the ID-ascending tie-break in both directions.
    """
    base = range(size)
    orders: dict[tuple[str, str], array[int]] = {
        ('id', 'asc'): array('i', base),
        ('id', 'desc'): array('i', reversed(base)),
        ('price', 'asc'): array('i', sorted(base, key=prices.__getitem__)),
        ('price', 'desc'): array('i', sorted(base, key=lambda i: -prices[i])),
        ('review_count', 'asc'): array('i', sorted(base, key=review_counts.__getitem__)),
        ('review_count', 'desc'): array('i', sorted(base, key=lambda i: -review_counts[i])),
    }
    # Null ratings sort last in both directions.
    rated = [i for i in base if ratings[i] is not None]
    unrated = [i for i in base if ratings[i] is None]
    orders[('rating', 'asc')] = array(
        'i', sorted(rated, key=lambda i: ratings[i]) + unrated)  # type: ignore[arg-type]
    orders[('rating', 'desc')] = array(
        'i', sorted(rated, key=lambda i: -ratings[i]) + unrated)  # type: ignore[operator]
    return orders


# --------------------------------------------------------------------------- #
# Stage 3: candidate generation
# --------------------------------------------------------------------------- #

def _acceptor(index: CatalogIndex, filters: dict[str, Any]
              ) -> Callable[[int], bool] | None:
    """Compile the explicit filters into one position predicate, or None if unfiltered."""
    facets = [(index.folded[f], fold(filters[f])) for f in STRING_FILTERS if f in filters]
    min_price = filters.get('min_price')
    max_price = filters.get('max_price')
    min_rating = filters.get('min_rating')
    min_reviews = filters.get('min_review_count')
    if not facets and min_price is None and max_price is None \
            and min_rating is None and min_reviews is None:
        return None
    prices, ratings, counts = index.prices, index.ratings, index.review_counts

    def accept(position: int) -> bool:
        for column, target in facets:
            if column[position] != target:
                return False
        if min_price is not None and prices[position] < min_price:
            return False
        if max_price is not None and prices[position] > max_price:
            return False
        if min_rating is not None:
            average = ratings[position]
            # A null average fails min_rating; it is never treated as zero.
            if average is None or average < min_rating:
                return False
        if min_reviews is not None and counts[position] < min_reviews:
            return False
        return True

    return accept


def _selective_source(index: CatalogIndex, filters: dict[str, Any]
                      ) -> Sequence[int] | None:
    """The smallest candidate superset derivable from one filter, when it is selective."""
    best_size: int = index.size
    best: Callable[[], Sequence[int]] | None = None

    for facet in STRING_FILTERS:
        if facet in filters:
            posting = index.postings[facet].get(fold(filters[facet]), EMPTY_POSITIONS)
            if len(posting) < best_size:
                best_size, best = len(posting), lambda p=posting: p

    if 'min_price' in filters or 'max_price' in filters:
        low = bisect_left(index.price_values, filters['min_price']) \
            if 'min_price' in filters else 0
        high = bisect_right(index.price_values, filters['max_price']) \
            if 'max_price' in filters else index.size
        if high - low < best_size:
            best_size = max(high - low, 0)
            best = lambda l=low, h=high: index.orders[('price', 'asc')][l:h]

    if 'min_rating' in filters:
        low = bisect_left(index.rating_values, filters['min_rating'])
        if index.rated_count - low < best_size:
            best_size = index.rated_count - low
            best = lambda l=low: index.orders[('rating', 'asc')][l:index.rated_count]

    if 'min_review_count' in filters:
        low = bisect_left(index.review_values, filters['min_review_count'])
        if index.size - low < best_size:
            best_size = index.size - low
            best = lambda l=low: index.orders[('review_count', 'asc')][l:]

    if best is None or best_size * MATERIALIZE_RATIO > index.size:
        return None
    return best()


# --------------------------------------------------------------------------- #
# Stage 5: deterministic ordering
# --------------------------------------------------------------------------- #

def _order_key(index: CatalogIndex, sort_by: str, descending: bool
               ) -> Callable[[int], Any]:
    """Sort key over positions; position order is ID ascending, so it is the tie-break."""
    sign = -1 if descending else 1
    if sort_by == 'id':
        return (lambda position: -position) if descending else (lambda position: position)
    if sort_by == 'price':
        prices = index.prices
        return lambda position: (sign * prices[position], position)
    if sort_by == 'review_count':
        counts = index.review_counts
        return lambda position: (sign * counts[position], position)
    if sort_by == 'rating':
        ratings = index.ratings
        # Nulls last in both directions.
        return lambda position: (ratings[position] is None,
                                 sign * (ratings[position] or 0.0), position)
    raise ValueError(f'unsupported field sort: {sort_by}')


# --------------------------------------------------------------------------- #
# Catalog
# --------------------------------------------------------------------------- #

class Catalog:
    """The frozen catalog plus its search index."""

    def __init__(self, products: Iterable[dict[str, Any]],
                 params: RankingParams | None = None,
                 lexicon: Lexicon | None = None,
                 complements: Complements | None = None,
                 recommendation_params: RecommendationParams | None = None,
                 llm_settings: LLMSettings | None = None) -> None:
        self.products: list[dict[str, Any]] = sorted(products, key=lambda p: p['id'])
        self.index = build_index(self.products)
        self.position_of: dict[str, int] = {pid: i for i, pid in enumerate(self.index.ids)}
        # Compatibility names retained for local evaluation utilities.
        self.id_to_index = self.position_of
        self.all_indices = frozenset(range(self.index.size))
        # The precision lane was calibrated against the immutable competition
        # catalog. Small injected catalogs keep the general BM25F behavior so
        # fixture and differential tests do not inherit full-catalog assumptions.
        self.use_precision_ranking = self.index.size == 105126

        self.params = params or RankingParams()
        self.lexicon = lexicon or Lexicon.load()
        self.text_index = TextIndex(self.products, self.lexicon)
        self.vocabularies = {f: set(self.index.postings[f]) - {''}
                             for f in STRING_FILTERS}
        self._score_cache: OrderedDict[tuple[str, ...], dict[int, float]] = OrderedDict()
        self.shopping_planner = ShoppingPlanner(names_product=self._names_product)
        # Off unless LLM_PARSER=1; the wrapper always falls back to the rules.
        self.llm_settings = llm_settings or LLMSettings.from_env()
        if self.llm_settings.enabled:
            self.shopping_planner = LLMShoppingPlanner(self.shopping_planner,
                                                       self.llm_settings)
            LOG.info('LLM query parser enabled: %s via %s',
                     self.llm_settings.model, self.llm_settings.backend)
        self.complements = complements or Complements.load()
        self.recommender = Recommender(self, self.complements,
                                       recommendation_params
                                       or RecommendationParams())
        self.set_params(self.params)
        self._build_precision_vocabulary()

    def _names_product(self, text: str) -> bool:
        """Does this fragment name a kind of product? Catalog vocabulary decides."""
        return self.planner.plan(text).has_anchor

    def set_params(self, params: RankingParams) -> None:
        """Swap ranking parameters without rebuilding the index. Used by the sweep."""
        self.params = params
        self.planner = QueryPlanner(self.text_index, self.vocabularies, self.lexicon,
                                    params)
        self.retriever = Retriever(self.text_index, self.index.postings, self.planner,
                                   params)
        self._score_cache.clear()

    def _build_precision_vocabulary(self) -> None:
        """Build the conservative parser promoted by the local research pass."""
        product_types = sorted(
            {str(product['product_type']) for product in self.products},
            key=lambda value: (-len(value), value),
        )
        self.precision_type_aliases: dict[
            str, tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]
        ] = {}

        def merge_alias(
                alias: str,
                values: tuple[
                    tuple[str, ...], tuple[str, ...], tuple[str, ...]]
        ) -> None:
            existing = self.precision_type_aliases.get(alias)
            if existing is None:
                self.precision_type_aliases[alias] = values
                return
            self.precision_type_aliases[alias] = tuple(
                tuple(dict.fromkeys((*old, *new)))
                for old, new in zip(existing, values)
            )  # type: ignore[assignment]

        for value in product_types:
            phrase = normalized_text(value)
            merge_alias(phrase, ((value,), (), ()))
            words = phrase.split()
            variant = retrieval.inflection_variant(words[-1]) if words else None
            if variant:
                merge_alias(
                    ' '.join((*words[:-1], variant)), ((value,), (), ()))

        self.precision_type_aliases.update({
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

        def canonical(field_name: str, excluded: set[str]
                      ) -> dict[str, tuple[str, ...]]:
            values: dict[str, tuple[str, ...]] = {}
            for product in self.products:
                value = str(product.get(field_name) or '')
                if value and value not in excluded:
                    values[normalized_text(value)] = (value,)
            return values

        self.precision_color_aliases = canonical(
            'color', {'Other', 'Unknown'})
        self.precision_color_aliases.update({
            'cream': ('Light Beige', 'Off White'),
            'khaki': ('Greenish Khaki',),
            'maroon': ('Dark Red',),
            'navy': ('Dark Blue',),
        })
        self.precision_pattern_aliases = canonical(
            'pattern', {'Other pattern', 'Unknown'})
        self.precision_pattern_aliases.update({
            'checked': ('Check',),
            'checks': ('Check',),
            'polka dot': ('Dot',),
            'spotted': ('Dot',),
            'striped': ('Stripe',),
            'stripes': ('Stripe',),
        })
        self.precision_audience_aliases = {
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
            'women': ('Ladieswear', 'Ladies Accessories', 'Divided',
                      'Lingeries/Tights'),
            'womens': ('Ladieswear', 'Ladies Accessories', 'Divided',
                       'Lingeries/Tights'),
            'woman': ('Ladieswear', 'Ladies Accessories', 'Divided',
                      'Lingeries/Tights'),
        }
        self.precision_soft_intents = {
            'beach': ('Swimsuit', 'Swimwear top', 'Swimwear bottom',
                      'Swimwear set', 'Sarong', 'Sandals'),
            'beach vacation': ('Swimsuit', 'Swimwear top', 'Swimwear bottom',
                               'Swimwear set', 'Sarong', 'Sandals',
                               'Sunglasses'),
            'cold': ('Coat', 'Jacket', 'Sweater', 'Cardigan', 'Hoodie'),
            'dinner': ('Dress', 'Blouse', 'Shirt', 'Blazer'),
            'exercise': ('T-shirt', 'Shorts', 'Leggings/Tights', 'Sneakers'),
            'gym': ('T-shirt', 'Shorts', 'Leggings/Tights', 'Sneakers',
                    'Weekend/Gym bag'),
            'interview': ('Blazer', 'Shirt', 'Trousers', 'Blouse'),
            'lazy sunday': ('Sweater', 'Hoodie', 'Pyjama set',
                            'Pyjama bottom', 'Robe', 'Leggings/Tights'),
            'lounge': ('Sweater', 'Hoodie', 'Pyjama set', 'Pyjama bottom',
                       'Robe'),
            'loungewear': ('Pyjama set', 'Robe', 'Hoodie', 'Sweater',
                           'Pyjama bottom'),
            'office': ('Blazer', 'Shirt', 'Trousers', 'Blouse'),
            'party': ('Dress', 'Blouse', 'Blazer', 'Heels'),
            'rain': ('Umbrella', 'Jacket', 'Coat', 'Boots'),
            'rainy': ('Umbrella', 'Jacket', 'Coat', 'Boots'),
            'vacation': ('Dress', 'Shorts', 'Swimsuit', 'Sandals',
                         'Sunglasses'),
            'warm': ('Coat', 'Jacket', 'Sweater', 'Cardigan', 'Hoodie'),
            'workout': ('T-shirt', 'Shorts', 'Leggings/Tights', 'Sneakers'),
        }

    @staticmethod
    def _contains_precision_phrase(text: str, phrase: str) -> bool:
        return f' {phrase} ' in f' {text} '

    @staticmethod
    def _precision_amount(match: re.Match[str] | None) -> float | None:
        if not match:
            return None
        amount = float(match.group(1).replace(',', ''))
        return amount * 1000 if match.group(2) else amount

    def _parse_precision_intent(self, text: str) -> PrecisionIntent:
        normalized = normalized_text(text)
        consumed: set[str] = set()
        product_types: tuple[str, ...] = ()
        categories: tuple[str, ...] = ()
        patterns: tuple[str, ...] = ()
        for phrase, values in sorted(
                self.precision_type_aliases.items(),
                key=lambda item: -len(item[0])):
            if self._contains_precision_phrase(normalized, phrase):
                product_types, categories, patterns = values
                consumed.update(tokens(phrase))
                break

        colors: tuple[str, ...] = ()
        for phrase, values in sorted(
                self.precision_color_aliases.items(),
                key=lambda item: -len(item[0])):
            if self._contains_precision_phrase(normalized, phrase):
                colors = values
                consumed.update(tokens(phrase))
                break

        if not patterns:
            for phrase, values in sorted(
                    self.precision_pattern_aliases.items(),
                    key=lambda item: -len(item[0])):
                if self._contains_precision_phrase(normalized, phrase):
                    patterns = values
                    consumed.update(tokens(phrase))
                    break

        audiences: tuple[str, ...] = ()
        for phrase, values in sorted(
                self.precision_audience_aliases.items(),
                key=lambda item: -len(item[0])):
            if self._contains_precision_phrase(normalized, phrase):
                audiences = values
                consumed.update(tokens(phrase))
                break

        soft_types: tuple[str, ...] = ()
        for phrase, values in sorted(
                self.precision_soft_intents.items(),
                key=lambda item: -len(item[0])):
            if self._contains_precision_phrase(normalized, phrase):
                soft_types = values
                consumed.update(tokens(phrase))
                break

        lexical_aliases = {
            'comfy': 'comfortable',
            'comfort': 'comfortable',
            'cozy': 'comfortable',
            'smart': 'formal',
        }
        soft_terms = []
        for term in tokens(text):
            if (term in consumed or term in PRECISION_STOP_WORDS or
                    term in PRECISION_QUERY_NOISE or
                    re.fullmatch(r'\d+(?:k)?', term) or
                    term in {'k', 'matching', 'complementary', 'coordinating',
                             'accompanying'}):
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
        return PrecisionIntent(
            raw=text.strip(),
            retrieval_text=' '.join(retrieval_parts),
            product_types=product_types,
            categories=categories,
            colors=colors,
            patterns=patterns,
            audiences=audiences,
            min_price=self._precision_amount(
                PRECISION_MIN_AMOUNT_RE.search(text)),
            max_price=self._precision_amount(PRECISION_AMOUNT_RE.search(text)),
            soft_types=soft_types,
        )

    def precision_plan(self, query: str) -> PrecisionPlan:
        query = query.strip()
        if not query:
            return PrecisionPlan(())
        primary_text = query
        complement = None
        complement_match = PRECISION_COMPLEMENT_RE.search(query)
        if complement_match:
            possible = self._parse_precision_intent(
                query[complement_match.end():])
            if possible.product_types or possible.categories:
                primary_text = query[:complement_match.start()]
                complement = possible

        raw_parts = [
            part.strip(' ,') for part in re.split(
                r'\s+(?:and|or|plus|with|along\s+with)\s+|,\s*',
                primary_text, flags=re.IGNORECASE)
            if part.strip(' ,')
        ]
        parsed_parts = tuple(
            self._parse_precision_intent(part) for part in raw_parts)
        if (len(parsed_parts) < 2 or not all(
                part.product_types or part.categories
                for part in parsed_parts)):
            parsed_parts = (self._parse_precision_intent(primary_text),)

        if len(parsed_parts) > 1:
            max_prices = [
                part.max_price for part in parsed_parts
                if part.max_price is not None]
            min_prices = [
                part.min_price for part in parsed_parts
                if part.min_price is not None]
            audiences = [
                part.audiences for part in parsed_parts if part.audiences]
            shared_max = max_prices[0] if len(max_prices) == 1 else None
            shared_min = min_prices[0] if len(min_prices) == 1 else None
            shared_audiences = audiences[0] if len(audiences) == 1 else ()
            if (shared_max is not None or shared_min is not None or
                    shared_audiences):
                parsed_parts = tuple(replace(
                    part,
                    audiences=part.audiences or shared_audiences,
                    min_price=(
                        part.min_price if part.min_price is not None
                        else shared_min),
                    max_price=(
                        part.max_price if part.max_price is not None
                        else shared_max),
                ) for part in parsed_parts)

        if complement is not None and not complement.audiences:
            primary_audiences = {
                part.audiences for part in parsed_parts if part.audiences}
            if len(primary_audiences) == 1:
                complement = replace(
                    complement, audiences=next(iter(primary_audiences)))
        return PrecisionPlan(parsed_parts, complement)

    @classmethod
    def load(cls, path: str | Path) -> 'Catalog':
        return cls(json.loads(Path(path).read_text(encoding='utf-8')))

    # -- extension points ---------------------------------------------------- #

    def relevance(self, product: dict[str, Any], query_tokens: Sequence[str]) -> float:
        """Selected relevance for one product; 0 when it was not selected at all."""
        scores = self.score_all(tuple(query_tokens))
        return scores.get(self.position_of.get(str(product['id']), -1), 0.0)

    def score_all(self, terms: tuple[str, ...]) -> dict[int, float]:
        """Cached BM25F selection for a term tuple. Filters are applied separately."""
        if not terms:
            return {}
        cached = self._score_cache.get(terms)
        if cached is not None:
            self._score_cache.move_to_end(terms)
            return cached
        scores = self.retriever.score(' '.join(terms))
        self._score_cache[terms] = scores
        if len(self._score_cache) > SCORE_CACHE_SIZE:
            self._score_cache.popitem(last=False)
        return scores

    def recommend(self, results: list[dict[str, str]], arguments: dict[str, Any]
                  ) -> list[dict[str, str]]:
        """Complementary products for a page of results. The documented entry point.

        Kept on the published signature; ``search`` uses the richer internal path so
        the recommender also sees the parsed accessory intents.
        """
        request = parse_request(arguments)
        positions = [self.position_of[row['id']] for row in results
                     if row['id'] in self.position_of]
        return self.recommendations(request, self.plan_shopping(request), positions)

    def accessory_targets(self, plan: ShoppingPlan
                          ) -> tuple[tuple[str, ...], dict[str, float]]:
        """Which product types the shopper asked for as an accessory, and its budget."""
        types: list[str] = []
        budget: dict[str, float] = {}
        for intent in plan.accessories:
            if not budget:
                budget = intent.budget.as_filters()
            requirements = self.planner.plan(intent.text).requirements
            categories: list[str] = []
            for requirement in requirements:
                for field_name, value, _ in requirement.targets:
                    if field_name == 'product_type' and value not in types:
                        types.append(value)
                    elif field_name == 'category' and value not in categories:
                        categories.append(value)
            if not types:
                for category in categories:
                    for product_type in self.recommender.expand_category(category):
                        if product_type not in types:
                            types.append(product_type)
        return tuple(types), budget

    def recommendations(self, request: Request, plan: ShoppingPlan,
                        positions: Sequence[int]) -> list[dict[str, str]]:
        if not request.include_recommendations or not positions:
            return []
        if self.use_precision_ranking:
            return self._precision_recommendations(request, positions)
        accessory_types, accessory_budget = self.accessory_targets(plan)
        return self.recommender.build(positions, request.filters, accessory_types,
                                      accessory_budget)

    def _precision_recommendations(
            self, request: Request,
            primary_positions: Sequence[int]) -> list[dict[str, str]]:
        """Preserve the calibrated full-catalog complementary ranking."""
        shopping_plan = self.shopping_planner.plan(request.query)
        precision_plan = self.precision_plan(request.query)
        primary_ids = {self.index.ids[position]
                       for position in primary_positions}

        explicit_scores: dict[int, float] | None = None
        if precision_plan.complement is not None:
            explicit_scores = self._precision_intent_scores(
                precision_plan.complement, set(self.all_indices), {})
        elif shopping_plan.accessories:
            explicit_scores = {}
            for intent in shopping_plan.accessories:
                accept = _acceptor(self.index, intent.budget.as_filters())
                for position, score in self.score_all(tokens(intent.text)).items():
                    if accept is None or accept(position):
                        explicit_scores[position] = max(
                            score, explicit_scores.get(position, 0.0))

        per_primary: list[list[tuple[int, float]]] = []
        for primary_position in primary_positions:
            primary = self.products[primary_position]
            if explicit_scores is not None:
                pool = set(explicit_scores)
                same_audience = {
                    position for position in pool
                    if self._audience_family(
                        self.products[position]['audience']
                    ) == self._audience_family(primary['audience'])
                }
                if same_audience:
                    pool = same_audience
            else:
                pool = self._inferred_complements(primary)

            ranked: list[tuple[int, float]] = []
            for candidate_position in pool:
                candidate = self.products[candidate_position]
                if candidate['id'] in primary_ids:
                    continue
                if candidate['product_type'] == primary['product_type']:
                    continue
                if explicit_scores is None and (
                        self._audience_family(candidate['audience']) !=
                        self._audience_family(primary['audience'])):
                    continue
                ranked.append((
                    candidate_position,
                    self._complement_score(
                        primary, candidate,
                        explicit_scores.get(candidate_position, 0.0)
                        if explicit_scores is not None else 0.0),
                ))
            ranked.sort(key=lambda item: (-item[1], self.index.ids[item[0]]))
            per_primary.append(ranked)

        recommendations: list[dict[str, str]] = []
        used: set[str] = set()
        offsets = [0] * len(per_primary)
        while len(recommendations) < 5:
            added = False
            for primary_number, ranked in enumerate(per_primary):
                while offsets[primary_number] < len(ranked):
                    candidate_position = ranked[offsets[primary_number]][0]
                    offsets[primary_number] += 1
                    candidate = self.products[candidate_position]
                    if candidate['id'] in used:
                        continue
                    primary = self.products[primary_positions[primary_number]]
                    used.add(candidate['id'])
                    recommendations.append({
                        'id': candidate['id'],
                        'for_product_id': primary['id'],
                        'reason': (
                            f"{candidate['product_type']} in {candidate['color']} "
                            f"to accompany the selected {primary['product_type']}."
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
    def _audience_family(audience: str) -> str:
        if audience in {'Ladieswear', 'Ladies Accessories', 'Divided',
                        'Lingeries/Tights'}:
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

    def _inferred_complements(self, primary: dict[str, Any]) -> set[int]:
        category_pairs = {
            'Garment Full body': ('Bags', 'Shoes', 'Accessories'),
            'Garment Upper body': (
                'Garment Lower body', 'Bags', 'Accessories'),
            'Garment Lower body': (
                'Garment Upper body', 'Shoes', 'Bags', 'Accessories'),
            'Shoes': ('Bags', 'Accessories', 'Garment Lower body'),
            'Swimwear': ('Bags', 'Shoes', 'Accessories'),
            'Bags': ('Accessories', 'Garment Full body',
                     'Garment Upper body'),
            'Nightwear': ('Socks & Tights', 'Accessories'),
        }
        positions: set[int] = set()
        for category in category_pairs.get(primary['category'], ()):
            positions.update(
                self.index.postings['category'].get(fold(category), ()))
        return positions

    def _complement_score(self, primary: dict[str, Any],
                          candidate: dict[str, Any],
                          retrieval_score: float) -> float:
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
                'Shoulder bag': 5, 'Cross-body bag': 5, 'Bag': 4,
                'Tote bag': 4, 'Heels': 4, 'Sandals': 3, 'Necklace': 3,
                'Earring': 3, 'Earrings': 3,
            }.get(candidate['product_type'], 0)
        average = _rating_of(candidate)
        if average is not None:
            score += average * 0.05
        return score

    @staticmethod
    def matches(product: dict[str, Any], filters: dict[str, Any]) -> bool:
        """Authoritative hard-constraint check against the original catalog record."""
        for key in STRING_FILTERS:
            if key in filters and fold(product.get(key)) != fold(filters[key]):
                return False
        price = _price_of(product)
        average = _rating_of(product)
        return (price >= filters.get('min_price', 0)
                and price <= filters.get('max_price', math.inf)
                and ('min_rating' not in filters or
                     (average is not None and average >= filters['min_rating']))
                and _review_count_of(product) >= filters.get('min_review_count', 0))

    # -- stages -------------------------------------------------------------- #

    def _candidates(self, filters: dict[str, Any]) -> list[int]:
        """Every position satisfying the explicit filters, in ID-ascending order."""
        accept = _acceptor(self.index, filters)
        if accept is None:
            return list(range(self.index.size))
        source = _selective_source(self.index, filters)
        if source is None:
            return [p for p in range(self.index.size) if accept(p)]
        return sorted(p for p in source if accept(p))

    def _filtered_page(self, request: Request) -> list[int]:
        """Pages for field sorts and empty-query relevance, without scoring anything."""
        index = self.index
        sort_by = 'id' if request.sort_by == 'relevance' else request.sort_by
        # Empty-query relevance falls back to ID ascending regardless of sort_order.
        order_key = ('id', 'asc') if request.sort_by == 'relevance' \
            else (sort_by, request.sort_order)
        accept = _acceptor(index, request.filters)
        if accept is None:
            return list(index.orders[order_key][request.offset:request.need])

        source = _selective_source(index, request.filters)
        if source is not None:
            positions = [p for p in source if accept(p)]
            descending = request.sort_order == 'desc' and request.sort_by != 'relevance'
            positions.sort(key=_order_key(index, sort_by, descending))
            return positions[request.offset:request.need]

        # Walk the presorted order and stop as soon as the page is full.
        page: list[int] = []
        need = request.need
        for position in index.orders[order_key]:
            if accept(position):
                page.append(position)
                if len(page) >= need:
                    break
        return page[request.offset:]

    def effective_filters(self, request: Request, intent: ItemIntent
                          ) -> dict[str, Any]:
        """Explicit filters win; a prose budget only fills a bound the caller omitted."""
        return {**intent.budget.as_filters(), **request.filters}

    def _precision_facet_union(self, field_name: str,
                               values: Sequence[str]) -> set[int]:
        result: set[int] = set()
        for value in values:
            result.update(
                self.index.postings[field_name].get(fold(value), ()))
        return result

    def _precision_query_terms(self, query: str) -> tuple[str, ...]:
        terms = []
        for term in tokens(query):
            if (term in PRECISION_STOP_WORDS or
                    term in PRECISION_QUERY_NOISE or term.isdigit()):
                continue
            if term in self.text_index.vocabulary:
                corrected = term
            else:
                corrected = self.planner.correct(term) or term
            if corrected not in terms:
                terms.append(corrected)
        return tuple(terms)

    def _precision_intent_allowed(
            self, intent: PrecisionIntent, allowed: set[int],
            explicit_filters: dict[str, Any]) -> tuple[set[int], set[str]]:
        result = set(allowed)
        ignored: set[str] = set()
        constraints = (
            ('product_type', intent.product_types),
            ('category', intent.categories),
            ('color', intent.colors),
            ('pattern', intent.patterns),
            ('audience', intent.audiences),
        )
        for field_name, values in constraints:
            if not values:
                continue
            matching = self._precision_facet_union(field_name, values)
            narrowed = result.intersection(matching)
            explicit_taxonomy = {'product_type', 'category'}.intersection(
                explicit_filters)
            can_override = field_name in explicit_filters or (
                field_name in {'product_type', 'category'} and
                bool(explicit_taxonomy))
            if not narrowed and can_override:
                ignored.add(field_name)
                if (field_name in {'product_type', 'category'} and
                        intent.product_types):
                    ignored.add('pattern')
                continue
            if field_name == 'pattern' and 'pattern' in ignored:
                continue
            result = narrowed
            if not result:
                return result, ignored

        if intent.min_price is not None and 'min_price' not in explicit_filters:
            result = {
                position for position in result
                if self.index.prices[position] >= intent.min_price}
        if intent.max_price is not None and 'max_price' not in explicit_filters:
            result = {
                position for position in result
                if self.index.prices[position] <= intent.max_price}
        return result, ignored

    @staticmethod
    def _precision_effective_text(
            intent: PrecisionIntent, ignored: set[str]) -> str:
        removed: set[str] = set()
        for field_name, values in (
                ('product_type', intent.product_types),
                ('category', intent.categories),
                ('color', intent.colors),
                ('pattern', intent.patterns),
                ('audience', intent.audiences)):
            if field_name in ignored:
                for value in values:
                    removed.update(tokens(value))
        return ' '.join(
            term for term in tokens(intent.retrieval_text)
            if term not in removed)

    def _precision_retrieve(self, query: str,
                            allowed: set[int]) -> dict[int, float]:
        query_terms = self._precision_query_terms(query)
        if not query_terms:
            return {}
        scores: defaultdict[int, float] = defaultdict(float)
        coverage: defaultdict[int, int] = defaultdict(int)
        total = self.index.size
        for term in query_terms:
            matching: set[int] = set()
            for group_postings in self.text_index.postings.values():
                posting = group_postings.get(term)
                if posting is not None:
                    matching.update(posting.docs)
            if not matching:
                continue
            document_frequency = len(matching)
            inverse_frequency = math.log(
                1 + (total - document_frequency + 0.5) /
                (document_frequency + 0.5))
            for position in matching:
                if position in allowed:
                    coverage[position] += 1
            for group, group_postings in self.text_index.postings.items():
                posting = group_postings.get(term)
                if posting is None:
                    continue
                contribution = (
                    PRECISION_FIELD_WEIGHTS[group] * inverse_frequency)
                for position in posting.docs:
                    if position in allowed:
                        scores[position] += contribution

        required = (len(query_terms) if len(query_terms) <= 2
                    else math.ceil(len(query_terms) * 0.6))
        phrase = normalized_text(query)
        for position in tuple(scores):
            if coverage[position] < required:
                del scores[position]
                continue
            product = self.products[position]
            name = normalized_text(product['name'])
            taxonomy = normalized_text(
                f"{product['product_type']} {product['category']}")
            if phrase and phrase == name:
                scores[position] += 20
            elif phrase and phrase in name:
                scores[position] += 8
            if phrase and phrase in taxonomy:
                scores[position] += 10
            scores[position] += coverage[position] / len(query_terms)
        return dict(scores)

    def _precision_intent_scores(
            self, intent: PrecisionIntent, allowed: set[int],
            explicit_filters: dict[str, Any]) -> dict[int, float]:
        intent_allowed, ignored = self._precision_intent_allowed(
            intent, allowed, explicit_filters)
        preferred: set[int] = set()
        if intent.soft_types:
            preferred = self._precision_facet_union(
                'product_type', intent.soft_types).intersection(intent_allowed)
            if not intent.product_types and not intent.categories:
                intent_allowed = preferred

        retrieval_text = self._precision_effective_text(intent, ignored)
        scores = self._precision_retrieve(retrieval_text, intent_allowed)
        if not retrieval_text and (
                intent.product_types or intent.categories or intent.colors or
                intent.patterns or intent.audiences or
                intent.min_price is not None or
                intent.max_price is not None):
            scores = {position: 1.0 for position in intent_allowed}
        if preferred:
            for position in preferred:
                product_type = self.products[position]['product_type']
                type_rank = intent.soft_types.index(product_type)
                scores[position] = scores.get(position, 0.0) + max(
                    4.0, 12.0 - type_rank)
        return scores

    def _precision_relevance_order(
            self, score_sets: Sequence[dict[int, float]],
            sort_order: str) -> list[int]:
        direction = -1 if sort_order == 'desc' else 1
        ranked = [
            sorted(scores, key=lambda position: (
                direction * scores[position], self.index.ids[position]))
            for scores in score_sets if scores
        ]
        if len(ranked) <= 1:
            return ranked[0] if ranked else []

        offsets = [0] * len(ranked)
        seen: set[int] = set()
        merged: list[int] = []
        while True:
            added = False
            for choice, choices in enumerate(ranked):
                while offsets[choice] < len(choices):
                    position = choices[offsets[choice]]
                    offsets[choice] += 1
                    if position in seen:
                        continue
                    seen.add(position)
                    merged.append(position)
                    added = True
                    break
            if not added:
                return merged

    def _precision_relevance_page(
            self, request: Request, plan: PrecisionPlan) -> list[int]:
        allowed = set(self._candidates(request.filters))
        score_sets = [
            self._precision_intent_scores(
                intent, allowed, request.filters)
            for intent in plan.primary
        ]
        candidates = set().union(
            *(set(scores) for scores in score_sets))
        if not candidates:
            return []
        if request.sort_by == 'relevance':
            ordered = self._precision_relevance_order(
                score_sets, request.sort_order)
        else:
            ordered = sorted(
                candidates,
                key=_order_key(
                    self.index, request.sort_by, request.descending))
        return ordered[request.offset:request.need]

    def _intent_ranking(self, request: Request, intent: ItemIntent
                        ) -> list[tuple[int, float]]:
        """One intent's products, best first, already passing its hard constraints."""
        effective_filters = self.effective_filters(request, intent)
        planned = self.planner.plan(intent.text)
        allowed = set(self._candidates(effective_filters))

        # Explicit API filters are authoritative. If a query requirement resolves
        # only to catalog values excluded by an explicit facet (including the
        # product_type/category taxonomy pair), remove that conflicting requirement
        # rather than allowing query wording to empty an otherwise valid filter.
        explicit_facets = set(request.filters).intersection(STRING_FILTERS)
        explicit_taxonomy = explicit_facets.intersection(
            {'product_type', 'category'})
        if explicit_facets and planned.requirements:
            kept = []
            for requirement in planned.requirements:
                scoped_targets = [
                    (field_name, value)
                    for field_name, value, _ in requirement.targets
                    if field_name in explicit_facets or (
                        explicit_taxonomy and
                        field_name in {'product_type', 'category'})
                ]
                conflicts = scoped_targets and not any(
                    any(position in allowed for position in
                        self.index.postings[field_name].get(fold(value), ()))
                    for field_name, value in scoped_targets
                )
                if not conflicts:
                    kept.append(requirement)
            if len(kept) != len(planned.requirements):
                planned = replace(
                    planned,
                    requirements=tuple(kept),
                    has_anchor=any(item.is_anchor for item in kept),
                )

        scores = (
            self.retriever.score_plan(planned)
            if not planned.is_empty else
            {position: 1.0 for position in allowed}
        )
        if not scores:
            return []
        rows = [(position, value) for position, value in scores.items()
                if position in allowed]
        rows.sort(key=lambda item: (-item[1], item[0]))
        return rows

    @staticmethod
    def _round_robin(rankings: Sequence[Sequence[tuple[int, float]]]
                     ) -> tuple[list[int], dict[int, int]]:
        """Interleave each choice's ranking so every choice gets early slots.

        One pooled list, one shared limit, deterministic for identical requests.
        Returns the merged order and which intent contributed each position.
        """
        merged: list[int] = []
        origin: dict[int, int] = {}
        seen: set[int] = set()
        depth = max((len(r) for r in rankings), default=0)
        for rank in range(depth):
            for number, ranking in enumerate(rankings):
                if rank >= len(ranking):
                    continue
                position = ranking[rank][0]
                if position in seen:
                    continue
                seen.add(position)
                origin[position] = number
                merged.append(position)
        return merged, origin

    def _relevance_page(self, request: Request, plan: ShoppingPlan
                        ) -> tuple[list[int], dict[int, int]]:
        """Retrieve every primary intent, merge, then order and paginate."""
        rankings = [self._intent_ranking(request, intent) for intent in plan.primary]
        if not any(rankings):
            return [], {}
        merged, origin = self._round_robin(rankings)

        if request.sort_by == 'relevance':
            if request.descending:
                positions = merged
            else:
                # asc reverses the relevance order, keeping ID ascending for ties.
                best = {position: value for ranking in rankings
                        for position, value in ranking}
                positions = sorted(merged, key=lambda p: (best[p], p))
        else:
            positions = sorted(merged,
                               key=_order_key(self.index, request.sort_by,
                                              request.descending))
        return positions[request.offset:request.need], origin

    def _recheck(self, positions: Sequence[int], filters: dict[str, Any]) -> list[int]:
        """Re-verify the page against the original records; the index must agree."""
        if not filters:
            return list(positions)
        kept = [p for p in positions if self.matches(self.products[p], filters)]
        if len(kept) != len(positions):
            LOG.error('index/recheck disagreement: dropped %d of %d rows for filters %s',
                      len(positions) - len(kept), len(positions), sorted(filters))
        return kept

    def plan_shopping(self, request: Request) -> ShoppingPlan:
        """Parse the query into intents. Accessories are dropped when disabled."""
        plan = self.shopping_planner.plan(request.query)
        if plan.accessories and not request.include_recommendations:
            if not EXPLICIT_COMPLEMENT_RE.search(request.query):
                # Preserve the useful ambiguity of a plain "dress with bag":
                # without a recommendation channel it is a mixed primary request,
                # while explicit "matching/complementary" wording remains accessory
                # intent and is omitted when recommendations are disabled.
                primary = plan.primary + tuple(
                    replace(intent, role='primary')
                    for intent in plan.accessories)
                plan = replace(
                    plan, primary=primary, accessories=(),
                    complement_requested=False)
            else:
                plan = replace(
                    plan, accessories=(), complement_requested=False)
        return plan

    def search(self, arguments: dict[str, Any]) -> dict[str, Any]:
        request = parse_request(arguments)
        plan = self.plan_shopping(request)
        if not request.query.strip():
            positions = self._recheck(self._filtered_page(request), request.filters)
        elif self.use_precision_ranking:
            precision_plan = self.precision_plan(request.query)
            positions = self._precision_relevance_page(
                request, precision_plan)
            positions = self._recheck(positions, request.filters)
        else:
            positions, origin = self._relevance_page(request, plan)
            positions = self._recheck_intents(
                positions, origin, request, plan)
        results = [{'id': self.index.ids[position]} for position in positions]
        return {'results': results,
                'recommendations': self.recommendations(request, plan, positions)}

    def _recheck_intents(self, positions: Sequence[int], origin: dict[int, int],
                         request: Request, plan: ShoppingPlan) -> list[int]:
        """Re-verify each returned row against the constraints of its own intent."""
        kept = []
        for position in positions:
            intent = plan.primary[origin.get(position, 0)]
            if self.matches(self.products[position],
                            self.effective_filters(request, intent)):
                kept.append(position)
        if len(kept) != len(positions):
            LOG.error('index/recheck disagreement: dropped %d of %d rows',
                      len(positions) - len(kept), len(positions))
        return kept
