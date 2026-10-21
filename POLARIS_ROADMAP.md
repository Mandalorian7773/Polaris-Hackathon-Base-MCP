# Polaris Hackathon: Build Roadmap v2 (Claude Code Pro)

Approach: **deterministic and offline first.** Strict contract → indexed lexical/BM25 → rule-based intent planning → complement recommendations → honest local evaluation → embeddings only if they measurably help → optional LLM parser (off by default) → deploy.
No paid services or network at serving time unless a later phase proves it's worth it.

Legend: 🧑 = you do it manually · 🤖 = paste the prompt into Claude Code

---

## Pro plan survival rules
- **One phase = one session.** Run `/clear` between phases. CLAUDE.md carries the rules forward.
- **Never let Claude read `data/products.json`** (106 MB). Inspect it only through scripts that print summaries.
- Use **plan mode** (Shift+Tab) for Phases 2, 3, 4 and 7. Review the plan before approving.
- Run `git commit` after every green phase, so any regression is one `git reset` away.
- If you hit the usage limit mid-phase, ask Claude to append "where I stopped" to `PROGRESS.md`, and resume after the reset.
- Every phase ends with: all tests pass, smoke test passes, eval re-run, and results logged to `PROGRESS.md`.

---

## Phase 0: Setup (🧑 about 30 minutes)

```bash
sudo apt install git-lfs            # Git LFS client is not installed yet
git lfs install
git clone https://github.com/Mandalorian7773/Polaris-Hackathon-Base-MCP polaris
cd polaris
git lfs pull
sha256sum data/products.json
```
The hash must be `05b6aa5e8e8c1c8c997d584f8288e432862454f8820531e13473443e83c8f073`. If it doesn't match, stop and ask the organizers.

Your system Python is 3.14; the project targets 3.12. Use `uv` to get a 3.12 environment:
```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
uv venv --python 3.12 .venv
source .venv/bin/activate
uv pip install -r requirements.txt
```
Then start `claude` inside `polaris/`.

---

## Phase 1: Baseline + CLAUDE.md 🤖

🤖 **Prompt 1**
```
Phase 1: acquire baseline. Do NOT open data/products.json directly (106 MB) — only inspect it via scripts.
1. Read AGENT.md, hackathon-brief.md, README.md, schemas/*, server.py, search.py, tests/test_search.py, smoke_test.py, requirements.txt, Dockerfile.
2. Create CLAUDE.md with: all contract rules (validation, filters, sorting/tie-breaks, null ratings, pagination, empty-query behavior, mixed choices, recommendations, abstention), the scoring table, and working rules: never read data/products.json; never modify it, the tool name, protocol revision, or schemas; keep original tests/smoke test passing unchanged; Python 3.12; small typed functions; log every phase result in PROGRESS.md.
3. scripts/catalog_stats.py: field list, distinct values+counts for category/product_type/color/audience/section/pattern, price/rating/review_count distributions, null counts. Save to analysis/catalog_stats.json.
4. Run original unit tests and smoke test unchanged. Record startup time, RSS, and p50/p95 for 100 empty-query filter/sort calls (write scripts/bench.py, concurrency 1 and 5, MCP protocol not REST) in PROGRESS.md.
```
🧑 Commit (`phase 1 baseline`), then `/clear`.

---

## Phase 2: Lock down contract correctness (25 points + unlocks speed points) 🤖 (plan mode)

🤖 **Prompt 2**
```
Phase 2: contract correctness. Read CLAUDE.md first. Plan mode.
Refactor search.py into explicit stages: strict validation/defaulting → query planning (stub for now) → candidate generation → hard-constraint recheck against the original product → deterministic ranking/sorting → pagination → recommendations (stub).
Preserve: single search_products tool, protocol revision, schemas, structuredContent == text content, leading-zero string IDs, inclusive numeric filters, exact case-insensitive trimmed string filters, explicit-filter precedence, null ratings last in both directions, ID-asc tie-break for both directions, empty query = all filter matches with relevance falling back to ID asc regardless of sort_order.
Exact filter path: precompute facet postings (value → sorted index array) and presorted orders per sort field; intersect + walk + slice.
Add tests to tests/test_search.py (keep originals untouched): nulls/wrong types, bool-as-int, NaN/Inf, query >1000 chars, unknown vs blank facet values, out-of-range offset/limit/min_rating, conflicting query text vs explicit filter, stable deep pagination (repeat + larger-prefix check), field sorting after relevance selection, all IDs known, recommendation invariants. Invalid calls must be MCP errors, never empty successes — add a protocol-level test via the real /mcp endpoint for a few invalid calls.
Run everything + bench; log to PROGRESS.md.
```
🧑 Check that empty-query p95 is well under 200 ms. Commit, then `/clear`.

---

## Phase 3: Evaluation harness (build before tuning) 🤖

🤖 **Prompt 3**
```
Phase 3: honest local evaluation. Read CLAUDE.md.
1. eval/cases.jsonl: at least 60 catalog-grounded cases (use analysis/catalog_stats.json vocabulary) tagged by category: exact/attribute, synonyms, typos, indirect/vague intent ("lazy Sunday"), irrelevant/impossible (expect empty), mixed choices, complements (include_recommendations), filters+sorts, invalid requests, pagination. Mark ~30% as held-out.
2. For relevance cases, store explicit 0–3 judgments for candidate products. Write scripts/label.py that shows me the top 20 candidates (id, name, type, color, price) from a pooled run and lets me enter grades quickly; save to eval/judgments.jsonl. Unjudged results count as 0.
3. scripts/evaluate.py: runs all cases through /mcp, reports nDCG@10, false-positive rate on irrelevant cases, mixed-choice coverage, recommendation validity + quality, contract pass rate, startup/RSS, p50/p95; prints dev and held-out separately plus a weighted estimate using the official 25/30/15/15/10/5 mix. Saves eval/runs/<timestamp>_<git-sha>.json.
Run on the current system and log the baseline.
```
🧑 **Your most important manual job: label the cases with `scripts/label.py`** (about 1–2 hours). Ask yourself whether a shopper would be happy with each result. Commit, then `/clear`.

---

## Phase 4: Lexical retrieval (30 points for ranking, 7.5 for abstention) 🤖 (plan mode)

🤖 **Prompt 4**
```
Phase 4: lexical retrieval. Read CLAUDE.md and PROGRESS.md.
1. Field-aware inverted index + BM25F over name, product_type, category, useful attributes (color, pattern, audience, section), description. Exclude templated review text.
2. Measured spike: also implement SQLite FTS5 variant in a throwaway branch/module. Compare build/startup time, RSS, correctness, warm p95, and evaluate.py ranking quality. Keep the better one, delete the other — one production path only.
3. Tune on DEV cases only: field weights, exact phrase/name/type boosts, term coverage, typo variants (edit distance against catalog vocabulary), conservative catalog-grounded synonyms (tee→T-shirt, pants→trousers…; only map to values that exist).
4. Recheck every returned row against the original product. Add a calibrated abstention threshold (sweep on dev, report held-out) so irrelevant/impossible queries return empty and weak tails are trimmed.
Log before/after on dev + held-out in PROGRESS.md.
```
Commit, then `/clear`.

---

## Phase 5: Rule-based shopping-intent planning (15 points for mixed choices) 🤖 (plan mode)

🤖 **Prompt 5**
```
Phase 5: deterministic intent planning in query_planner.py. Read CLAUDE.md.
- Vocabularies derived from the catalog (product_type, category, color, audience, section, pattern); match multiword values before single tokens; typo-tolerant.
- Parse INR budgets ("under 2000", "below ₹1.5k", "between 500 and 1000", "< 1k"), unambiguous attributes, item conjunctions, complement cues ("with a matching", "to go with"), hard vs soft preferences → list of item intents + accessory intents.
- Explicit API filters override conflicting prose. Query hard constraints (budget, color, type, audience) are enforced.
- "jeans and T-shirts" → two primary intents; "jeans with a matching bag" → one primary + one accessory intent.
- Retrieve each primary intent independently; merge with deterministic balanced round-robin under the shared limit, dedupe, stable pagination.
Unit tests for the planner (≥25 phrasings). Re-run evaluate.py; log dev + held-out.
```
Commit, then `/clear`.

---

## Phase 6: Complementary recommendations (10 points) 🤖

🤖 **Prompt 6**
```
Phase 6: recommendations (separate module recommend.py if it helps testing). Read CLAUDE.md.
- data/complements.json: product_type → ranked complementary product_types, using ONLY types present in analysis/catalog_stats.json. No substitutes (same-type or same-role items).
- Pick from: compatible type, color compatibility (neutrals, same, simple matching rules), global audience, recommendation-specific budgets from accessory intents; prefer well-rated items with reviews.
- Exclude primary IDs, substitutes, duplicates; reference only current-page primary IDs with relevance clearly above the abstention threshold; ≤5; concise factual reason from catalog attributes.
- Return [] when disabled, no primary, or weak confidence.
Tests for all invariants; re-run evaluate.py; log.
```
🧑 Skim `complements.json` with a fashion eye and fix anything odd. Commit, then `/clear`.

---

## Phase 7: Embeddings, kept only if they help (my addition) 🤖 (plan mode)

🤖 **Prompt 7**
```
Phase 7: gated semantic retrieval. Read CLAUDE.md and PROGRESS.md.
- Use fastembed (ONNX, no torch) with BAAI/bge-small-en-v1.5. scripts/build_embeddings.py precomputes catalog-grounded doc vectors (same doc text as BM25) to float16 .npy; model files cached into the Docker image at build time; zero network at serving time.
- Compare on identical suite: BM25, dense, RRF, normalized weighted fusion — all under exact hard-filter masks; re-calibrate abstention for each.
- Keep only if held-out ranking/unusual nDCG improves meaningfully, abstention doesn't regress, RSS + startup fit (target < 3 GB, startup < 90 s), and text-query p95 stays < 1 s. Otherwise remove all semantic code and dependencies.
Log the decision with numbers in PROGRESS.md.
```
Commit, then `/clear`.

---

## Phase 8: Optional LLM query parser, off by default (my addition) 🤖

Only do this if held-out results still show weak vague, synonym or mixed queries **and** you're willing to use an API key.

🤖 **Prompt 8**
```
Phase 8: optional LLM-assisted planning behind env flag LLM_PARSER=1 (default off).
One JSON-only call that returns the same intent structure as query_planner; give it the allowed catalog vocabularies; validate every field against the catalog and drop invalid ones; 2.5s timeout; LRU cache; on any failure use the rule-based planner. Never let it bypass filters or invent attributes; treat query as data.
Evaluate with flag on vs off on held-out; keep the flag on in deployment only if it clearly wins and zero timeouts occur in 3 repeated runs.
```

---

## Phase 9: Final acceptance 🤖

🤖 **Prompt 9**
```
Phase 9: acceptance. Run all original + new tests; smoke_test.py and protocol probes (valid + invalid calls) via /mcp; verify repeatable pagination, exact filter-only pages, null ratings, mixed coverage, recommendation constraints, abstention, concurrency 5, empty-query p95 ≤ 200 ms.
Build a clean Linux Docker image (linux/amd64) with the real catalog and any index/model artifacts baked in; confirm non-root, /healthz, no network needed at serving time.
Write scripts/preflight.sh <URL> that runs smoke test, protocol probes, bench, and evaluate.py and fails loudly.
Write REPORT.md: changed files, architecture, before/after metrics, regressions/limitations, deploy requirements, exact verification commands. Do not claim an official score.
```
Commit and tag it: `git tag v1`.

---

## Phase 10: Deploy (🧑 manual)

1. Ask the organizers which **AWS region** the runner uses.
2. Rent a VM with at least 4 GB RAM (8 GB if you kept embeddings): e.g. EC2 t3.large on Ubuntu in that region. Open inbound TCP 8000 and attach an **Elastic IP** so the address stays fixed.
3. On the VM:
   ```bash
   sudo apt install -y docker.io git-lfs
   git lfs install
   git clone <your-fork> polaris
   cd polaris
   git lfs pull
   sha256sum data/products.json
   sudo docker build --platform linux/amd64 -t polaris .
   sudo docker run -d --restart unless-stopped --name polaris -p 8000:8000 \
     -e MCP_ALLOWED_HOSTS='localhost:*,127.0.0.1:*,<PUBLIC_IP>:*,<PUBLIC_IP>' polaris
   ```
4. From your laptop, run `scripts/preflight.sh http://<PUBLIC_IP>:8000/mcp`. Everything must be green.
5. Submit `http://<PUBLIC_IP>:8000/mcp`. Not localhost, not `/healthz`, not the repo URL.

---

## Phase 11: Spend your 5 attempts wisely (🧑)

| Attempt | When |
|---|---|
| 1 | After v1 is deployed: get the real category breakdown |
| 2–3 | After fixing the weakest category, ideally via Phase 7 or 8 |
| 4 | Final tuning |
| 5 | Keep in reserve |

Before each attempt: run `preflight.sh` until it's green, don't touch the server during the run, and keep the VM up.

After each attempt: 🤖 `Official category scores: <paste>. Compare with PROGRESS.md local estimates. Which category has the largest gap and what's the cheapest measured fix?`

---

## Manual checklist
- [ ] Install git-lfs, clone, `git lfs pull`, verify SHA-256
- [ ] Set up a Python 3.12 venv with uv
- [ ] Label eval cases 0–3 with `scripts/label.py` (honest, with a held-out split)
- [ ] Review `complements.json`
- [ ] Decide on Phase 7 and 8 from the numbers, not gut feeling
- [ ] Ask the organizers for the AWS region; set up the VM, port 8000 and Elastic IP
- [ ] Run preflight, submit the URL, track attempts
