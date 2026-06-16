"""#954 P0 — the `--executor-recipe` seam (A/B prerequisite).

Proves the executor's specialist manual can be swapped WITHOUT touching the engine, and that the seam is
behavior-preserving when unused and fail-closed on a bogus recipe name. No models / no GPU: the CLI parse +
profile mutation are pure-python, and the build-side test stubs a fake `lfah run` that records its argv.
"""
import json
import subprocess
import sys

import pytest

import lfah.cli as cli
import lfah.relay as relay
from lfah import build

CLOUD_ROLES = {"planner": "opus", "executor": "opus", "evaluator": "opus"}
CLOUD_BACKENDS = {"planner": "cloud", "executor": "cloud", "evaluator": "cloud"}

# An EXISTING alternate manual (ships in bundle/skills/) — proves the override loads a DIFFERENT recipe
# than the default executor manual, without needing the (not-yet-authored) build-execute-specialist.
ALT_RECIPE = "codefix-plan-specialist"
DEFAULT_EXECUTOR_RECIPE = "codefix-execute-specialist"


# --------------------------------------------------------------------------- AC1: the flag is exposed
def test_run_help_lists_executor_recipe():
    """AC1: `lfah run --help` advertises --executor-recipe."""
    out = subprocess.run([sys.executable, "-m", "lfah.cli", "run", "--help"],
                         capture_output=True, text=True)
    assert out.returncode == 0
    assert "--executor-recipe" in out.stdout


# --------------------------------------------------------------------------- AC3: behavior-preserving default
def test_flag_defaults_to_none():
    """AC3: absent flag parses to None (the override branch is skipped -> baseline behavior)."""
    args = cli._build_parser().parse_args(
        ["run", "--instance", "x.json"])
    assert args.executor_recipe is None


def test_default_profiles_keep_codefix_executor():
    """AC3: with no override, both profiles still carry the baseline executor recipe (nothing swapped)."""
    assert relay.make_codefix_profile()["recipes"]["executor"] == DEFAULT_EXECUTOR_RECIPE
    assert relay.make_jest_profile()["recipes"]["executor"] == DEFAULT_EXECUTOR_RECIPE


# --------------------------------------------------------------------------- AC2: override loads alt manual
def test_override_loads_alternate_executor_manual():
    """AC2: overriding profile['recipes']['executor'] makes parse_role load the ALT manual, not the default.

    This is exactly what cli._run does after select_profile when --executor-recipe is set. select_profile
    returns a FRESH dict, so the mutation is run-local.
    """
    profile = relay.select_profile({"language": "javascript"})
    profile["recipes"]["executor"] = ALT_RECIPE          # what the CLI override does
    spec = relay.parse_role("executor", skill_override=profile["recipes"]["executor"])
    assert spec["skills"] == [ALT_RECIPE]

    alt_text = (relay.SKILLS_DIR / ALT_RECIPE / "SKILL.md").read_text()
    default_text = (relay.SKILLS_DIR / DEFAULT_EXECUTOR_RECIPE / "SKILL.md").read_text()
    assert alt_text in spec["system_prompt"]             # the ALT manual is actually loaded
    assert alt_text != default_text                      # ...and it differs from the default manual

    # the un-overridden default still loads the baseline manual (no cross-contamination)
    base_spec = relay.parse_role("executor", skill_override=DEFAULT_EXECUTOR_RECIPE)
    assert base_spec["skills"] == [DEFAULT_EXECUTOR_RECIPE]


def test_select_profile_returns_fresh_dict_so_override_is_run_local():
    """AC2/safety: select_profile yields a fresh dict each call — mutating one run's recipe cannot leak
    into the next run's profile."""
    p1 = relay.select_profile({"language": "javascript"})
    p1["recipes"]["executor"] = ALT_RECIPE
    p2 = relay.select_profile({"language": "javascript"})
    assert p2["recipes"]["executor"] == DEFAULT_EXECUTOR_RECIPE


# --------------------------------------------------------------------------- AC5: fail-closed on bogus name
def test_bogus_recipe_is_refused_by_completeness_gate():
    """AC5: an override to a recipe whose SKILL.md does not exist raises (refused before any model call)."""
    profile = relay.select_profile({"language": "javascript"})
    profile["recipes"]["executor"] = "no-such-recipe-xyz"
    with pytest.raises(RuntimeError):
        relay.assert_profile_complete(profile, CLOUD_ROLES, CLOUD_BACKENDS)


# --------------------------------------------------------------------------- AC4: build threads flag to argv
# A fake `lfah run` that RECORDS its argv to out/argv-<iid>.json (and writes a SHIP result so the build
# advances). Mirrors tests/test_build.py's stub shape.
_ARGV_STUB = '''import json, os, sys
a = sys.argv
inst = a[a.index("--instance") + 1]; out = a[a.index("--out") + 1]
iid = json.load(open(inst))["instance_id"]
os.makedirs(out, exist_ok=True)
json.dump(a, open(os.path.join(out, f"argv-{iid}.json"), "w"))
repo = os.path.join(os.path.dirname(inst), "repo")
os.makedirs(os.path.join(repo, "src"), exist_ok=True)
open(os.path.join(repo, "src", iid + ".txt"), "w").write("impl " + iid + "\\n")
json.dump({"instance_id": iid, "final_resolved": True, "verdict": "SHIP",
           "loop_signal": "both", "iterations": 1, "handoff": None,
           "telemetry": {"cost": {"chain_total_cost_usd": 0.0}}},
          open(os.path.join(out, f"lfah-{iid}-c.json"), "w"))
'''

_PHASES = [
    {"id": "bp1", "title": "one", "test_file": "p1.test", "test_path": "__tests__/p1.test",
     "f2p": "__tests__/p1.test", "p2p": [], "problem_statement": "make p1 pass"},
]


def _setup(tmp_path):
    md = tmp_path / "manifest"; md.mkdir()
    (md / "p1.test").write_text("// red 1\n")
    stub = tmp_path / "stub_run.py"; stub.write_text(_ARGV_STUB)
    return md, [sys.executable, str(stub)]


def _recorded_argv(out_dir, iid="bp1"):
    return json.loads((out_dir / f"argv-{iid}.json").read_text())


def test_build_threads_executor_recipe_into_run_argv(tmp_path):
    """AC4: a manifest carrying executor_recipe puts `--executor-recipe <name>` into the per-phase argv."""
    md, run_cmd = _setup(tmp_path)
    manifest = {"project_name": "app", "language": "text",
                "executor_recipe": ALT_RECIPE, "phases": _PHASES}
    out = tmp_path / "out"
    build.run_build(manifest=manifest, project=tmp_path / "project", data=tmp_path / "data",
                    out=out, manifest_dir=md, run_cmd=run_cmd, npm_install=False)
    argv = _recorded_argv(out)
    assert "--executor-recipe" in argv
    assert argv[argv.index("--executor-recipe") + 1] == ALT_RECIPE


def test_build_without_field_has_no_recipe_flag(tmp_path):
    """AC4 (negative): no executor_recipe field -> no --executor-recipe flag (behavior-preserving)."""
    md, run_cmd = _setup(tmp_path)
    manifest = {"project_name": "app", "language": "text", "phases": _PHASES}
    out = tmp_path / "out"
    build.run_build(manifest=manifest, project=tmp_path / "project", data=tmp_path / "data",
                    out=out, manifest_dir=md, run_cmd=run_cmd, npm_install=False)
    argv = _recorded_argv(out)
    assert "--executor-recipe" not in argv


# --------------------------------------------------------------------------- #961: build planner/evaluator
# model pass-through (lets the paid A/B run the two cloud roles on cheap Sonnet; default opus is unchanged).

def test_build_help_lists_planner_evaluator():
    """`lfah build --help` advertises --planner and --evaluator."""
    out = subprocess.run([sys.executable, "-m", "lfah.cli", "build", "--help"],
                         capture_output=True, text=True)
    assert "--planner" in out.stdout
    assert "--evaluator" in out.stdout


def test_build_threads_model_flags_into_run_argv(tmp_path):
    """A non-default planner/evaluator puts `--planner <m> --evaluator <m>` into the per-phase run argv."""
    md, run_cmd = _setup(tmp_path)
    manifest = {"project_name": "app", "language": "text", "phases": _PHASES}
    out = tmp_path / "out"
    build.run_build(manifest=manifest, project=tmp_path / "project", data=tmp_path / "data",
                    out=out, manifest_dir=md, run_cmd=run_cmd, npm_install=False,
                    planner_model="sonnet", evaluator_model="haiku")
    argv = _recorded_argv(out)
    assert argv[argv.index("--planner") + 1] == "sonnet"
    assert argv[argv.index("--evaluator") + 1] == "haiku"


def test_build_defaults_models_to_opus(tmp_path):
    """Behavior-preserving: with no model args the per-phase argv threads opus (== `lfah run` default)."""
    md, run_cmd = _setup(tmp_path)
    manifest = {"project_name": "app", "language": "text", "phases": _PHASES}
    out = tmp_path / "out"
    build.run_build(manifest=manifest, project=tmp_path / "project", data=tmp_path / "data",
                    out=out, manifest_dir=md, run_cmd=run_cmd, npm_install=False)
    argv = _recorded_argv(out)
    assert argv[argv.index("--planner") + 1] == "opus"
    assert argv[argv.index("--evaluator") + 1] == "opus"
