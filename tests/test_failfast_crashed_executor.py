"""#709: a LOCAL executor that CRASHED this round (non-zero exit / <synthetic> model -- any non-empty
soft_error, not only "timeout"/"stuck") must FAIL FAST -> SHIP-CAPPED so the cloud handoff can rescue it,
instead of re-running a dead local role and burning the iteration budget. The `and not passed_now` guard
must still let a crash AFTER the code is already correct (oracle/evaluator green) ship normally.

Found via the dogfood P4a bp6 per-round review (2026-06-08): round 1's executor crashed (soft_error="exit 1"
-> model_resolved=<synthetic>) yet the chain kept treating it as a real round. Outcome was benign there
(round 0 had already solved it, oracle+evaluator green), but a generic crash slipped past the
timeout/stuck-only fail-fast.
"""
from pathlib import Path
import lfah.relay as relay


def _stub_chain(monkeypatch):
    """Common monkeypatches: no real models, no network, no git, no cloud handoff."""
    monkeypatch.setattr(relay, "reset_repo", lambda *a, **k: None)
    monkeypatch.setattr(relay, "lessons_find", lambda *a, **k: "")
    monkeypatch.setattr(relay, "parse_role", lambda *a, **k: {"skill": None})
    monkeypatch.setattr(relay, "git_diff", lambda *a, **k: "")          # no git on tmp_path
    monkeypatch.setattr(relay, "CLOUD_HANDOFF", False)                  # isolate the loop from the handoff
    monkeypatch.setattr(relay, "LOOP_SIGNAL", "both")                   # the dogfood gate
    if hasattr(relay, "_local_backend_reachable"):
        monkeypatch.setattr(relay, "_local_backend_reachable", lambda *a, **k: (True, "stub"))


def _role(resp, *, soft_error="", model="qwen-local"):
    return {"response": resp, "tool_uses": [], "cost_usd": 0.0, "soft_error": soft_error,
            "num_turns": 1, "wall_s": 0.5, "model_resolved": model, "output_tokens": 0,
            "input_tokens": 0, "output_tps": 0.0, "stuck_evidence": None}


def _run(monkeypatch, tmp_path, *, oracle_resolved, exec_soft_error, exec_model="qwen-local", eval_verdict="VERDICT: ISSUE-CODE"):
    """Drive run_chain with stub roles. Call order: planner, pre-code evaluator, executor, evaluator."""
    calls = {"n": 0}

    def fake_run_role(**kwargs):
        calls["n"] += 1
        n = calls["n"]
        if n == 1:                                  # planner
            return _role("PLAN: create the file.")
        if n == 2:                                  # pre-code evaluator (must NOT say ISSUE-PLAN)
            return _role("Looks fine, proceed.")
        if n == 3:                                  # executor — CRASHED this round
            return _role("", soft_error=exec_soft_error, model=exec_model)
        return _role(eval_verdict)                  # evaluator (n>=4)

    monkeypatch.setattr(relay, "run_role", fake_run_role)
    _stub_chain(monkeypatch)

    profile = {
        "recipes": {"planner": None, "executor": None, "evaluator": None},
        "category": "code-fix",
        "oracle": {"wrapper": "eval_patch_jest.sh",
                   "fn": lambda *a, **k: {"resolved": oracle_resolved, "report": None,
                                          "rc": 0 if oracle_resolved else 1}},
    }
    instance = {"instance_id": "demo-1", "problem_statement": "make the test pass",
                "FAIL_TO_PASS": "x.test.ts", "base_commit": "abc123", "repo": "local/app"}
    repo = tmp_path / "repo"; repo.mkdir()           # repo.parent (tmp_path) must be writable for patch.round*.diff
    result = relay.run_chain(
        instance=instance, repo=repo, mode="c", profile=profile, dry_run=False,
        role_models={"planner": "qwen", "executor": "qwen", "evaluator": "qwen"},
        role_backends={"planner": "local", "executor": "local", "evaluator": "local"})
    return result, calls["n"]


def test_crashed_local_executor_unsolved_fails_fast(monkeypatch, tmp_path):
    """exit-1 crash + code NOT passing -> SHIP-CAPPED on the FIRST round (no iteration burn)."""
    result, _ = _run(monkeypatch, tmp_path, oracle_resolved=False, exec_soft_error="exit 1: boom")
    assert result["verdict"] == "SHIP-CAPPED"
    assert len(result["rounds"]) == 1                                   # failed fast, did not keep iterating
    assert result["rounds"][0]["action"]["reason"] == "local_executor_exit_1_no_retry"
    assert result["final_resolved"] is False


def test_timeout_still_fails_fast(monkeypatch, tmp_path):
    """Behavior-preserving: the original timeout/stuck fail-fast still works after the broadening."""
    result, _ = _run(monkeypatch, tmp_path, oracle_resolved=False, exec_soft_error="timeout")
    assert result["verdict"] == "SHIP-CAPPED"
    assert result["rounds"][0]["action"]["reason"] == "local_executor_timeout_no_retry"


def test_crashed_local_executor_but_code_already_correct_ships_normally(monkeypatch, tmp_path):
    """The #709 bp6 case: a crash AFTER the code is correct (oracle + evaluator green this round) must still
    SHIP normally -- the `and not passed_now` guard protects a verified-correct result from the fail-fast."""
    result, _ = _run(monkeypatch, tmp_path, oracle_resolved=True, exec_soft_error="exit 1: boom",
                     eval_verdict="VERDICT: PASS")
    assert result["verdict"] == "SHIP"                                  # not SHIP-CAPPED
    assert result["final_resolved"] is True
    assert result["rounds"][0]["action"].get("reason") != "local_executor_exit_1_no_retry"


def test_clean_local_executor_unsolved_iterates_not_failfast(monkeypatch, tmp_path):
    """A CLEAN executor (no soft_error) that hasn't solved it yet must NOT fail-fast on the broadened rule
    -- it should iterate/cap normally. Guards against the broadening over-firing on empty soft_error."""
    result, ncalls = _run(monkeypatch, tmp_path, oracle_resolved=False, exec_soft_error="")
    # no fail-fast: the round's action is not the no-retry SHIP; it iterated or capped instead
    assert result["rounds"][0]["action"].get("reason") != "local_executor__no_retry"
    assert ncalls >= 4                                                  # at least one full executor->eval round ran
