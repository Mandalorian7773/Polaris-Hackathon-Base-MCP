"""Fail loudly when data/lexicon.json names a value the catalog does not have.

The lexicon is the only place where shopper language is mapped to products, so a
typo in it silently removes a mapping instead of breaking anything. Run it after
every lexicon edit.

    .venv/bin/python scripts/check_lexicon.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from recommend import Complements  # noqa: E402
from retrieval import BOOST_FIELDS, Lexicon  # noqa: E402

STATS = ROOT / 'analysis/catalog_stats.json'


def vocabularies_from_stats(path: Path = STATS) -> dict[str, set[str]]:
    stats = json.loads(path.read_text(encoding='utf-8'))
    return {field_name: {value.strip().casefold()
                         for value in stats['facets'][field_name]['values']}
            for field_name in BOOST_FIELDS}


def main() -> int:
    if not STATS.exists():
        raise SystemExit(f'{STATS} missing; run scripts/catalog_stats.py first')
    lexicon = Lexicon.load()
    vocabularies = vocabularies_from_stats()
    missing = lexicon.unknown_values(vocabularies)

    entries = len(lexicon.synonyms) + len(lexicon.intents)
    targets = sum(len(values) for source in (lexicon.synonyms, lexicon.intents)
                  for mapping in source.values() for values in mapping.values())
    print(f'lexicon:     {len(lexicon.synonyms)} synonyms, {len(lexicon.intents)} '
          f'intents, {entries} entries, {targets} catalog targets, '
          f'{len(lexicon.stopwords)} stopwords')

    complements = Complements.load()
    pairs = sum(len(v) for v in complements.complements.values())
    missing += [f'complements.json -> {row}' for row in complements.unknown_values(
        vocabularies['product_type'], vocabularies['category'],
        vocabularies['color'], vocabularies['audience'])]
    print(f'complements: {len(complements.complements)} types mapped, {pairs} pairings, '
          f'{len(complements.roles)} role groups, '
          f'{len(complements.no_complement)} types with no complement')

    substitutes = [f'complements[{key}] lists same-role {value!r}'
                   for key, values in complements.complements.items()
                   for value in values
                   if complements.same_role(key, value.strip().casefold())]
    missing += substitutes

    if missing:
        print(f'\n{len(missing)} problem(s):')
        for row in missing:
            print(f'  {row}')
        return 1
    print('every lexicon and complement target exists in the catalog, '
          'and no complement is a substitute')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
