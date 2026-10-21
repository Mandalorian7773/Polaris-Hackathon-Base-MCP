# Polaris Search Research and Experiment Report

Date: 21 October 2026.

This report covers local research and experiments only. None of the metrics is
an organizer score, no official attempt was spent, and nothing was deployed.

## 1. Research findings

The detailed source record is in
[`research/SEARCH_RESEARCH.md`](SEARCH_RESEARCH.md). The most consequential
findings were:

- [SQLite FTS5](https://www.sqlite.org/fts5.html) provides weighted BM25 with
  term-frequency saturation and document-length normalization, but candidate
  correctness and measured held-out gains must justify replacing a simpler
  in-memory index.
- [OpenSearch RRF](https://docs.opensearch.org/latest/vector-search/ai-search/hybrid-search/rrf/)
  is a robust starting point when lane scores are incomparable, while its
  [normalization processor](https://docs.opensearch.org/latest/search-plugins/search-pipelines/normalization-processor/)
  preserves score margins. Neither method guarantees an improvement.
- [Qdrant's 2026 tuning study](https://qdrant.tech/articles/how-to-tune-hybrid-search/)
  recommends proving that fusion beats each lane, comparing RRF with
  distribution-based fusion, tuning weights last, and validating held-out. Its
  own five-dataset experiment had one case where fusion lost.
- [Sentence Transformers retrieve-and-rerank guidance](https://www.sbert.net/examples/sentence_transformer/applications/retrieve_rerank/README.html)
  supports broad first-stage retrieval followed by a CrossEncoder over a small
  candidate set. A reranker cannot recover missing candidates.
- [GetYourGuide's 2024 report](https://getyourguide.careers/posts/from-text-to-context-how-we-introduced-a-modern-hybrid-search-at-getyourguide)
  found that hybrid retrieval hurt exact queries and therefore segmented query
  classes instead of applying semantic retrieval universally.
- The [Qdrant RRF issue](https://github.com/qdrant/qdrant/issues/4937) confirms
  that fused rank scores are not confidence scores. Thresholding belongs in
  each source lane or in a separately calibrated acceptance model.
- Product-search and IR evaluation sources support nDCG plus candidate recall,
  pooled judgments, query-family split isolation, and independent manual
  labels. The local labels remain fact rules, not independent human judgments.
- The normative MCP reference remains
  [revision 2025-11-25](https://modelcontextprotocol.io/specification/2025-11-25/server/tools).
  No protocol migration was attempted.

X was inaccessible without authentication, so no X claims were included.

## 2. Existing implementation and audit

Before this pass, `search.py` already contained:

- Field-separated in-memory postings over name, taxonomy, attributes, and
  description with weighted IDF scoring.
- Facet indexes and precomputed deterministic field-sort orders.
- Exact explicit-filter semantics and final hard-constraint rechecks.
- Phrase/name/taxonomy boosts and minimum query-term coverage.
- A deterministic parser for taxonomy, colors, patterns, audiences, prices,
  mixed choices, complementary clauses, and a small soft-intent map.
- Round-robin mixed-choice merging.
- A separate recommendation path with accessory constraints, audience family,
  color compatibility, budgets, uniqueness, and current-page references.

Confirmed weaknesses:

1. Typo correction used `difflib` over the entire 15k-term corpus vocabulary.
   It changed `snikers` to `snickers` and `blaser` to `laser`, producing
   unrelated products.
2. Taxonomy aliases used naive plural construction. Singular/plural catalog
   values such as `Costumes`/`costume` and `Dress`/`dresses` were missed.
3. Alias collisions selected only one real catalog product type. For example,
   `earrings` can legitimately refer to both `Earring` and `Earrings`.
4. Descriptive intent handling was narrow and visibly coupled to the original
   local query set.
5. The 56-case "heldout" set is not sealed: it is every fifth visible case,
   shares query families with development, uses broad fact predicates, and has
   phrases mirrored by parser rules. Its 0.910 aggregate is a regression metric,
   not independent evidence of hidden-benchmark quality.
6. The initial evaluation did not separate candidate success/recall from final
   ranking.

## 3. Controlled experiments

An additional fixed query-family suite was created in
`evaluation/run_research_experiments.py`: 20 development and 19 held-out cases
covering unseen typo forms, taxonomy morphology, descriptive tasks, and
near-domain negatives. It uses exhaustive catalog-fact rules. These labels are
more independent of result IDs than the original suite but are still authored
locally and are not human semantic judgments.

### Configuration results

| Configuration | Development score | Held-out score | Held-out success@10 | Held-out Recall@100 | Held-out false-positive rate | Held-out p95 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Pre-promotion implementation | 0.300 | 0.387 | 0.263 | 0.018 | 0.250 | 25.6 ms |
| Taxonomy morphology + conservative correction | 0.518 | 0.526 | 0.368 | 0.090 | 0.250 | 36.5 ms |
| Development-only broad intent expansion | 0.918 | 0.632 | 0.474 | 0.091 | 0.250 | 21.6 ms |
| Promoted conservative configuration | 0.618 | 0.632 | 0.474 | 0.091 | 0.250 | 34.2 ms |
| FTS5 ranking over promoted candidates | 0.618 | 0.632 | 0.474 | 0.091 | 0.250 | 39.4 ms |

Recall is standard recall against all products matching broad fact rules, so its
denominator can contain thousands of interchangeable products. Success@10 is
more informative for these shopping queries. The 25% held-out false-positive
rate is one of four negatives: `trouser press machine` matches catalog clothing
language. It existed before promotion and remains unresolved.

### Dense and fusion ablation

The optional dense experiment used `minishlab/potion-base-8M` over all 105,126
products:

- 256 dimensions and 102.7 MiB of float32 vectors.
- Approximately 5.15 seconds to load/encode in the warm cached experiment.
- End-to-end experimental query p50/p95 of 9.7/37.6 ms, excluding production
  packaging and server transport.

After promoting lexical normalization:

| Dense configuration | Main standard nDCG@10 | Research-development nDCG@10 |
| --- | ---: | ---: |
| Promoted lexical | 0.972 | 0.618 |
| Dense only | 0.895 | 0.446 |
| RRF, constant 60 | 0.917 | 0.451 |
| Min-max fusion, 70% lexical | 0.970 | 0.454 |
| Min-max fusion, 70% dense | 0.895 | 0.446 |

Dense top-score ranges overlapped substantially:

- Meaningful task queries: 0.386–0.642.
- Irrelevant queries: 0.347–0.551.

No universal threshold could accept the useful cases while rejecting the
irrelevant cases. This agrees with the external evidence against generic dense
fallback thresholds.

## 4. Promoted and rejected changes

Promoted:

1. Catalog-derived singular/plural taxonomy aliases.
2. Alias merging when multiple real product types share the same query form.
3. Typo correction restricted to canonical taxonomy/color/pattern terms, with
   edit distance at most two and length difference at most one. Exact catalog
   vocabulary always wins.
4. `cold`/`warm` outerwear and `interview` outfit cues, the only new intent
   families that improved the held-out query-family set.
5. Four focused regression tests for typo correction, morphology, unrelated
   near spellings, and generalized intents.

Rejected:

- FTS5 ranking: no development or held-out quality gain over identical
  candidates and higher startup/latency.
- Dense-only retrieval and RRF: both regressed the promoted lexical ranking.
- Normalized dense fusion: no post-promotion gain and a deployable model/vector
  dependency would be required.
- Cross-encoder reranking: not attempted after neither dense nor fused candidate
  ranking beat lexical retrieval; adding a heavier second stage lacked evidence.
- A universal dense abstention threshold: useful and irrelevant score ranges
  overlapped.
- Five development-only intent expansions: they added no held-out gain beyond
  the two promoted intent families.

## 5. Final measurements

Original 56-case local suite:

- Ranking: 0.940 before this pass, 0.964 after.
- Mixed choices: 1.000, unchanged.
- Unusual queries: 0.942, unchanged.
- Irrelevant correct-empty rate: 1.000, unchanged.
- Recommendations: 0.978, unchanged.
- Contract and invalid calls: 1.000, unchanged.
- Visible held-out aggregate: 0.910, unchanged.
- Startup: approximately 2.88 seconds in the final run.
- Peak RSS: approximately 702 MiB in the final run.
- Query p50/p95 within the 56-case process: 4.7/15.8 ms.

Performance pool:

- 300/300 exact responses.
- p50 0.256 ms.
- p95 42.666 ms.
- maximum 109.671 ms.

Tests:

- 20/20 unit tests pass.
- Public MCP smoke test passes.
- Extended MCP protocol/constraint/pagination/recommendation test passes.
- Twenty concurrent protocol calls completed in 0.345 seconds.
- Catalog SHA-256 remains
  `05b6aa5e8e8c1c8c997d584f8288e432862454f8820531e13473443e83c8f073`.

## 6. Risks and remaining uncertainty

- No local score predicts the organizer's Bedrock judgments.
- The original held-out split is contaminated by visible templates and parser
  rules. The research split is fixed and query-family based, but still
  locally authored and fact-rule graded.
- Broad fact-rule Recall@K treats many interchangeable products as relevant and
  therefore produces low absolute recall values.
- `trouser press machine` remains a near-domain false positive. A generic
  coverage or dense-score threshold could reject valid descriptive queries, so
  no unsupported threshold was added.
- The soft-intent map remains manually maintained and English-only.
- Docker was not rebuilt in this pass; the production dependency set did not
  change. A prior attempt was blocked by an unavailable local Docker daemon.
- The optional dense utility relies on packages present only in the experimental
  virtual environment; those packages are intentionally absent from
  `requirements.txt`.

Recommended next step: have a second person author and seal at least 30 new
query families with pooled, blinded 0–3 product-ID judgments from the
pre-promotion, promoted, FTS5, and dense candidate unions. Re-evaluate candidate
Success/Recall@K and nDCG before adding more intent rules or any model.

## 7. Reproduction commands

```sh
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python evaluation/run_benchmark.py --compare
.venv/bin/python evaluation/run_performance.py

.venv/bin/python evaluation/run_research_experiments.py \
  --split development \
  --output evaluation/research_development_results.json
.venv/bin/python evaluation/run_research_experiments.py \
  --split heldout \
  --output evaluation/research_heldout_results.json

# Optional rejected dense trial; requires fastembed and numpy only in the
# experimental virtual environment.
.venv/bin/python evaluation/run_dense_experiment.py

.venv/bin/python server.py
.venv/bin/python smoke_test.py http://127.0.0.1:8000/mcp
.venv/bin/python protocol_acceptance_test.py http://127.0.0.1:8000/mcp
shasum -a 256 data/products.json
```
