"""Precompute catalog document vectors offline (phase 7 experiment).

Uses fastembed (ONNX, no torch) with BAAI/bge-small-en-v1.5 and writes float16
vectors plus build metadata, so nothing is downloaded or embedded at serving time.
The document text is built from exactly the fields BM25F indexes, so the comparison
in scripts/spike_dense.py is about the retrieval method and not about the input.

Synthetic review bodies are deliberately excluded: AGENT.md notes the templates are
repetitive and unrelated to most product distinctions.

Run with the isolated experiment interpreter, not the pinned production one:

    .venv-embed/bin/python scripts/build_embeddings.py
"""
from __future__ import annotations

import argparse
import json
import resource
import sys
import time
from pathlib import Path
from typing import Any, Iterator, Sequence

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from retrieval import GROUP_SOURCES  # noqa: E402

MODEL_NAME = 'BAAI/bge-small-en-v1.5'
OUT_DIR = ROOT / 'analysis/embeddings'
#: Field order is fixed so a rebuild produces byte-identical documents.
DOCUMENT_FIELDS = tuple(source for group in ('type', 'cat', 'attr', 'name', 'desc')
                        for source in GROUP_SOURCES[group])


def document_text(product: dict[str, Any]) -> str:
    """One catalog document per product, from the same fields BM25F indexes."""
    parts = [str(product.get(field) or '').strip() for field in DOCUMENT_FIELDS]
    return '. '.join(part for part in parts if part)


def batched(items: Sequence[str], size: int) -> Iterator[Sequence[str]]:
    for start in range(0, len(items), size):
        yield items[start:start + size]


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--catalog', type=Path, default=ROOT / 'data/products.json')
    parser.add_argument('--out', type=Path, default=OUT_DIR)
    parser.add_argument('--model', default=MODEL_NAME)
    parser.add_argument('--batch', type=int, default=256)
    parser.add_argument('--threads', type=int, default=0,
                        help='0 lets onnxruntime choose')
    parser.add_argument('--limit', type=int, default=0,
                        help='embed only the first N products (for a smoke run)')
    args = parser.parse_args(argv)

    try:
        import numpy as np
        from fastembed import TextEmbedding
    except ImportError as exc:  # pragma: no cover - experiment-only dependency
        raise SystemExit(f'{exc}; run this with .venv-embed/bin/python') from exc

    print(f'loading catalog from {args.catalog} ...')
    products = json.loads(args.catalog.read_text(encoding='utf-8'))
    if args.limit:
        products = products[:args.limit]
    products.sort(key=lambda p: p['id'])        # same order as Catalog.products
    ids = [str(p['id']) for p in products]
    documents = [document_text(p) for p in products]
    print(f'{len(documents)} documents, '
          f'mean {sum(len(d) for d in documents) / len(documents):.0f} characters')

    started = time.perf_counter()
    options: dict[str, Any] = {}
    if args.threads:
        options['threads'] = args.threads
    model = TextEmbedding(args.model, **options)
    load_seconds = time.perf_counter() - started
    print(f'model ready in {load_seconds:.1f}s')

    started = time.perf_counter()
    vectors: list[Any] = []
    done = 0
    for batch in batched(documents, args.batch):
        vectors.extend(model.embed(list(batch)))
        done += len(batch)
        if done % (args.batch * 40) == 0:
            rate = done / (time.perf_counter() - started)
            print(f'  {done}/{len(documents)}  {rate:.0f} docs/s')
    embed_seconds = time.perf_counter() - started

    matrix = np.asarray(vectors, dtype='float32')
    # bge vectors come out L2-normalised; normalise again so cosine is a dot product.
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    matrix /= norms
    half = matrix.astype('float16')

    args.out.mkdir(parents=True, exist_ok=True)
    slug = args.model.split('/')[-1]
    vectors_path = args.out / f'{slug}.f16.npy'
    np.save(vectors_path, half)
    meta = {
        'model': args.model,
        'dimension': int(matrix.shape[1]),
        'products': len(ids),
        'dtype': 'float16',
        'normalized': True,
        'similarity': 'cosine (dot product on normalised vectors)',
        'document_fields': list(DOCUMENT_FIELDS),
        'reviews_included': False,
        'model_load_seconds': round(load_seconds, 2),
        'embed_seconds': round(embed_seconds, 1),
        'docs_per_second': round(len(documents) / embed_seconds, 1),
        'megabytes': round(vectors_path.stat().st_size / 1e6, 1),
        'peak_rss_mb': round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 1),
        'built_at': time.strftime('%Y-%m-%dT%H:%M:%S%z'),
    }
    (args.out / f'{slug}.meta.json').write_text(json.dumps(meta, indent=2),
                                                encoding='utf-8')
    (args.out / f'{slug}.ids.json').write_text(json.dumps(ids), encoding='utf-8')

    print(json.dumps(meta, indent=2))
    print(f'vectors -> {vectors_path}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
