"""Field-aware BM25F retrieval with catalog-grounded expansion and abstention.

Why not plain BM25 over one text blob: catalog names are internal style codes
(``SWEATSHIRT OC``, ``tvp``, ``Mimmi sneaker``), so the shopper's words usually land
on ``product_type`` and the other facets rather than on the name. The index therefore
scores five field groups separately and weights ``product_type`` far above the rest.

A query becomes a list of :class:`Requirement` objects. Each requirement carries the
literal terms to score and the catalog attribute values it resolves to through
``data/lexicon.json``; a product satisfies a requirement by matching either. The
fraction of requirements a product satisfies drives both ranking and abstention, so
an unrelated query has nothing to satisfy and correctly returns nothing.
"""
from __future__ import annotations

import json
import math
import re
from array import array
from collections import defaultdict
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable, Sequence

ROOT = Path(__file__).resolve().parent

#: Field groups, highest signal first. ``attr`` pools the remaining facets.
GROUPS = ('type', 'cat', 'attr', 'name', 'desc')
GROUP_SOURCES: dict[str, tuple[str, ...]] = {
    'type': ('product_type',),
    'cat': ('category',),
    'attr': ('color', 'pattern', 'audience', 'section'),
    'name': ('name',),
    'desc': ('description',),
}
BOOST_FIELDS = ('product_type', 'category', 'color', 'pattern', 'audience', 'section')
#: Only product_type and category anchor a query to a real kind of product.
ANCHOR_FIELDS = ('product_type', 'category')

TOKEN_RE = re.compile(r'\w+')


@dataclass(frozen=True)
class RankingParams:
    """Every knob that affects ranking. Swept on dev cases, reported on held out."""

    k1: float = 1.2
    weights: tuple[float, ...] = (9.0, 3.0, 3.5, 2.0, 1.0)      # per GROUPS
    b: tuple[float, ...] = (0.0, 0.0, 0.2, 0.5, 0.75)            # per GROUPS
    synonym_boost: float = 7.0
    intent_boost: float = 2.5
    typo_penalty: float = 0.6
    #: Terms rarer than this in the catalog are treated as possible misspellings,
    #: because the catalog text contains typos of its own ("sunglases", df 1).
    typo_min_df: int = 8
    #: ...and truncated style names of its own: "dres" appears 19 times, above
    #: typo_min_df, so rarity alone is not enough. Also correct when the candidate is
    #: this many times more common than the term as written ("dress" 7,877 vs 19).
    typo_df_ratio: int = 20
    #: Once a query names a product kind, do not pad the page with products that
    #: merely mention the word -- but only when enough anchored products exist.
    anchor_dominance_min: int = 20
    #: Description postings are skipped for terms more common than this share.
    desc_df_share: float = 0.12
    #: Reward satisfying more of the query. Swept on dev: 1.0 beat 1.5-3.0 on dev
    #: and was neutral on held out.
    coverage_exponent: float = 1.0
    #: Keep products satisfying within this many requirements of the best seen.
    coverage_slack: int = 1
    #: Keep only products scoring at least this share of the best score. 0.05-0.15
    #: were indistinguishable in quality, so take the cheapest.
    score_floor: float = 0.15
    #: A requirement is "unexplained" when it resolves to no catalog attribute and
    #: every one of its terms is this rare in the catalog. Words the catalog genuinely
    #: cannot explain have df 0 ("titanium", "armour", "tyres", "spaceship"), while
    #: ordinary English in a real shopping query does not ("face" 75, "day" 35).
    unexplained_df: int = 0
    #: Abstain below this share of requirements satisfied, when nothing in the query
    #: named a kind of product.
    min_coverage: float = 0.34
    max_results: int = 11000

    def weight(self, group: str) -> float:
        return self.weights[GROUPS.index(group)]

    def length_norm(self, group: str) -> float:
        return self.b[GROUPS.index(group)]


def tokenize(text: str) -> list[str]:
    return TOKEN_RE.findall(text.casefold())


def inflection_variant(term: str) -> str | None:
    """Return one conservative singular/plural form for a catalog term."""
    if term.endswith('ies') and len(term) > 3:
        return f'{term[:-3]}y'
    if term.endswith('sses'):
        return term[:-2]
    if term.endswith('s') and not term.endswith('ss'):
        return term[:-1]
    if term.endswith(('ss', 'sh', 'ch', 'x', 'z')):
        return f'{term}es'
    if len(term) > 1 and term.endswith('y') and term[-2] not in 'aeiou':
        return f'{term[:-1]}ies'
    return f'{term}s' if term else None


# --------------------------------------------------------------------------- #
# Lexicon
# --------------------------------------------------------------------------- #

@dataclass
class Lexicon:
    stopwords: frozenset[str]
    synonyms: dict[str, dict[str, list[str]]]
    intents: dict[str, dict[str, list[str]]]
    extra_vocabulary: tuple[str, ...]

    @classmethod
    def load(cls, path: Path | str = ROOT / 'data/lexicon.json') -> 'Lexicon':
        raw = json.loads(Path(path).read_text(encoding='utf-8'))
        return cls(
            stopwords=frozenset(raw.get('stopwords', [])),
            # Normalize punctuation in keys exactly as query text is tokenized.
            synonyms={' '.join(tokenize(k)): v
                      for k, v in raw.get('synonyms', {}).items()},
            intents={' '.join(tokenize(k)): v
                     for k, v in raw.get('intents', {}).items()},
            extra_vocabulary=tuple(raw.get('typo_extra_vocabulary', [])),
        )

    def unknown_values(self, vocabularies: dict[str, set[str]]) -> list[str]:
        """Lexicon targets that name no catalog value; used by check_lexicon.py."""
        missing = []
        for source in (self.synonyms, self.intents):
            for key, targets in source.items():
                for field_name, values in targets.items():
                    known = vocabularies.get(field_name, set())
                    for value in values:
                        if value.strip().casefold() not in known:
                            missing.append(f'{key} -> {field_name}: {value!r}')
        return missing


# --------------------------------------------------------------------------- #
# Index
# --------------------------------------------------------------------------- #

@dataclass
class Posting:
    docs: array
    freqs: array


class TextIndex:
    """Inverted index with one posting list per (term, field group)."""

    def __init__(self, products: Sequence[dict[str, Any]], lexicon: Lexicon) -> None:
        self.size = len(products)
        self.lexicon = lexicon
        self.postings: dict[str, dict[str, Posting]] = {g: {} for g in GROUPS}
        self.document_frequency: dict[str, int] = {}
        self.average_length: dict[str, float] = {}
        self._build(products)

    def _build(self, products: Sequence[dict[str, Any]]) -> None:
        raw: dict[str, dict[str, list[tuple[int, int]]]] = {g: defaultdict(list)
                                                            for g in GROUPS}
        lengths = {g: 0 for g in GROUPS}
        seen_terms: list[set[str]] = []
        for position, product in enumerate(products):
            terms_here: set[str] = set()
            for group, sources in GROUP_SOURCES.items():
                counts: dict[str, int] = {}
                for source in sources:
                    for term in tokenize(str(product.get(source) or '')):
                        counts[term] = counts.get(term, 0) + 1
                lengths[group] += sum(counts.values())
                bucket = raw[group]
                for term, count in counts.items():
                    bucket[term].append((position, count))
                    terms_here.add(term)
            seen_terms.append(terms_here)

        document_frequency: dict[str, int] = {}
        for terms_here in seen_terms:
            for term in terms_here:
                document_frequency[term] = document_frequency.get(term, 0) + 1
        self.document_frequency = document_frequency

        for group in GROUPS:
            self.average_length[group] = lengths[group] / self.size if self.size else 0.0
            for term, entries in raw[group].items():
                docs = array('i', (position for position, _ in entries))
                freqs = array('f', (float(count) for _, count in entries))
                self.postings[group][term] = Posting(docs, freqs)

        self.lengths_by_group = {
            group: array('i', (sum(len(tokenize(str(p.get(s) or '')))
                                   for s in GROUP_SOURCES[group]) for p in products))
            for group in GROUPS
        }
        self.vocabulary = frozenset(document_frequency)

    def idf(self, term: str) -> float:
        df = self.document_frequency.get(term, 0)
        if df == 0:
            return 0.0
        return math.log(1 + (self.size - df + 0.5) / (df + 0.5))


# --------------------------------------------------------------------------- #
# Query planning
# --------------------------------------------------------------------------- #

@dataclass
class Requirement:
    """One thing the shopper asked for, and every way a product can satisfy it."""

    source: str
    terms: tuple[tuple[str, float], ...] = ()
    #: (field, value, boost, anchors) -- intent targets score but never anchor.
    targets: tuple[tuple[str, str, float], ...] = ()
    is_anchor: bool = False


@dataclass
class PlannedQuery:
    requirements: tuple[Requirement, ...] = ()
    has_anchor: bool = False
    corrections: tuple[tuple[str, str], ...] = ()

    @property
    def is_empty(self) -> bool:
        return not self.requirements


def levenshtein_within(a: str, b: str, limit: int) -> int | None:
    """Edit distance, abandoned as soon as it exceeds ``limit``."""
    if abs(len(a) - len(b)) > limit:
        return None
    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        current = [i]
        best = i
        for j, cb in enumerate(b, 1):
            cost = 0 if ca == cb else 1
            value = min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + cost)
            current.append(value)
            best = min(best, value)
        if best > limit:
            return None
        previous = current
    return previous[-1] if previous[-1] <= limit else None


class QueryPlanner:
    """Turns query text into requirements, using only catalog-grounded vocabulary."""

    def __init__(self, index: TextIndex, vocabularies: dict[str, set[str]],
                 lexicon: Lexicon, params: RankingParams) -> None:
        self.index = index
        self.lexicon = lexicon
        self.params = params
        self.vocabularies = vocabularies
        # Catalog values are matched with stopwords stripped, otherwise a value like
        # "Dog Wear" could never be matched as a phrase because "wear" is a stopword.
        self.value_tokens: dict[str, dict[str, list[str]]] = {
            field_name: {value: self._value_tokens(value) for value in values}
            for field_name, values in vocabularies.items()
        }
        #: Single tokens that unambiguously name one catalog value of a field.
        self.token_to_values: dict[str, list[tuple[str, str]]] = defaultdict(list)
        for field_name, values in self.value_tokens.items():
            for value, tokens in values.items():
                if len(tokens) == 1:
                    aliases = {tokens[0], inflection_variant(tokens[0])}
                    for alias in aliases - {None}:
                        entry = (field_name, value)
                        if entry not in self.token_to_values[alias]:
                            self.token_to_values[alias].append(entry)
        #: Small catalog-grounded vocabulary used for spelling correction.
        #: Synonym/intent keys are deliberately excluded: correcting toward an
        #: expansion such as ``dresses`` made near-domain words such as ``dresser``
        #: retrieve products. Exact aliases still resolve through the lexicon.
        correction_terms = {token for tokens in
                            (t for values in self.value_tokens.values()
                             for t in values.values()) for token in tokens}
        correction_terms |= set(self.lexicon.extra_vocabulary)
        self.correction_vocabulary = tuple(sorted(
            t for t in correction_terms if len(t) >= 4))
        self.known_words = frozenset(self.correction_vocabulary)

    @staticmethod
    def singular(token: str) -> str | None:
        """Conservative de-pluralisation, for matching catalog values only."""
        if len(token) > 4 and token.endswith(('sses', 'shes', 'ches', 'xes', 'zes')):
            return token[:-2]
        if len(token) > 4 and token.endswith('ies'):
            return token[:-3] + 'y'
        if len(token) > 4 and token.endswith('es'):
            return token[:-1]
        if len(token) > 3 and token.endswith('s') and not token.endswith('ss'):
            return token[:-1]
        return None

    def _window_variants(self, window: Sequence[str]) -> list[list[str]]:
        """The window, plus one with its head noun de-pluralised.

        "t-shirts" tokenises to ["t", "shirts"], which does not equal the catalog
        value tokens for "T-shirt" (["t", "shirt"]); without this the query resolved
        to product_type Shirt instead.
        """
        variants = [list(window)]
        singular = self.singular(window[-1])
        if singular:
            variants.append(list(window[:-1]) + [singular])
        return variants

    def _value_tokens(self, value: str) -> list[str]:
        """Tokens of a catalog value, minus stopwords unless that empties it."""
        tokens = tokenize(value)
        kept = [t for t in tokens if t not in self.lexicon.stopwords]
        return kept or tokens

    # -- spelling ---------------------------------------------------------- #

    @lru_cache(maxsize=4096)
    def correct(self, term: str) -> str | None:
        """Nearest reviewed vocabulary term, or None when nothing is close enough."""
        if len(term) < 4 or term in self.known_words:
            return None       # already a reviewed shopping word, so spelled correctly
        frequency = self.index.document_frequency
        own = frequency.get(term, 0)
        limit = 1 if len(term) <= 5 else 2
        best: tuple[int, int, str] | None = None
        for candidate in self.correction_vocabulary:
            if abs(len(term) - len(candidate)) > 1:
                continue
            distance = levenshtein_within(term, candidate, limit)
            if distance is None or distance == 0:
                continue
            # Prefer the smaller edit distance, then the more common term.
            rank = (distance, -frequency.get(candidate, 0), candidate)
            if best is None or rank < best:
                best = rank
        if best is None:
            return None
        if own < self.params.typo_min_df:
            return best[2]
        # The term exists in the catalog, but a near neighbour dominates it so
        # heavily that the term is almost certainly a misspelling or a truncation.
        if frequency.get(best[2], 0) >= self.params.typo_df_ratio * max(own, 1):
            return best[2]
        return None

    # -- planning ---------------------------------------------------------- #

    def plan(self, text: str) -> PlannedQuery:
        tokens = [t for t in tokenize(text) if t not in self.lexicon.stopwords]
        if not tokens:
            return PlannedQuery()

        consumed = [False] * len(tokens)
        requirements: list[Requirement] = []
        corrections: list[tuple[str, str]] = []

        # Multi-token catalog values and lexicon phrases first, longest match wins.
        for width in (4, 3, 2):
            for start in range(len(tokens) - width + 1):
                if any(consumed[start:start + width]):
                    continue
                window = tokens[start:start + width]
                phrase = ' '.join(window)
                targets, anchored = self._phrase_targets(window, phrase)
                if targets:
                    requirements.append(Requirement(
                        source=phrase,
                        terms=tuple((t, 1.0) for t in window),
                        targets=tuple(targets),
                        is_anchor=anchored))
                    for offset in range(width):
                        consumed[start + offset] = True

        for position, token in enumerate(tokens):
            if consumed[position]:
                continue
            requirements.append(self._single_token_requirement(token, corrections))

        return PlannedQuery(
            requirements=tuple(requirements),
            has_anchor=any(r.is_anchor for r in requirements),
            corrections=tuple(corrections),
        )

    def _phrase_targets(self, window: Sequence[str], phrase: str
                        ) -> tuple[list[tuple[str, str, float]], bool]:
        """Returns the scoring targets and whether any of them anchors the query."""
        targets: list[tuple[str, str, float]] = []
        anchored = False
        variants = self._window_variants(window)
        forms = {' '.join(variant) for variant in variants}
        variant = inflection_variant(window[-1]) if window else None
        if variant:
            forms.add(' '.join((*window[:-1], variant)))
        for field_name, values in self.value_tokens.items():
            for value, tokens in values.items():
                if tokens in variants:
                    targets.append((field_name, value, self.params.synonym_boost))
                    anchored = anchored or field_name in ANCHOR_FIELDS
        for form in forms:
            mapping = self.lexicon.synonyms.get(form)
            if mapping:
                found = self._mapping_targets(
                    mapping, self.params.synonym_boost)
                targets.extend(found)
                anchored = anchored or any(
                    f in ANCHOR_FIELDS for f, _, _ in found)
            intent = self.lexicon.intents.get(form)
            if intent:
                targets.extend(self._mapping_targets(
                    intent, self.params.intent_boost, ordered=True))
        return targets, anchored

    def _single_token_requirement(self, token: str,
                                  corrections: list[tuple[str, str]]) -> Requirement:
        terms: list[tuple[str, float]] = [(token, 1.0)]
        targets: list[tuple[str, str, float]] = []
        anchored = False

        for field_name, value in self.token_to_values.get(token, ()):
            targets.append((field_name, value, self.params.synonym_boost))
            anchored = anchored or field_name in ANCHOR_FIELDS
        mapping = self.lexicon.synonyms.get(token)
        if mapping:
            found = self._mapping_targets(mapping, self.params.synonym_boost)
            targets.extend(found)
            anchored = anchored or any(f in ANCHOR_FIELDS for f, _, _ in found)
        intent = self.lexicon.intents.get(token)
        if intent:
            targets.extend(self._mapping_targets(
                intent, self.params.intent_boost, ordered=True))

        # correct() decides for itself whether the term looks misspelled; gating the
        # call on rarity here is what hid "dres" (df 19) from correction.
        if not targets:
            replacement = self.correct(token)
            if replacement:
                corrections.append((token, replacement))
                terms.append((replacement, self.params.typo_penalty))
                # A corrected term scores and counts as catalog evidence, but it
                # must not anchor: an anchor lets a query skip the abstention rules,
                # and "spaceship engine parts" should not qualify just because
                # "parts" is one edit away from "pants".
                penalty = self.params.typo_penalty
                for field_name, value in self.token_to_values.get(replacement, ()):
                    targets.append((field_name, value,
                                    self.params.synonym_boost * penalty))
                mapped = self.lexicon.synonyms.get(replacement)
                if mapped:
                    targets.extend(self._mapping_targets(
                        mapped, self.params.synonym_boost * penalty))
                mapped_intent = self.lexicon.intents.get(replacement)
                if mapped_intent:
                    targets.extend(self._mapping_targets(
                        mapped_intent, self.params.intent_boost * penalty,
                        ordered=True))

        return Requirement(source=token, terms=tuple(terms), targets=tuple(targets),
                           is_anchor=anchored)

    def _mapping_targets(self, mapping: dict[str, list[str]], boost: float,
                         ordered: bool = False
                         ) -> list[tuple[str, str, float]]:
        targets = []
        rank = 0
        for field_name, values in mapping.items():
            if field_name not in BOOST_FIELDS:
                continue
            known = self.vocabularies.get(field_name, set())
            for value in values:
                folded = value.strip().casefold()
                if folded in known:
                    target_boost = boost * (1 - min(rank, 100) * 0.001) \
                        if ordered else boost
                    targets.append((field_name, value, target_boost))
                    rank += 1
        return targets


# --------------------------------------------------------------------------- #
# Scoring
# --------------------------------------------------------------------------- #

class Retriever:
    """Scores the catalog for a planned query and decides when to abstain."""

    def __init__(self, index: TextIndex, facet_postings: dict[str, dict[str, Any]],
                 planner: QueryPlanner, params: RankingParams) -> None:
        self.index = index
        self.facet_postings = facet_postings
        self.planner = planner
        self.params = params
        self._desc_df_cap = max(1, int(params.desc_df_share * index.size))

    def score(self, text: str) -> dict[int, float]:
        """Position -> relevance for the selected products; others are absent."""
        plan = self.planner.plan(text)
        if plan.is_empty:
            return {}
        return self.score_plan(plan)

    def is_explained(self, requirement: Requirement) -> bool:
        """Does the catalog hold any evidence at all about this requirement?"""
        if requirement.targets:
            return True
        frequency = self.index.document_frequency
        return any(frequency.get(term, 0) > self.params.unexplained_df
                   for term, _ in requirement.terms)

    def score_plan(self, plan: PlannedQuery) -> dict[int, float]:
        size = self.index.size
        totals = [0.0] * size
        satisfied = [0] * size
        grounded = [0] * size
        anchored = [0] * size
        owner = [-1] * size
        anchor_owner = [-1] * size
        touched: list[int] = []

        for number, requirement in enumerate(plan.requirements):
            counters = (totals, satisfied, grounded, anchored, owner, anchor_owner,
                        touched)
            grounds = bool(requirement.targets)
            for term, term_weight in requirement.terms:
                self._accumulate_term(term, term_weight, number, grounds, counters)
            for field_name, value, boost in requirement.targets:
                self._accumulate_target(field_name, value, boost, number, counters)

        if not touched:
            return {}

        required = len(plan.requirements)
        best_satisfied = max(satisfied[p] for p in touched)

        if not plan.has_anchor:
            # No named product kind, so absolute coverage has to carry the query.
            if best_satisfied / required < self.params.min_coverage:
                return {}
            # Nothing named a kind of product, so the query only earns an answer when
            # the catalog can account for all of it and some of it resolves to a real
            # attribute. "rainy day essentials" passes; "titanium armour" does not.
            if any(not self.is_explained(r) for r in plan.requirements):
                return {}
            # Either some word resolved to a real catalog attribute, or every word in
            # the query was matched. "cotton top" names no attribute value but is
            # fully accounted for; "second hand" is neither.
            if max(grounded[p] for p in touched) < 1 and best_satisfied < required:
                return {}
        # With an anchor the named product type is the evidence, so absolute coverage
        # is not required: "a dress for my trip to Reykjavik" is still a dress query.

        # Anchor dominance: once the query names a kind of product and the catalog
        # holds enough of it, drop products that only mention the word. Without this,
        # "dress" admits 267 tops and blouses whose names contain "dress", and a
        # price-ascending page surfaces those first.
        best_anchored = max(anchored[p] for p in touched)
        anchor_cut = 0
        if best_anchored > 0:
            qualifying = sum(1 for p in touched if anchored[p] >= best_anchored)
            if qualifying >= self.params.anchor_dominance_min:
                anchor_cut = best_anchored

        coverage_cut = max(1, best_satisfied - self.params.coverage_slack)
        scored: dict[int, float] = {}
        best_score = 0.0
        for position in touched:
            if satisfied[position] < coverage_cut or anchored[position] < anchor_cut:
                continue
            value = totals[position] * (satisfied[position] / required
                                        ) ** self.params.coverage_exponent
            if value <= 0.0:
                continue
            scored[position] = value
            best_score = max(best_score, value)

        floor = best_score * self.params.score_floor
        selected = {p: v for p, v in scored.items() if v >= floor}
        if len(selected) > self.params.max_results:
            ranked = sorted(selected.items(), key=lambda kv: (-kv[1], kv[0]))
            selected = dict(ranked[:self.params.max_results])
        return selected

    def _accumulate_term(self, term: str, term_weight: float, number: int,
                         grounds: bool, counters: tuple) -> None:
        totals, satisfied, grounded, _anchored, owner, _anchor_owner, touched = counters
        idf = self.index.idf(term)
        if idf <= 0.0:
            return
        document_frequency = self.index.document_frequency.get(term, 0)
        for group in GROUPS:
            posting = self.index.postings[group].get(term)
            if posting is None:
                continue
            if group == 'desc' and document_frequency > self._desc_df_cap:
                continue       # near-stopword in this catalog; other fields carry it
            weight = self.params.weight(group) * term_weight
            b = self.params.length_norm(group)
            average = self.index.average_length[group] or 1.0
            lengths = self.index.lengths_by_group[group]
            docs, freqs = posting.docs, posting.freqs
            for offset in range(len(docs)):
                position = docs[offset]
                norm = 1.0 - b + b * (lengths[position] / average) if b else 1.0
                weighted = weight * freqs[offset] / norm
                contribution = idf * weighted / (self.params.k1 + weighted)
                if owner[position] != number:
                    if totals[position] == 0.0 and satisfied[position] == 0:
                        touched.append(position)
                    owner[position] = number
                    satisfied[position] += 1
                    if grounds:
                        grounded[position] += 1
                totals[position] += contribution

    def _accumulate_target(self, field_name: str, value: str, boost: float, number: int,
                           counters: tuple) -> None:
        (totals, satisfied, grounded, anchored, owner, anchor_owner,
         touched) = counters
        bucket = self.facet_postings.get(field_name, {}).get(value.strip().casefold())
        if not bucket:
            return
        anchors = field_name in ANCHOR_FIELDS
        for position in bucket:
            if owner[position] != number:
                if totals[position] == 0.0 and satisfied[position] == 0:
                    touched.append(position)
                owner[position] = number
                satisfied[position] += 1
                grounded[position] += 1
            if anchors and anchor_owner[position] != number:
                anchor_owner[position] = number
                anchored[position] += 1
            totals[position] += boost
