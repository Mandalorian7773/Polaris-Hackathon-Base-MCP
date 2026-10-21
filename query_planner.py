"""Deterministic shopping-intent planning: budgets, mixed choices, complements.

Turns one query string into a :class:`ShoppingPlan`:

    "black jeans and white t-shirts"          -> 2 primary intents
    "a black dress with a matching bag"       -> 1 primary + 1 accessory intent
    "a dress under 2000 with a bag under 1000" -> per-item budgets on each

Rules this encodes, from hackathon-brief.md:

* Named choices joined by *and / or / ,* are **mixed**: both belong in primary
  results, sharing one limit.
* An item introduced as *accompanying, matching or complementary* belongs only in
  recommendations, and is dropped entirely when recommendations are disabled.
* Budgets are **per item** unless the query assigns a different one to the accessory.
* Explicit API filters take precedence, so a prose budget only fills a bound the
  caller did not set.

No model, no network: this is rules over catalog-derived vocabulary, so the same
query always plans the same way.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Iterable, Sequence

#: Amount with an optional currency marker and an optional k/lakh multiplier.
_AMOUNT = r'(?:(?:₹|rs\.?|inr)\s*)?(\d[\d,]*(?:\.\d+)?)\s*(k|lakh|lac)?'
_MULTIPLIER = {'k': 1_000.0, 'lakh': 100_000.0, 'lac': 100_000.0}

_MAX_CUES = (r'under', r'below', r'less than', r'lower than', r'cheaper than',
             r'within', r'up ?to', r'upto', r'at most', r'no more than',
             r'not more than', r'max(?:imum)?(?: of)?', r'<=?')
_MIN_CUES = (r'over', r'above', r'more than', r'at least', r'min(?:imum)?(?: of)?',
             r'starting (?:at|from)', r'from', r'>=?')

RANGE_RE = re.compile(
    rf'(?<!\w)(?:between|from)\s+{_AMOUNT}\s*(?:and|to|-|–)\s*{_AMOUNT}', re.I)
# (?<!\w) rather than \b: "\b" does not match between a space and "<", so
# "under 2000" parsed but "< 2000" did not.
MAX_RE = re.compile(rf'(?<!\w)(?:{"|".join(_MAX_CUES)})\s*{_AMOUNT}', re.I)
MIN_RE = re.compile(rf'(?<!\w)(?:{"|".join(_MIN_CUES)})\s*{_AMOUNT}', re.I)
MAX_SUFFIX_RE = re.compile(rf'{_AMOUNT}\s*(?:or less|or under|or below|and under|'
                           rf'and below)', re.I)
MIN_SUFFIX_RE = re.compile(rf'{_AMOUNT}\s*(?:or more|or above|and above|and up|'
                           rf'plus)', re.I)

#: "each" means the budget repeats per item rather than covering the basket.
PER_ITEM_RE = re.compile(r'\b(?:each|apiece|a piece|per item|per piece)\b', re.I)

#: Cues that trail the accessory: "... with a sarong to match".
TRAILING_CUE_RE = re.compile(
    r'\s+(?:to match(?:\s+it|\s+them)?|that match(?:es|)(?:\s+it|\s+them)?|'
    r'to go with(?:\s+it|\s+them)?|that go(?:es)? with(?:\s+it|\s+them)?|'
    r'to accompany(?:\s+it|\s+them)?|to pair with(?:\s+it|\s+them)?|'
    r'paired with(?:\s+it|\s+them)?|to complete (?:the |my )?(?:look|outfit)|'
    r'to suit(?:\s+it|\s+them)?)\s*$', re.I)

#: Cues that introduce the accessory inline: "... with a matching bag".
INLINE_CUE_RE = re.compile(
    r'\s+(?:with|plus|and)\s+(?:a |an |some |the )?'
    r'(?:matching|complementary|coordinating|accompanying)\s+', re.I)

#: A plain "with" only introduces an accessory when what follows names a product.
WITH_RE = re.compile(r'\s+with\s+', re.I)

CONJUNCTION_RE = re.compile(r'\s*(?:,\s*(?:and\s+|or\s+)?|\s+and\s+|\s+or\s+|'
                            r'\s+&\s+|\s+plus\s+)\s*', re.I)


@dataclass(frozen=True)
class Budget:
    """A price range stated in the query, in INR."""

    min_price: float | None = None
    max_price: float | None = None

    @property
    def is_empty(self) -> bool:
        return self.min_price is None and self.max_price is None

    def as_filters(self) -> dict[str, float]:
        bounds: dict[str, float] = {}
        if self.min_price is not None:
            bounds['min_price'] = self.min_price
        if self.max_price is not None:
            bounds['max_price'] = self.max_price
        return bounds

    def merge(self, other: 'Budget') -> 'Budget':
        """Keep our own bounds; take the other's only where we have none."""
        return Budget(
            min_price=self.min_price if self.min_price is not None else other.min_price,
            max_price=self.max_price if self.max_price is not None else other.max_price,
        )


@dataclass(frozen=True)
class ItemIntent:
    """One thing the shopper asked for, with the constraints stated about it."""

    text: str
    budget: Budget = Budget()
    role: str = 'primary'

    @property
    def is_accessory(self) -> bool:
        return self.role == 'accessory'


@dataclass(frozen=True)
class ShoppingPlan:
    primary: tuple[ItemIntent, ...] = ()
    accessories: tuple[ItemIntent, ...] = ()
    per_item_budget: bool = False
    complement_requested: bool = False

    @property
    def is_empty(self) -> bool:
        return not self.primary and not self.accessories

    @property
    def is_mixed(self) -> bool:
        return len(self.primary) > 1


def parse_amount(digits: str, suffix: str | None) -> float | None:
    try:
        value = float(digits.replace(',', ''))
    except ValueError:
        return None
    if suffix:
        value *= _MULTIPLIER.get(suffix.casefold(), 1.0)
    return value if value >= 0 else None


def parse_budget(text: str) -> tuple[Budget, str]:
    """Extract a price range and return it with the budget wording removed."""
    minimum: float | None = None
    maximum: float | None = None
    remaining = text

    def take(pattern: re.Pattern[str],
             apply: Callable[[re.Match[str]], None]) -> None:
        nonlocal remaining
        match = pattern.search(remaining)
        while match:
            apply(match)
            remaining = (remaining[:match.start()] + ' ' + remaining[match.end():])
            match = pattern.search(remaining)

    def set_range(match: re.Match[str]) -> None:
        nonlocal minimum, maximum
        low = parse_amount(match.group(1), match.group(2))
        high = parse_amount(match.group(3), match.group(4))
        if low is not None and high is not None:
            minimum = low if minimum is None else min(minimum, low)
            maximum = high if maximum is None else max(maximum, high)

    def set_max(match: re.Match[str]) -> None:
        nonlocal maximum
        value = parse_amount(match.group(1), match.group(2))
        if value is not None:
            maximum = value if maximum is None else min(maximum, value)

    def set_min(match: re.Match[str]) -> None:
        nonlocal minimum
        value = parse_amount(match.group(1), match.group(2))
        if value is not None:
            minimum = value if minimum is None else max(minimum, value)

    take(RANGE_RE, set_range)
    take(MAX_SUFFIX_RE, set_max)
    take(MIN_SUFFIX_RE, set_min)
    take(MAX_RE, set_max)
    take(MIN_RE, set_min)

    if minimum is not None and maximum is not None and minimum > maximum:
        # Contradictory wording: keep the upper bound, which is the usual intent.
        minimum = None
    return Budget(minimum, maximum), re.sub(r'\s+', ' ', remaining).strip()


class ShoppingPlanner:
    """Splits a query into primary and accessory intents with their budgets."""

    def __init__(self, names_product: Callable[[str], bool]) -> None:
        #: Decides whether a fragment names a kind of product, using catalog
        #: vocabulary and the reviewed lexicon. Injected so this module stays
        #: independent of the retrieval index.
        self.names_product = names_product

    def plan(self, query: str) -> ShoppingPlan:
        text = re.sub(r'\s+', ' ', (query or '').strip())
        if not text:
            return ShoppingPlan()
        per_item = bool(PER_ITEM_RE.search(text))

        primary_text, accessory_text, requested = self._split_complement(text)
        primary_intents = self._intents(primary_text, 'primary')
        accessory_intents = self._intents(accessory_text, 'accessory') \
            if accessory_text else ()

        if not primary_intents and accessory_intents:
            # "a matching bag" with nothing to match: treat it as the primary ask.
            primary_intents = tuple(replace(i, role='primary')
                                    for i in accessory_intents)
            accessory_intents, requested = (), False

        primary_intents = self._spread_budget(primary_intents, per_item)
        accessory_intents = self._spread_budget(accessory_intents, per_item)
        if accessory_intents and per_item is False:
            accessory_intents = self._inherit_budget(accessory_intents, primary_intents)

        return ShoppingPlan(primary=primary_intents, accessories=accessory_intents,
                            per_item_budget=per_item,
                            complement_requested=requested and bool(accessory_intents))

    # -- complement detection ---------------------------------------------- #

    def _split_complement(self, text: str) -> tuple[str, str, bool]:
        """Return (primary text, accessory text, an accessory was asked for)."""
        trailing = TRAILING_CUE_RE.search(text)
        body = text[:trailing.start()] if trailing else text

        inline = self._last_match(INLINE_CUE_RE, body)
        if inline:
            return body[:inline.start()], body[inline.end():], True

        # A trailing "to match" makes the final "with"/"and" clause the accessory.
        if trailing:
            split = self._last_match(WITH_RE, body) or self._last_match(
                re.compile(r'\s+and\s+', re.I), body)
            if split:
                left, right = body[:split.start()], body[split.end():]
                if self.names_product(right):
                    return left, right, True
            return body, '', False

        # Plain "with": an accessory only if the right-hand side names a product and
        # the left-hand side does too, so "a dress with a floral pattern" stays one item.
        split = self._last_match(WITH_RE, body)
        if split:
            left, right = body[:split.start()], body[split.end():]
            if self.names_product(left) and self.names_product(right):
                return left, right, True
        return body, '', False

    @staticmethod
    def _last_match(pattern: re.Pattern[str], text: str) -> re.Match[str] | None:
        found = None
        for match in pattern.finditer(text):
            found = match
        return found

    # -- choice splitting -------------------------------------------------- #

    def _intents(self, text: str, role: str) -> tuple[ItemIntent, ...]:
        text = text.strip()
        if not text:
            return ()
        fragments = [f.strip() for f in CONJUNCTION_RE.split(text) if f.strip()]
        if len(fragments) > 1:
            fragments = self._merge_unanchored(fragments)
        if len(fragments) <= 1:
            budget, remainder = parse_budget(text)
            return (ItemIntent(remainder or text, budget, role),) if (remainder or text) \
                else ()

        intents = []
        for fragment in fragments:
            budget, remainder = parse_budget(fragment)
            if remainder:
                intents.append(ItemIntent(remainder, budget, role))
        return tuple(intents)

    def _merge_unanchored(self, fragments: Sequence[str]) -> list[str]:
        """Fold fragments that name no product into their neighbour.

        "black and white dress" is one item, not a "black" item and a "white dress"
        item; "a dress and a bag" is two.
        """
        merged: list[str] = []
        carry = ''
        for fragment in fragments:
            candidate = f'{carry} {fragment}'.strip() if carry else fragment
            if self.names_product(self._without_budget(fragment)):
                merged.append(candidate)
                carry = ''
            else:
                carry = candidate
        if carry:
            if merged:
                merged[-1] = f'{merged[-1]} {carry}'.strip()
            else:
                merged.append(carry)
        return merged

    @staticmethod
    def _without_budget(text: str) -> str:
        return parse_budget(text)[1]

    # -- budgets ----------------------------------------------------------- #

    @staticmethod
    def _spread_budget(intents: tuple[ItemIntent, ...], per_item: bool
                       ) -> tuple[ItemIntent, ...]:
        """With "each", one stated budget applies to every item."""
        if not per_item or len(intents) < 2:
            return intents
        stated = {i.budget for i in intents if not i.budget.is_empty}
        if len(stated) != 1:
            return intents
        shared = stated.pop()
        return tuple(i if not i.budget.is_empty else replace(i, budget=shared)
                     for i in intents)

    @staticmethod
    def _inherit_budget(accessories: tuple[ItemIntent, ...],
                        primary: tuple[ItemIntent, ...]) -> tuple[ItemIntent, ...]:
        """An accessory with no budget of its own keeps whatever it was given.

        The brief makes budgets per item, so an unqualified global budget applies to
        the accessory too, while an explicit accessory budget wins.
        """
        shared = {i.budget for i in primary if not i.budget.is_empty}
        if len(shared) != 1:
            return accessories
        inherited = shared.pop()
        return tuple(i if not i.budget.is_empty else replace(i, budget=inherited)
                     for i in accessories)
