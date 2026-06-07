"""Phase 9b (#515): the JavaScript/jest profile -- the first NON-Python grader.

Proves the profile/oracle seam generalizes across languages:
  - AC1/AC2/AC5 are pure-python (no docker/node) and run everywhere incl. CI.
  - AC3/AC4 exercise the real jest oracle in docker and are skipped when docker/node are absent
    (CI has neither); they are the grader's own both-ends correctness proof and are run + recorded
    locally.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

import lfah.relay as relay

FIXTURES = Path(__file__).parent / "fixtures" / "jest"
sys.path.insert(0, str(FIXTURES))
import materialize as fixt  # noqa: E402  (fixtures dir added to path just above)

CLOUD_ROLES = {"planner": "opus", "executor": "opus", "evaluator": "opus"}
CLOUD_BACKENDS = {"planner": "cloud", "executor": "cloud", "evaluator": "cloud"}


def _docker_ok() -> bool:
    try:
        return subprocess.run(["docker", "info"], capture_output=True, timeout=20).returncode == 0
    except Exception:
        return False


# --------------------------------------------------------------------------- AC1/AC2/AC5 (pure python)
def test_jest_profile_passes_completeness_gate():
    """AC1: make_jest_profile() is a COMPLETE profile (3 recipes + oracle wrapper file + callable fn)."""
    gate = relay.assert_profile_complete(relay.make_jest_profile(), CLOUD_ROLES, CLOUD_BACKENDS)
    assert gate["oracle_ok"] and gate["recipes_ok"] and gate["faithfulness_asserts_ok"]


def test_jest_profile_wires_the_jest_oracle():
    p = relay.make_jest_profile()
    assert p["language"] == "javascript"
    assert p["oracle"]["wrapper"] == "eval_patch_jest.sh"
    assert p["oracle"]["fn"] is relay.jest_oracle_eval
    # the wrapper file the gate validates actually ships next to relay.py
    assert (Path(relay.__file__).parent / "eval_patch_jest.sh").exists()


def test_select_profile_language_axis():
    """AC2: javascript AND typescript -> jest oracle (ts-jest grades .ts under the same npx-jest oracle);
    everything else -> the default pytest codefix oracle."""
    for lang in ("javascript", "JavaScript", "js", "JS", "typescript", "TypeScript", "ts", "TS"):
        assert relay.select_profile({"language": lang})["oracle"]["fn"] is relay.jest_oracle_eval
    for inst in ({}, {"language": "python"}, {"language": ""}, {"language": "go"}, None):
        assert relay.select_profile(inst)["oracle"]["fn"] is relay.oracle_eval


def _report(*files):
    """Build a synthetic jest --json report. Each file is (path, suite_status, [(title, status), ...])."""
    return {"testResults": [
        {"name": f"/work/{path}", "status": ss,
         "assertionResults": [{"title": t, "fullName": t, "ancestorTitles": [], "status": s}
                              for (t, s) in asserts]}
        for (path, ss, asserts) in files]}


def test_jest_targeted_resolve_both_ends():
    """The new targeted grader's own correctness (NO docker). FAIL_TO_PASS must pass + PASS_TO_PASS must
    stay green; UNRELATED failures elsewhere are ignored; file-level and file:test-level ids both work."""
    rep = _report(
        ("test/plugin/isToday.test.js", "passed", [("works", "passed")]),          # F2P (file-level) -> pass
        ("test/get-set.test.js", "passed", [("Add Time days (DST)", "passed")]),    # P2P (test-level) -> pass
        ("test/unrelated.test.js", "failed", [("pre-existing flake", "failed")]),   # UNRELATED -> must be ignored
    )
    f2p = ["test/plugin/isToday.test.js"]
    p2p = ["test/get-set.test.js:Add Time days (DST)"]

    g = relay.jest_targeted_resolve(rep, f2p, p2p)
    assert g["resolved"] is True and g["f2p_pass"] == 1 and g["p2p_pass"] == 1  # unrelated failure ignored

    # F2P still failing -> NOT resolved
    bad_f2p = _report(("test/plugin/isToday.test.js", "failed", [("works", "failed")]),
                      ("test/get-set.test.js", "passed", [("Add Time days (DST)", "passed")]))
    assert relay.jest_targeted_resolve(bad_f2p, f2p, p2p)["resolved"] is False

    # P2P regressed -> NOT resolved (no-regression guard)
    reg = _report(("test/plugin/isToday.test.js", "passed", [("works", "passed")]),
                  ("test/get-set.test.js", "failed", [("Add Time days (DST)", "failed")]))
    assert relay.jest_targeted_resolve(reg, f2p, p2p)["resolved"] is False

    # F2P file never ran (candidate broke compile) -> NOT resolved
    assert relay.jest_targeted_resolve(_report(("test/get-set.test.js", "passed",
                                                [("Add Time days (DST)", "passed")])), f2p, p2p)["resolved"] is False

    # empty F2P can never read as resolved (degenerate spec guard)
    assert relay.jest_targeted_resolve(rep, [], p2p)["resolved"] is False


def test_classify_eval_verdict():
    c = relay.classify_eval_verdict
    assert c("blah\nVERDICT: PASS") == "PASS"
    assert c("VERDICT: ISSUE-PLAN") == "ISSUE-PLAN"
    assert c("reasons...\nVERDICT: ISSUE-CODE\n") == "ISSUE-CODE"
    assert c("the fix looks right, PASS") == "PASS"                 # free-form fallback
    assert c("ISSUE-CODE: off-by-one") == "ISSUE-CODE"
    assert c("P2P 56/56 PASS_TO_PASS all green") == "UNCLEAR"       # PASS_TO_PASS must NOT read as PASS
    assert c("") == "UNCLEAR"


def test_decide_action_loop_signal():
    """The core production-fidelity switch: with oracle UNRESOLVED but the evaluator saying PASS,
    oracle-mode keeps ITERATING (it knows the truth) while evaluator-mode SHIPS (it trusts its reviewer)."""
    # oracle resolved -> SHIP in oracle mode regardless of evaluator
    assert relay.decide_action(oracle_resolved=True, eval_text="VERDICT: ISSUE-CODE",
                               n1_left=1, n2_left=1, loop_signal="oracle")["action"] == "SHIP"
    # THE CONTRAST — oracle says unresolved, evaluator says PASS:
    o = relay.decide_action(oracle_resolved=False, eval_text="VERDICT: PASS",
                            n1_left=1, n2_left=1, loop_signal="oracle")
    e = relay.decide_action(oracle_resolved=False, eval_text="VERDICT: PASS",
                            n1_left=1, n2_left=1, loop_signal="evaluator")
    assert o["action"].startswith("ITERATE") and e == {"action": "SHIP", "reason": "evaluator_pass"}
    # evaluator mode, ISSUE-PLAN + replan budget -> ITERATE-REPLAN; UNCLEAR -> not a ship
    assert relay.decide_action(oracle_resolved=False, eval_text="VERDICT: ISSUE-PLAN",
                               n1_left=0, n2_left=1, loop_signal="evaluator")["action"] == "ITERATE-REPLAN"
    assert relay.decide_action(oracle_resolved=False, eval_text="hmm not sure",
                               n1_left=1, n2_left=0, loop_signal="evaluator")["action"] == "ITERATE-EXECUTOR"
    # no budget -> capped ship in both modes
    assert relay.decide_action(oracle_resolved=False, eval_text="VERDICT: ISSUE-CODE",
                               n1_left=0, n2_left=0, loop_signal="evaluator")["reason"] == "budget_exhausted"


def test_decide_action_both_gate():
    """`both` mode: SHIP iff oracle resolved AND evaluator PASS. The whole point — a green-but-weak oracle
    (a coverage-gap test) with a concrete evaluator ISSUE-CODE must NOT ship; it iterates so the executor
    gets the named defect. This is exactly the case oracle-mode shipped (the lcp-p2 verifyNumbers bug)."""
    # oracle resolved BUT evaluator ISSUE-CODE -> NOT a ship in `both` (oracle-mode WOULD ship here):
    b = relay.decide_action(oracle_resolved=True, eval_text="VERDICT: ISSUE-CODE",
                            n1_left=1, n2_left=1, loop_signal="both")
    assert b["action"].startswith("ITERATE"), b
    # contrast: same inputs in oracle-mode DO ship (documents the fixed gap)
    assert relay.decide_action(oracle_resolved=True, eval_text="VERDICT: ISSUE-CODE",
                               n1_left=1, n2_left=1, loop_signal="oracle")["action"] == "SHIP"
    # oracle resolved AND evaluator PASS -> SHIP
    assert relay.decide_action(oracle_resolved=True, eval_text="VERDICT: PASS",
                               n1_left=1, n2_left=1, loop_signal="both") == {"action": "SHIP", "reason": "oracle_resolved+evaluator_pass"}
    # oracle UNRESOLVED + evaluator PASS -> NOT a ship in `both` (needs the real test too)
    assert relay.decide_action(oracle_resolved=False, eval_text="VERDICT: PASS",
                               n1_left=1, n2_left=0, loop_signal="both")["action"].startswith("ITERATE")
    # both + no budget -> capped ship (resolved-count preserved; only an extra fix try was added)
    assert relay.decide_action(oracle_resolved=True, eval_text="VERDICT: ISSUE-CODE",
                               n1_left=0, n2_left=0, loop_signal="both")["reason"] == "budget_exhausted"


def test_existing_python_path_unchanged():
    """AC5 (unit): an instance with no language field is byte-for-byte the old codefix profile."""
    chosen = relay.select_profile({"instance_id": "pytest-dev__pytest-1"})
    codefix = relay.make_codefix_profile()
    assert chosen["oracle"]["wrapper"] == codefix["oracle"]["wrapper"] == "eval_patch.sh"
    assert chosen["oracle"]["fn"] is codefix["oracle"]["fn"] is relay.oracle_eval


# --------------------------------------------------------------------------- AC3/AC4 (real jest oracle)
@pytest.mark.skipif(not _docker_ok(), reason="jest oracle needs a reachable docker daemon")
def test_jest_oracle_both_ends(tmp_path):
    """AC3: the grader's own correctness, NO LLM. For BOTH fixtures: gold patch -> resolved True,
    empty patch -> resolved False. Materialize + run under the lfah repo tree (colima mounts $HOME)
    so the docker volume mount works; /var tmp dirs are not mounted."""
    repo_root = Path(relay.__file__).resolve().parents[2]   # .../local-first-agent-harness
    work = Path(tempfile.mkdtemp(prefix=".jest-oracle-testrun-", dir=repo_root))
    try:
        ids = fixt.materialize_all(work)
        assert ids, "no jest fixtures materialized"
        env_backup = os.environ.get("LFAH_DATA_DIR"), os.environ.get("LFAH_JEST_WORKROOT")
        os.environ["LFAH_DATA_DIR"] = str(work)
        os.environ["LFAH_JEST_WORKROOT"] = str(work / "_runs")
        try:
            for iid in ids:
                gold = work / "instances" / iid / "gold.patch"
                assert gold.read_text().strip(), f"empty gold patch for {iid}"
                res_gold = relay.jest_oracle_eval(iid, gold, f"gold-{iid}")
                assert res_gold["resolved"] is True, f"gold patch did not resolve {iid}: {res_gold}"

                empty = work / f"empty-{iid}.diff"
                empty.write_text("")
                res_base = relay.jest_oracle_eval(iid, empty, f"base-{iid}")
                assert res_base["resolved"] is False, f"baseline wrongly resolved {iid}: {res_base}"
        finally:
            for k, v in zip(("LFAH_DATA_DIR", "LFAH_JEST_WORKROOT"), env_backup):
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
    finally:
        shutil.rmtree(work, ignore_errors=True)


@pytest.mark.skipif(not _docker_ok(), reason="eval_patch_jest.sh runs the jest oracle in docker")
def test_eval_patch_jest_wrapper_prints_resolved(tmp_path):
    """AC4: the evaluator-facing wrapper prints exactly RESOLVED=true/false. Run it from a checkout
    whose working tree carries the gold change (so its `git diff` IS the gold patch)."""
    repo_root = Path(relay.__file__).resolve().parents[2]
    work = Path(tempfile.mkdtemp(prefix=".jest-wrapper-testrun-", dir=repo_root))
    try:
        ids = fixt.materialize_all(work)
        iid = ids[0]
        checkout = work / "instances" / iid / "repo"
        # apply gold into the checkout's working tree (uncommitted) -> `git diff` == gold patch
        subprocess.run(["git", "-C", str(checkout), "apply", "--whitespace=nowarn",
                        str(work / "instances" / iid / "gold.patch")], check=True)
        wrapper = Path(relay.__file__).parent / "eval_patch_jest.sh"
        env = dict(os.environ, LFAH_DATA_DIR=str(work), LFAH_JEST_WORKROOT=str(work / "_runs"))
        r = subprocess.run(["bash", str(wrapper), iid], cwd=str(checkout), env=env,
                           capture_output=True, text=True, timeout=900)
        assert "RESOLVED=true" in r.stdout, f"wrapper stdout={r.stdout!r} stderr={r.stderr[-2000:]!r}"

        # and with a clean checkout (no diff) -> RESOLVED=false
        subprocess.run(["git", "-C", str(checkout), "checkout", "--", "."], check=True)
        r2 = subprocess.run(["bash", str(wrapper), iid], cwd=str(checkout), env=env,
                            capture_output=True, text=True, timeout=900)
        assert "RESOLVED=false" in r2.stdout, f"wrapper stdout={r2.stdout!r}"
    finally:
        shutil.rmtree(work, ignore_errors=True)


@pytest.mark.skipif(not _docker_ok(), reason="jest oracle needs a reachable docker daemon")
def test_jest_oracle_reimposes_graded_test_against_tampering(tmp_path):
    """AC6 (#607): a candidate that NEUTERS the graded test (rewrites it to a trivially-passing
    no-op) WITHOUT fixing the source must NOT be scored resolved. The oracle re-imposes the
    canonical test from base_commit, so grader-gaming is defeated. Contrast: without re-imposition
    the neutered suite would go green and falsely resolve. Honest gold fix still resolves."""
    repo_root = Path(relay.__file__).resolve().parents[2]
    work = Path(tempfile.mkdtemp(prefix=".jest-tamper-testrun-", dir=repo_root))
    try:
        ids = fixt.materialize_all(work)
        iid = ids[0]                                    # jest-fixture__sum-1, test_files=[sum.test.js]
        inst = json.loads((work / "instances" / iid / "instance.json").read_text())
        assert inst.get("test_files"), "fixture must declare test_files for the hardening to engage"
        tfile = inst["test_files"][0]
        checkout = work / "instances" / iid / "repo"

        # craft a TAMPER candidate: rewrite the graded test to a no-op pass, NO source fix.
        (checkout / tfile).write_text("test('neutered', () => { expect(1).toBe(1); });\n")
        tamper = subprocess.run(["git", "-C", str(checkout), "diff"], capture_output=True,
                                text=True, check=True).stdout
        subprocess.run(["git", "-C", str(checkout), "checkout", "--", "."], check=True)  # restore
        assert tfile in tamper and "neutered" in tamper, "tamper diff did not target the test file"

        env_backup = os.environ.get("LFAH_DATA_DIR"), os.environ.get("LFAH_JEST_WORKROOT")
        os.environ["LFAH_DATA_DIR"] = str(work)
        os.environ["LFAH_JEST_WORKROOT"] = str(work / "_runs")
        try:
            tdiff = work / f"tamper-{iid}.diff"; tdiff.write_text(tamper)
            res_tamper = relay.jest_oracle_eval(iid, tdiff, f"tamper-{iid}")
            assert res_tamper["resolved"] is False, f"tamper wrongly resolved {iid}: {res_tamper}"

            gold = work / "instances" / iid / "gold.patch"
            res_gold = relay.jest_oracle_eval(iid, gold, f"gold2-{iid}")
            assert res_gold["resolved"] is True, f"gold patch did not resolve {iid}: {res_gold}"
        finally:
            for k, v in zip(("LFAH_DATA_DIR", "LFAH_JEST_WORKROOT"), env_backup):
                os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    # convenience: `python tests/test_jest_profile.py` materializes + dumps the fixtures for inspection
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(tempfile.mkdtemp(prefix="jestfix-"))
    print("materialized:", fixt.materialize_all(out), "->", out)
    print("gold patch (instance A):")
    print((out / "instances" / json.loads((FIXTURES / "instanceA-sum" / "meta.json").read_text())
           ["instance_id"] / "gold.patch").read_text())
