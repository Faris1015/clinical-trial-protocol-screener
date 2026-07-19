#!/usr/bin/env bash
# Post-deploy smoke test: proves a freshly deployed instance is actually serving
# the new build and can drive a screening end-to-end. Run by CD after a rollout,
# but self-contained so you can point it at any environment by hand:
#
#   scripts/smoke_test.sh https://screener-frontend.fly.dev [expected_sha]
#
#   BASE_URL       target origin (arg 1, or $BASE_URL)
#   EXPECTED_SHA   commit the deploy should report at /health (arg 2, or $EXPECTED_SHA;
#                  optional — skipped if empty, so it works against any env)
#   SMOKE_TIMEOUT  seconds to let the pipeline reach a terminal/gate state (default 300)
#
# Exits non-zero (failing the workflow) on any failed check.
set -euo pipefail

BASE_URL="${1:-${BASE_URL:-}}"
EXPECTED_SHA="${2:-${EXPECTED_SHA:-}}"
SMOKE_TIMEOUT="${SMOKE_TIMEOUT:-300}"

if [ -z "$BASE_URL" ]; then
    echo "usage: $0 <base_url> [expected_sha]" >&2
    exit 2
fi
BASE_URL="${BASE_URL%/}"   # strip any trailing slash

workdir="$(mktemp -d)"
trap 'rm -rf "$workdir"' EXIT

fail() { echo "❌ SMOKE FAIL: $*" >&2; exit 1; }
step() { echo "── $*"; }

# 1. Liveness — and, if given, prove the *new* build is the one answering.
step "GET /health"
health="$(curl -fsS --max-time 15 "$BASE_URL/health")" || fail "/health did not return 200"
echo "   $health"
if [ -n "$EXPECTED_SHA" ]; then
    got="$(printf '%s' "$health" | sed -n 's/.*"commit"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p')"
    # Match on the short SHA CI injects (a prefix of the full commit).
    case "$EXPECTED_SHA" in
        "$got"*) echo "   commit $got matches deployed build ✓" ;;
        *) fail "deployed commit '$got' != expected '$EXPECTED_SHA' — the rollout did not cut over" ;;
    esac
fi

# 2. Readiness — every dependency (LLM, rules, EHR, DB) reachable. Capture the
#    body *and* status without --fail: /ready's whole value is the 503 body's
#    per-check breakdown, which --fail would discard exactly when it's needed.
step "GET /ready"
ready_code="$(curl -sS --max-time 15 -o "$workdir/ready.json" -w '%{http_code}' "$BASE_URL/ready" || echo 000)"
echo "   [$ready_code] $(cat "$workdir/ready.json" 2>/dev/null)"
[ "$ready_code" = "200" ] || fail "/ready returned $ready_code (a dependency is down — see breakdown above)"

# 3. End-to-end: create a screening from a stub protocol, drive the pipeline via
#    the stream, and require it to reach a healthy terminal state (the human gate
#    for a valid protocol, or a clean end) rather than an error or a timeout.
step "POST /api/screenings (stub protocol)"
cat > "$workdir/stub.md" <<'EOF'
# Smoke-Test Protocol

## Inclusion Criteria
- Adults aged 18 to 65 years.
- Confirmed diagnosis of type 2 diabetes.

## Exclusion Criteria
- Pregnancy or breastfeeding.
- eGFR below 30 mL/min.
EOF

create="$(curl -fsS --max-time 30 -X POST "$BASE_URL/api/screenings" \
    -F "file=@$workdir/stub.md;type=text/markdown")" || fail "screening creation failed"
thread_id="$(printf '%s' "$create" | sed -n 's/.*"thread_id"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p')"
[ -n "$thread_id" ] || fail "no thread_id in create response: $create"
echo "   thread_id=$thread_id"

step "drive pipeline via /stream (timeout ${SMOKE_TIMEOUT}s)"
# The stream endpoint executes the graph as it's consumed; --max-time bounds it.
# A valid protocol runs Router→Parser→Critic and pauses at the human gate, so a
# success is the __interrupt__ or __end__ sentinel; __error__ or no terminal
# frame is a failure.
stream="$workdir/stream.txt"
curl -fsS --no-buffer --max-time "$SMOKE_TIMEOUT" \
    "$BASE_URL/api/screenings/$thread_id/stream" > "$stream" 2>/dev/null || true

if grep -q '"node":[[:space:]]*"__error__"' "$stream"; then
    echo "   $(grep '"node":[[:space:]]*"__error__"' "$stream" | tail -1)" >&2
    fail "pipeline ended in error"
fi
if grep -Eq '"node":[[:space:]]*"(__interrupt__|__end__)"' "$stream"; then
    echo "   pipeline reached a healthy terminal state ✓"
else
    echo "   --- last stream frames ---" >&2
    tail -5 "$stream" >&2
    fail "pipeline did not reach a terminal state within ${SMOKE_TIMEOUT}s"
fi

echo "✅ SMOKE PASS"