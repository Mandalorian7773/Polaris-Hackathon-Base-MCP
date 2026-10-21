# PROGRESS

Measured results per phase. Numbers are local measurements on the development machine,
never an official benchmark score.

Machine: Linux, 16 logical CPUs, 23.7 GB RAM, Python 3.12.3 (`.venv`), MCP SDK 1.30.0.

---

## Phase 1 — Baseline (2026-10-21)

Goal: read the contract, write it down, measure the unmodified starter. `search.py` and
`server.py` were **not** changed in this phase.

### Added

| Path | Purpose |
| --- | --- |
| `CLAUDE.md` | Full contract (validation, filters, sorting/tie-breaks, null ratings, pagination, empty query, mixed choices, recommendations, abstention), scoring table, working rules |
| `scripts/catalog_stats.py` | Catalog summariser; never prints raw rows |
| `analysis/catalog_stats.json` | Generated field/facet/distribution summary |
| `scripts/bench.py` | Real MCP-client latency bench (not REST); optional managed server start/stop with startup time and RSS |
| `analysis/bench_phase1.json`, `analysis/bench_phase1_run2.json` | Bench reports |
| `PROGRESS.md` | This log |

### Correctness baseline

| Check | Result |
| --- | --- |
| `python -m unittest discover -s tests -v` | **5/5 OK**, unchanged |
| `python smoke_test.py http://127.0.0.1:8000/mcp` | **PASS** (0.88 s), unchanged |
| `/healthz` | `{"status": "ok", "products": 105126}` |
| Page stability across repeated identical requests | 50/50 distinct pool pages identical on repeat |
| Output-schema validation + `content[0].text == structuredContent` | 200/200 timed calls valid |

### Startup and memory (managed by `scripts/bench.py --manage`)

| Metric | Run 1 | Run 2 |
| --- | --- | --- |
| Startup to first healthy `/healthz` | **2.63 s** | 2.82 s |
| RSS after startup | **777 MB** | 773 MB |
| RSS after bench | 792 MB | 787 MB |
| Peak RSS (`VmHWM`) | 792 MB | 787 MB |

The README's "around 2 GB" guidance is pessimistic for this machine: the baseline settles
at roughly 0.78 GB resident after loading 105,126 products plus the per-product token sets.

### Latency — empty-query filter/sort pool over MCP

Pool: 10 filter sets × 5 sort configurations = 50 fixed ordered requests, `query: ""`,
`limit: 10`, `offset: 0`, recommendations off. 20 warm-up calls, then 100 timed calls per
workload. Timing spans request dispatch → fully parsed tool response; initialization
happens before the clock starts. Failed calls would count as 10,000 ms (none occurred).

| Workload | Calls | Errors | p50 | p95 | p99 | max |
| --- | --- | --- | --- | --- | --- | --- |
| Concurrency 1 — run 1 | 100 | 0 | **58.7 ms** | **86.7 ms** | 112.0 ms | 113.4 ms |
| Concurrency 1 — run 2 | 100 | 0 | 55.6 ms | 87.3 ms | 113.6 ms | 119.3 ms |
| Concurrency 5 — run 1 | 100 | 0 | **309.3 ms** | **557.5 ms** | 675.0 ms | 799.2 ms |
| Concurrency 5 — run 2 | 100 | 0 | 354.8 ms | 620.3 ms | 696.3 ms | 706.6 ms |
| Pooled — run 1 | 200 | 0 | 113.4 ms | 491.4 ms | 645.0 ms | 799.2 ms |
| Pooled — run 2 | 200 | 0 | 113.6 ms | 547.5 ms | 690.8 ms | 706.6 ms |

Correct response rate: **1.0000** in both runs.

### In-process search cost (no MCP transport, median of 7)

| Request | Median |
| --- | --- |
| Empty query, no filters | 40.9 ms |
| Empty query, `color=Black` | 35.4 ms |
| Empty query, `sort_by=price asc` | 70.1 ms |
| Empty query, `min_rating=4, min_review_count=3` | 41.0 ms |
| Empty query, `offset=9990` | 41.4 ms |
| `query="cotton top"` | 148.4 ms |
| `query="black dress"` | 137.8 ms |
| `Catalog.load` from JSON | 2.27 s |

### Catalog shape (`analysis/catalog_stats.json`)

- **105,126** products, **105,126** unique IDs, **all** IDs have a leading zero — string
  IDs are mandatory.
- All 13 fields are always present: `id`, `name`, `description`, `product_type`,
  `category`, `audience`, `section`, `color`, `pattern`, `image_url`, `price`, `rating`,
  `reviews`. Descriptions are never actually null in this file (min length 11, mean 142),
  though the contract allows null — keep handling it.
- Facet vocabulary sizes: `category` 19, `product_type` 131, `color` 50, `audience` 10,
  `section` 56, `pattern` 30. No blank or null facet values.
- **Case collision found:** `product_type` contains both `Dog Wear` and `Dog wear`. Exact
  case-insensitive filter matching must fold both into one filter value — this is a real
  test case for Phase 2, not a theoretical one.
- `price.amount`: INR only, 89 distinct values, 199 … 8,999, p50 1,999, p95 4,499.
- `rating.average`: present for 93,566, **null for 11,560** — exactly the products with
  `rating.count == 0` and `reviews == []`. No product has a null average with a positive
  count, and none has an average with a zero count. Null averages must fail `min_rating`
  and sort last.
- `rating.count` is 0–8 and almost perfectly uniform (~11.6k products per value).
- Largest facet buckets: `Garment Upper body` 42,593; `Trousers` 11,140; `Black` 22,559;
  `Ladieswear` 25,912; `Solid` 49,548.

### Observations carried into Phase 2

1. **Concurrency 5 is the bottleneck, not single-call latency.** The p95 jumps from ~87 ms
   to ~560–620 ms. `Catalog.search` is a full 105k-row Python scan dispatched to a thread,
   so five concurrent calls contend on the GIL and serialize. The brief pools concurrency-1
   and concurrency-5 calls into one p95, so this alone currently costs most of the 5
   performance points. Facet postings plus presorted per-field orders (intersect, walk,
   slice) should turn these into near-constant-time page builds.
2. **Field sorting re-sorts the whole candidate list** (`sort_by=price` is 70 ms versus
   41 ms for the unsorted path). Presorted orders remove that cost.
3. **Text queries cost ~140 ms in-process** because `relevance` is evaluated against every
   filter-passing product. Not in the scored performance pool, but it sets the floor for
   the 10-second functional deadline once richer ranking is added.
4. **Functional gaps are as documented**: `recommend` returns `[]` (0 of 10
   recommendation points), strict AND token matching means no synonyms, typos, prose
   budgets, mixed-choice splitting, or calibrated abstention. `query="spaceship engine"`
   happens to return empty only because AND matching fails, not because of a real
   abstention rule.
5. Performance points need **≥ 85.5 / 95 functional points** first, so Phase 2 contract
   correctness gates the speed work regardless of latency.

### Reproduce

```sh
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python scripts/catalog_stats.py
.venv/bin/python scripts/bench.py --manage --smoke --out analysis/bench_phase1.json
```

---

## Phase 2 — Contract correctness and the exact filter index (2026-10-21)

Goal: make the contract impossible to get wrong by accident, and make exact
filter-only pages cheap. `search.py` was restructured into explicit stages; the
published schemas, tool name, protocol revision, and output shape are unchanged.

### Architecture

`search.py` now runs one request through named stages:

```
parse_request   validate against the schema, then apply defaults ourselves
plan_query      QueryPlan (terms today; real intent planning in phase 5)
_acceptor       compile explicit filters into one position predicate
_candidates     selective posting/range source, or an ordered walk
_order_key      field sort keys with ID-ascending tie-break, nulls last
_recheck        re-verify the page against the original catalog records
recommend       still a stub, returns []
```

`CatalogIndex` is built once at load: folded facet columns, numeric columns,
position postings per facet value, eight presorted orders (four fields × two
directions), ascending value arrays for `bisect`, and per-product term sets.

Candidate generation takes one of two paths and both must agree:

- **Selective source** — the smallest superset derivable from a single filter
  (a facet posting, or a `bisect` range over the price / rating / review-count
  order). Taken when it covers at most `1/MATERIALIZE_RATIO` (1/8) of the catalog.
- **Ordered walk** — walk the presorted order, test the predicate, stop as soon as
  `offset + limit` rows are collected.

Unfiltered requests skip both and slice the presorted order directly.

### Two real bugs found and fixed

1. **`GET /mcp` hung open an SSE stream** instead of returning 405 as the brief
   requires. `server.py` now routes only `POST` and `DELETE`, so Starlette answers
   `GET` with 405 and an `Allow` header. Measured: `GET` → 405, `DELETE` → 405,
   a plain REST `POST` → 406, `/healthz` → 200.
2. **`{"limit": 10.0}` crashed with a `TypeError`.** JSON Schema 2020-12 counts a
   float with a zero fractional part as an integer, so `10.0` is a *valid* argument;
   it reached a list slice and raised. A crash on a valid call would have cost a
   whole validity case. `parse_request` now coerces `offset`, `limit`, and
   `min_review_count` to `int`, while `10.5` stays invalid.

### Tests

`tests/test_search.py`: the five original fixtures are untouched; 45 cases added
across validation (wrong types, nulls, bool-as-int, NaN/Inf, out-of-range, boundary
values, `query` > 1000 chars, unknown vs blank facet values, unknown properties),
filter semantics (trimming, case folding, inclusive bounds, null ratings vs
`min_rating`, AND combination, explicit filter beating conflicting query wording),
ordering (empty-query ID fallback, ID-ascending tie-breaks in both directions, nulls
last, field sorting after relevance selection, relevance `asc` reversal), pagination
(repeat stability, prefix consistency, no duplicates or gaps, deep offsets, short
pages), contract invariants (known string IDs, leading zeros, both keys always
present, recommendation invariants), index invariants (postings partition the
catalog, `matches` agrees with the compiled acceptor at every position), and query
planning (whitespace-only, Unicode).

`tests/test_protocol.py` (new): starts a real `server.py` process against a 60-row
fixture catalog via `CATALOG_PATH` and checks protocol revision `2025-11-25`, exactly
one advertised tool with both exact schemas, `structuredContent` equal to the text
block, empty success for no matches, **17 invalid calls all returning `isError`**,
unknown tool name, over-the-wire pagination stability and prefix consistency,
recommendations disabled by default, and the HTTP method contract.

| Suite | Result |
| --- | --- |
| `python -m unittest discover -s tests` | **61 tests OK in 2.85 s** |
| Original 5 starter cases | pass, unchanged |
| `smoke_test.py` | **PASS** (0.57 s), unchanged |
| Differential check vs brute-force reference, real catalog | **1,630 combos, 0 mismatches** |
| Differential check vs brute-force reference, fixture, both paths forced | **1,800 combos, 0 mismatches** |

The real-catalog differential run covered every filter set × sort × query × page
combination including the `Dog Wear` / `Dog wear` case collision, unknown facet
values, `min_price = max_price`, deep offsets, and `limit = 100`.

### Measured: latency

| Workload | Phase 1 p50 | Phase 2 p50 | Phase 1 p95 | Phase 2 p95 |
| --- | --- | --- | --- | --- |
| Concurrency 1 | 58.7 ms | **11.5 ms** | 86.7 ms | **16.0 ms** |
| Concurrency 5 | 309.3 ms | **41.5 ms** | 557.5 ms | **57.6 ms** |
| Pooled (200 calls) | 113.4 ms | **17.6 ms** | 491.4 ms | **52.0 ms** |

0 errors, correct response rate 1.0000, 50/50 pool pages stable on repeat.

### Measured: in-process search cost (median of 9)

| Request | Phase 1 | Phase 2 |
| --- | --- | --- |
| Empty query, no filters | 40.9 ms | **0.01 ms** |
| Empty query, `color=Black` | 35.4 ms | **0.04 ms** |
| Empty query, `sort_by=price asc` | 70.1 ms | **0.02 ms** |
| Empty query, `min_rating=4, min_review_count=3` | 41.0 ms | **0.05 ms** |
| Empty query, `offset=9990` | 41.4 ms | **0.02 ms** |
| Empty query, 3 facets + 3 numeric filters | — | 0.87 ms |
| `query="cotton top"` | 148.4 ms | 43.9 ms |
| `query="black dress"` | 137.8 ms | 38.6 ms |

The empty-query path is now ~1,000× cheaper and the remaining ~11 ms p50 over MCP is
almost entirely SDK, uvicorn, and JSON transport overhead — the in-process search is
0.01–0.9 ms. Text queries still scan every filter-passing product because `relevance`
is unchanged; phase 4 replaces that with an inverted index.

### Startup and memory

| Metric | Phase 1 | Phase 2 |
| --- | --- | --- |
| Startup to healthy `/healthz` | 2.63 s | 3.23 s |
| RSS after startup | 777 MB | 803 MB |
| Peak RSS | 792 MB | 805 MB |

+0.6 s and +26 MB buys the index: 8 presorted orders, 6 facet posting maps, numeric
columns, and ascending value arrays.

### Still open after phase 2

- `recommend` returns `[]` — 0 of the 10 recommendation points.
- Relevance is still strict lexical AND: no synonyms, typos, prose budgets,
  mixed-choice splitting, or calibrated abstention.
- Text-query latency (~40 ms in process) is not in the scored performance pool but
  sets the floor for richer ranking.

---

## Phase 3 — Honest local evaluation (2026-10-21)

Goal: build the measuring instrument before tuning anything. No change to `search.py`
or `server.py` in this phase, so the numbers below are the phase 2 system scored for
the first time.

### Added

| Path | Purpose |
| --- | --- |
| `scripts/build_cases.py` | Reviewable Python source of the case set; writes `eval/cases.jsonl` |
| `eval/cases.jsonl` | **104 cases**, 72 dev / 32 held out (31 %) |
| `scripts/evalkit.py` | Selectors, rubric grading, nDCG, case/judgment loading |
| `scripts/label.py` | Interactive 0–3 labelling of pooled candidates → `eval/judgments.jsonl` |
| `scripts/evaluate.py` | Runs every case over `/mcp`, scores it, writes `eval/runs/<ts>_<sha>.json` |

### Case set

| Category | Cases | Held out | Scored area |
| --- | --- | --- | --- |
| `exact` | 10 | 3 | ranking |
| `synonym` | 8 | 2 | ranking |
| `typo` | 8 | 3 | ranking |
| `vague` | 10 | 4 | unusual |
| `irrelevant` | 12 | 4 | irrelevant (expect empty) |
| `mixed` | 12 | 4 | mixed choices |
| `complement` | 10 | 3 | recommendations |
| `filter_sort` | 10 | 3 | validity (exact page) |
| `pagination` | 6 | 2 | validity (probes) |
| `invalid` | 18 | 4 | validity (expect MCP error) |

Every facet value used in a case exists in the catalog. Cases encode hard constraints
stated in prose (`a red dress and black heels under INR 3000 each`), the
`Dog Wear` / `Dog wear` collision, impossible budgets (cheapest item is INR 199,
dearest INR 8,999), a prompt-injection query, and a three-choice mixed query.

### How grading works, and what it is not

A case carries an ordered **rubric** of `{grade, selector}` tiers; a product takes the
grade of the first tier it matches, 0 if none, and 0 if it violates the case's hard
constraints. Metrics follow the brief exactly: `DCG = Σ (2^g − 1) / log2(rank + 1)`
normalised against a fixed ceiling of ten grade-3 primary slots and five
recommendation slots, mixed coverage = fraction of choices with a grade-≥2 result,
recommendation quality = nDCG@5 where a recommendation only scores if it references a
current-page primary graded ≥ 2.

**This is a proxy, not the organizer's judgment.** The rubric is attribute-derived, so
it rewards a system that finds the right `product_type` / `colour` / `pattern` and
cannot see whether a specific garment actually suits the request. Bedrock will.
`scripts/label.py` exists so human grades in `eval/judgments.jsonl` override the
rubric per (case, product); that file is **empty right now** — the 1–2 hours of
labelling is the outstanding manual job, and until it is done the weighted total
should be read as "did retrieval find the right kind of product", not "would a shopper
be happy".

`scripts/evaluate.py` cross-checks exact pages with `reference_page`, a brute-force
implementation written from the brief and deliberately independent of `search.py`.

### Baseline score of the phase 2 system

Local weighted estimate: **52.26 / 100** (functional 52.26 / 95, performance 0 / 5).

| Area | Max | Mean score | Points | Dev | Held out |
| --- | --- | --- | --- | --- | --- |
| Validity and constraints | 25 | 1.0000 | **25.000** | 1.0000 | 1.0000 |
| Ranking relevance | 30 | 0.5378 | **16.135** | 0.5150 | 0.5891 |
| Unusual (meaningful) | 7.5 | 0.0000 | **0.000** | 0.0000 | 0.0000 |
| Irrelevant (abstention) | 7.5 | 1.0000 | **7.500** | 1.0000 | 1.0000 |
| Mixed choices | 15 | 0.1747 | **2.620** | 0.0537 | 0.4167 |
| Complementary recommendations | 10 | 0.1000 | **1.000** | 0.1429 | 0.0000 |
| Performance | 5 | — | **0.000** | — | — |

Diagnostics: mean nDCG@10 **0.2951**, abstention **1.0000**, false-positive rate
**0.0000**, mixed coverage **0.1528**, recommendation quality@5 **0.1000**, mean
validity **0.9087**, contract pass rate (all four checks) **0.6346**.

Latency during the run: concurrency 1 p50 11.7 ms / p95 16.3 ms, concurrency 5 p50
47.0 ms / p95 63.6 ms, pooled p95 **60.1 ms**, 0 errors. Startup 3.23 s, RSS 803 MB,
peak 810 MB. Per-case round trip p50 46.9 ms, p95 54.1 ms.

### What the baseline score actually says

1. **Validity is already full marks locally** (25/25 across all 34 validity cases:
   18 invalid calls all error, 16 exact-page and pagination cases all match the
   independent reference). The `contract_pass_rate` of 0.63 is lower only because it
   counts *every* case including the semantic ones, where "expected results, got an
   empty page" fails the presence check.
2. **Abstention is 7.5/7.5 for the wrong reason.** Strict lexical AND matching returns
   nothing for almost any multi-word natural query, so it abstains on nonsense *and*
   on 10 of 10 meaningful unusual queries, all 10 complement cases, and 9 of 12 mixed
   queries. This score is fragile: phase 4 will raise recall and abstention must be
   re-calibrated, not assumed.
3. **Unusual queries score exactly 0.** The catalog has no occasion vocabulary at all
   — `party` appears in 5 descriptions, `wedding` in 1, `office`, `sunday` and `lazy`
   in none — so "something comfortable for a lazy Sunday" has no lexical evidence and
   must be answered by reasoning over `product_type` / `section` / `pattern`.
4. **Product names are internal style codes**, not shopping text: `SWEATSHIRT OC`,
   `tvp`, `Jerry jogger bottoms`, `Mimmi sneaker`. `sneakers` appears in 13 names but
   1,590 products have `product_type = Sneakers`, and `jeans` appears in 30 names
   while 2,870 Trousers have `pattern = Denim`. The baseline's product-name boost is
   therefore close to useless, and field weighting must favour `product_type`.
5. Ranking already gets 0.54 of a *fixed grade-3 ceiling*, which is healthier than it
   looks: single-word attribute queries (`sunglasses`, `jeans`, `black dress`) work
   because the attribute text is matched; everything multi-word fails.

### Reproduce

```sh
.venv/bin/python scripts/build_cases.py
.venv/bin/python scripts/evaluate.py --manage
.venv/bin/python scripts/label.py --manage --skip-labelled   # the manual job
```

---

## Phase 4 — Lexical retrieval: BM25F, expansion, abstention (2026-10-21)

Goal: replace strict lexical AND matching with field-aware BM25F plus
catalog-grounded query expansion, and calibrate abstention so raising recall does not
start answering nonsense.

Local weighted estimate: **52.26 → 80.67 / 100**. Dev **81.20**, held out **80.41** —
the gain generalises.

### Added

| Path | Purpose |
| --- | --- |
| `retrieval.py` | BM25F index, query planner, spelling correction, scorer, abstention |
| `data/lexicon.json` | **Reviewable** shopper-language → catalog-value mapping |
| `scripts/check_lexicon.py` | Fails if any lexicon target names no catalog value |
| `scripts/tune.py` | Dev-only parameter sweep and coordinate descent, held out reported |
| `scripts/spike_fts5.py` | Throwaway SQLite FTS5 comparison (evidence, not a path) |
| `tests/test_retrieval.py` | 31 cases: expansion, spelling, abstention, determinism |

### Why BM25F over one text blob

Catalog names are internal style codes — `SWEATSHIRT OC`, `tvp`, `Jerry jogger
bottoms`, `Mimmi sneaker` — so a shopper's word usually lands on `product_type`, not
the name. Five field groups are indexed separately (`type`, `cat`, `attr`, `name`,
`desc`) with `product_type` weighted 9× against description 1×.

A query becomes a list of **requirements**. Each carries the literal terms to score
*and* the catalog attribute values it resolves to; a product satisfies a requirement
by matching either. The share of requirements a product satisfies drives ranking and
abstention, so an unrelated query has nothing to satisfy and returns nothing.

### The lexicon, and the honesty problem with it

`data/lexicon.json` holds 185 synonyms, 56 intents, 116 stopwords and 558 catalog
targets, all verified present in the catalog by `scripts/check_lexicon.py`.

- **Synonyms** map shopper words to catalog values and **anchor** a query:
  `sneakers → product_type Sneakers` (1,590 products, but only 13 names contain
  "sneaker"), `jeans → Trousers + pattern Denim` (2,870 products; no Jeans type
  exists).
- **Intents** map an occasion to defensible attributes but deliberately **never
  anchor**: `lazy / sunday / comfortable → Hoodie, Pyjama set, Robe, Slippers`.
  Anchoring on intents made `a titanium wedding suit of armour` return dresses,
  because "wedding" and "suit" are intent words. With intents non-anchoring, that
  query correctly returns empty while `something comfortable for a lazy Sunday`
  returns hoodies.

**I wrote both the eval cases and this lexicon**, so the `vague` and `synonym` gains
are partly self-confirming — the held-out split was written by the same hand. The
real tests are the user's own labels (`scripts/label.py`) and the hidden suite.
Treat the `unusual` numbers as the least trustworthy in the table, and review the
lexicon with a fashion eye.

### Spelling correction

Edit distance against a **468-term reviewed vocabulary** (facet value tokens, synonym
and intent keys, a short material/fit list) — not the whole 15,402-term index, so a
typo can only ever resolve to a shopping word. Limit 1 edit under 6 characters, 2
above.

One real finding: the **catalog's own text contains typos**. `sunglases` appears in a
product's text with document frequency 1, so "is it in the vocabulary?" wrongly
treated it as correctly spelled and the query matched exactly one product out of 618
Sunglasses. Correction now triggers for any term rarer than `typo_min_df` (8), not
just unknown terms.

Also fixed: catalog values are now matched with stopwords stripped. `wear` is a
stopword, so `Dog Wear` could never be matched as a phrase; it is now reachable
through `dog` alone. Seven catalog values were affected and the only collision
created is `Dog Wear` / `Dog wear`, which is the desired one.

### Tuning: what the sweep actually bought

Coordinate descent over 15 axes on dev: **dev 54.15 → 57.57, but held out
55.41 → 55.01**. That is overfitting, so the descent result was **rejected**. Per-axis
sweeps against both splits adopted only changes that help or are neutral on both:

| Axis | Default | Adopted | Dev | Held out | Why |
| --- | --- | --- | --- | --- | --- |
| `no_anchor_coverage` | 0.75 | **0.9** | +0.94 | 0.00 | dev abstention 0.75 → 0.88, no held-out cost |
| `coverage_exponent` | 2.0 | **1.0** | +1.12 | +0.00 | neutral on held out |
| `score_floor` | 0.08 | **0.15** | 0.00 | 0.00 | same quality, p95 24 → 20 ms |
| `synonym_boost` | 7.0 | 7.0 (kept) | +0.03 | −0.52 | **rejected**, hurts held out |
| `typo_min_df` | 8 | 8 (kept) | +0.13 | 0.00 | **rejected**, more aggressive correction for nothing |
| `desc_df_share` | 0.12 | 0.12 (kept) | +0.06 | +0.12 | **rejected**, +2 ms p95 for noise |
| `coverage_slack` | 1 | 1 (kept) | 0 is −2.5 | 0 is −3.4 | 1 and 2 identical |

Adopted combination: dev 54.15 → **56.20**, held out 55.41 → **55.41**. So tuning
contributed about +2 dev points and **zero** held-out points: the phase's gain came
from the architecture, not the knobs.

### Measured spike: SQLite FTS5 vs in-process BM25F

Cores only — expansion, correction, anchoring and abstention disabled on both sides —
over the same documents, same query terms, same rubric, 25 relevance cases:

| Core | Build | nDCG@10 | p50 | p95 | Artefact |
| --- | --- | --- | --- | --- | --- |
| In-process BM25F | 5.56 s | **0.5570** | 1.24 ms | **14.54 ms** | none |
| SQLite FTS5 | **1.17 s** | 0.5353 | **0.84 ms** | 18.92 ms | 54 MB database |

**Decision: keep in-process BM25F, drop FTS5.** The cores are within noise on
quality, FTS5 builds 4.8× faster and has a better p50, but a worse p95 — and p95 is
what is scored. The decisive reason is not in the table: `bm25()` returns one opaque
score per row, so FTS5 cannot express attribute boosts or per-document requirement
coverage, which is where this phase's gain actually comes from. Adopting it would
mean shipping a 54 MB second store *and* reimplementing that layer on top of it, for
no measured gain. `scripts/spike_fts5.py` is kept as evidence; there is one
production path.

### Score movement

| Area | Max | Phase 3 | Phase 4 | Dev | Held out |
| --- | --- | --- | --- | --- | --- |
| Validity and constraints | 25 | 25.000 | **25.000** | 1.0000 | 1.0000 |
| Ranking relevance | 30 | 16.135 | **28.864** | 0.9453 | 1.0000 |
| Unusual (meaningful) | 7.5 | 0.000 | **6.000** | 1.0000 | 0.5000 |
| Irrelevant (abstention) | 7.5 | 7.500 | **6.875** | 0.8750 | 1.0000 |
| Mixed choices | 15 | 2.620 | **11.631** | 0.7537 | 0.8187 |
| Complementary recommendations | 10 | 1.000 | **2.299** | 0.2477 | 0.1883 |
| **Functional** | 95 | 52.255 | **80.669** | 81.204 | 80.414 |

Diagnostics: nDCG@10 **0.2951 → 0.8122**, mixed coverage **0.1528 → 0.6111**,
contract pass rate **0.6346 → 0.9327**, mean validity **0.9087 → 0.9832**.

**Abstention went down, as predicted in phase 3**: 1.0000 → 0.9167. The phase 3 score
was perfect because nothing matched anything; one case now fails honestly
(`irr-09`, "a dress for under INR 50") because the engine finds dresses and does not
yet read the impossible budget out of the prose.

### Cost

| Metric | Phase 2/3 | Phase 4 |
| --- | --- | --- |
| Startup to healthy `/healthz` | 3.23 s | **6.83 s** |
| RSS after startup | 803 MB | **1,103 MB** |
| Empty-query pool p95 (concurrency 1) | 16.3 ms | 15.4 ms |
| Empty-query pool p95 (concurrency 5) | 63.6 ms | 61.0 ms |
| Pooled p95 | 60.1 ms | 55.7 ms |
| Per-case round trip p95 (incl. text queries) | 54.1 ms | 36.6 ms |
| Tests | 61 | **92** |

The empty-query pool is untouched by this phase — it never scores text — so the
performance picture is unchanged. +3.6 s startup and +300 MB buy the inverted index.

### Still open after phase 4 (7 failing cases)

1. `irr-09`, `mix-09` — **budgets written in prose are not enforced**. "under INR 50"
   and "under INR 3000 each" are read as words, not constraints. Phase 5.
2. `rec-05`, `rec-06`, `rec-10` — **complement phrasing dilutes coverage** and the
   page comes back empty: "sneakers with socks to go with them" spends requirements
   on the accessory. Splitting primary from accessory intent is Phase 5.
3. `vague-08`, `vague-10` — "something to keep my hair out of my face", "rainy day
   essentials" still abstain; the intent words are outvoted by ordinary English.
4. `recommend` is still a stub, so complement scores 2.3/10 purely from primary
   quality. Phase 6.
