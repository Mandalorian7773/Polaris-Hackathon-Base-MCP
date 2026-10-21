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
from dataclasses import dataclass, field
from collections import OrderedDict
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

from jsonschema import Draft202012Validator, ValidationError

import retrieval
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
                 lexicon: Lexicon | None = None) -> None:
        self.products: list[dict[str, Any]] = sorted(products, key=lambda p: p['id'])
        self.index = build_index(self.products)
        self.position_of: dict[str, int] = {pid: i for i, pid in enumerate(self.index.ids)}

        self.params = params or RankingParams()
        self.lexicon = lexicon or Lexicon.load()
        self.text_index = TextIndex(self.products, self.lexicon)
        self.vocabularies = {f: set(self.index.postings[f]) - {''}
                             for f in STRING_FILTERS}
        self._score_cache: OrderedDict[tuple[str, ...], dict[int, float]] = OrderedDict()
        self.set_params(self.params)

    def set_params(self, params: RankingParams) -> None:
        """Swap ranking parameters without rebuilding the index. Used by the sweep."""
        self.params = params
        self.planner = QueryPlanner(self.text_index, self.vocabularies, self.lexicon,
                                    params)
        self.retriever = Retriever(self.text_index, self.index.postings, self.planner,
                                   params)
        self._score_cache.clear()

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
        """Student extension: return up to five complementary product references."""
        return []

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

    def _relevance_page(self, request: Request, plan: QueryPlan) -> list[int]:
        """Score every filter-passing product, then order by relevance or a field."""
        index = self.index
        scores = self.score_all(plan.terms)
        if not scores:
            return []
        accept = _acceptor(index, request.filters)
        scored = [(position, value) for position, value in scores.items()
                  if accept is None or accept(position)]
        if not scored:
            return []

        if request.sort_by == 'relevance':
            sign = -1 if request.descending else 1
            scored.sort(key=lambda item: (sign * item[1], item[0]))
            positions = [position for position, _ in scored]
        else:
            positions = [position for position, _ in scored]
            positions.sort(key=_order_key(index, request.sort_by, request.descending))
        return positions[request.offset:request.need]

    def _recheck(self, positions: Sequence[int], filters: dict[str, Any]) -> list[int]:
        """Re-verify the page against the original records; the index must agree."""
        if not filters:
            return list(positions)
        kept = [p for p in positions if self.matches(self.products[p], filters)]
        if len(kept) != len(positions):
            LOG.error('index/recheck disagreement: dropped %d of %d rows for filters %s',
                      len(positions) - len(kept), len(positions), sorted(filters))
        return kept

    def search(self, arguments: dict[str, Any]) -> dict[str, Any]:
        request = parse_request(arguments)
        plan = plan_query(request.query)
        if plan.is_empty:
            positions = self._filtered_page(request)
        else:
            positions = self._relevance_page(request, plan)
        positions = self._recheck(positions, request.filters)
        results = [{'id': self.index.ids[position]} for position in positions]
        recommendations = (self.recommend(results, arguments)
                           if results and request.include_recommendations else [])
        return {'results': results, 'recommendations': recommendations}
