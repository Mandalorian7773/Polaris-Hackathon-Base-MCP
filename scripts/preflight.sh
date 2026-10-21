#!/usr/bin/env bash
# Pre-submission check against a deployed endpoint. Fails loudly on the first problem.
#
#   scripts/preflight.sh http://<PUBLIC_IP>:8000/mcp
#   scripts/preflight.sh                      # defaults to a local server
#
# Runs, in order: unit tests, lexicon and complement validation, the real MCP smoke
# test, HTTP method probes, invalid-call probes, the latency pool, and the full case
# suite. Every step must pass; the script exits non-zero at the first failure.
#
# Run this until it is green before spending a benchmark attempt.
set -u -o pipefail

URL="${1:-http://127.0.0.1:8000/mcp}"
BASE="${URL%/mcp}"
HEALTH="$BASE/healthz"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="${PYTHON:-$ROOT/.venv/bin/python}"
cd "$ROOT"

PASS=0
FAIL=0
FAILED_STEPS=()

bold() { printf '\n\033[1m== %s ==\033[0m\n' "$1"; }
ok()   { printf '   \033[32mPASS\033[0m %s\n' "$1"; PASS=$((PASS + 1)); }
bad()  { printf '   \033[31mFAIL\033[0m %s\n' "$1"; FAIL=$((FAIL + 1)); FAILED_STEPS+=("$1"); }

step() {         # step <name> <command...>
  local name="$1"; shift
  local log
  log="$(mktemp)"
  if "$@" >"$log" 2>&1; then
    ok "$name"
  else
    bad "$name"
    printf '        --- last 25 lines of output ---\n'
    tail -25 "$log" | sed 's/^/        /'
  fi
  rm -f "$log"
}

expect_status() { # expect_status <name> <method> <url> <expected code>
  local name="$1" method="$2" url="$3" expected="$4" actual
  actual="$(curl -s -m 10 -o /dev/null -w '%{http_code}' -X "$method" "$url" \
            -H 'Accept: text/event-stream' \
            -H 'MCP-Protocol-Version: 2025-11-25' 2>/dev/null || echo 000)"
  if [ "$actual" = "$expected" ]; then
    ok "$name ($actual)"
  else
    bad "$name (expected $expected, got $actual)"
  fi
}

printf '\033[1mpreflight\033[0m  endpoint %s\n' "$URL"
printf 'python %s\n' "$PYTHON"

bold 'catalog integrity'
EXPECTED_SHA=05b6aa5e8e8c1c8c997d584f8288e432862454f8820531e13473443e83c8f073
if [ -f data/products.json ]; then
  ACTUAL_SHA="$(sha256sum data/products.json | cut -d' ' -f1)"
  if [ "$ACTUAL_SHA" = "$EXPECTED_SHA" ]; then
    ok 'data/products.json SHA-256 matches the frozen catalog'
  else
    bad "data/products.json SHA-256 is $ACTUAL_SHA, expected $EXPECTED_SHA"
  fi
else
  bad 'data/products.json is missing (git lfs pull?)'
fi

bold 'local checks'
step 'unit tests'            "$PYTHON" -m unittest discover -s tests
step 'lexicon + complements' "$PYTHON" scripts/check_lexicon.py

bold 'endpoint reachability'
if curl -sf -m 15 "$HEALTH" >/dev/null 2>&1; then
  ok "healthz reachable at $HEALTH"
  curl -s -m 15 "$HEALTH" | sed 's/^/        /'
  printf '\n'
else
  bad "healthz unreachable at $HEALTH -- is the server up and the port open?"
fi

bold 'MCP contract'
step 'smoke test (real MCP client)' "$PYTHON" smoke_test.py "$URL"

bold 'HTTP method contract'
expect_status 'standalone GET /mcp returns 405' GET "$URL" 405
expect_status 'GET /healthz returns 200'        GET "$HEALTH" 200

bold 'latency pool'
step 'bench (concurrency 1 and 5, empty-query pool)' \
  "$PYTHON" scripts/bench.py --url "$URL" --out analysis/preflight_bench.json

bold 'case suite'
step 'evaluate (104 cases, all categories)' \
  "$PYTHON" scripts/evaluate.py --url "$URL" --out analysis/preflight_eval.json

bold 'summary'
if [ -f analysis/preflight_bench.json ]; then
  "$PYTHON" - <<'PY' || true
import json
from pathlib import Path
report = json.loads(Path('analysis/preflight_bench.json').read_text())
pooled = report['bench']['pooled']
print(f"   pooled p50 {pooled['p50_ms']} ms   p95 {pooled['p95_ms']} ms   "
      f"errors {pooled['errors']}   correct {pooled['correct_response_rate']}")
PY
fi
if [ -f analysis/preflight_eval.json ]; then
  "$PYTHON" - <<'PY' || true
import json
from pathlib import Path
report = json.loads(Path('analysis/preflight_eval.json').read_text())
estimate = report['estimate']
overall = report['overall']
print(f"   local estimate {estimate['total']:.2f}/100 "
      f"(functional {estimate['functional']:.2f}/95, "
      f"performance {estimate['performance']:.2f}/5)")
print(f"   validity {overall['mean_validity']:.4f}   "
      f"nDCG@10 {overall['mean_ndcg_at_10']:.4f}   "
      f"abstention {overall['abstention_rate']:.4f}")
failures = overall['cases_with_failures']
if failures:
    print(f"   cases with failures: {', '.join(failures)}")
PY
fi

printf '\n'
if [ "$FAIL" -eq 0 ]; then
  printf '\033[32mPREFLIGHT GREEN\033[0m  %d checks passed\n' "$PASS"
  printf 'The local estimate above is NOT an official score.\n'
  exit 0
fi
printf '\033[31mPREFLIGHT RED\033[0m  %d passed, %d failed\n' "$PASS" "$FAIL"
for name in "${FAILED_STEPS[@]}"; do printf '   - %s\n' "$name"; done
printf 'Do not spend a benchmark attempt until this is green.\n'
exit 1
