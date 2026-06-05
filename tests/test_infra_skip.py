"""#623: a provider rate-limit/quota notice returned in place of real model output must be detected and
abort the instance as INFRA-SKIP -- NOT consume the iteration budget on garbage (the 2026-06-04 #601
session-limit incident). Tests the detector + that run_chain early-returns before any executor round."""
from pathlib import Path
import lfah.relay as relay

LIMIT = "You've hit your session limit · resets 2:20am (Asia/Kuala_Lumpur)"


def test_provider_limit_hit_positive_and_negative():
    assert relay.provider_limit_hit({"response": LIMIT})
    assert relay.provider_limit_hit({"response": "quota exceeded for this org"})
    assert relay.provider_limit_hit({"response": "Error: 429 Too Many Requests"})
    assert relay.provider_limit_hit({"response": "overloaded_error: server busy"})
    # real model output must NOT trip it
    assert not relay.provider_limit_hit({"response": "All 730 tests pass (88 suites)."})
    assert not relay.provider_limit_hit({"response": "VERDICT: PASS"})
    assert not relay.provider_limit_hit({"response": ""})
    assert not relay.provider_limit_hit(None)


def test_first_limit_names_the_role():
    assert relay._first_limit(planner={"response": "ok"}, evaluator={"response": LIMIT})[0] == "evaluator"
    assert relay._first_limit(planner={"response": "fine"}, executor={"response": "done"}) is None


def test_run_chain_infra_skips_before_any_executor_round(tmp_path, monkeypatch):
    """When the planner/precode role returns a rate-limit notice, run_chain returns INFRA-SKIP and never
    runs an executor round (no burned iterations)."""
    calls = {"n": 0}

    def fake_run_role(**kwargs):
        calls["n"] += 1
        return {"response": LIMIT, "tool_uses": [], "cost_usd": 0.0, "soft_error": "rate_limit",
                "num_turns": 1, "wall_s": 0.1, "model_resolved": "<synthetic>"}

    monkeypatch.setattr(relay, "run_role", fake_run_role)
    monkeypatch.setattr(relay, "reset_repo", lambda *a, **k: None)
    monkeypatch.setattr(relay, "lessons_find", lambda *a, **k: "")
    monkeypatch.setattr(relay, "parse_role", lambda *a, **k: {"skill": None})

    profile = {
        "recipes": {"planner": None, "executor": None, "evaluator": None},
        "category": "code-fix",
        "oracle": {"wrapper": "eval_patch_jest.sh",
                   "fn": lambda *a, **k: {"resolved": False, "report": None, "rc": 1}},
    }
    instance = {"instance_id": "iamkun__dayjs-1964", "problem_statement": "bug",
                "FAIL_TO_PASS": "test/x.test.js", "base_commit": "abc123", "repo": "iamkun/dayjs"}

    result = relay.run_chain(
        instance=instance, repo=Path(tmp_path), mode="c", profile=profile, dry_run=True,
        role_models={"planner": "opus", "executor": "sonnet", "evaluator": "opus"},
        role_backends={"planner": "cloud", "executor": "cloud", "evaluator": "cloud"})

    assert result["verdict"] == "INFRA-SKIP"
    assert result["infra_skip"] is True
    assert result["final_resolved"] is None
    assert "provider_rate_limit" in result["infra_reason"]
    # planner + pre-code evaluator only -> aborted BEFORE the executor loop (would be >=3 calls otherwise)
    assert calls["n"] == 2, f"expected abort after 2 role calls, got {calls['n']} (executor round burned)"
