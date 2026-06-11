"""#653 slice 1 — the red-test mutation gate. The roles (author/reviewer) and the jest oracle are STUBBED,
so these prove the GATE LOGIC deterministically with no models / no node / no GPU:

  (i)   a discriminating test (fails the wrong-stub, passes the reference)        -> discriminates:true
  (ii)  a NON-discriminating test (passes BOTH mutants)                           -> REJECTED
  (iii) an import-only / trivial test (passes anything that merely loads)         -> REJECTED
  (iv)  the author prompt-builder is NEVER handed executor code                   -> hard error / signature

Plus the near-correct-mutant surface guard (a wrong-stub with a DIFFERENT export surface cannot count as
discrimination even if jest reports it unresolved) and the no-approval refusal.
"""
import json
from pathlib import Path

import pytest

from lfah import authortest


# --------------------------------------------------------------------------- helpers
_PICKS = {
    "phase": "demo",
    "module": "src/m.js",
    "test_path": "__tests__/m.test.js",
    "spec": "export f(x): returns x doubled when x is even, else x tripled.",
    "accept": ["even input -> doubled", "odd input -> tripled"],
    "reject": ["even input -> tripled (wrong branch)"],
    "example_table": [{"input": "f(4)", "reference_output": "8", "wrong_stub_output": "12",
                       "why": "even must double, not triple"}],
}

_REFERENCE = "function f(x){ return x % 2 === 0 ? x*2 : x*3; }\nmodule.exports = { f };\n"
_WRONG_SAME_SURFACE = "function f(x){ return x*3; }\nmodule.exports = { f };\n"   # one localized change, same exports
_WRONG_DIFF_SURFACE = "function g(x){ return x*3; }\nmodule.exports = { g };\n"   # DIFFERENT export surface
_AGENT_TEST = "const { f } = require('../src/m');\ntest('even doubles', () => expect(f(4)).toBe(8));\n"
_TRIVIAL_TEST = "const m = require('../src/m');\ntest('loads', () => expect(m).toBeDefined());\n"


def _write(tmp_path, *, reference=_REFERENCE, wrong=_WRONG_SAME_SURFACE, test=_AGENT_TEST, picks=_PICKS):
    p = tmp_path / "picks.json"; p.write_text(json.dumps(picks))
    ref = tmp_path / "ref.js"; ref.write_text(reference)
    ws = tmp_path / "wrong.js"; ws.write_text(wrong)
    at = tmp_path / "test.js"; at.write_text(test)
    return p, ref, ws, at


def _derive(tmp_path, **kw):
    p, ref, ws, at = _write(tmp_path, **kw)
    bundle = tmp_path / "bundle"
    return authortest.derive(picks_path=p, out_dir=bundle, source="recorded-fallback",
                             reference_path=ref, wrong_stub_paths=[ws], agent_test_path=at,
                             author_model="opus", executor_model="sonnet", reviewer_model="sonnet")


def _fake_review(verdict="PASS"):
    def _r(*, spec, model, backend, user_prompt, cwd, max_turns, dry_run=False):
        return {"response": f"looks faithful and concrete.\nVERDICT: {verdict}", "cost_usd": 0.0}
    return _r


def _fake_jest(resolved_by_runid):
    """run_id 'reference' and 'wrong-<label>' -> canned resolved bools (no real jest)."""
    def _j(instance_id, diff_path, run_id):
        return {"resolved": resolved_by_runid[run_id], "rc": 0, "reimpose_rc": 0}
    return _j


def _gate(tmp_path, bundle, resolved_by_runid, reviewer_verdict="PASS"):
    return authortest.gate(
        bundle_dir=bundle, results_dir=tmp_path / "results", work_dir=tmp_path / "work",
        approved=True, jest_eval=_fake_jest(resolved_by_runid), run_role=_fake_review(reviewer_verdict))


# --------------------------------------------------------------------------- (i) discriminating -> true
def test_discriminating_test_passes_gate(tmp_path):
    m = _derive(tmp_path)
    assert m["files"]["wrong_stubs"][0]["surface_match"] is True       # near-correct mutant: same exports
    log = _gate(tmp_path, tmp_path / "bundle", {"reference": True, "wrong-wrong": False})
    assert log["discriminates"] is True
    assert log["mutants"]["reference"]["resolved"] is True
    assert log["mutants"]["wrong"]["resolved"] is False
    assert log["author"]["model"] != log["executor_model"]            # A2 provable independence (model)
    assert log["reviewer"]["verdict"] == "PASS" and log["reviewer"]["model"] != log["author"]["model"]
    assert Path(log["picks_file"]).name == "picks.json" and len(log["picks_file"]) > 0
    # the committed gate-log satisfies the plan's binary AC jq exactly:
    j = json.loads(Path(log["_gate_log_path"]).read_text())
    assert (j["discriminates"] is True and j["mutants"]["wrong"]["resolved"] is False
            and j["mutants"]["reference"]["resolved"] is True
            and j["author"]["model"] != j["executor_model"] and j["reviewer"]["verdict"] is not None
            and len(j["picks_file"]) > 0)


# --------------------------------------------------------------------------- (ii) passes both -> rejected
def test_non_discriminating_test_is_rejected(tmp_path):
    _derive(tmp_path)
    # the test passes against BOTH the reference AND the wrong-stub -> it is not a real oracle.
    log = _gate(tmp_path, tmp_path / "bundle", {"reference": True, "wrong-wrong": True})
    assert log["discriminates"] is False
    assert log["mutants"]["reference"]["resolved"] is True and log["mutants"]["wrong"]["resolved"] is True


# --------------------------------------------------------------------------- (iii) trivial/import-only -> rejected
def test_import_only_trivial_test_is_rejected(tmp_path):
    """An import-only test passes against anything that merely loads, so it resolves against BOTH the
    reference and the (loadable) wrong-stub -> the gate must REJECT it (anti-trivial-discrimination)."""
    _derive(tmp_path, test=_TRIVIAL_TEST)
    log = _gate(tmp_path, tmp_path / "bundle", {"reference": True, "wrong-wrong": True})
    assert log["discriminates"] is False


# --------------------------------------------------------------------------- surface guard
def test_wrong_stub_with_different_surface_does_not_count(tmp_path):
    """A wrong-stub that fails merely because it does NOT load (different exports) must NOT be scored as
    discrimination, even when jest reports it unresolved — otherwise the gate proves nothing."""
    m = _derive(tmp_path, wrong=_WRONG_DIFF_SURFACE)
    assert m["files"]["wrong_stubs"][0]["surface_match"] is False
    log = _gate(tmp_path, tmp_path / "bundle", {"reference": True, "wrong-wrong": False})
    assert log["discriminates"] is False   # reference passes, wrong "fails" — but surface mismatch -> not proven


# --------------------------------------------------------------------------- (iv) author never sees executor code
def test_author_prompt_builder_never_handed_executor_code(tmp_path):
    # the builder's signature cannot accept executor code (keyword-only spec/picks/example_table):
    with pytest.raises(TypeError):
        authortest.build_author_prompt(spec="s", picks=_PICKS, example_table=[], executor_plan="leak")  # type: ignore
    # the independence gate refuses any forbidden input key:
    with pytest.raises(ValueError):
        authortest.assert_author_inputs_clean({"spec": "s", "picks": {}, "example_table": [],
                                               "executor_code": "function f(){}"})
    # and a real derive records author_inputs with ONLY the allowed keys:
    m = _derive(tmp_path)
    assert set(m["author_inputs"]) == authortest.ALLOWED_AUTHOR_INPUT_KEYS
    prompt = authortest.build_author_prompt(spec=_PICKS["spec"], picks=_PICKS,
                                            example_table=_PICKS["example_table"])
    assert "even input -> doubled" in prompt and "f(4)" in prompt   # built from picks, not from code


# --------------------------------------------------------------------------- gate refuses without approval
def test_gate_refuses_without_approval(tmp_path):
    _derive(tmp_path)
    with pytest.raises(PermissionError):
        authortest.gate(bundle_dir=tmp_path / "bundle", results_dir=tmp_path / "results",
                        work_dir=tmp_path / "work", approved=False,
                        jest_eval=_fake_jest({"reference": True, "wrong-wrong": False}),
                        run_role=_fake_review())


# --------------------------------------------------------------------------- reviewer model must differ
def test_reviewer_model_must_differ_from_author(tmp_path):
    m = _derive(tmp_path)
    with pytest.raises(ValueError):
        authortest.review(derive_manifest=m, gate_result={"mutants": {}}, bundle_dir=tmp_path / "bundle",
                          reviewer_model="opus", author_model="opus", run_role=_fake_review())


# --------------------------------------------------------------------------- live-author JSON parsing
def test_parse_author_response_and_live_derive(tmp_path):
    payload = {"agent_test": _AGENT_TEST, "reference": _REFERENCE,
               "wrong_stubs": [{"label": "branch", "why": "even -> tripled", "code": _WRONG_SAME_SURFACE}],
               "eli5": "the wrong one triples evens"}
    parsed = authortest.parse_author_response("prose...\n```json\n" + json.dumps(payload) + "\n```\n")
    assert parsed["reference"] == _REFERENCE and parsed["wrong_stubs"][0]["label"] == "branch"

    def _fake_author(*, spec, model, backend, user_prompt, cwd, max_turns, dry_run=False):
        assert "function f" not in user_prompt   # author prompt carries NO implementation code
        return {"response": "```json\n" + json.dumps(payload) + "\n```", "cost_usd": 0.01,
                "model_resolved": model}

    p = tmp_path / "picks.json"; p.write_text(json.dumps(_PICKS))
    m = authortest.derive(picks_path=p, out_dir=tmp_path / "lb", source="live-agent",
                          author_model="opus", executor_model="sonnet", run_role=_fake_author)
    assert m["source"] == "live-agent" and m["author"]["source"] == "live-agent"
    assert m["files"]["wrong_stubs"][0]["surface_match"] is True


def test_module_exports_parsing():
    assert authortest.module_exports(_REFERENCE) == {"f"}
    assert authortest.module_exports("exports.a = 1; module.exports.b = 2;") == {"a", "b"}


# --------------------------------------------------------------------------- slice-2 helpers + nits
def test_reviewer_verdict_ok_only_pass():
    assert authortest.reviewer_verdict_ok("PASS") is True
    assert authortest.reviewer_verdict_ok("pass") is True
    for bad in ("CONCERN", "UNCLEAR", "", None, 0):
        assert authortest.reviewer_verdict_ok(bad) is False


def test_nit_parse_author_response_nondict_stub_is_clear_error():
    """Nit 1: a wrong_stubs entry that is NOT an object must raise a CLEAR ValueError (it used to raise a
    cryptic AttributeError: 'str' object has no attribute 'get')."""
    payload = {"agent_test": "t", "reference": "r", "wrong_stubs": ["not-an-object"], "eli5": "e"}
    with pytest.raises(ValueError, match="must be an object"):
        authortest.parse_author_response("```json\n" + json.dumps(payload) + "\n```")


def test_nit_gate_fails_fast_on_reviewer_equal_author(tmp_path):
    """Nit 2: gate() must reject reviewer_model == author_model BEFORE running the (expensive) mutation
    gate. Both opus -> ValueError, and the jest stub must NEVER be called."""
    _write(tmp_path, picks=_PICKS)
    p, ref, ws, at = _write(tmp_path)
    bundle = tmp_path / "bundle"
    authortest.derive(picks_path=p, out_dir=bundle, source="recorded-fallback",
                      reference_path=ref, wrong_stub_paths=[ws], agent_test_path=at,
                      author_model="opus", executor_model="sonnet", reviewer_model="opus")

    called = {"jest": 0}

    def _spy_jest(instance_id, diff_path, run_id):
        called["jest"] += 1
        return {"resolved": True, "rc": 0}

    with pytest.raises(ValueError, match="reviewer model must differ"):
        authortest.gate(bundle_dir=bundle, results_dir=tmp_path / "results", work_dir=tmp_path / "work",
                        approved=True, jest_eval=_spy_jest, run_role=_fake_review())
    assert called["jest"] == 0   # fail-fast: the mutation gate never ran


def test_gate_block_on_reviewer_records_refusal(tmp_path):
    """Slice 2: with block_on_reviewer=True a non-PASS reviewer marks the gate-log refused (but the gate()
    standalone still RETURNS the log — the CLI/build caller decides to block)."""
    _derive(tmp_path)
    log = authortest.gate(
        bundle_dir=tmp_path / "bundle", results_dir=tmp_path / "results", work_dir=tmp_path / "work",
        approved=True, jest_eval=_fake_jest({"reference": True, "wrong-wrong": False}),
        run_role=_fake_review("CONCERN"), block_on_reviewer=True)
    assert log["discriminates"] is True
    assert log["refused"] is True and "not PASS" in log["refusal_reason"]
    assert log["reviewer"]["blocking"] is True and log["reviewer"]["advisory"] is False


def test_gate_phase_test_blocks_nondiscriminating(tmp_path):
    """gate_phase_test (the build entrypoint) RAISES GateRefusal when the test passes BOTH mutants."""
    with pytest.raises(authortest.GateRefusal, match="did not go RED|discriminate"):
        authortest.gate_phase_test(
            picks=_PICKS, reference=_REFERENCE,
            wrong_stubs=[{"label": "branch", "code": _WRONG_SAME_SURFACE}], agent_test=_TRIVIAL_TEST,
            phase="demo", module="src/m.js", test_path="__tests__/m.test.js",
            work_dir=tmp_path / "w", results_dir=tmp_path / "r",
            jest_eval=_fake_jest({"reference": True, "wrong-branch": True}),
            run_role=_fake_review("PASS"))


def test_gate_phase_test_blocks_non_pass_reviewer(tmp_path):
    """gate_phase_test RAISES when the test discriminates but the reviewer does not PASS (blocking)."""
    with pytest.raises(authortest.GateRefusal, match="not PASS"):
        authortest.gate_phase_test(
            picks=_PICKS, reference=_REFERENCE,
            wrong_stubs=[{"label": "branch", "code": _WRONG_SAME_SURFACE}], agent_test=_AGENT_TEST,
            phase="demo", module="src/m.js", test_path="__tests__/m.test.js",
            work_dir=tmp_path / "w", results_dir=tmp_path / "r",
            jest_eval=_fake_jest({"reference": True, "wrong-branch": False}),
            run_role=_fake_review("CONCERN"))


def test_gate_phase_test_passes_discriminating(tmp_path):
    """gate_phase_test returns the gate-log (no raise) for a discriminating test + PASS reviewer."""
    log = authortest.gate_phase_test(
        picks=_PICKS, reference=_REFERENCE,
        wrong_stubs=[{"label": "branch", "code": _WRONG_SAME_SURFACE}], agent_test=_AGENT_TEST,
        phase="demo", module="src/m.js", test_path="__tests__/m.test.js",
        work_dir=tmp_path / "w", results_dir=tmp_path / "r",
        jest_eval=_fake_jest({"reference": True, "wrong-branch": False}),
        run_role=_fake_review("PASS"))
    assert log["discriminates"] is True and log["refused"] is False
    assert log["reviewer"]["verdict"] == "PASS" and log["reviewer"]["blocking"] is True


def test_safe_label_slug_strips_path_separators():
    """#831 review nit: a wrong-stub label used as a filename component must be slugified — a `/` (or other
    path-unsafe char) must not escape the inputs dir or land in a subdir, and the slug must round-trip as a
    filename STEM (no separators)."""
    assert "/" not in authortest._safe_label_slug("a/b", "stub0")
    assert authortest._safe_label_slug("a/b", "stub0") == "a-b"
    assert authortest._safe_label_slug("../escape", "stub0") == "escape"        # no leading dots/seps
    assert authortest._safe_label_slug("good-label_1.x", "stub0") == "good-label_1.x"  # safe chars kept
    assert authortest._safe_label_slug("///", "stub3") == "stub3"               # nothing usable -> fallback


def test_gate_phase_test_sanitizes_slashed_label_filename(tmp_path):
    """#831 review nit (end-to-end): a wrong-stub whose label contains `/` must not write outside the
    inputs dir; gate_phase_test slugifies the label so the on-disk file is a safe stem and the gate still
    discriminates (run_id becomes `wrong-<slug>`)."""
    log = authortest.gate_phase_test(
        picks=_PICKS, reference=_REFERENCE,
        wrong_stubs=[{"label": "branch/evil", "code": _WRONG_SAME_SURFACE}], agent_test=_AGENT_TEST,
        phase="demo", module="src/m.js", test_path="__tests__/m.test.js",
        work_dir=tmp_path / "w", results_dir=tmp_path / "r",
        jest_eval=_fake_jest({"reference": True, "wrong-branch-evil": False}),  # slugified run_id
        run_role=_fake_review("PASS"))
    assert log["discriminates"] is True and log["refused"] is False
    inputs_dir = tmp_path / "w" / "inputs"
    files = sorted(p.name for p in inputs_dir.iterdir() if p.is_file())
    assert "branch-evil.js" in files                          # slugified, single flat file
    assert not (inputs_dir / "branch").exists()               # no `branch/` subdir created
