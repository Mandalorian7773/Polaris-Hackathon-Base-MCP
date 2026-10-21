# Polaris Product Search: Retrieval Research

Research date: 21 October 2026.

Scope: product retrieval, candidate generation, ranking, fusion, reranking,
abstention, relevance evaluation, and the MCP tool contract. Sources were
live-checked where access permitted. Living documentation without a displayed
publication date is marked "date not stated." X search required authentication
and direct fetching returned an access error, so no X posts are cited.

This document records hypotheses, not implementation decisions. Published
results do not establish expected gains on the frozen Polaris catalog.

## Main conclusions

1. Measure candidate Recall@20/50/100 before changing final ranking. A reranker
   cannot recover a relevant product that no first-stage retriever found.
2. Preserve exact names, taxonomy phrases, explicit filters, and filter-only
   enumeration outside broad semantic fallback. Production reports repeatedly
   find that dense or hybrid retrieval can hurt exact queries.
3. Weighted lexical retrieval remains a strong, inexpensive baseline. SQLite
   FTS5 provides BM25 term-frequency saturation and document-length
   normalization, but its fixed `k1` and `b` and SQL integration must beat the
   current custom index on held-out data to justify replacement.
4. Hybrid retrieval is not inherently better. Compare each lane independently,
   the candidate union, RRF, and normalized score fusion at identical depths.
5. Treat fused rank scores as ranking signals, not confidence. Calibrate
   abstention on original-lane or reranker scores and report false acceptance
   and false rejection separately.
6. Cross-encoder reranking is only worth testing after candidate recall is
   known. Candidate depth can improve recall while worsening final precision
   and latency.
7. The current visible "heldout" split cannot be considered sealed. It is useful
   for regression testing, but not an independent estimate of hidden-benchmark
   performance.

## Official retrieval documentation

### SQLite FTS5 weighted BM25

- Source: [SQLite FTS5 Extension](https://www.sqlite.org/fts5.html)
- Date/type: date not stated; official SQLite reference documentation.
- Finding: `bm25()` uses fixed `k1=1.2` and `b=0.75`, returns better matches as
  numerically smaller values, and accepts per-column weights. `ORDER BY rank`
  can be more efficient than evaluating and sorting `bm25()` directly when a
  limit is used.
- Transfer: name, taxonomy, attributes, and description map naturally to
  separate FTS columns for 105,126 products.
- Expected benefit: term-frequency saturation and length normalization absent
  from a binary term-presence scorer.
- Cost/risks: an FTS table or persisted artifact, SQL query construction,
  reversed score direction, fixed BM25 constants, and care around complete
  candidate semantics for field sorting.
- Local experiment: build a temporary four-column FTS5 index; compare candidate
  Recall@20/50/100, nDCG@10, startup, RSS, and p95 with the current index using
  identical query plans and hard filters.

### Elasticsearch field weighting

- Source: [Multi-match query](https://www.elastic.co/docs/reference/query-languages/query-dsl/query-dsl-multi-match-query)
- Date/type: date not stated; official Elasticsearch documentation.
- Finding: field boosts use caret syntax; `best_fields` uses `dis_max`,
  `tie_breaker` adds a fraction of additional field matches, and `most_fields`
  sums evidence across analyzed field copies.
- Transfer: directly relevant to emphasizing product name and taxonomy over
  repetitive descriptions.
- Expected benefit: stronger exact-field precision and controllable evidence
  accumulation.
- Cost/risks: an external search service would add deployment complexity;
  missing fields, field lengths, and analyzers make boosts non-comparable.
- Local experiment: reproduce `best_fields` and `most_fields` behavior in the
  in-memory scorer before considering a service; tune on development and inspect
  held-out per-query deltas.

### Elasticsearch search-time synonyms

- Source: [Synonym graph token filter](https://www.elastic.co/docs/reference/text-analysis/analysis-synonym-graph-tokenfilter)
- Date/type: date not stated; official Elasticsearch documentation.
- Finding: `synonym_graph` represents multi-token synonyms correctly and is
  intended for search-time analysis. Explicit one-way rules avoid forcing every
  term into an equivalence class.
- Transfer: useful for `trainers -> sneakers`, `tee -> t-shirt`, and catalog
  color aliases.
- Expected benefit: conservative recall gains without rebuilding document text.
- Cost/risks: reciprocal or generated synonym lists cause query drift; analyzer
  order matters; invalid managed synonym sets can make an index unusable.
- Local experiment: compare a small reviewed one-way map against no expansion.
  Require exact phrase/token matches to outrank synonym and typo matches.

### Elasticsearch spelling suggestions

- Source: [Search suggesters](https://www.elastic.co/docs/reference/elasticsearch/rest-apis/search-suggesters)
- Date/type: date not stated; official Elasticsearch documentation.
- Finding: phrase suggesters score whole corrections with n-gram context;
  candidate generators and `collate` can reject suggestions that match nothing.
- Transfer: applicable to misspelled product types and names.
- Expected benefit: safer contextual correction than isolated edit distance.
- Cost/risks: an extra shingle field and query work; valid unusual names, IDs,
  and model numbers are easy to over-correct.
- Local experiment: generate realistic deletion/transposition/substitution
  typos, include valid near-neighbor catalog names as negatives, and measure
  correction precision plus resulting nDCG.

### Elasticsearch rank evaluation

- Source: [Ranking evaluation API](https://www.elastic.co/docs/reference/elasticsearch/rest-apis/search-rank-eval)
- Date/type: date not stated; official Elasticsearch documentation.
- Finding: accepts representative requests and integer relevance ratings, with
  precision, recall, MRR, DCG/nDCG, ERR, templates, and per-query details.
- Transfer: the Polaris local judgments already resemble this structure.
- Expected benefit: repeatable, per-query configuration comparisons.
- Cost/risks: the API does not create unbiased labels; incomplete judgments and
  metric choice can reverse conclusions.
- Local experiment: add standard qrel-based nDCG and candidate recall alongside
  the competition's fixed-ceiling score.

### OpenSearch score normalization

- Source: [Normalization processor](https://docs.opensearch.org/latest/search-plugins/search-pipelines/normalization-processor/)
- Date/type: date not stated; official OpenSearch documentation, feature marked
  introduced in 2.10.
- Finding: supports min-max, L2, and z-score normalization followed by
  arithmetic, geometric, or harmonic score combination and optional weights.
- Transfer: relevant only if Polaris has genuinely distinct lexical and dense
  score lanes.
- Expected benefit: retains meaningful score margins that rank-only fusion
  discards.
- Cost/risks: min-max and L2 react to outliers; z-score behavior depends on each
  returned score distribution; adds a second retrieval stack.
- Local experiment: hold lane depth fixed and compare RRF, per-query min-max,
  and z-score weighted fusion, selecting weights only on development data.

### OpenSearch reciprocal rank fusion

- Sources:
  - [Reciprocal rank fusion](https://docs.opensearch.org/latest/vector-search/ai-search/hybrid-search/rrf/)
  - [Score ranker processor](https://docs.opensearch.org/latest/search-plugins/search-pipelines/score-ranker-processor/)
- Date/type: date not stated; official OpenSearch documentation, RRF marked
  introduced in 2.19.
- Finding: RRF combines positions as `1/(k + rank)`, ignores original score
  scale, defaults to rank constant 60, and can use lane weights.
- Transfer: a robust baseline when lexical and semantic scores are incomparable.
- Expected benefit: simple union fusion without score calibration.
- Cost/risks: loses score margins, is candidate-depth sensitive, and cannot
  recover documents absent from both lanes. Its fused score is not a
  cross-query confidence measure.
- Local experiment: compare lane-only, union-only, and RRF at constants
  1/5/20/60 with deterministic ascending-ID tie-breaking.

### OpenSearch hybrid optimization and evaluation

- Sources:
  - [Optimizing hybrid search](https://docs.opensearch.org/latest/search-plugins/search-relevance/optimize-hybrid-search/)
  - [Evaluating search quality](https://docs.opensearch.org/latest/search-plugins/search-relevance/evaluate-search-quality/)
- Date/type: date not stated; official OpenSearch documentation.
- Finding: Search Relevance Workbench evaluates normalization, weight, and RRF
  variants against query sets and judgments; it reports coverage and ranking
  metrics.
- Transfer: the one-change-at-a-time workflow maps directly to local ablations.
- Expected benefit: discourages selecting a fashionable fusion default.
- Cost/risks: large sweeps overfit small labels; coverage means judgment
  coverage, not mixed-intent coverage.
- Local experiment: retain a small predeclared configuration set and report
  paired per-query changes, not only a macro average.

### Qdrant hybrid and multi-stage queries

- Source: [Hybrid and multi-stage queries](https://qdrant.tech/documentation/search/hybrid-queries/)
- Date/type: date not stated; official Qdrant documentation.
- Finding: named dense and sparse prefetches can be fused by RRF or
  distribution-based score fusion (DBSF), or followed by another rescoring
  stage.
- Transfer: relevant to exact taxonomy plus descriptive intent retrieval.
- Expected benefit: independent recall lanes and explicit multistage retrieval.
- Cost/risks: duplicate vectors/indexes; fusion sees only each prefetch's
  candidates; score distributions and shard placement affect DBSF.
- Local experiment: measure dense-only, sparse-only, their raw union, RRF, and
  DBSF at identical candidate depth before tuning weights.

### Qdrant tuning evidence

- Source: [How to Tune Hybrid Search in Qdrant](https://qdrant.tech/articles/how-to-tune-hybrid-search/)
- Date/type: published 22 August 2026; official Qdrant engineering benchmark.
- Finding: on five public datasets of 5,183–100,000 documents, fusion beat the
  better single lane on four and lost on one. The article recommends proving
  fusion helps, comparing RRF/DBSF, tuning the rank constant, tuning weights
  last, and validating held-out.
- Transfer: WANDS is product-search evidence and 100k is scale-adjacent, but
  neither determines Polaris behavior.
- Expected benefit: a disciplined low-dimensional tuning order.
- Cost/risks: one idle-laptop container, public datasets, one shard, and 200
  candidates per lane do not predict deployed p95.
- Local experiment: follow the same order with fixed candidate depths and
  per-query bootstrap intervals; accept "no fusion" as a valid winner.

## Semantic retrieval and reranking

### Sentence Transformers exact semantic search

- Source: [Semantic Search](https://sbert.net/examples/sentence_transformer/applications/semantic-search/)
- Date/type: date not stated; official Sentence Transformers documentation.
- Finding: exact semantic search is documented for corpora up to about one
  million entries; `semantic_search` chunks the corpus and returns cosine top-k.
- Transfer: 105,126 products fit the documented size range.
- Expected benefit: no ANN recall loss and simple filtered exact scoring.
- Cost/risks: this is sizing guidance, not a latency guarantee; query encoding,
  vector memory, and hard-filter masking remain application work.
- Local experiment: exact normalized matrix multiplication with a compact local
  model, measuring query encoding separately from vector scoring.

### Retrieve and rerank

- Sources:
  - [Retrieve & Re-Rank](https://www.sbert.net/examples/sentence_transformer/applications/retrieve_rerank/README.html)
  - [CrossEncoder usage](https://sbert.net/docs/cross_encoder/usage/usage.html)
- Date/type: date not stated; official Sentence Transformers documentation.
- Finding: retrieve a broad candidate set with lexical or bi-encoder search,
  then jointly score only those query-document pairs with a CrossEncoder.
- Transfer: suitable for improving top-ten precision if first-stage Recall@K is
  already high.
- Expected benefit: query/document token interaction unavailable to bi-encoders.
- Cost/risks: latency scales with candidate count and text length; generic
  passage rerankers are not calibrated product judges.
- Local experiment: candidate depths 20/50/100 with title+taxonomy versus full
  description, recording Recall@K, nDCG@10, and warm p95.

### Evaluating first-stage retrieval separately

- Sources:
  - [SentenceTransformer evaluation](https://sbert.net/docs/package_reference/sentence_transformer/evaluation.html)
  - [CrossEncoder evaluation](https://www.sbert.net/docs/package_reference/cross_encoder/evaluation.html)
- Date/type: date not stated; official Sentence Transformers API docs.
- Finding: IR evaluation includes Recall@K, MRR, nDCG, precision, and MAP.
  CrossEncoder evaluation can compare before/after reranking. Automatically
  inserting known positives into reranker candidates masks retrieval failure.
- Transfer: directly addresses whether Polaris misses candidates or ranks them
  badly.
- Expected benefit: correct stage-level diagnosis.
- Cost/risks: recall is only against the judged pool when judgments are
  incomplete.
- Local experiment: evaluate actual candidates without injecting positives;
  report candidate Recall@20/50/100 and an oracle top-ten upper bound.

### Candidate depth can hurt reranking

- Source: [Drowning in Documents: Consequences of Scaling Reranker Inference](https://arxiv.org/abs/2411.11767)
- Date/type: submitted 18 November 2024, revised 11 July 2025; ReNeuIR 2025
  workshop paper/preprint.
- Finding: increasing reranker candidate count can introduce high-scoring
  "phantom hits" and reduce final quality even while first-stage recall rises.
- Transfer: relevant to highly similar product variants and repetitive
  descriptions.
- Expected benefit: supports choosing candidate depth empirically.
- Cost/risks: workshop evidence and non-product datasets do not guarantee the
  same effect.
- Local experiment: plot candidate Recall@K and final nDCG@10 at
  K=20/50/100/200 and manually inspect candidates newly promoted at larger K.

### Product-search cross-encoder deployment

- Source: [Large Scale Deployment of BERT Based Cross Encoder Model for Re-Ranking in Walmart Search Engine](https://dl.acm.org/doi/10.1145/3726302.3731965)
- Date/type: 13 July 2025; peer-reviewed SIGIR 2025 short paper.
- Finding: Walmart reports production product reranking using cached product
  processing, batching, operator fusion, vectorization, and payload reduction.
- Transfer: establishes operational feasibility, not quality or latency on this
  hardware.
- Expected benefit: evidence that selective local reranking can be engineered.
- Cost/risks: closed data and hardware; no transferable absolute p95 guarantee.
- Local experiment: pretokenize product text and compare title+taxonomy against
  full text at small candidate depths.

### Product candidate recall

- Source: [GRIT: Graph-based Recall Improvement for Task-oriented E-commerce Queries](https://dl.acm.org/doi/10.1145/3701716.3717859)
- Date/type: 23 May 2025; peer-reviewed WWW 2025 Companion paper.
- Finding: task-oriented ecommerce queries expose first-stage recall failures;
  omitted products cannot be repaired by ranking.
- Transfer: relevant to vague use-case queries, although Polaris lacks click or
  co-purchase graph evidence.
- Expected benefit: reinforces candidate-recall measurement.
- Cost/risks: graph expansion without genuine behavioral evidence would invent
  relationships and violate project constraints.
- Local experiment: measure lexical candidate recall on manually judged
  task-oriented queries; do not implement graph expansion without catalog-
  grounded edges.

## Abstention and confidence

### Reranking abstention

- Source: [Towards Trustworthy Reranking: A Simple yet Effective Abstention Mechanism](https://openreview.net/forum?id=iMKUMWfRIj)
- Date/type: 1 October 2024; peer-reviewed TMLR paper.
- Finding: maximum score, score standard deviation, top-score margin, or a small
  learned combination can support risk-coverage abstention.
- Transfer: relevant to separating meaningful unusual queries from unrelated
  requests.
- Expected benefit: calibrated false-positive control at low runtime cost.
- Cost/risks: learned calibration transferred poorly across domains; generic
  model scores are not probabilities.
- Local experiment: compare top score, top1-top2 margin, matched-term coverage,
  and a regularized calibrator using development only; report false-positive and
  false-empty rates on sealed queries.

### Catalog-aligned OOD detection

- Source: [Knowing When Not to Answer: Lightweight KB-Aligned OOD Detection for Safe RAG](https://aclanthology.org/2026.acl-long.740/)
- Date/type: July 2026; peer-reviewed ACL 2026 paper.
- Finding: projections and lightweight classifiers over corpus embeddings can
  gate out-of-domain queries cheaply.
- Transfer: potentially useful only if a dense query embedding is already
  deployed.
- Expected benefit: early rejection before expensive reranking.
- Cost/risks: requires representative near-OOD labels and adds no value without
  an embedding lane.
- Local experiment: only after a dense retriever wins, compare nearest-neighbor
  distance and a PCA/Mahalanobis gate against simple lexical confidence.

## Evaluation methodology

### Reproducible IR baselines

- Source: [Resources for Brewing BEIR](https://dl.acm.org/doi/10.1145/3626772.3657862)
- Date/type: 11 July 2024; peer-reviewed SIGIR 2024 resource paper.
- Finding: use reproducible BM25/dense/sparse baselines, pair nDCG@10 with
  Recall@100, and inspect effects by task rather than trusting one average.
- Transfer: metric discipline transfers; BEIR domains do not substitute for
  product judgments.
- Local experiment: report per-query and per-category deltas plus paired
  bootstrap intervals.

### TREC product search

- Sources:
  - [TREC 2024 overview](https://trec.nist.gov/pubs/trec33/papers/overview_33.pdf)
  - [TREC 2025 Product Search results](https://trec.nist.gov/pubs/trec34/appendices/trec2025-product-search.html)
- Date/type: official NIST benchmark material; TREC 2024 meeting
  18–22 November 2024; TREC 2025 results page created 17 March 2026.
- Finding: product-search evaluation uses graded judgments and reports ranking
  together with candidate recall.
- Transfer: supports 0–3 labels, nDCG, and Recall@K.
- Cost/risks: pooled judgments miss products unique to new systems; TREC gain
  mappings differ from the hackathon.
- Local experiment: pool candidates from every contender before judging and
  keep the competition formula distinct from standard qrel nDCG.

### Judgment-pool completeness

- Source: [Can We Use LLMs to Fill Relevance Judgment Holes?](https://arxiv.org/abs/2405.05600)
- Date/type: submitted 9 May 2024; workshop/preprint.
- Finding: labeling only documents newly retrieved by one system can bias
  comparisons; apply a consistent judge to the complete union pool.
- Transfer: directly relevant when adding FTS, semantic, or reranking candidates.
- Expected benefit: fairer ablations.
- Cost/risks: manual pooling is expensive; LLM labels need human calibration.
- Local experiment: blind system identity, deduplicate pooled top-100 IDs, label
  once, and reuse those labels for every contender.

### Train/test query leakage

- Source: [How Train-Test Leakage Affects Zero-Shot Retrieval](https://doi.org/10.1007/978-3-031-20643-6_11)
- Date/type: 1 November 2022; peer-reviewed SPIRE paper, retained because it
  directly studies retrieval leakage.
- Finding: near-duplicate queries can materially alter apparent system ordering.
- Transfer: the current every-fifth-case split shares templates, aliases, and
  implementation rules.
- Expected benefit: motivates a sealed query-family split.
- Cost/risks: no automatic threshold proves semantic independence.
- Local experiment: group normalized exact matches, token Jaccard, character
  n-gram matches, and manually reviewed semantic neighbors before splitting.

## Official MCP contract reference

### MCP tools revision 2025-11-25

- Source: [MCP tools specification](https://modelcontextprotocol.io/specification/2025-11-25/server/tools)
- Date/type: normative official specification, revision 2025-11-25.
- Finding: servers declare tools capability; `tools/list` advertises valid JSON
  schemas; `tools/call` returns conforming `structuredContent`; serialized JSON
  should also be present as text for compatibility; malformed/unknown requests
  and execution errors have distinct mechanisms.
- Transfer: directly authoritative for Polaris transport correctness.
- Expected benefit: contract stability, not ranking quality.
- Cost/risks: migrating to a newer revision would violate the competition.
- Local experiment: retain revision `2025-11-25`; add an unknown-tool protocol
  error check and continue validating schemas and mirrored text content.

## Production and community engineering reports

These sources are useful failure reports, not independent scientific proof.

### GetYourGuide segmented hybrid search

- Source: [From Text to Context: How We Introduced a Modern Hybrid Search at GetYourGuide](https://getyourguide.careers/posts/from-text-to-context-how-we-introduced-a-modern-hybrid-search-at-getyourguide)
- Date/type: 20 November 2024; first-party engineering report.
- Finding: on 150k+ activities, hybrid hurt exact location queries. The team
  excluded that query class, learned a linear lexical/vector/product blend, and
  used progressively looser thresholds. It reports statistically significant
  +3.4% relevance and +2.6% revenue after iterative experiments.
- Transfer: catalog scale is close and exact-query segmentation is relevant.
- Expected benefit: semantic retrieval only where lexical evidence is weak.
- Cost/risks: travel, multilingual data, click-derived labels, and popularity
  are unlike Polaris; self-reported business metrics are not reproducible here.
- Local experiment: classify exact taxonomy/name versus descriptive queries;
  allow semantic candidates only in the latter and report each slice separately.

### Takealot hybrid product search

- Source: [Hybrid product search at Takealot](https://medium.com/takealot-engineering/hybrid-product-search-at-takealot-24c6e97a84eb)
- Date/type: 18 November 2024; first-party engineering report.
- Finding: dedicated text embeddings plus lexical retrieval outperformed an
  image-text approach; exact terms remained lexical. The report states an
  approximately 6% listing-CTR increase.
- Transfer: direct ecommerce evidence that modality/model choice matters.
- Expected benefit: supports text-only dense testing rather than image scope.
- Cost/risks: CTR is not pure relevance and the deployment is not reproducible.
- Local experiment: compare compact text models on catalog judgments; do not add
  image embeddings without image-specific labels.

### Digitec Galaxus vector fallback

- Source: [The Digitec Galaxus Vector Search Journey](https://mices.co/mices2025/slides/abel-camacho-joel-widmer-mices-2025.pdf)
- Date/type: 18 June 2025; first-party conference slides.
- Finding: weak vector fallback produced worse search success and more exits than
  honest zero-results pages; taxonomy hard negatives and behavioral examples
  were needed.
- Transfer: directly relevant to Polaris abstention.
- Expected benefit: evidence against filling result slots with nearest neighbors.
- Cost/risks: slides provide limited reproduction detail.
- Local experiment: manually judge zero-result, vector-fallback, and abstention
  outcomes on hard in-domain and unrelated requests.

### Qdrant RRF confidence failure

- Source: [Filtered hybrid searches sometimes return unrelated points with a high score](https://github.com/qdrant/qdrant/issues/4937)
- Date/type: opened 22 August 2024; GitHub issue and maintainer response.
- Finding: RRF ignores original scores; the maintainer advised applying score
  thresholds inside each prefetch lane rather than to fused rank scores.
- Transfer: fully relevant if RRF is trialed.
- Expected benefit: avoids false confidence from a rank-one fused result.
- Cost/risks: one issue report and a separate filtering anomaly were not a
  general benchmark.
- Local experiment: compare lane thresholds, fused-score thresholds, and a
  separately calibrated acceptance rule on nonsense and near-OOD queries.

### Typesense exact match versus fuzzy synonym expansion

- Source: [Add exact match option for synonyms](https://github.com/typesense/typesense/issues/2180)
- Date/type: opened 6 February 2025, fixed October 2025; GitHub issue.
- Finding: synonym and typo expansion allowed a near spelling to outrank an
  exact token, including harmful prefix behavior.
- Transfer: product names, colors, acronyms, and IDs have the same risk.
- Expected benefit: validates ordering exact phrase/token above synonym, typo,
  and prefix evidence.
- Cost/risks: Typesense-specific behavior.
- Local experiment: construct edit-distance-near catalog terms and assert the
  evidence-priority ordering.

### Meilisearch synonym dictionary cost

- Source: [Synonym and segmentation dictionaries can significantly reduce search performance](https://github.com/meilisearch/meilisearch/issues/5265)
- Date/type: opened 22 January 2025, fixed 8 October 2025; GitHub issue.
- Finding: very large synonym and segmentation dictionaries caused substantial
  latency degradation in one installation.
- Transfer: cautions against generated catalog-wide synonym lists.
- Expected benefit: supports keeping expansions reviewed and small.
- Cost/risks: version- and engine-specific anecdote.
- Local experiment: increase synonym count in controlled steps and record p95,
  memory, startup, and relevance.

### Reddit: grocery ecommerce relevance failures

- Source: [Struggling with a Poorly Implemented Elasticsearch in Grocery E-Commerce](https://www.reddit.com/r/elasticsearch/comments/1j5i56i/struggling_with_a_poorly_implemented/)
- Date/type: 7 March 2025; unverified community anecdote.
- Finding: reports compound-word, morphology, ambiguity, over-recall, and
  commercial-boosting failures.
- Transfer: useful as a regression taxonomy, not evidence of prevalence.
- Local experiment: add compound splitting, morphology, and ambiguity cases;
  ensure relevance eligibility precedes optional business signals.

### Reddit: intersection-only RRF bug

- Source: [RAG - Hybrid search RRF issue](https://www.reddit.com/r/LocalLLaMA/comments/1bevto7/rag_hybrid_search_rrf_issue/)
- Date/type: 14 March 2024; unverified community anecdote.
- Finding: an implementation apparently intersected dense and sparse candidates,
  destroying candidate yield. Standard RRF operates on the union.
- Transfer: candidate-union correctness is universally relevant.
- Local experiment: assert fused candidate count/recall against each lane's
  union and separately report overlap.

### Reddit: hybrid/reranking regression

- Source: [Hybrid search and reranking made my RAG worse](https://www.reddit.com/r/Rag/comments/1v7g3oe/hybrid_search_and_reranking_made_my_rag_worse/)
- Date/type: 26 July 2026; unverified community anecdote.
- Finding: the author reports that hybrid and reranking regressed a 116-query
  set and that some positives never appeared in the top 50.
- Transfer: the diagnostic separation between missing candidates and bad
  ranking is valuable; the small non-product corpus is not.
- Local experiment: record first relevant rank before fusion/reranking and
  reject stages that lower held-out quality.

## Recommended experiment shortlist for Polaris

1. Evaluation audit and candidate-recall instrumentation before ranking changes.
2. Current scorer versus a controlled BM25/FTS5 scorer with the same candidates,
   parser, filters, and deterministic tie rules.
3. Evidence-priority ablation: exact phrase/name/type boosts before synonyms and
   typo corrections, with targeted negative cases.
4. Query-slice abstention calibration using meaningful unusual, impossible
   shopping, near-domain, and unrelated requests.
5. Dense or CrossEncoder trials only if the lexical candidate audit shows a
   recall/ranking gap and a sealed held-out pool can measure it.

Promotion requires held-out quality gain, unchanged contract correctness, and no
unacceptable startup/RSS/p95 regression. A rejected candidate is a valid
experimental result.
