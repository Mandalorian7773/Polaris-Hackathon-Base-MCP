# Handoff context for another AI tool

Everything a fresh assistant needs to continue this work. Read this, then `CLAUDE.md`
(the contract and working rules), then `PROGRESS.md` (per-phase measurements) and
`REPORT.md` (the summary). `POLARIS_ROADMAP.md` is the original plan.

---

## 1. Where things stand

**Phases 1–6b, 8 and 9 are done and committed. Phase 7 is in progress. Phase 10
(deploy) is manual and not started.**

Local weighted estimate **98.46 / 100** (functional 93.46 / 95, performance 5 / 5),
dev 98.42 and held out 98.69, zero failing cases, 221 tests passing.
**This is not an official score** — see §5.

| Phase | State | Commit |
| --- | --- | --- |
| 1 Baseline + CLAUDE.md | done | `2cb7637` |
| 2 Contract correctness + index | done | `5b452a1` |
| 3 Evaluation harness | done | `1773e1e` |
| 4 Lexical retrieval (BM25F) | done | `f629168` |
| 5 Shopping-intent planning | done | `0f14aac` |
| 6 Complementary recommendations | done | `d546880` |
| 6b Ranking defect fixes | done | `245111d` |
| 7 Gated embeddings | **experiment tooling committed, decision NOT made** | `a3705f8` |
| 8 Optional LLM parser (off by default) | done | `c74a8f9` |
| 9 Acceptance, preflight, REPORT.md | done | this commit |
| 10 Deploy | **not started, manual** | — |

---

## 2. Two things that need a human decision

### 2a. A teammate has a parallel implementation on `origin/main`

`origin/main` is at `1383cf1`, two commits by **Aditya Jagrani** pushed 2026-10-21
17:58–17:59, which independently rewrote `search.py` (+712 lines) and
`tests/test_search.py` from the same base (`e1cbc5f`) and added `evaluation/`,
`protocol_acceptance_test.py`.

**All work described here lives on the branch `origin/polaris-phases-1-5` instead.**
`main` was deliberately never touched. The two lines conflict structurally, not
textually: Aditya's is one monolithic `search.py`; this one splits into
`search.py` + `retrieval.py` + `query_planner.py` + `recommend.py`. Merging means
choosing an architecture, which is the team's call, not an assistant's.

Notable difference: Aditya's version implements `recommend()`; so does this one now
(phase 6). Both built their own evaluation harness independently.

A read-only way to make this decision on evidence: check out `origin/main`, run this
repo's `scripts/evaluate.py` against *their* `search.py`, and compare both on the same
104 cases.

### 2b. Phase 7 is undecided and a build is probably still running

At handoff a full-catalog embedding build was running:

```
.venv-embed/bin/python -u scripts/build_embeddings.py --threads 12
# log: analysis/embed_run.log   last seen 20,480 / 105,126 at 51 docs/s
```

Measured so far: 384-dim float16, ~80 MB of vectors for the full catalog, ~39–62
docs/s so the build is ~30–45 minutes, process peak RSS 2.4 GB during the build,
model load 0.2–9.4 s.

To finish: let it complete (or redo it in chunks with `--offset/--limit/--shard`,
which exist for exactly that), then run
`.venv-embed/bin/python scripts/spike_dense.py` and apply the roadmap's gate — keep
only if **held-out** ranking/unusual nDCG improves meaningfully, abstention does not
regress, RSS and startup fit (< 3 GB, < 90 s), and text-query p95 stays under 1 s.

**Expect to reject it**, and say so with numbers either way. Held-out ranking is
already 0.9868 and 51 of 59 relevance cases sit at the fixed grade-3 ceiling, so the
gate has very little room to measure an improvement. `AGENT.md` also warns that
similarity search returns neighbours for nonsense — the interesting measurement is
whether dense retrieval *damages* abstention, which is currently 1.0000.

If rejected, delete `.venv-embed/`, `analysis/embeddings/`,
`scripts/build_embeddings.py` and `scripts/spike_dense.py`. Nothing was added to
`requirements.txt`, so there is nothing else to unwind.

---

## 3. Environment and commands

```sh
cd ~/projects/polaris
.venv/bin/python …          # production interpreter, Python 3.12, pinned
.venv-embed/bin/python …    # phase 7 experiment only (fastembed), gitignored
```

- `.venv` has **no pip**; use `uv pip install --python .venv …`.
- No Docker daemon and no passwordless sudo on this machine: the image has never been
  built. `docker` is installed.
- PyPI and huggingface.co are reachable. No Anthropic API credentials are configured.
- **Backgrounded long jobs get reaped here.** Run long builds in the foreground or in
  chunks. Also: `pgrep -f <pattern>` matches the shell running your own command, so
  process checks look like false positives — grep for the interpreter path instead
  (`ps -eo pid,cmd | grep '[v]env-embed/bin/python'`).

```sh
.venv/bin/python -m unittest discover -s tests          # 221 tests, ~9 s
.venv/bin/python scripts/check_lexicon.py               # lexicon + complements
.venv/bin/python scripts/evaluate.py --manage           # 104 cases, dev + held out
.venv/bin/python scripts/bench.py --manage --smoke      # latency, startup, RSS
.venv/bin/python scripts/tune.py --sweep axis=v1,v2     # dev sweep, held out reported
scripts/preflight.sh <URL>                              # everything, against a URL
```

Run the latency pool on an **idle** machine; a background job inflated one acceptance
run from 60 ms to 157 ms p95.

---

## 4. Hard rules (also in `CLAUDE.md`)

- **Never read `data/products.json` directly** (106 MB). Inspect only via scripts that
  print summaries.
- **Never modify the catalog.** SHA-256 must stay
  `05b6aa5e8e8c1c8c997d584f8288e432862454f8820531e13473443e83c8f073`.
- **Never change** the tool name `search_products`, protocol revision `2025-11-25`, or
  the structure of `schemas/*.json`.
- **The five original tests in `tests/test_search.py` and `smoke_test.py` must keep
  passing, unchanged.** Added cases go below the originals. One of those five caught a
  real regression during phase 5 — do not weaken them.
- Explicit API filters are hard constraints and beat conflicting query wording.
- Log every phase result in `PROGRESS.md`; never claim an official score.

---

## 5. What to distrust in the numbers

State these plainly rather than repeating the headline figure.

1. **`eval/judgments.jsonl` is empty.** Every relevance grade comes from the rubric in
   `scripts/build_cases.py`. It rewards finding the right *kind* of product and cannot
   judge whether a specific garment suits the request. Bedrock will.
   **`scripts/label.py --manage --skip-labelled` is the highest-value task left** and
   needs a human (1–2 hours).
2. **The same author wrote the eval cases and the lexicon they are scored against**,
   including the held-out split, so `unusual` and `synonym` are partly
   self-confirming.
3. **The complement rubric is the most generous**: grade 3 for being a complementary
   *type*. Expect the real complement score below 9.25.
4. **The suite is ~86 % saturated** (51/59 relevance cases at ceiling), so it has
   limited power to detect further gains — which is also why Phase 7's gate is hard to
   evaluate.
5. `data/complements.json` is 516 human judgements with no fashion-eye review yet.

---

## 6. Lessons that cost time, so you do not repeat them

- **Catalog names are internal style codes** (`SWEATSHIRT OC`, `tvp`,
  `Jerry jogger bottoms`). "sneakers" is in 13 names but 1,590 products are
  `Sneakers`; "jeans" is in 30 names but 2,870 Trousers are `pattern = Denim`. Rank on
  attributes, not names.
- **There is no occasion vocabulary in the catalog**: `party` appears in 5
  descriptions, `wedding` in 1, `office`/`sunday`/`lazy` in none. Vague queries must be
  answered through attributes, which is what `data/lexicon.json` intents do.
- **The catalog's own text contains typos and truncations.** `sunglases` has df 1 and
  `dres` has df 19, so "is it in the index?" and "is it rare?" both fail as spelling
  tests. Correction now also fires when a near neighbour dominates (`dress` 7,877 vs
  `dres` 19), and a word already in the reviewed vocabulary is never corrected
  (`cotton` → `button` was a real bug).
- **A spelling guess must never anchor a query.** `parts` is one edit from `pants`, so
  `spaceship engine parts` started returning trousers.
- **`product_type` contains both `Dog Wear` and `Dog wear`** — a real case-folding
  collision in live data.
- **JSON Schema counts `10.0` as an integer**, so `{"limit": 10.0}` is a *valid* call;
  it used to crash on a list slice.
- **`GET /mcp` must return 405.** Letting the SDK handle `GET` opens a hanging SSE
  stream instead.
- **Role groups for complements are easy to get too coarse.** A necklace with
  earrings, a cardigan over a tee, tights under a skirt and a robe over pyjamas are
  all complements, not substitutes. `scripts/check_lexicon.py` catches these.
- **Audience must carry over to recommendations by *family*, not exact value** — a
  ladies' handbag is `Ladies Accessories`, not `Ladieswear`.
- **Tune on dev, report held out, and reject what does not generalise.** Coordinate
  descent gained +3.4 dev and −0.4 held out; it was thrown away.

---

## 7. Suggested order of work

1. Decide 2a (whose `search.py` the team ships) — blocking for everything downstream.
2. Finish or drop Phase 7 (2b), record the numbers in `PROGRESS.md`.
3. Have a human run `scripts/label.py`, then re-run `scripts/evaluate.py` and report
   how much the headline moves. This is the difference between a proxy and evidence.
4. Review `data/complements.json` and `data/lexicon.json`.
5. Build the container, deploy, and run `scripts/preflight.sh <URL>` until green
   (Phase 10 in the roadmap has the VM steps).
6. Spend benchmark attempts per roadmap Phase 11, comparing official category scores
   against the local estimates in `PROGRESS.md`.
