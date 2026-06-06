"""#645: a LOCAL-backend connection failure (the CCR proxy / Ollama is down) makes the local executor's
`claude -p` return the CLI's connection-failure banner ("API Error: Unable to connect to API
(ConnectionRefused)") in place of real output -- 0 tool uses, no patch, on every round. The role does NOT
raise, so #641's failure-capture never fires and #623's rate-limit regex doesn't match -> the arm would be
counted as a MODEL LOSS (a down proxy silently makes the local arm look incapable). These tests cover the
detector, the local-backend scoping, the pre-chain reachability preflight, and the mid-chain abort."""
from pathlib import Path
import lfah.relay as relay

BANNER = "API Error: Unable to connect to API (ConnectionRefused)"


def test_conn_fail_hit_positive_and_negative():
    # the CLI banner on an EMPTY run (no tool uses) is an infra failure
    assert relay.conn_fail_hit({"response": BANNER, "tool_uses": []})
    assert relay.conn_fail_hit({"response": "Error: connect ECONNREFUSED 127.0.0.1:3456", "tool_uses": []})
    assert relay.conn_fail_hit({"response": "", "soft_error": "exit 1: ConnectionRefused", "tool_uses": []})
    # a PRODUCTIVE run that merely MENTIONS the words must NOT trip it (it has tool uses + a patch)
    assert not relay.conn_fail_hit({"response": "I fixed the ECONNREFUSED handling in net.js",
                                    "tool_uses": [{"name": "Edit"}]})
    # clean output / empties / non-dicts
    assert not relay.conn_fail_hit({"response": "All 730 tests pass.", "tool_uses": []})
    assert not relay.conn_fail_hit({"response": "", "tool_uses": []})
    assert not relay.conn_fail_hit(None)


def test_first_conn_fail_scoped_to_local_backend():
    backends = {"planner": "cloud", "executor": "local", "evaluator": "cloud"}
    # a cloud role returning the banner is OUT of scope (#645 is the local proxy-down case)
    assert relay._first_conn_fail(backends, planner={"response": BANNER, "tool_uses": []}) is None
    # the LOCAL executor returning the banner is flagged, naming the role
    role, snippet = relay._first_conn_fail(backends, executor={"response": BANNER, "tool_uses": []})
    assert role == "executor"
    assert "Unable to connect" in snippet


def _profile(resolved=False):
    return {"recipes": {"planner": None, "executor": None, "evaluator": None},
            "category": "code-fix",
            "oracle": {"wrapper": "eval_patch_jest.sh",
                       "fn": lambda *a, **k: {"resolved": resolved, "report": None, "rc": 1}}}


def _instance():
    return {"instance_id": "iamkun__dayjs-1964", "problem_statement": "bug",
            "FAIL_TO_PASS": "test/x.test.js", "base_commit": "abc123", "repo": "iamkun/dayjs"}


def _patch_relay(monkeypatch):
    monkeypatch.setattr(relay, "reset_repo", lambda *a, **k: None)
    monkeypatch.setattr(relay, "lessons_find", lambda *a, **k: "")
    monkeypatch.setattr(relay, "parse_role", lambda *a, **k: {"skill": None})
    monkeypatch.setattr(relay, "git_diff", lambda *a, **k: "")


def test_run_chain_preflight_aborts_when_proxy_down(tmp_path, monkeypatch):
    """When a LOCAL backend is in use and the proxy is unreachable, run_chain INFRA-SKIPs up front --
    before the planner -- so a dead backend never burns a single role call or records a false model loss."""
    calls = {"n": 0}

    def fake_run_role(**kwargs):
        calls["n"] += 1
        return {"response": "should never run", "tool_uses": [], "soft_error": "", "wall_s": 0.0}

    _patch_relay(monkeypatch)
    monkeypatch.setattr(relay, "run_role", fake_run_role)
    monkeypatch.setattr(relay, "_local_backend_reachable", lambda *a, **k: (False, "ConnectionRefusedError"))

    result = relay.run_chain(
        instance=_instance(), repo=Path(tmp_path), mode="c", profile=_profile(), dry_run=False,
        role_models={"planner": "opus", "executor": "qwen", "evaluator": "sonnet"},
        role_backends={"planner": "cloud", "executor": "local", "evaluator": "cloud"})

    assert result["verdict"] == "INFRA-SKIP"
    assert result["infra_skip"] is True
    assert result["final_resolved"] is None
    assert "local_backend_unreachable" in result["infra_reason"]
    assert calls["n"] == 0, f"preflight must abort BEFORE any role call, got {calls['n']}"


def test_run_chain_infra_skips_on_local_conn_fail_midchain(tmp_path, monkeypatch):
    """Even if the preflight passed (proxy up at start, then dies), a LOCAL executor that returns the
    connection banner mid-chain aborts the instance as INFRA-SKIP (reason local_connection_failure),
    NOT a model loss. dry_run=True skips the preflight so this isolates the in-loop detector."""
    def fake_run_role(**kwargs):
        # only the LOCAL executor hits the dead proxy; cloud planner/evaluator answer normally
        if kwargs.get("backend") == "local":
            return {"response": BANNER, "tool_uses": [], "cost_usd": 0.0, "soft_error": "exit 1: ConnectionRefused",
                    "num_turns": 1, "wall_s": 0.1, "model_resolved": "", "output_tps": 0.0}
        return {"response": "VERDICT: FAIL -- patch empty", "tool_uses": [], "cost_usd": 0.01,
                "num_turns": 1, "wall_s": 0.2, "model_resolved": "claude-opus-4-8"}

    _patch_relay(monkeypatch)
    monkeypatch.setattr(relay, "run_role", fake_run_role)

    result = relay.run_chain(
        instance=_instance(), repo=Path(tmp_path), mode="c", profile=_profile(), dry_run=True,
        role_models={"planner": "opus", "executor": "qwen", "evaluator": "sonnet"},
        role_backends={"planner": "cloud", "executor": "local", "evaluator": "cloud"})

    assert result["verdict"] == "INFRA-SKIP"
    assert result["infra_skip"] is True
    assert result["final_resolved"] is None
    assert result["infra_reason"] == "local_connection_failure:executor"


def test_local_backend_reachable_reports_down_on_refused(monkeypatch):
    """The reachability probe returns ok=False (not an exception) when the proxy port is closed --
    a closed localhost port refuses the connection."""
    monkeypatch.setattr(relay, "CCR_BASE_URL", "http://127.0.0.1:1")   # port 1: nothing listens -> refused
    ok, detail = relay._local_backend_reachable(timeout_s=2.0)
    assert ok is False
    assert isinstance(detail, str) and detail
