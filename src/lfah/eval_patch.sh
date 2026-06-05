#!/usr/bin/env bash
# eval_patch.sh <instance_id>
#
# The evaluator role runs THIS from its repo checkout (cwd) to verify the executor's
# change against the REAL SWE-bench test, independently of the executor's self-report.
# It is a thin wrapper over the SAME scoring path the engine's oracle_eval() uses -- in
# fact it CALLS oracle_eval() -- so the evaluator's RESOLVED verdict and the
# orchestrator-owned oracle.resolved can never disagree.
#
# Behavior: capture the current `git diff` in cwd as the candidate patch, score it via
# the canonical SWE-bench docker harness (default princeton-nlp/SWE-bench_Verified, run
# via the configured python against the configured docker socket), and print exactly one
# line:
#     RESOLVED=true   (the patch makes the FAIL_TO_PASS test pass)
#     RESOLVED=false  (it does not)
#
# Config (override via env; defaults mirror oracle_eval in relay.py):
#   LFAH_VENV_PY    - python with the `swebench` package installed (default: `python3`)
#   LFAH_DOCKER_HOST / DOCKER_HOST - docker socket (default: unix:///var/run/docker.sock)
set -uo pipefail

IID="${1:?usage: eval_patch.sh <instance_id>}"
HARNESS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_PY="${LFAH_VENV_PY:-python3}"
export DOCKER_HOST="${LFAH_DOCKER_HOST:-${DOCKER_HOST:-unix:///var/run/docker.sock}}"

TS="$(date +%s)"
WORK="$PWD/.eval_patch-${IID}-${TS}"
mkdir -p "$WORK"
# Candidate patch = the executor's uncommitted change in this checkout, INCLUDING new files.
# Plain `git diff` is blind to untracked files (a new-file fix would look empty). Stage all
# changes (honors .gitignore so build junk is excluded), capture the staged diff (renders new
# files as apply-able `new file mode` hunks), then unstage so the working tree is left exactly
# as the executor left it. Mirrors git_diff() in relay.py.
git add -A
git diff --cached > "$WORK/patch.diff"
git reset -q
RUN_ID="evalpatch-${IID}-${TS}"

"$VENV_PY" - "$IID" "$WORK/patch.diff" "$RUN_ID" "$HARNESS_DIR" <<'PY'
import sys
from pathlib import Path
iid, diff_path, run_id, harness_dir = sys.argv[1:5]
sys.path.insert(0, harness_dir)
import relay as rk
res = rk.oracle_eval(iid, Path(diff_path), run_id)
print("RESOLVED=true" if res["resolved"] else "RESOLVED=false")
PY
