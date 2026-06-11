"""Smoke tests: the package imports, the bundle is present, and the CLI wiring is sane."""
from pathlib import Path

import lfah
from lfah import relay
from lfah import cli


def test_version():
    assert lfah.__version__ == "0.3.0"


def test_relay_imports_and_profile_complete():
    profile = relay.make_codefix_profile()
    role_models = {"planner": "opus", "executor": "sonnet", "evaluator": "opus"}
    role_backends = {r: "cloud" for r in role_models}
    gate = relay.assert_profile_complete(profile, role_models, role_backends)
    assert gate["recipes_ok"] and gate["oracle_ok"] and gate["faithfulness_asserts_ok"]


def test_bundle_files_present():
    bundle = Path(relay.__file__).parent / "bundle"
    for role in ("planner", "executor", "evaluator"):
        assert (bundle / "agents" / f"{role}.md").exists()
    for skill in ("codefix-plan-specialist", "codefix-execute-specialist",
                  "codefix-evaluate-specialist"):
        assert (bundle / "skills" / skill / "SKILL.md").exists()
    assert (Path(relay.__file__).parent / "eval_patch.sh").exists()


def test_parse_role_reads_bundle():
    spec = relay.parse_role("planner", skill_override="codefix-plan-specialist")
    assert spec["role"] == "planner"
    assert "Read" in spec["tools"]
    assert "planner-specialist" in spec["system_prompt"]


def test_decide_action_rule_table():
    assert relay.decide_action(oracle_resolved=True, eval_text="PASS",
                               n1_left=1, n2_left=1)["action"] == "SHIP"
    assert relay.decide_action(oracle_resolved=False, eval_text="ISSUE-PLAN: x",
                               n1_left=1, n2_left=1)["action"] == "ITERATE-REPLAN"
    assert relay.decide_action(oracle_resolved=False, eval_text="ISSUE-CODE: x",
                               n1_left=1, n2_left=0)["action"] == "ITERATE-EXECUTOR"
    assert relay.decide_action(oracle_resolved=False, eval_text="ISSUE-CODE: x",
                               n1_left=0, n2_left=0)["action"] == "SHIP"


def test_cli_parser_builds():
    parser = cli._build_parser()
    args = parser.parse_args(["run", "--instance", "x.json", "--local"])
    role_models, role_backends = cli._resolve_models(args)
    assert role_backends["executor"] == "local"
    assert role_backends["planner"] == "cloud"
    assert role_backends["evaluator"] == "cloud"
