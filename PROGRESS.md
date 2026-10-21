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
