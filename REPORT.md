# Polaris product search — implementation report

A working `search_products` MCP server built on the starter, phases 1–9 of
`POLARIS_ROADMAP.md`. Per-phase measurements and decisions are in `PROGRESS.md`;
this is the summary a reviewer or deployer needs.

**No official score is claimed here.** Every number below is a local measurement.
Relevance grades come from a reviewable rubric in `scripts/build_cases.py`, not from
the organizer's Bedrock judge or hidden suite. See *Limitations*.

---

## 1. What changed

### Modified

| File | Change |
| --- | --- |
| `search.py` | Rewritten into explicit stages; facet/numeric index; multi-intent retrieval and round-robin merge; recommendations wired in |
| `server.py` | One line: `/mcp` routes `POST` and `DELETE` only, so a standalone `GET` returns 405 as the brief requires |
| `tests/test_search.py` | The five original fixtures are **untouched**; 45 cases added below them |
| `.dockerignore` | Excludes the experiment venv, measurements, tests and spike scripts |
| `.gitignore` | Excludes the experiment venv, vectors and logs |

### Added — serving path

| File | Purpose |
| --- | --- |
| `retrieval.py` | Field-aware BM25F index, query planner, spelling correction, scorer, abstention |
| `query_planner.py` | Budget parsing, mixed-choice splitting, complement cues |
| `recommend.py` | Complement selection, scoring, reason text |
| `llm_planner.py` | Optional LLM segmentation, **off** unless `LLM_PARSER=1` |
| `data/lexicon.json` | Reviewable shopper-language → catalog-value map |
| `data/complements.json` | Reviewable complement map, role groups, neutrals, audience families |

### Added — tooling and tests

| File | Purpose |
| --- | --- |
| `scripts/catalog_stats.py` | Catalog summariser → `analysis/catalog_stats.json` |
| `scripts/check_lexicon.py` | Fails if a lexicon/complement target names no catalog value, or if a complement is a substitute |
| `scripts/build_cases.py` | Source of the 104-case eval set → `eval/cases.jsonl` |
| `scripts/evalkit.py` | Selectors, rubric grading, nDCG |
| `scripts/evaluate.py` | Runs every case over `/mcp` and scores it |
| `scripts/label.py` | Interactive 0–3 human labelling → `eval/judgments.jsonl` |
| `scripts/bench.py` | Real MCP-client latency pool, startup and RSS |
| `scripts/tune.py` | Dev-only parameter sweep, held out reported separately |
| `scripts/preflight.sh` | Pre-submission gate against a deployed URL |
| `scripts/spike_fts5.py`, `scripts/spike_dense.py`, `scripts/build_embeddings.py` | Rejected/undecided experiments, kept as evidence |
| `tests/` | 221 tests across search, protocol, retrieval, planner, recommendations, LLM parser |

The tool name, protocol revision `2025-11-25`, `schemas/*.json`, `smoke_test.py` and
the catalog are unchanged. Catalog SHA-256 verified:
`05b6aa5e8e8c1c8c997d584f8288e432862454f8820531e13473443e83c8f073`.

---

## 2. Architecture

```
tools/call search_products
  |
  parse_request          validate against the published schema, then apply defaults
  |                      ourselves (JSON Schema defaults are annotations)
  plan_shopping          query -> primary intents + accessory intents + per-item
  |                      budgets. Rules only; the LLM wrapper is off by default
  |
  per primary intent:
  |   score_all          BM25F over 5 field groups + catalog-attribute boosts,
  |   |                  requirement coverage, anchor dominance, abstention
  |   acceptor           explicit filters AND the intent's prose budget
  |
  round-robin merge      one pooled ranked list, one shared limit, deduplicated
  |
  order + paginate       field sorts with ID-ascending ties, null ratings last
  |
  recheck                every returned row re-verified against its own intent's
  |                      constraints, on the original catalog record
  recommend              complement type from the reviewed map, never a substitute
```

Built once at load: facet postings, numeric columns, eight presorted orders, the
BM25F inverted index, per-type quality orders, and category→type expansion.

**Three design choices that mattered most**, each forced by a measurement rather than
a preference:

1. **Attributes carry relevance, not names.** Catalog names are internal style codes
   (`SWEATSHIRT OC`, `tvp`, `Mimmi sneaker`). "sneakers" appears in 13 names but
   1,590 products are `product_type = Sneakers`. `product_type` is weighted 9× the
   description.
2. **Synonyms anchor a query; intents never do.** Letting occasion words anchor made
   "a titanium wedding suit of armour" return dresses. Abstention instead turns on
   whether the catalog can explain the query at all — unexplained words have document
   frequency 0 (`titanium`, `armour`, `tyres`), while meaningful queries use
   rare-but-present words (`face` 75, `day` 35).
3. **Complements are chosen by type first, then ranked.** Similarity returns
   substitutes, which the brief says earn nothing. A 24-group role table excludes
   anything filling the same wardrobe slot as the primary.

---

## 3. Before and after

Local weighted estimate, using the official 25/30/15/15/10/5 mix:

| Area | Max | Starter (phase 3) | Final | Dev | Held out |
| --- | --- | --- | --- | --- | --- |
| Validity and constraints | 25 | 25.000 | **25.000** | 1.0000 | 1.0000 |
| Ranking relevance | 30 | 16.135 | **29.604** | 0.9868 | 0.9868 |
| Unusual (meaningful) | 7.5 | 0.000 | **7.272** | 1.0000 | 0.9091 |
| Irrelevant (abstention) | 7.5 | 7.500 | **7.500** | 1.0000 | 1.0000 |
| Mixed choices | 15 | 2.620 | **14.827** | 1.0000 | 1.0000 |
| Complementary recommendations | 10 | 1.000 | **9.254** | 0.9253 | 0.9255 |
| Performance | 5 | 0.000 | **5.000** | — | — |
| **Total** | 100 | 52.255 | **98.457** | 98.42 | 98.69 |

Diagnostics: nDCG@10 **0.2951 → 0.9747**, mixed coverage **0.1528 → 1.0000**,
recommendation quality@5 **0 → 0.9079**, abstention **1.0000** with false positives
**0.0000**, mean validity **1.0000**, contract pass rate **1.0000**, zero failing
cases.

Dev and held out are within 0.3 points of each other, which is the main reason to
believe the gains are retrieval rather than fitting.

### Latency, startup, memory

| Metric | Starter | Final |
| --- | --- | --- |
| Empty-query pool p95, concurrency 1 | 86.7 ms | **16.9 ms** |
| Empty-query pool p95, concurrency 5 | 557.5 ms | **64.1 ms** |
| Pooled p95 (200 calls) | 491.4 ms | **60.6 ms** |
| In-process empty-query search | 40.9 ms | **0.01 ms** |
| Startup to healthy `/healthz` | 2.63 s | **6.8 s** |
| RSS after startup | 777 MB | **1,103 MB** |
| Errors across the pool | 0 | **0** |

Measure the pool on an **idle** machine. The phase 9 acceptance run reported
p95 157 ms only because a 12-thread embedding experiment was running beside it
(load average 23.8); the 60.6 ms figure is from an idle run.

---

## 4. Experiments that were rejected

Recorded because the cost of a wrong "keep" is paid at every request.

| Experiment | Result | Decision |
| --- | --- | --- |
| **SQLite FTS5** instead of in-process BM25F | nDCG 0.5353 vs 0.5570; builds 4.8× faster; p95 **worse** (18.9 vs 14.5 ms); 54 MB database | **Rejected.** `bm25()` returns one opaque score, so it cannot express attribute boosts or requirement coverage — where the gain actually is. |
| **Coordinate-descent parameter tuning** | dev 54.15 → 57.57, held out 55.41 → **55.01** | **Rejected.** Only the three axes that help or are neutral on both splits were adopted, worth +2 dev and **0** held out. |
| **Dense retrieval** (fastembed, `bge-small-en-v1.5`) | **Undecided — see Limitations** | Nothing added to the serving path; `requirements.txt` untouched. |

---

## 5. Limitations

Ordered by how much they should affect confidence.

1. **Every relevance grade is rubric-derived.** `eval/judgments.jsonl` is empty. The
   rubric rewards finding the right *kind* of product and cannot see whether a
   specific garment suits the request; Bedrock will. `scripts/label.py --manage` is
   the outstanding manual job and the single highest-value thing left to do.
2. **I wrote both the eval cases and the lexicon they are scored against**, including
   the held-out split. The `unusual` and `synonym` numbers are therefore partly
   self-confirming. Treat `unusual` as the least trustworthy row.
3. **The complement rubric is the most generous of them.** It awards grade 3 for being
   a complementary *type*. Expect the real complement score below 9.25.
4. **`data/complements.json` is 516 judgements, not catalog facts**, and has not had
   the fashion-eye review the roadmap asks for. `scripts/check_lexicon.py` proves
   every value exists and that no complement is a substitute; it cannot tell you
   whether a scarf suits a swimsuit.
5. **Phase 7 (dense retrieval) is undecided.** The full-catalog build is ~44 minutes
   at the measured 39–62 docs/s and could not be completed in this environment.
   Measured so far: 384-dim float16, ~80 MB of vectors for the catalog, process peak
   RSS 2.4 GB during the build, model load 0.2–9.4 s. The gate also has little room
   to measure: held-out ranking is already 0.9868. See *Next steps*.
6. **The LLM parser's on-path has never run against a live endpoint.** Only the
   off-path and every failure path are tested. It is off by default.
7. **The Docker image was never built here** — no reachable Docker daemon and no
   passwordless sudo. The Dockerfile is unchanged from the starter and verified by
   inspection only.
8. **The local suite is ~86 % saturated**: 51 of 59 relevance cases sit at the fixed
   grade-3 ceiling, so it has limited power to detect further improvement.

---

## 6. Deployment requirements

- **Python 3.12**, `requirements.txt` exactly as pinned (MCP SDK 1.30.0, v1 line).
- **RAM: 2 GB minimum, 4 GB recommended.** Steady state is ~1.1 GB resident.
- **Disk:** ~250 MB plus the 106 MB catalog.
- **No network at serving time.** No model downloads, no API calls, no database.
  `requirements-llm.txt` is optional and only needed if `LLM_PARSER=1`.
- **Startup ~7 s** before `/healthz` reports ready. Wait for it before submitting.
- `MCP_ALLOWED_HOSTS` **must** include the public host, or the SDK rejects the Host
  header:

```sh
docker build --platform linux/amd64 -t polaris .
docker run -d --restart unless-stopped --name polaris -p 8000:8000 \
  -e MCP_ALLOWED_HOSTS='localhost:*,127.0.0.1:*,<PUBLIC_IP>:*,<PUBLIC_IP>' polaris
```

- Submit `http://<PUBLIC_IP>:8000/mcp` — not `localhost`, not `/healthz`, not the repo.
- The image runs as non-root (uid 10001) with a `/healthz` health check, and
  `.dockerignore` keeps the experiment venv, measurements and tests out.

---

## 7. Verification commands

```sh
# everything, against a deployed endpoint; exits non-zero on the first failure
scripts/preflight.sh http://<PUBLIC_IP>:8000/mcp

# individually
.venv/bin/python -m unittest discover -s tests -v          # 221 tests
.venv/bin/python scripts/check_lexicon.py                  # lexicon + complements
.venv/bin/python server.py                                 # then, in another shell:
.venv/bin/python smoke_test.py http://127.0.0.1:8000/mcp
.venv/bin/python scripts/bench.py --manage --smoke         # latency, startup, RSS
.venv/bin/python scripts/evaluate.py --manage              # 104 cases, dev + held out
.venv/bin/python scripts/catalog_stats.py                  # regenerate catalog summary
.venv/bin/python scripts/build_cases.py                    # regenerate eval/cases.jsonl

# the outstanding manual job
.venv/bin/python scripts/label.py --manage --skip-labelled

# HTTP method contract
curl -s -o /dev/null -w '%{http_code}\n' -X GET http://127.0.0.1:8000/mcp   # 405
curl -s http://127.0.0.1:8000/healthz                                        # 200
```

Run the latency pool on an idle machine.

---

## 8. Next steps

1. **Label the eval cases** (`scripts/label.py`). Until then the headline number is a
   proxy, not evidence.
2. **Review `data/complements.json` and `data/lexicon.json`** with a fashion eye.
3. **Finish or drop Phase 7.** To finish: run
   `.venv-embed/bin/python -u scripts/build_embeddings.py --threads 12` to completion
   (~44 min; it needs a machine that will not reap it, and `--offset/--limit/--shard`
   exist to do it in chunks), then
   `.venv-embed/bin/python scripts/spike_dense.py` and apply the gate — keep only if
   held-out ranking/unusual improves meaningfully, abstention does not regress, RSS
   and startup fit, and text-query p95 stays under 1 s. Otherwise delete
   `.venv-embed`, `analysis/embeddings/`, `scripts/build_embeddings.py` and
   `scripts/spike_dense.py`.
4. **Build and test the container**, then deploy and run `scripts/preflight.sh`
   against the public URL until green.
5. **Resolve the divergence with the parallel implementation** on `origin/main`
   (see `HANDOFF.md`).
