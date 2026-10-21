"""Complementary recommendations: items worn or carried *with* a primary result.

The hard part is not finding similar products, it is refusing to. Similarity returns
substitutes — a second dress for a dress — and a substitute earns nothing. So the
candidate type is decided first, from a reviewed map in ``data/complements.json``,
and anything filling the same wardrobe slot as the primary is excluded outright.

Contract rules enforced here (hackathon-brief.md):

* empty when recommendations are disabled, when there are no primary results, or when
  no useful complement exists -- a forced recommendation is worth less than none;
* at most five, unique IDs, none of them a primary result on this page;
* ``for_product_id`` always references a primary result on the current page;
* every explicit API filter constrains the primary only; query-derived accessory
  constraints and unqualified query audiences apply to recommendations;
* an accessory budget stated in the query wins over an inherited one;
* ``reason`` is built only from catalog attributes, never invented.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

ROOT = Path(__file__).resolve().parent

#: Facets that describe *what kind of thing* an item is. A complement is a different
#: kind by definition, so these never carry over from the primary request.
KIND_FILTERS = ('category', 'product_type', 'color', 'pattern', 'section')
#: Explicit API filters constrain primary results only. Query-derived global
#: requirements are represented in RecommendationIntent target groups instead.
GLOBAL_FILTERS: tuple[str, ...] = ()
#: Audience carries over as a *family* rather than an exact value: a ladies' handbag
#: is filed under "Ladies Accessories", not "Ladieswear", and excluding it would
#: throw away the obvious complement for a Ladieswear dress.
FAMILY_FILTERS = ('audience',)


@dataclass(frozen=True)
class RecommendationParams:
    """Knobs for complement ranking."""

    limit: int = 5
    #: Only the strongest primaries anchor recommendations.
    max_anchors: int = 3
    max_per_type: int = 3
    max_per_anchor: int = 3
    #: Candidates scanned per (anchor, type) before giving up on that type.
    scan_depth: int = 600
    type_rank_decay: float = 0.75
    same_color_bonus: float = 0.6
    neutral_color_bonus: float = 0.35
    same_pattern_bonus: float = 0.1
    same_audience_bonus: float = 0.4
    requested_type_bonus: float = 6.0
    rating_weight: float = 0.12
    review_weight: float = 0.04
    anchor_decay: float = 0.9


@dataclass(frozen=True)
class RecommendationIntent:
    """Catalog-grounded hard constraints for one requested accompanying item.

    Each target group corresponds to one query requirement. Targets inside a
    group are alternatives (for example audience=Sport OR section=Kids Sports);
    every non-empty group must be satisfied. This preserves the retrieval
    planner's semantics without importing search.py or reparsing the raw query.
    """

    target_groups: tuple[tuple[tuple[str, str], ...], ...] = ()
    min_price: float | None = None
    max_price: float | None = None

    @property
    def product_types(self) -> tuple[str, ...]:
        return self._values('product_type')

    @property
    def categories(self) -> tuple[str, ...]:
        return self._values('category')

    @property
    def has_kind(self) -> bool:
        return bool(self.product_types or self.categories)

    def _values(self, field_name: str) -> tuple[str, ...]:
        return tuple(dict.fromkeys(
            value for group in self.target_groups
            for field, value in group if field == field_name
        ))


@dataclass
class Complements:
    """The reviewed complement map, indexed for lookup."""

    roles: dict[str, tuple[str, ...]]
    complements: dict[str, tuple[str, ...]]
    category_fallback: dict[str, tuple[str, ...]]
    no_complement: frozenset[str]
    neutral_colors: frozenset[str]
    audience_families: dict[str, tuple[str, ...]]
    audience_compatible: dict[str, frozenset[str]]
    role_of: dict[str, str] = field(default_factory=dict)
    family_of: dict[str, str] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path | str = ROOT / 'data/complements.json') -> 'Complements':
        raw = json.loads(Path(path).read_text(encoding='utf-8'))

        def folded(values: Iterable[str]) -> tuple[str, ...]:
            return tuple(v.strip().casefold() for v in values)

        instance = cls(
            roles={name: folded(values) for name, values in raw['roles'].items()},
            complements={k.strip().casefold(): tuple(raw['complements'][k])
                         for k in raw['complements']},
            category_fallback={k.strip().casefold(): tuple(v)
                               for k, v in raw['category_fallback'].items()},
            no_complement=frozenset(folded(raw['no_complement'])),
            neutral_colors=frozenset(folded(raw['neutral_colors'])),
            audience_families={name: folded(values)
                               for name, values in raw['audience_families'].items()},
            audience_compatible={k: frozenset(v)
                                 for k, v in raw['audience_compatible'].items()},
        )
        instance.role_of = {value: role for role, values in instance.roles.items()
                            for value in values}
        instance.family_of = {value: family
                              for family, values in instance.audience_families.items()
                              for value in values}
        return instance

    def ranked_for(self, product_type: str, category: str) -> tuple[str, ...]:
        """Complement types for a primary, best first; empty when none is useful."""
        if product_type in self.no_complement:
            return ()
        ranked = self.complements.get(product_type)
        if ranked is None:
            ranked = self.category_fallback.get(category, ())
        return tuple(value for value in ranked
                     if not self.same_role(product_type, value.strip().casefold()))

    def same_role(self, left: str, right: str) -> bool:
        """Do these two types fill the same wardrobe slot? Then one is a substitute."""
        if left == right:
            return True
        role = self.role_of.get(left)
        return role is not None and role == self.role_of.get(right)

    def audience_ok(self, primary_audience: str, candidate_audience: str) -> bool:
        primary_family = self.family_of.get(primary_audience)
        candidate_family = self.family_of.get(candidate_audience)
        if primary_family is None or candidate_family is None:
            return True
        return candidate_family in self.audience_compatible.get(primary_family,
                                                                frozenset())

    def unknown_values(self, product_types: set[str], categories: set[str],
                       colors: set[str], audiences: set[str]) -> list[str]:
        """Complement targets naming no catalog value; used by check_lexicon.py."""
        missing = []
        for source, label in ((self.complements, 'complements key'),
                              (self.category_fallback, 'category_fallback key')):
            for key in source:
                known = product_types if 'complements' in label else categories
                if key not in known:
                    missing.append(f'{label}: {key!r}')
        for key, values in self.complements.items():
            for value in values:
                if value.strip().casefold() not in product_types:
                    missing.append(f'complements[{key}]: {value!r}')
        for key, values in self.category_fallback.items():
            for value in values:
                if value.strip().casefold() not in product_types:
                    missing.append(f'category_fallback[{key}]: {value!r}')
        for role, values in self.roles.items():
            for value in values:
                if value not in product_types:
                    missing.append(f'roles[{role}]: {value!r}')
        for value in self.no_complement:
            if value not in product_types:
                missing.append(f'no_complement: {value!r}')
        for value in self.neutral_colors:
            if value not in colors:
                missing.append(f'neutral_colors: {value!r}')
        for family, values in self.audience_families.items():
            for value in values:
                if value not in audiences:
                    missing.append(f'audience_families[{family}]: {value!r}')
        return missing


@dataclass
class Candidate:
    position: int
    anchor: int
    product_type: str
    score: float


class Recommender:
    """Builds complementary recommendations for a page of primary results."""

    def __init__(self, catalog: Any, complements: Complements | None = None,
                 params: RecommendationParams | None = None) -> None:
        self.catalog = catalog
        self.complements = complements or Complements.load()
        self.params = params or RecommendationParams()
        self.quality_order = self._build_quality_order()
        self.types_by_category = self._build_types_by_category()

    def _build_quality_order(self) -> dict[str, list[int]]:
        """Per product_type, positions ordered by rating then reviews then ID.

        Lets the scan look at plausible items first instead of walking a whole type.
        """
        index = self.catalog.index
        ratings, counts = index.ratings, index.review_counts
        order: dict[str, list[int]] = {}
        for value, posting in index.postings['product_type'].items():
            order[value] = sorted(
                posting,
                key=lambda p: (-(ratings[p] if ratings[p] is not None else -1.0),
                               -counts[p], p))
        return order

    def _build_types_by_category(self) -> dict[str, list[str]]:
        """Product types in each category, most common first.

        Lets an accessory named only by category ("with matching shoes") resolve to
        the types that category actually contains.
        """
        index = self.catalog.index
        counts: dict[str, dict[str, int]] = {}
        for position in range(index.size):
            bucket = counts.setdefault(index.folded['category'][position], {})
            product_type = index.folded['product_type'][position]
            bucket[product_type] = bucket.get(product_type, 0) + 1
        return {category: [t for t, _ in sorted(bucket.items(),
                                                key=lambda kv: (-kv[1], kv[0]))]
                for category, bucket in counts.items()}

    def expand_category(self, category: str, limit: int = 8) -> list[str]:
        return self.types_by_category.get(category, [])[:limit]

    # -- constraints -------------------------------------------------------- #

    def recommendation_filters(self, explicit: dict[str, Any],
                               accessory_budget: dict[str, float] | None
                               ) -> dict[str, Any]:
        """Legacy adapter: explicit API filters never constrain recommendations."""
        filters: dict[str, Any] = {}
        if accessory_budget:
            filters.update(accessory_budget)
        return filters

    def split_filters(self, explicit: dict[str, Any],
                      accessory_budget: dict[str, float] | None
                      ) -> tuple[dict[str, Any], str | None]:
        """Compatibility adapter for callers using the pre-intent API."""
        filters = self.recommendation_filters(explicit, accessory_budget)
        return filters, None

    # -- ranking ------------------------------------------------------------ #

    @staticmethod
    def _fold(value: Any) -> str:
        return str(value or '').strip().casefold()

    def _matches_intent(self, position: int,
                        intent: RecommendationIntent) -> bool:
        """Apply query-derived accessory constraints as hard requirements."""
        index = self.catalog.index
        price = index.prices[position]
        if intent.min_price is not None and price < intent.min_price:
            return False
        if intent.max_price is not None and price > intent.max_price:
            return False

        candidate_audience = index.folded['audience'][position]
        for group in intent.target_groups:
            matched = False
            for field_name, value in group:
                if field_name not in index.folded:
                    continue
                wanted = self._fold(value)
                if field_name == 'audience':
                    matched = self.complements.audience_ok(
                        wanted, candidate_audience)
                else:
                    matched = index.folded[field_name][position] == wanted
                if matched:
                    break
            if not matched:
                return False
        return True

    def _ranked_types(self, anchor: int,
                      intent: RecommendationIntent | None) -> list[str]:
        """Resolve a requested kind or the reviewed complement map to type locks."""
        index = self.catalog.index
        primary_type = index.folded['product_type'][anchor]
        primary_category = index.folded['category'][anchor]

        if intent is not None and intent.has_kind:
            # Category values are catalog concepts too. Put their concrete types
            # first, then add direct product_type targets as recall fallbacks. This
            # keeps broad nouns such as "shoes" useful without losing exact aliases.
            ranked = []
            for category in intent.categories:
                ranked.extend(self.expand_category(
                    self._fold(category), limit=len(self.quality_order)))
            ranked.extend(self._fold(value) for value in intent.product_types)
        else:
            ranked = [
                self._fold(value) for value in
                self.complements.ranked_for(primary_type, primary_category)
            ]

        return [
            product_type for product_type in dict.fromkeys(ranked)
            if not self.complements.same_role(primary_type, product_type)
        ]

    def _score(self, position: int, anchor: int, type_rank: int, anchor_rank: int,
               requested: bool) -> float:
        index = self.catalog.index
        params = self.params
        score = params.type_rank_decay ** type_rank
        if requested:
            # Requested alternatives are ordered: an exact target should remain
            # ahead of broader synonyms even when the latter has a higher rating.
            score += (
                params.requested_type_bonus *
                params.type_rank_decay ** type_rank
            )
        score *= params.anchor_decay ** anchor_rank

        primary_color = index.folded['color'][anchor]
        candidate_color = index.folded['color'][position]
        if candidate_color == primary_color:
            score += params.same_color_bonus
        elif candidate_color in self.complements.neutral_colors:
            score += params.neutral_color_bonus
        if index.folded['pattern'][position] == index.folded['pattern'][anchor]:
            score += params.same_pattern_bonus
        if index.folded['audience'][position] == index.folded['audience'][anchor]:
            score += params.same_audience_bonus

        average = index.ratings[position]
        if average is not None:
            score += params.rating_weight * average
            score += params.review_weight * min(index.review_counts[position], 8)
        return score

    def _candidates_for(self, anchor: int, anchor_rank: int,
                        intent: RecommendationIntent | None,
                        excluded: set[int], max_per_type: int) -> list[Candidate]:
        index = self.catalog.index
        primary_type = index.folded['product_type'][anchor]
        primary_audience = index.folded['audience'][anchor]
        ranked = self._ranked_types(anchor, intent)
        if not ranked:
            return []

        requested = bool(intent is not None and intent.has_kind)
        requested_categories = {
            self._fold(value) for value in intent.categories
        } if intent is not None else set()
        found: list[Candidate] = []
        category_found: list[Candidate] = []
        for type_rank, product_type in enumerate(ranked):
            folded_type = self._fold(product_type)
            positions = self.quality_order.get(folded_type)
            if not positions:
                continue
            taken = 0
            category_taken = 0
            for scanned, position in enumerate(positions):
                if (not requested_categories and
                        scanned >= self.params.scan_depth and
                        (taken or category_taken)):
                    break
                if position in excluded:
                    continue
                candidate_audience = index.folded['audience'][position]
                if not self.complements.audience_ok(primary_audience,
                                                    candidate_audience):
                    continue
                if intent is not None and not self._matches_intent(
                        position, intent):
                    continue
                if self.complements.same_role(
                        primary_type,
                        index.folded['product_type'][position]):
                    continue
                score = self._score(
                    position, anchor, type_rank, anchor_rank, requested)
                if index.folded['category'][position] in requested_categories:
                    # When a noun resolves to both a category and a type (notably
                    # "bag"), exact-category evidence is the stronger meaning.
                    score += self.params.requested_type_bonus * 0.5
                    if category_taken < max_per_type:
                        category_found.append(Candidate(
                            position, anchor, folded_type, score))
                        category_taken += 1
                elif taken < max_per_type:
                    found.append(Candidate(
                        position, anchor, folded_type, score))
                    taken += 1
                if (taken >= max_per_type and
                        (not requested_categories or
                         category_taken >= max_per_type)):
                    break
        # Do not pad a category-grounded request with a weaker cross-category
        # interpretation merely to reach five. Fall back only when the requested
        # category has no viable inventory for this anchor.
        return category_found or found

    # -- reasons ------------------------------------------------------------ #

    def reason(self, position: int, anchor: int) -> str:
        """One sentence, built only from catalog attributes."""
        products = self.catalog.products
        candidate, primary = products[position], products[anchor]
        candidate_type = str(candidate.get('product_type') or 'item')
        primary_type = str(primary.get('product_type') or 'item').lower()
        candidate_color = str(candidate.get('color') or '')
        primary_color = str(primary.get('color') or '').lower()

        text = (f'{candidate_color.lower()} {candidate_type.lower()} to wear with '
                f'the {primary_color} {primary_type}').strip()
        if candidate_color.strip().casefold() == str(primary.get('color') or
                                                     '').strip().casefold():
            text += '; the same colour'
        elif candidate_color.strip().casefold() in self.complements.neutral_colors:
            text += f'; {candidate_color.lower()} is a neutral'
        average = (candidate.get('rating') or {}).get('average')
        count = (candidate.get('rating') or {}).get('count') or 0
        if isinstance(average, (int, float)) and count:
            text += f'. Rated {average:.1f} from {count} review'
            text += 's.' if count != 1 else '.'
        else:
            text += '.'
        text = text[:1].upper() + text[1:]
        return (text[:299] + '.') if len(text) > 300 else text

    # -- entry point -------------------------------------------------------- #

    def build(self, primary_positions: Sequence[int], explicit_filters: dict[str, Any],
              accessory_types: Sequence[str] = (),
              accessory_budget: dict[str, float] | None = None,
              recommendation_intents: Sequence[RecommendationIntent] = (),
              excluded_positions: Sequence[int] | None = None,
              ) -> list[dict[str, str]]:
        if not primary_positions:
            return []
        excluded = set(
            primary_positions if excluded_positions is None
            else excluded_positions)

        intents = list(recommendation_intents)
        if not intents and accessory_types:
            budget = accessory_budget or {}
            intents.append(RecommendationIntent(
                target_groups=(tuple(
                    ('product_type', value) for value in accessory_types),),
                min_price=budget.get('min_price'),
                max_price=budget.get('max_price'),
            ))
        # No named accessory means one inferred request driven entirely by the
        # reviewed complement map. Explicit API filters are intentionally unused.
        planned: list[RecommendationIntent | None] = intents or [None]
        anchors = list(primary_positions[:self.params.max_anchors])
        pools: list[list[Candidate]] = []
        for intent in planned:
            requested = bool(intent is not None and intent.has_kind)
            max_per_type = (
                self.params.limit if requested else self.params.max_per_type)
            pool = []
            for anchor_rank, anchor in enumerate(anchors):
                pool.extend(self._candidates_for(
                    anchor, anchor_rank, intent, excluded, max_per_type))
            if intent is not None and intent.categories:
                wanted_categories = {
                    self._fold(value) for value in intent.categories}
                category_pool = [
                    candidate for candidate in pool
                    if self.catalog.index.folded['category'][
                        candidate.position] in wanted_categories
                ]
                if category_pool:
                    pool = category_pool
            pool.sort(
                key=lambda candidate: (
                    -candidate.score,
                    self.catalog.index.ids[candidate.position],
                    self.catalog.index.ids[candidate.anchor],
                ))
            pools.append(pool)
        if not any(pools):
            return []

        chosen: list[Candidate] = []
        seen: set[int] = set()
        per_type: dict[str, int] = {}
        per_anchor: dict[int, int] = {}
        offsets = [0] * len(pools)
        while len(chosen) < self.params.limit:
            added = False
            for intent_number, pool in enumerate(pools):
                intent = planned[intent_number]
                requested = bool(intent is not None and intent.has_kind)
                max_per_type = (
                    self.params.limit if requested else self.params.max_per_type)
                max_per_anchor = (
                    self.params.limit if requested else self.params.max_per_anchor)
                while offsets[intent_number] < len(pool):
                    candidate = pool[offsets[intent_number]]
                    offsets[intent_number] += 1
                    if (candidate.position in seen or
                            candidate.position in excluded):
                        continue
                    if per_type.get(candidate.product_type, 0) >= max_per_type:
                        continue
                    if per_anchor.get(candidate.anchor, 0) >= max_per_anchor:
                        continue
                    seen.add(candidate.position)
                    per_type[candidate.product_type] = (
                        per_type.get(candidate.product_type, 0) + 1)
                    per_anchor[candidate.anchor] = (
                        per_anchor.get(candidate.anchor, 0) + 1)
                    chosen.append(candidate)
                    added = True
                    break
                if len(chosen) >= self.params.limit:
                    break
            if not added:
                break

        ids = self.catalog.index.ids
        return [{'id': ids[c.position], 'for_product_id': ids[c.anchor],
                 'reason': self.reason(c.position, c.anchor)} for c in chosen]
