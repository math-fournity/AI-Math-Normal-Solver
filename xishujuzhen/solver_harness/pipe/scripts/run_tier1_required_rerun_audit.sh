#!/usr/bin/env bash
# Run the complete read-only audit suite used by report 409.

set -euo pipefail

REPO_ROOT="/Users/user/AI-Math-Normal-Solver"
ENV_FILE="/Users/user/glm5.2-math-worktree/.env"
PYTHON_BIN="/Users/user/glm5.2-math-worktree/.venv/bin/python3"
SCRIPT_DIR="$REPO_ROOT/xishujuzhen/solver_harness/pipe/scripts"
SOURCE_ROOT="/Users/user/glm5.2-math-worktree"
AUDIT_OUT="${1:-/tmp/tier1-required-rerun-audit-$(date +%Y%m%d-%H%M%S)}"

source "$ENV_FILE"
if [[ "${ARANGO_DB:-}" != "xishujuzhen_math_glm52" ]]; then
  echo "ERROR: ARANGO_DB must be xishujuzhen_math_glm52, got ${ARANGO_DB:-<unset>}" >&2
  exit 1
fi

mkdir -p "$AUDIT_OUT"
cd "$REPO_ROOT"
export PYTHONPATH="$REPO_ROOT/xishujuzhen/solver_harness/pipe"

echo "ARANGO_DB=$ARANGO_DB"
echo "Audit output: $AUDIT_OUT"

run_audit() {
  local name="$1"
  shift
  echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] START $name"
  "$PYTHON_BIN" "$@" --export "$AUDIT_OUT/$name.json" >"$AUDIT_OUT/$name.stdout.log"
  echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] DONE  $name"
}

run_audit baseline_completion \
  "$SCRIPT_DIR/check_tier1_completion.py" --tier 1
run_audit infra_retry_coverage \
  "$SCRIPT_DIR/audit_infra_retry_coverage.py" --tier 1
run_audit candidate_tool_and_assets \
  "$SCRIPT_DIR/audit_candidate_tool_and_assets.py" --tier 1
run_audit solved_missing_assets \
  "$SCRIPT_DIR/audit_solved_missing_assets.py" --tier 1
run_audit empty_problem_sources \
  "$SCRIPT_DIR/audit_empty_problem_sources.py" --tier 1 --source-root "$SOURCE_ROOT"
run_audit required_reruns \
  "$SCRIPT_DIR/audit_tier1_required_reruns.py" --tier 1

echo
echo "=== Final reproducible summary ==="
jq '.summary, .must_run_breakdowns.classification' "$AUDIT_OUT/required_reruns.json"
echo
echo "All audit artifacts: $AUDIT_OUT"
