#!/usr/bin/env bash
# eval_patch_jest.sh <instance_id>
#
# The JavaScript/jest analogue of eval_patch.sh -- the evaluator role runs THIS from its repo
# checkout (cwd) to verify the executor's change against the REAL jest suite, independently of the
# executor's self-report. It is a thin wrapper over the SAME scoring path the engine's
# jest_oracle_eval() uses -- in fact it CALLS jest_oracle_eval() -- so the evaluator's RESOLVED
# verdict and the orchestrator-owned oracle.resolved can never disagree.
#
# Behavior: capture the current `git diff` in cwd as the candidate patch, score it via the jest
# oracle (fresh copy of the instance repo under LFAH_DATA_DIR -> git apply -> run the suite, in a
# node docker container by default), and print exactly one line:
#     RESOLVED=true   (the patch makes the jest suite pass)
#     RESOLVED=false  (it does not)
#
# Config (override via env; defaults mirror jest_oracle_eval in relay.py):
#   LFAH_DATA_DIR    - root holding instances/<id>/{instance.json,repo} (REQUIRED for jest)
#   LFAH_JEST_DOCKER - "1" (default) run jest hermetically in docker; "0" run on host node
#   LFAH_DOCKER_HOST / DOCKER_HOST - docker socket (default: unix:///var/run/docker.sock)
set -uo pipefail

IID="${1:?usage: eval_patch_jest.sh <instance_id>}"
HARNESS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_PY="${LFAH_VENV_PY:-python3}"

TS="$(date +%s)"
WORK="$PWD/.eval_patch_jest-${IID}-${TS}"
mkdir -p "$WORK"
# Candidate patch = the executor's uncommitted change in this checkout.
git diff > "$WORK/patch.diff"
RUN_ID="evalpatchjest-${IID}-${TS}"

"$VENV_PY" - "$IID" "$WORK/patch.diff" "$RUN_ID" "$HARNESS_DIR" <<'PY'
import sys
from pathlib import Path
iid, diff_path, run_id, harness_dir = sys.argv[1:5]
sys.path.insert(0, harness_dir)
import relay as rk
res = rk.jest_oracle_eval(iid, Path(diff_path), run_id)
print("RESOLVED=true" if res["resolved"] else "RESOLVED=false")
PY
