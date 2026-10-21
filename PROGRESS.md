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

---

## Phase 5 — Deterministic shopping-intent planning (2026-10-21)

Goal: read what the shopper actually asked for — budgets in prose, several choices,
an accompanying item — and route each part to the right place.

Local weighted estimate: **80.67 → 93.11 / 100**. Dev **88.29**, held out **88.20**.
**Zero failing cases**, contract pass rate **1.0000**. The performance gate
(≥ 85.5 of 95 functional points) is now cleared, so the 5 performance points count
for the first time.

### Added

| Path | Purpose |
| --- | --- |
| `query_planner.py` | Budget parsing, mixed-choice splitting, complement cues |
| `tests/test_query_planner.py` | 32 cases over ~45 phrasings, no index needed |
| `tests/test_retrieval.py` (extended) | Unexplained terms, mixed merge, prose budgets, complement planning |

`ShoppingPlanner` takes an injected `names_product` callable, so the module stays
independent of the retrieval index and is testable against a 50-word vocabulary. No
model and no network: rules over catalog-derived vocabulary, so a query always plans
the same way.

### What it parses

- **Budgets**: `under / below / less than / within / up to / at most / no more than /
  max / cheaper than / < / <=`, the `min` equivalents, `between X and Y`, `from X to
  Y`, and suffix forms (`2000 or less`, `1000 and above`). Amounts accept `₹`, `Rs`,
  `Rs.`, `INR`, thousands separators, and `k` / `lakh` multipliers. The budget wording
  is stripped from the text before retrieval, so "up to ₹3,000" does not pollute the
  terms.
- **Mixed choices**: split on `and / or / , / & / plus`. A fragment that names no kind
  of product is folded into its neighbour, so `black and white dress` stays **one**
  item while `a dress and a bag` is **two**.
- **Complements**: inline cues (`with a matching bag`) and trailing cues
  (`... with a belt to go with them`, `... with a sarong to match`). A plain `with`
  only introduces an accessory when **both** sides name a product, so
  `a dress with a floral pattern` stays one item.
- **Per-item budgets**: `each` propagates a single stated budget to every choice, so
  `a red dress and black heels under INR 3000 each` constrains both. Different
  budgets per item are kept separate: `a dress under 2000 with a bag under 1000`.

### Retrieval and merging

Each primary intent is retrieved independently and the rankings are merged by
**deterministic round-robin**, so every choice gets an early slot in one pooled list
under one shared limit, deduplicated by catalog ID, stable for repeated requests and
prefix-consistent for pagination. Which intent contributed each row is tracked, so
the page is rechecked against **that intent's own** constraints.

Accessory intents are parsed out of the primary query and dropped entirely when
`include_recommendations` is false, exactly as the brief requires.

### Hard constraints from prose

A prose budget becomes a real filter, but **explicit API filters take precedence**: a
parsed budget only fills a bound the caller did not set. Colours, types and audiences
named in the query stay *ranking* signals rather than hard filters — a budget is
unambiguous, "white" is not (the catalog distinguishes `White` from `Off White`), and
the brief only requires unambiguous requirements to be enforced.

### Three real bugs found by writing the tests

1. **`< 2000` and `> 1000` never parsed.** The patterns began with `\b`, which does
   not match between a space and `<`. Replaced with `(?<!\w)`.
2. **A spelling guess could license an unrelated query.** `parts` is one edit from
   `pants`, so `spaceship engine parts` acquired a `product_type Trousers` anchor and
   started returning trousers. A corrected term now scores and counts as catalog
   evidence but **never anchors**, because an anchor lets a query bypass abstention.
3. **An anchored query was held to absolute coverage.** `a dress for my trip to
   Reykjavik` abstained at 1/3 coverage. With a named product type the anchor *is* the
   evidence, so `min_coverage` now applies only to unanchored queries.

### A better abstention rule

Phase 4 used "without an anchor, require 90 % coverage", which abstained on
`rainy day essentials` and `something to keep my hair out of my face`. The document
frequencies show the real discriminator:

| Query | Words the catalog cannot explain |
| --- | --- |
| `a titanium wedding suit of armour` | `titanium` 0, `armour` 0 |
| `spaceship engine parts` | `spaceship` 0 |
| `car tyres 205/55 R16` | `tyres` 0 |
| `fresh organic vegetables delivered` | `vegetables` 0, `delivered` 0 |
| `something to keep my hair out of my face` | none — `keep` 165, `out` 423, `face` 75 |
| `rainy day essentials` | none — `day` 35, `essentials` 4,938 |

So for an unanchored query: abstain if any requirement is **unexplained** (resolves to
no catalog attribute *and* every term has document frequency 0), and otherwise answer
only if some word resolved to a real attribute **or** every word was matched. That
last clause matters: `cotton top` names no attribute value but is fully accounted
for — and it is one of the five original starter tests, which this rule broke before
the clause was added.

Result: abstention is back to **1.0000** with false positives **0.0000**, while
`unusual` rose from 6.000 to 6.779 — stricter *and* more useful, rather than one at
the other's expense.

### Score movement

| Area | Max | Phase 3 | Phase 4 | Phase 5 | Dev | Held out |
| --- | --- | --- | --- | --- | --- | --- |
| Validity and constraints | 25 | 25.000 | 25.000 | **25.000** | 1.0000 | 1.0000 |
| Ranking relevance | 30 | 16.135 | 28.864 | **28.864** | 0.9453 | 1.0000 |
| Unusual (meaningful) | 7.5 | 0.000 | 6.000 | **6.779** | 1.0000 | 0.7598 |
| Irrelevant (abstention) | 7.5 | 7.500 | 6.875 | **7.500** | 1.0000 | 1.0000 |
| Mixed choices | 15 | 2.620 | 11.631 | **14.752** | 0.9752 | 1.0000 |
| Complementary recommendations | 10 | 1.000 | 2.299 | **5.214** | 0.5306 | 0.5000 |
| Performance | 5 | 0.000 | 0.000 | **5.000** | — | — |
| **Total (local estimate)** | 100 | 52.255 | 80.669 | **93.109** | 93.29 | 93.20 |

Diagnostics: nDCG@10 **0.9342**, mixed coverage **0.6111 → 1.0000**, mean validity
**1.0000**, contract pass rate **1.0000**, recommendation quality@5 still **0.1000**.

### Cost

| Metric | Phase 4 | Phase 5 |
| --- | --- | --- |
| Startup to healthy `/healthz` | 6.83 s | 6.83 s |
| RSS after startup | 1,103 MB | 1,103 MB |
| Empty-query pool p95, concurrency 1 | 15.4 ms | 16.4 ms |
| Empty-query pool p95, concurrency 5 | 61.0 ms | 58.9 ms |
| Pooled p95 | 55.7 ms | 53.5 ms |
| Tests | 92 | **146** |
| `smoke_test.py` | PASS | PASS |

Planning is pure string work on one short query, so it costs nothing measurable. The
empty-query pool never reaches the planner at all.

### One eval case was rewritten

`rec-10` was `"recommendations are disabled so only return the dress"` — an artificial
sentence that tested the harness rather than the contract. It is now
`"a dress for dinner with a matching bag"` with `include_recommendations: false`,
which is the brief's actual rule: ignore the complementary portion and return the
primary matches. Noted because rewriting an eval case while chasing a score is
exactly the kind of change that deserves to be visible.

### Still open after phase 5

1. **`recommend` is still a stub.** Complement scores 5.21/10 entirely from primary
   quality; recommendation quality@5 is 0.1000, and the 0.1 is only the one case that
   legitimately expects an empty list. Phase 6 is worth ~5 points.
2. `unusual` held out is 0.7598 against 1.0000 on dev — the widest dev/held-out gap
   left, and the area most exposed to my having written both the cases and the
   lexicon.
3. `eval/judgments.jsonl` is still empty: every grade is rubric-derived.

---

## Phase 6 — Complementary recommendations (2026-10-21)

Goal: implement `recommend`, which has been a stub since the starter.

Local weighted estimate: **93.11 → 97.15 / 100**. Dev **92.24**, held out **92.45**.
Recommendation quality@5 **0.1000 → 0.9079**, complement area **5.214 → 9.254 / 10**.

### Added

| Path | Purpose |
| --- | --- |
| `recommend.py` | Complement map loading, candidate scanning, scoring, reason text |
| `data/complements.json` | **Reviewable** complement map, role groups, neutrals, audience families |
| `tests/test_recommend.py` | 29 cases: contract invariants, substitutes, constraints, internals |
| `scripts/check_lexicon.py` (extended) | Validates complements too, and rejects substitutes |

### The actual problem: refusing to return substitutes

Similarity returns a second dress for a dress, and the brief is explicit that
substitutes earn nothing. So the candidate *type* is chosen first from a reviewed map
(116 types mapped, 516 pairings), and anything filling the same wardrobe slot as the
primary is excluded outright by a `roles` table of 24 groups. 14 types are declared to
have no useful complement at all (`Dog Wear`, `Zipper head`, `Sewing kit`, …) and
return an empty list rather than something forced.

Candidates are scanned per type in a precomputed **quality order** (rating, then review
count, then ID), so plausible items are considered first instead of walking an
11,000-product type. Scoring combines complement-type rank, colour compatibility
(same colour, or one of 12 catalog neutrals), shared pattern, shared audience, and
rating with review count.

### What the validator caught

Extending `scripts/check_lexicon.py` to the complement map surfaced **33 problems** on
the first run. Two were real errors in my map; the rest showed my **role groups were
too coarse**, treating genuine complements as substitutes:

| Flagged pairing | Verdict |
| --- | --- |
| `necklace` ↔ `earring` ↔ `bracelet` ↔ `ring` | **Role too coarse.** Different body slots, worn together. Split `jewellery` into `neck` / `ears` / `wrist` / `finger`. |
| `cardigan` ↔ `t-shirt` / `blouse` / `vest top` | **Role too coarse.** Layering, not substitution. Split `upper` into `upper_base` / `upper_layer`. |
| `skirt` ↔ `leggings/tights` | **Role too coarse.** Tights under a skirt. New `tights` role. |
| `robe` ↔ `pyjama set` / `night gown` | **Role too coarse.** A robe goes over pyjamas. Split `sleep` into `sleepwear` / `robe`. |
| `underdress` → `dress` | **Role too coarse.** Worn underneath. New `underlayer` role. |
| `watch` ↔ `bracelet` | **Genuine substitute** — same wrist. Pairing removed. |
| `nipple covers` → `bra` | **Genuine substitute** — worn instead of one. Pairing removed. |
| `no_complement: Interior textile` | **Real error** — that is a *category*, not a product type. Removed. |

The map now passes: every target exists in the catalog and no complement is a
substitute, checked both by the script and by a unit test.

### One design decision worth recording

The brief says explicit filters constrain the primary only, but "unqualified global
requirements such as an audience apply to recommendations too". Applying `audience`
as an **exact** value match broke the obvious case: a ladies' handbag is filed under
`Ladies Accessories`, not `Ladieswear`, so a `Ladieswear` filter threw away every bag
for a `Ladieswear` dress. Audience now carries over as a **family**
(ladies / men / divided / kids / baby / sport), which still blocks menswear for a
ladies' dress and babywear for an adult, but keeps the accessory sections reachable.

Split of what carries over to recommendations:

| Carries over | Does not |
| --- | --- |
| `audience` (by family), `min_price`, `max_price`, `min_rating`, `min_review_count` | `category`, `product_type`, `color`, `pattern`, `section` |

An accessory budget stated in the query (`a dress under INR 2000 with a bag under
INR 1000`) replaces an inherited one.

When the shopper names the accessory, all five slots are filled with that type rather
than spread for variety they did not ask for; when they do not, the ranked complement
list spreads across types (a dress gets a bag, heeled sandals, a blazer).

### Reasons

Built only from catalog attributes, 1–300 characters, never invented:

> *Black bag to wear with the black dress; the same colour. Rated 5.0 from 4 reviews.*
> *Beige bag to wear with the black dress; beige is a neutral. Rated 5.0 from 5 reviews.*

Reasons are not machine-graded, so they exist for the reader, not the score.

### Score movement

| Area | Max | Phase 4 | Phase 5 | Phase 6 | Dev | Held out |
| --- | --- | --- | --- | --- | --- | --- |
| Validity and constraints | 25 | 25.000 | 25.000 | **25.000** | 1.0000 | 1.0000 |
| Ranking relevance | 30 | 28.864 | 28.864 | **28.864** | 0.9453 | 1.0000 |
| Unusual (meaningful) | 7.5 | 6.000 | 6.779 | **6.779** | 1.0000 | 0.7598 |
| Irrelevant (abstention) | 7.5 | 6.875 | 7.500 | **7.500** | 1.0000 | 1.0000 |
| Mixed choices | 15 | 11.631 | 14.752 | **14.752** | 0.9752 | 1.0000 |
| Complementary recommendations | 10 | 2.299 | 5.214 | **9.254** | 0.9253 | 0.9255 |
| Performance | 5 | 0.000 | 5.000 | **5.000** | — | — |
| **Total (local estimate)** | 100 | 80.669 | 93.109 | **97.149** | 97.24 | 97.45 |

Recommendation quality@5: **0.9079** overall, 0.9322 dev, 0.8510 held out. Zero failing
cases; contract pass rate 1.0000.

### Cost

| Metric | Phase 5 | Phase 6 |
| --- | --- | --- |
| Startup to healthy `/healthz` | 6.83 s | 6.83 s |
| RSS after startup | 1,103 MB | 1,103 MB |
| Pooled empty-query p95 | 53.5 ms | 58.4 ms |
| Concurrency 1 p95 | 16.4 ms | 17.0 ms |
| Concurrency 5 p95 | 58.9 ms | 62.3 ms |
| Tests | 146 | **176** |
| `smoke_test.py` | PASS | PASS |

The quality order and category expansion are built once at load and cost nothing
measurable at startup; the empty-query pool never requests recommendations, so the
p95 movement here is run-to-run noise rather than a real regression.

### Still open after phase 6

1. **`eval/judgments.jsonl` is still empty.** Every grade is rubric-derived, and the
   complement rubric is the most generous of them: it awards grade 3 for being a
   complementary *type*, where Bedrock will judge whether that specific bag suits that
   specific dress. Expect the real complement score to be lower than 9.25.
2. `data/complements.json` is 516 judgements I made, not catalog facts. The roadmap
   asks for a fashion-eye review and it has not had one.
3. `unusual` held out (0.7598) against dev (1.0000) is still the widest split gap.
4. Phases 7 (gated embeddings), 8 (optional LLM parser), 9 (acceptance, Docker,
   `REPORT.md`) and 10 (deploy) remain.

---

## Phase 6b — Three ranking defects found by reading the per-case numbers (2026-10-21)

Before starting phase 7 I checked where the remaining loss actually was. **51 of 59
relevance cases were already at the perfect nDCG ceiling**, and the eight that were
not pointed at real defects rather than rubric artefacts.

Local weighted estimate: **97.15 → 98.46 / 100**. Dev **93.42**, held out **93.69**.

| Case | Before | Diagnosis |
| --- | --- | --- |
| `pag-05` | nDCG 0.029 | `dress` selected 10,585 products, of which **267 were not dresses** — tops and blouses whose *names* contain "dress" (`LOGG Olivia dress` is a `Top`). They scored ~2.2 against a real dress's ~12, but the relative `score_floor` of 0.15 let them through. Sorting that page by price ascending surfaced the cheap impostors first. |
| `typo-01` | nDCG 0.359 | `blak dres` returned two dresses then a belt, an umbrella and a **side table**. `correct('dres')` returned `None`. |
| `mix-01` | nDCG 0.881 | `white t-shirts` resolved to `product_type Shirt`, not `T-shirt`. |

### 1. Anchor dominance

Once a query names a kind of product and the catalog holds enough of it, products that
merely *mention* the word are dropped. `dress` now selects exactly **10,318** products,
all of them `Dress`, and the price-ascending page at offset 10 is all dresses.

Guarded by `anchor_dominance_min` (20): if too few products carry the anchor, the rule
does not fire, so a narrow query keeps its tail.

### 2. Spelling correction, third attempt

`typo_min_df` was 8, but **the catalog contains truncated style names**: `dres` appears
19 times (`EQ Terriyaki dres`, `RR 35 5.Fit and flare tee dres`), so rarity alone said
"spelled correctly". This is the third variant of the same lesson — phase 4 found
`sunglases` at df 1, and phase 5 found that a correction must never anchor.

Correction now also fires when a near neighbour **dominates** the term:
`typo_df_ratio` 20, and `dress` at 7,877 against `dres` at 19 is a ratio of 415. Two
further guards came out of writing the tests:

- the call was still **gated** on `typo_min_df` before reaching `correct()`, which is
  what actually hid `dres`. `correct()` now decides for itself.
- `correct('cotton')` returned **`button`**. A term already in the reviewed vocabulary
  is spelled correctly by construction, so those are never corrected.

All eight typo cases now land on the right product type:

| Query | Top types |
| --- | --- |
| `blak dres` | Dress, Dress, Dress, Dress |
| `snakers` | Sneakers ×4 |
| `swetar` | Sweater ×4 |
| `tshart` | T-shirt ×4 |
| `troussers` | Trousers ×4 |
| `sunglases` | Sunglasses ×4 |
| `neklace` | Necklace ×4 |
| `hoddie for men` | Hoodie ×4 |

### 3. Conservative plural matching

`t-shirts` tokenises to `['t', 'shirts']`, which does not equal the `T-shirt` value
tokens `['t', 'shirt']`, so the single-token synonym `shirts → Shirt` won and anchor
dominance then excluded every actual T-shirt. Catalog values are now matched against
the window with its head noun de-pluralised as well.

Writing the test found the rule was wrong: `singular('dresses')` returned `dresse`.
It now handles `sses/shes/ches/xes/zes`, `ies → y`, `es`, and bare `s`, while leaving
`dress` alone.

`black jeans and white t-shirts` now interleaves black denim Trousers with white
T-shirts, which is what the brief's own example asks for.

### Score movement

| Area | Max | Phase 6 | Phase 6b | Dev | Held out |
| --- | --- | --- | --- | --- | --- |
| Validity and constraints | 25 | 25.000 | **25.000** | 1.0000 | 1.0000 |
| Ranking relevance | 30 | 28.864 | **29.604** | 0.9868 | 0.9868 |
| Unusual (meaningful) | 7.5 | 6.779 | **7.272** | 1.0000 | 0.9091 |
| Irrelevant (abstention) | 7.5 | 7.500 | **7.500** | 1.0000 | 1.0000 |
| Mixed choices | 15 | 14.752 | **14.827** | 1.0000 | 1.0000 |
| Complementary recommendations | 10 | 9.254 | **9.254** | 0.9253 | 0.9255 |
| Performance | 5 | 5.000 | **5.000** | — | — |
| **Total (local estimate)** | 100 | 97.149 | **98.457** | 98.42 | 98.69 |

nDCG@10 **0.9342 → 0.9747**, mixed coverage 1.0000, abstention 1.0000, false positives
0.0000, contract pass rate 1.0000, 186 tests, smoke test PASS, pooled p95 60.6 ms,
startup 6.8 s, RSS 1,103 MB.

### What this means for phase 7

The local suite is now **86 % saturated**: 51 of 59 relevance cases sit at the fixed
grade-3 ceiling, and held-out ranking is 0.9868. Phase 7's gate is "keep embeddings
only if held-out ranking/unusual nDCG improves meaningfully" — there is almost no
headroom left for that gate to measure, which is itself a finding about the eval
rather than about embeddings.

---

## Phase 8 — Optional LLM query parser, off by default (2026-10-21)

The roadmap gates this phase on "held-out results still show weak vague, synonym or
mixed queries **and** you're willing to use an API key". Held-out synonym and mixed
are both at ceiling and no API credentials were available here, so it is implemented
**off by default and unexercised against a live endpoint** rather than skipped: the
code, the safety envelope and the fallback path are in place and tested, and turning
it on is one environment variable.

**No change to the serving path.** `requirements.txt` is untouched, the `anthropic`
SDK is imported lazily, and with `LLM_PARSER` unset the model is never constructed.

### What the model is allowed to do

Only segment the query and read budgets out of it:

```json
{"primary":     [{"text": "...", "min_price": n|null, "max_price": n|null}],
 "accessories": [{"text": "...", "min_price": n|null, "max_price": n|null}]}
```

The response schema has `additionalProperties: false` and **no attribute fields at
all** — the model cannot name a colour, a product type or an ID, so it cannot invent
one. On top of that, **every token of every returned segment must already appear in
the query**; a segment containing a word the shopper did not write is dropped. The
segments then go through the unchanged deterministic pipeline, which only ever matches
real catalog values, and explicit API filters are applied afterwards exactly as
before. A model answer cannot widen a filter, relax a budget or fabricate a product.

### Fallback, which is the normal path

Rule-based planning runs first, always. The model result replaces it only if every
check passes. Falls back on: the flag being off, a missing SDK, missing credentials,
a timeout, a transport or API error, malformed JSON, a schema violation, a
`refusal` or `max_tokens` stop reason, invented tokens, or nothing surviving
validation. A call is skipped entirely for a blank query, a query the rules already
reduce to nothing, and a single-word query — there is nothing to segment.

Deadline 2.5 s per the roadmap, `max_retries=0` so the deadline is the deadline, and
an LRU cache keyed on the query so the benchmark's repeated pagination probes cost one
call, not three.

### Configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `LLM_PARSER` | *(off)* | `1`/`true`/`yes`/`on` enables it |
| `LLM_PARSER_BACKEND` | `api` | `api` (Claude API) or `bedrock` (Amazon Bedrock) |
| `LLM_PARSER_MODEL` | `claude-opus-5-5` | Bedrock ids get an `anthropic.` prefix automatically |
| `LLM_PARSER_REGION` | — | AWS region, `bedrock` backend only |
| `LLM_PARSER_TIMEOUT` | `2.5` | seconds |
| `LLM_PARSER_EFFORT` | `low` | `output_config.effort` |
| `LLM_PARSER_CACHE` | `512` | LRU entries |

A Bedrock backend is offered because the deployment is already on AWS, which avoids a
second credential path.

**One thing to decide before enabling it:** the default model is `claude-opus-5-5`,
and a 2.5-second deadline may be tight for it. `LLM_PARSER_MODEL=claude-haiku-5-5` is
the cheap, fast alternative, but that is a cost/quality decision for the team rather
than one to make silently — measure both with `scripts/evaluate.py` before deploying
either.

### Tests

35 cases in `tests/test_llm_planner.py`, with the SDK call replaced by a stub so no
network or credentials are needed: default-off, truthy/falsy flag parsing, malformed
numeric config, every structurally invalid answer shape, over-long and over-numerous
segments, nonsense and inverted budgets, invented-token rejection, prompt-injection
attempts, a model-supplied `color`/`product_type` field being discarded, timeout and
exception fallback, refusal and truncation fallback, unavailable client, call-skipping
guards, request shape (schema, deadline, effort), Bedrock prefixing, caching, and
`Catalog` leaving it off by default.

Writing them caught two bugs in the tests themselves (a missing `plan()` call that
made a fallback assertion vacuous, and a "spends no call" case whose premise was
wrong) and turned the third guard into a real one: a single-word query now skips the
call, which it previously did not.

Total suite: **221 tests**, all passing.

### Honest limitation

The on-path has never run against a real endpoint from this machine. What is proven
is that the system behaves exactly as before with the flag off, and that every
failure and malicious-answer path falls back safely. Before enabling it in
deployment, the roadmap's own bar applies: it must clearly win on held out with zero
timeouts across three repeated runs.

---

## Phase 7 — Gated embeddings: NOT DECIDED (2026-10-21)

Status: **experiment built and running, gate not applied.** Nothing entered the
serving path — `requirements.txt` is untouched and `fastembed` lives only in the
gitignored `.venv-embed`.

### What was measured

| Metric | Value |
| --- | --- |
| Model | `BAAI/bge-small-en-v1.5` via fastembed 0.9.0 (ONNX, no torch) |
| Dimension / dtype | 384, float16, L2-normalised |
| Throughput | **39 docs/s** single-threaded, 51–62 docs/s at 12 threads |
| Full build time | **~30–45 minutes** for 105,126 documents |
| Vector size | 2.3 MB per 3,000 docs → **~80 MB** for the catalog |
| Process peak RSS during build | **2.4 GB** |
| Model load | 0.2 s warm, 9.4 s cold (first download ~7 s) |
| Experiment venv size | 207 MB (onnxruntime, numpy, tokenizers, huggingface-hub) |

A 3,000-document and a 12,000-document build both completed and produced valid
vectors; the full build was at 20,480/105,126 at handoff.

### Why the gate was not applied

Two reasons, and the first is the honest one:

1. **This environment reaps long-running background jobs.** Three attempts to build
   the full catalog detached were killed. `--offset/--limit/--shard` were added to
   `scripts/build_embeddings.py` so the build can be done in foreground chunks, which
   is how the next run should do it.
2. **The gate has almost nothing left to measure.** It asks to keep embeddings only
   if *held-out* ranking/unusual nDCG improves meaningfully. After phase 6b, held-out
   ranking is **0.9868** and **51 of 59 relevance cases sit at the fixed grade-3
   ceiling**. A 4-case held-out `unusual` slice cannot establish "meaningful".

That saturation is a finding about the eval, not about embeddings. The measurement
that would actually be informative is the opposite one: whether dense retrieval
*damages* abstention, which is currently 1.0000 with 0.0000 false positives.
`AGENT.md` warns that nearest-neighbour search returns neighbours for nonsense, so
`scripts/spike_dense.py` sweeps a similarity threshold over the irrelevant cases
specifically.

### What the next run should do

```sh
.venv-embed/bin/python -u scripts/build_embeddings.py --threads 12   # to completion
.venv-embed/bin/python scripts/spike_dense.py
```

Then apply the gate: keep only if held-out ranking/unusual improves meaningfully,
abstention does not regress, RSS and startup fit (< 3 GB, < 90 s), and text-query p95
stays under 1 s. Otherwise delete `.venv-embed/`, `analysis/embeddings/`,
`scripts/build_embeddings.py` and `scripts/spike_dense.py` — there is nothing else to
unwind.

---

## Phase 9 — Acceptance (2026-10-21)

### Results

| Check | Result |
| --- | --- |
| `python -m unittest discover -s tests` | **221 tests OK** (9.1 s) |
| Original five starter fixtures | pass, unchanged |
| `smoke_test.py` | **PASS** |
| `scripts/check_lexicon.py` | **PASS** — every lexicon and complement target exists; no complement is a substitute |
| Catalog SHA-256 | **matches** `05b6aa5e…c8f073` |
| `scripts/evaluate.py --manage` (104 cases) | **98.36 / 100**, zero failing cases, contract pass rate 1.0000 |
| Validity / nDCG@10 / abstention | 1.0000 / 0.9747 / 1.0000 |
| Page stability across repeated requests | 50/50 pool pages identical |
| `GET /mcp` → 405, `GET /healthz` → 200 | verified |

### A measurement caveat worth recording

The acceptance bench reported pooled p95 **157 ms** and the eval reported **234 ms**,
against 60.6 ms on an idle machine. The cause was the phase 7 embedding build running
beside them at 1,020 % CPU, load average 23.8. The idle figures stand; the lesson is
that the latency pool must be run on a quiet machine, which `scripts/preflight.sh`
now says explicitly. Startup also inflated 6.8 s → 16.1 s under the same load.

### Added

| Path | Purpose |
| --- | --- |
| `scripts/preflight.sh` | Pre-submission gate: catalog SHA, tests, validators, smoke test, 405 contract, latency pool, full case suite. Exits non-zero on the first failure. |
| `REPORT.md` | Changed files, architecture, before/after, rejected experiments, limitations, deploy requirements, verification commands |
| `HANDOFF.md` | Context for another assistant to continue, including the two open decisions |

### Not done, and why

**The Docker image was never built.** No reachable Docker daemon and no passwordless
sudo on this machine. The Dockerfile is unchanged from the starter and verified by
inspection only: `python:3.12-slim`, non-root uid 10001, `/healthz` health check with
a 60 s start period (startup is ~7 s), `COPY . .` after the tightened
`.dockerignore`, and `CMD ["python", "server.py"]`. The build and container test must
be run by the team — ideally on the deploy VM, where it matters:

```sh
docker build --platform linux/amd64 -t polaris .
docker run --rm -p 8000:8000 -e MCP_ALLOWED_HOSTS='localhost:*,127.0.0.1:*' polaris
.venv/bin/python smoke_test.py http://127.0.0.1:8000/mcp
```
