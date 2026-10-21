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
