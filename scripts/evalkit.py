"""Shared evaluation vocabulary: selectors, rubric grading, and nDCG.

A *selector* is a small JSON object of catalog predicates that AND together; a list
value is an OR within its key. Selectors describe what a case asks for, so grading
stays catalog-grounded and auditable instead of hand-waved.

Supported keys:

    category / product_type / color / audience / section / pattern
        exact, case-insensitive, trimmed; list = any of
    not_<facet>                 none of those values
    <field>_contains            substring in name / description / text / any facet
    min_price / max_price / min_rating / max_rating / min_review_count
    any_of                      list of selectors; true when any matches

A *rubric* is an ordered list of ``{"grade": g, "match": selector}`` tiers. A product
takes the grade of the first tier it matches, 0 if none. A product violating the
case's hard ``constraints`` is forced to 0, mirroring the brief's rule that
hard-constraint violators earn no gain.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

ROOT = Path(__file__).resolve().parent.parent
CASES_PATH = ROOT / 'eval/cases.jsonl'
JUDGMENTS_PATH = ROOT / 'eval/judgments.jsonl'

FACETS = ('category', 'product_type', 'color', 'audience', 'section', 'pattern')
TEXT_SOURCES = ('name', 'description')

#: Case categories and which scored area each one feeds.
AREA_OF_CATEGORY = {
    'exact': 'ranking',
    'synonym': 'ranking',
    'typo': 'ranking',
    'vague': 'unusual',
    'irrelevant': 'irrelevant',
    'mixed': 'mixed',
    'complement': 'complement',
    'filter_sort': 'validity',
    'pagination': 'validity',
    'invalid': 'validity',
}

#: Official weights from hackathon-brief.md.
AREA_POINTS = {'validity': 25, 'ranking': 30, 'unusual': 7.5, 'irrelevant': 7.5,
               'mixed': 15, 'complement': 10, 'performance': 5}


def norm(value: Any) -> str:
    return str(value or '').strip().casefold()


def _price(product: dict[str, Any]) -> float:
    amount = (product.get('price') or {}).get('amount')
    return float(amount) if isinstance(amount, (int, float)) else 0.0


def _rating(product: dict[str, Any]) -> float | None:
    average = (product.get('rating') or {}).get('average')
    return float(average) if isinstance(average, (int, float)) else None


def _reviews(product: dict[str, Any]) -> int:
    count = (product.get('rating') or {}).get('count')
    return int(count) if isinstance(count, (int, float)) else 0


def _haystack(product: dict[str, Any], field_name: str) -> str:
    if field_name == 'text':
        return norm(' '.join(str(product.get(k) or '') for k in TEXT_SOURCES))
    return norm(product.get(field_name))


def selector_matches(product: dict[str, Any], selector: dict[str, Any]) -> bool:
    """True when the product satisfies every key of the selector."""
    for key, want in selector.items():
        if key == 'any_of':
            if not any(selector_matches(product, alternative) for alternative in want):
                return False
        elif key in FACETS:
            if norm(product.get(key)) not in {norm(v) for v in _listed(want)}:
                return False
        elif key.startswith('not_') and key[4:] in FACETS:
            if norm(product.get(key[4:])) in {norm(v) for v in _listed(want)}:
                return False
        elif key.endswith('_contains'):
            haystack = _haystack(product, key[:-len('_contains')])
            if not any(norm(v) in haystack for v in _listed(want)):
                return False
        elif key == 'min_price':
            if _price(product) < want:
                return False
        elif key == 'max_price':
            if _price(product) > want:
                return False
        elif key == 'min_rating':
            average = _rating(product)
            if average is None or average < want:
                return False
        elif key == 'max_rating':
            average = _rating(product)
            if average is None or average > want:
                return False
        elif key == 'min_review_count':
            if _reviews(product) < want:
                return False
        else:
            raise ValueError(f'unknown selector key: {key}')
    return True


def _listed(value: Any) -> list[Any]:
    return value if isinstance(value, list) else [value]


def rubric_grade(product: dict[str, Any], rubric: Sequence[dict[str, Any]]) -> int:
    for tier in rubric:
        if selector_matches(product, tier['match']):
            return int(tier['grade'])
    return 0


# --------------------------------------------------------------------------- #
# Cases and judgments
# --------------------------------------------------------------------------- #

@dataclass
class Case:
    id: str
    category: str
    split: str
    arguments: dict[str, Any]
    notes: str = ''
    expect_error: bool = False
    expect_empty: bool = False
    constraints: dict[str, Any] = field(default_factory=dict)
    grades: list[dict[str, Any]] = field(default_factory=list)
    choices: list[dict[str, Any]] = field(default_factory=list)
    complement: dict[str, Any] = field(default_factory=dict)
    exact_page: bool = False
    pagination_probe: bool = False

    @property
    def area(self) -> str:
        return AREA_OF_CATEGORY[self.category]

    @property
    def is_relevance_case(self) -> bool:
        return bool(self.grades)


def load_cases(path: Path = CASES_PATH) -> list[Case]:
    cases = []
    for line in path.read_text(encoding='utf-8').splitlines():
        line = line.strip()
        if line and not line.startswith('//'):
            cases.append(Case(**json.loads(line)))
        if len({c.id for c in cases}) != len(cases):
            raise ValueError(f'duplicate case id near: {line[:60]}')
    return cases


def load_judgments(path: Path = JUDGMENTS_PATH) -> dict[tuple[str, str], int]:
    """Human grades keyed by (case id, product id); later lines win."""
    if not path.exists():
        return {}
    judgments: dict[tuple[str, str], int] = {}
    for line in path.read_text(encoding='utf-8').splitlines():
        line = line.strip()
        if line and not line.startswith('//'):
            row = json.loads(line)
            judgments[(row['case'], row['id'])] = int(row['grade'])
    return judgments


def append_judgments(rows: Iterable[dict[str, Any]],
                     path: Path = JUDGMENTS_PATH) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    with path.open('a', encoding='utf-8') as handle:
        for row in rows:
            handle.write(json.dumps(row, sort_keys=True) + '\n')
            written += 1
    return written


class Grader:
    """Grades a product for a case: a human judgment if present, else the rubric."""

    def __init__(self, products_by_id: dict[str, dict[str, Any]],
                 judgments: dict[tuple[str, str], int] | None = None) -> None:
        self.products = products_by_id
        self.judgments = judgments or {}

    def product(self, product_id: str) -> dict[str, Any] | None:
        return self.products.get(product_id)

    def obeys_constraints(self, product_id: str, constraints: dict[str, Any]) -> bool:
        product = self.products.get(product_id)
        return product is not None and selector_matches(product, constraints)

    def grade(self, case_id: str, product_id: str, rubric: Sequence[dict[str, Any]],
              constraints: dict[str, Any]) -> tuple[int, str]:
        product = self.products.get(product_id)
        if product is None:
            return 0, 'unknown-id'
        if constraints and not selector_matches(product, constraints):
            return 0, 'constraint-violation'
        human = self.judgments.get((case_id, product_id))
        if human is not None:
            return human, 'human'
        return rubric_grade(product, rubric), 'rubric'


# --------------------------------------------------------------------------- #
# Metrics
# --------------------------------------------------------------------------- #

def dcg(grades: Sequence[int]) -> float:
    return sum((2 ** g - 1) / math.log2(rank + 1) for rank, g in enumerate(grades, 1))


def ideal_dcg(slots: int, best_grade: int = 3) -> float:
    """The brief's fixed ceiling: `slots` reference slots all at grade 3."""
    return dcg([best_grade] * slots)


def ndcg(grades: Sequence[int], slots: int) -> float:
    ceiling = ideal_dcg(slots)
    return dcg(list(grades)[:slots]) / ceiling if ceiling else 0.0


def percentile(values: Sequence[float], pct: float) -> float:
    """Nearest-rank percentile, as the brief specifies."""
    if not values:
        return math.nan
    ordered = sorted(values)
    rank = max(1, math.ceil(pct / 100 * len(ordered)))
    return float(ordered[rank - 1])


def mean(values: Iterable[float]) -> float:
    items = list(values)
    return sum(items) / len(items) if items else 0.0
