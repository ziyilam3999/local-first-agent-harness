"""#954 step 2 — the build-execute-specialist manual (Arm B of the builder A/B).

Free smoke (no models / no GPU): proves the new authoring manual is loadable through the #954 P0
`--executor-recipe` seam, passes the completeness gate, preserves every anti-gaming / self-check / output
guard from the baseline, and actually reframes the job from bug-fixing to authoring (so the A/B has a real
treatment, not a renamed copy).
"""
import lfah.relay as relay

BUILD_RECIPE = "build-execute-specialist"
BASELINE_RECIPE = "codefix-execute-specialist"

CLOUD_ROLES = {"planner": "opus", "executor": "opus", "evaluator": "opus"}
CLOUD_BACKENDS = {"planner": "cloud", "executor": "cloud", "evaluator": "cloud"}


def _manual_text():
    return (relay.SKILLS_DIR / BUILD_RECIPE / "SKILL.md").read_text()


# --------------------------------------------------------------------------- it exists and loads
def test_manual_file_exists():
    """The new manual ships in bundle/skills/ so the directory-discovery seam can find it."""
    assert (relay.SKILLS_DIR / BUILD_RECIPE / "SKILL.md").exists()


def test_override_loads_build_manual():
    """Overriding the executor recipe to build-execute-specialist loads THIS manual into the prompt."""
    spec = relay.parse_role("executor", skill_override=BUILD_RECIPE)
    assert spec["skills"] == [BUILD_RECIPE]
    assert _manual_text() in spec["system_prompt"]


def test_profile_with_build_recipe_passes_completeness_gate():
    """A profile whose executor recipe is the new manual passes assert_profile_complete (fail-open only on
    a real missing file — proves the new recipe is a valid, gate-accepted name)."""
    profile = relay.select_profile({"language": "javascript"})
    profile["recipes"]["executor"] = BUILD_RECIPE
    gate = relay.assert_profile_complete(profile, CLOUD_ROLES, CLOUD_BACKENDS)
    assert gate["recipes_ok"] is True


# --------------------------------------------------------------------------- guards preserved verbatim
def test_anti_gaming_and_output_guards_preserved():
    """Every safety/honesty guard the baseline carries must survive the reframe."""
    t = _manual_text()
    # anti-test-editing + no hard-coding + hidden PASS_TO_PASS warning
    assert "edit the test files" in t
    assert "hard-code" in t
    assert "PASS_TO_PASS" in t
    # mandatory Bash self-check
    assert "Self-check with Bash" in t
    # the 4-line output contract
    for line in ("CHANGED:", "APPROACH:", "SELFCHECK:", "NOTES:"):
        assert line in t
    # leave the change uncommitted on disk for the harness git diff
    assert "git commit" in t


# --------------------------------------------------------------------------- it is a real reframe, not a copy
def test_manual_is_authoring_framed_not_a_baseline_copy():
    """The treatment must differ from baseline: authoring framing + complete-surface + sibling-wiring."""
    build_t = _manual_text()
    baseline_t = (relay.SKILLS_DIR / BASELINE_RECIPE / "SKILL.md").read_text()
    assert build_t != baseline_t
    low = build_t.lower()
    assert "author" in low                       # authoring mental model
    assert "whole public surface" in low or "complete" in low  # build the full surface, not a stub
    assert "sibling" in low                       # wire against real sibling modules
    assert "stub" in low                          # explicit anti-stub guidance
