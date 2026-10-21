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
* explicit category/colour/pattern filters constrain the primary only, while
  unqualified global requirements (audience, rating, review count, price) also apply;
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
#: Requirements about the shopper or the budget, which do carry over.
GLOBAL_FILTERS = ('audience', 'min_price', 'max_price', 'min_rating',
                  'min_review_count')
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
        """Global requirements carry over; the kind of product never does."""
        filters = {key: value for key, value in explicit.items()
                   if key in GLOBAL_FILTERS}
        if accessory_budget:
            # An accessory budget stated in the query replaces an inherited one.
            filters.pop('min_price', None)
            filters.pop('max_price', None)
            filters.update(accessory_budget)
        return filters

    def split_filters(self, explicit: dict[str, Any],
                      accessory_budget: dict[str, float] | None
                      ) -> tuple[dict[str, Any], str | None]:
        """Exact filters to apply, plus an audience to match by family."""
        filters = self.recommendation_filters(explicit, accessory_budget)
        audience = filters.pop('audience', None)
        return filters, (str(audience).strip().casefold() if audience else None)

    # -- ranking ------------------------------------------------------------ #

    def _score(self, position: int, anchor: int, type_rank: int, anchor_rank: int,
               requested: bool) -> float:
        index = self.catalog.index
        params = self.params
        score = params.type_rank_decay ** type_rank
        if requested:
            score += params.requested_type_bonus
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
                        requested_types: Sequence[str], filters: dict[str, Any],
                        excluded: set[int], max_per_type: int,
                        audience: str | None = None) -> list[Candidate]:
        index = self.catalog.index
        primary_type = index.folded['product_type'][anchor]
        primary_category = index.folded['category'][anchor]
        primary_audience = index.folded['audience'][anchor]

        if requested_types:
            ranked = [t for t in requested_types
                      if not self.complements.same_role(primary_type,
                                                        t.strip().casefold())]
        else:
            ranked = list(self.complements.ranked_for(primary_type, primary_category))
        if not ranked:
            return []

        found: list[Candidate] = []
        for type_rank, product_type in enumerate(ranked):
            folded_type = product_type.strip().casefold()
            positions = self.quality_order.get(folded_type)
            if not positions:
                continue
            taken = 0
            for scanned, position in enumerate(positions):
                if scanned >= self.params.scan_depth and taken:
                    break
                if position in excluded:
                    continue
                candidate_audience = index.folded['audience'][position]
                if not self.complements.audience_ok(primary_audience,
                                                    candidate_audience):
                    continue
                if audience and not self.complements.audience_ok(audience,
                                                                 candidate_audience):
                    continue
                if not self.catalog.matches(self.catalog.products[position], filters):
                    continue
                found.append(Candidate(position, anchor, folded_type,
                                       self._score(position, anchor, type_rank,
                                                   anchor_rank,
                                                   bool(requested_types))))
                taken += 1
                if taken >= max_per_type:
                    break
        return found

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
              accessory_budget: dict[str, float] | None = None) -> list[dict[str, str]]:
        if not primary_positions:
            return []
        filters, audience = self.split_filters(explicit_filters, accessory_budget)
        excluded = set(primary_positions)

        # When the shopper named the accessory, fill the page with it rather than
        # spreading across types for variety they did not ask for.
        requested = bool(accessory_types)
        max_per_type = self.params.limit if requested else self.params.max_per_type
        max_per_anchor = self.params.limit if requested else self.params.max_per_anchor

        pool: list[Candidate] = []
        anchors = list(primary_positions[:self.params.max_anchors])
        for anchor_rank, anchor in enumerate(anchors):
            pool.extend(self._candidates_for(anchor, anchor_rank, accessory_types,
                                             filters, excluded, max_per_type,
                                             audience))
        if not pool:
            return []

        pool.sort(key=lambda c: (-c.score, self.catalog.index.ids[c.position]))
        chosen: list[Candidate] = []
        seen: set[int] = set()
        per_type: dict[str, int] = {}
        per_anchor: dict[int, int] = {}
        for candidate in pool:
            if len(chosen) >= self.params.limit:
                break
            if candidate.position in seen or candidate.position in excluded:
                continue
            if per_type.get(candidate.product_type, 0) >= max_per_type:
                continue
            if per_anchor.get(candidate.anchor, 0) >= max_per_anchor:
                continue
            seen.add(candidate.position)
            per_type[candidate.product_type] = per_type.get(candidate.product_type, 0) + 1
            per_anchor[candidate.anchor] = per_anchor.get(candidate.anchor, 0) + 1
            chosen.append(candidate)

        ids = self.catalog.index.ids
        return [{'id': ids[c.position], 'for_product_id': ids[c.anchor],
                 'reason': self.reason(c.position, c.anchor)} for c in chosen]
