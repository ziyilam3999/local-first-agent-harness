"""Safe agent-authored red tests via a mutation gate (#653 slice 1).

A human-written phase test is both "what done means" AND the judge. If the same robot that writes the
feature also writes its own test, it writes an easy match-my-build test (a student grading their own
exam). #653 lets a *different* robot write the test safely, then PROVES the test is a real oracle with a
mutation gate.

Two-step flow, reusing existing engine primitives ONLY (`relay.run_role` + `relay.jest_oracle_eval` —
no new chain roles, no edits to relay.py):

  derive: an independent CLOUD author role (model != the build's executor) writes, from the operator's
          MCQ picks + an input->output example table ONLY (never any executor code/plan), (a) the red
          acceptance TEST, (b) the REFERENCE code (satisfies every pick), and (c) one WRONG-STUB per
          must-REJECT pick (reference + exactly one localized change, IDENTICAL module surface). Emits an
          ELI5 explanation of each derived version, then STOPS for human approval.

  gate:   runs ONLY with a recorded approval. The mutation gate = `relay.jest_oracle_eval` ×2 in a fresh
          minimal THROWAWAY scaffold (NOT a symlink — the throwaway commits only the agent test at base,
          src/ absent, instance.json f2p_tests=[the agent test]): wrong-stub diff -> expect resolved:false,
          reference diff -> expect resolved:true. A test that can't tell them apart is REJECTED as a fake
          oracle. Then an advisory fresh-eyes reviewer role (model != author). Emits the committed gate-log
          results/AUTHORTEST-<phase>.json.

The MCQ collection + ELI5 approval happen at the ORCHESTRATOR level because roles cannot call
AskUserQuestion (it is in the engine DISALLOWED_TOOLS). The committed picks.json is the replayable
spec-of-record.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

from . import relay

# The author may be handed ONLY these inputs. Any other key (esp. executor plan/code) is a hard error:
# the independence guarantee is that the author writes the test BEFORE any code exists and never peeks.
ALLOWED_AUTHOR_INPUT_KEYS = {"spec", "picks", "example_table"}

# Recipe folders (auto-packaged via pyproject `bundle/skills/*/SKILL.md`).
AUTHOR_RECIPE = "redtest-author-specialist"
REVIEW_RECIPE = "redtest-review-specialist"

# How many trailing chars of the reviewer's reply to retain in the gate-log (the VERDICT line is last,
# so we keep the tail). Named rather than a magic literal so the retention budget lives in one place.
_REVIEWER_NOTES_MAXLEN = 1200

# A reviewer verdict counts as a PASS only when it is exactly this. Anything else (CONCERN, UNCLEAR) is
# treated as non-PASS — which is advisory in slice 1 but BLOCKS the build path / CLI in slice 2.
_REVIEWER_PASS = "PASS"


# ---------------------------------------------------------------------------
# Author independence: the prompt is built from picks ONLY (never executor output)
# ---------------------------------------------------------------------------
def assert_author_inputs_clean(author_inputs: dict) -> dict:
    """Hard gate: the author's inputs contain ONLY {spec, picks, example_table}. Refuses if any other key
    is present (an executor plan/code leak destroys independence). Returns the inputs on ALLOW."""
    extra = set(author_inputs) - ALLOWED_AUTHOR_INPUT_KEYS
    if extra:
        raise ValueError(
            f"author independence violated: prompt inputs carry forbidden key(s) {sorted(extra)} "
            f"(allowed: {sorted(ALLOWED_AUTHOR_INPUT_KEYS)}). The author must NEVER see executor code/plan.")
    return author_inputs


def build_author_prompt(*, spec: str, picks: dict, example_table: list) -> str:
    """Render the author's user prompt from the operator's picks ONLY. By signature this function can
    NEVER be handed executor code — it accepts the spec text, the MCQ picks, and the example table, and
    nothing else. The author returns a single strict-JSON object (parsed by `parse_author_response`)."""
    accept = picks.get("accept") or []
    reject = picks.get("reject") or []
    module = picks.get("module", "src/module.js")
    test_path = picks.get("test_path", "__tests__/module.test.js")
    lines = []
    lines.append("You are an INDEPENDENT red-test author. You write a FAILING acceptance test plus a")
    lines.append("REFERENCE implementation and one or more WRONG-STUBS, from the operator's multiple-choice")
    lines.append("picks ONLY. You have NOT seen — and must never ask for — any other robot's code or plan.")
    lines.append("")
    lines.append(f"## Module under test\n- file: {module}\n- test file: {test_path}")
    lines.append("")
    lines.append("## Module surface / spec (the requirement)\n" + spec.strip())
    lines.append("")
    lines.append("## The operator picked — the test MUST ACCEPT:")
    for a in accept:
        lines.append(f"  - {a}")
    lines.append("## The operator picked — the test MUST REJECT:")
    for r in reject:
        lines.append(f"  - {r}")
    lines.append("")
    lines.append("## Discriminating input -> output examples (reference vs a planted-wrong version):")
    for ex in example_table:
        lines.append(f"  - input: {ex.get('input')}")
        lines.append(f"      reference output: {ex.get('reference_output')}")
        lines.append(f"      wrong-stub output: {ex.get('wrong_stub_output')}")
        if ex.get("why"):
            lines.append(f"      why it discriminates: {ex.get('why')}")
    lines.append("")
    lines.append("## Output contract (STRICT) — emit ONE fenced ```json block and nothing else:")
    lines.append("{")
    lines.append('  "agent_test": "<full content of the failing acceptance test file>",')
    lines.append('  "reference": "<full content of the reference module that satisfies EVERY pick>",')
    lines.append('  "wrong_stubs": [')
    lines.append('     {"label": "<short-id>", "why": "<which REJECT pick it violates>",')
    lines.append('      "code": "<reference + EXACTLY ONE localized change; IDENTICAL module.exports surface>"}')
    lines.append("  ],")
    lines.append('  "eli5": "<plain-language explanation of why the wrong version is wrong, and right is right>"')
    lines.append("}")
    lines.append("")
    lines.append("Rules: each wrong-stub = the reference with EXACTLY ONE localized, AC-relevant change and")
    lines.append("the SAME exports (so 'fails vs wrong-stub' is never trivially satisfied by merely loading")
    lines.append("the module). The test must FAIL against every wrong-stub and PASS against the reference.")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Parse the author's strict-JSON output
# ---------------------------------------------------------------------------
def parse_author_response(text: str) -> dict:
    """Extract the author's JSON object: prefer the last ```json fenced block, else the last {...} span.
    Validates the required keys are present and well-typed."""
    block = None
    fences = re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fences:
        block = fences[-1]
    else:
        # fall back to the outermost brace span
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1 and end > start:
            block = text[start:end + 1]
    if not block:
        raise ValueError("author response contained no JSON object")
    obj = json.loads(block)
    for k in ("agent_test", "reference", "wrong_stubs", "eli5"):
        if k not in obj:
            raise ValueError(f"author JSON missing required key: {k}")
    if not isinstance(obj["wrong_stubs"], list) or not obj["wrong_stubs"]:
        raise ValueError("author JSON: wrong_stubs must be a non-empty list")
    for ws in obj["wrong_stubs"]:
        # Guard the element TYPE before `.get` — a bare string/None element would otherwise raise a
        # cryptic AttributeError instead of the clear contract-violation ValueError everything else uses.
        if not isinstance(ws, dict):
            raise ValueError(f"author JSON: each wrong_stub must be an object, got {type(ws).__name__}")
        if not ws.get("label") or not ws.get("code"):
            raise ValueError("author JSON: each wrong_stub needs a label and code")
    return obj


# ---------------------------------------------------------------------------
# Near-correct-mutant guard: identical module surface (same exports)
# ---------------------------------------------------------------------------
def module_exports(js_text: str) -> set:
    """Best-effort extraction of CommonJS exported names from `module.exports = { a, b, ... }` (and
    `exports.x = ...`). Used to assert a wrong-stub exposes the IDENTICAL surface as the reference, so a
    test cannot 'discriminate' merely because the stub fails to load."""
    names: set = set()
    m = re.search(r"module\.exports\s*=\s*\{([^}]*)\}", js_text, re.DOTALL)
    if m:
        for part in m.group(1).split(","):
            key = part.split(":")[0].strip()
            key = re.sub(r"\s+as\s+\w+$", "", key)
            if re.fullmatch(r"[A-Za-z_$][\w$]*", key):
                names.add(key)
    for em in re.finditer(r"(?:module\.)?exports\.([A-Za-z_$][\w$]*)\s*=", js_text):
        names.add(em.group(1))
    return names


def _safe_label_slug(label: str, fallback: str) -> str:
    """Slugify a wrong-stub label so it is safe as a filename COMPONENT (#831 review nit). The label is
    written to disk as `<slug><suffix>` and `derive(recorded-fallback)` re-derives the label from the file
    STEM, so a label containing `/` (or other path separators / unsafe chars) would write to a subdir, can
    escape inputs_dir, and the round-tripped stem would not match. Strip path separators + any char unsafe
    for a filename down to a stable [A-Za-z0-9._-] slug; collapse runs of `-`; trim leading/trailing `.-`;
    fall back to `fallback` (e.g. `stub<i>`) when nothing usable survives so distinct stubs stay distinct."""
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", str(label))
    slug = re.sub(r"-{2,}", "-", slug).strip("-.")
    return slug or fallback


def _author_recipe_spec(recipe: str) -> dict:
    """Build a run_role spec for an author/reviewer role directly from its SKILL.md recipe (reuses
    relay.SKILLS_DIR). Pure text-generation roles — NO file tools — so they need no repo checkout and are
    trivially stubbable. This deliberately does NOT add a new chain role (no agents/<role>.md, not in
    REQUIRED_ROLES), so the profile completeness gate is untouched."""
    sp = relay.SKILLS_DIR / recipe / "SKILL.md"
    skill_text = sp.read_text() if sp.exists() else ""
    body = ("You are a single-shot specialist invoked by the lfah red-test mutation gate. Follow your "
            "specialist manual below exactly and emit ONLY what it asks for.")
    return {"role": recipe, "tools": [], "skills": [recipe], "system_prompt": body + "\n\n" + skill_text}


# ---------------------------------------------------------------------------
# derive — author writes test + reference + wrong-stubs from picks; emits ELI5; STOPS for approval
# ---------------------------------------------------------------------------
def derive(*, picks_path: Path, out_dir: Path, author_model: str = "opus",
           executor_model: str = "sonnet", reviewer_model: str = "sonnet",
           source: str = "live-agent", reference_path: Path | None = None,
           wrong_stub_paths: list | None = None, agent_test_path: Path | None = None,
           run_role=None, dry_run: bool = False) -> dict:
    """Step 1. The author derives the test + reference + wrong-stubs from the picks ONLY.

    source="live-agent"      -> invoke the CLOUD author role via relay.run_role (model != executor_model).
    source="recorded-fallback" -> assemble the bundle from on-disk reference + wrong-stub + test files
                                   (used when the live cloud author role is unreachable; the gate still
                                   proves discrimination deterministically — only provenance differs).

    Writes the derive bundle (agent test, reference, wrong-stubs, ELI5.md, derive.json) into out_dir and
    returns the manifest. Does NOT run the gate."""
    run_role = run_role or relay.run_role
    picks = json.loads(Path(picks_path).read_text())
    spec = picks.get("spec", "")
    example_table = picks.get("example_table") or []
    module = picks.get("module", "src/module.js")
    test_path = picks.get("test_path", "__tests__/module.test.js")
    phase = picks.get("phase", "phase")

    assert_author_inputs_clean({"spec": spec, "picks": picks, "example_table": example_table})
    prompt = build_author_prompt(spec=spec, picks=picks, example_table=example_table)

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    if source == "live-agent":
        spec_role = _author_recipe_spec(AUTHOR_RECIPE)
        resp = run_role(spec=spec_role, model=author_model, backend="cloud", user_prompt=prompt,
                        cwd=out_dir, max_turns=4, dry_run=dry_run)
        bundle = parse_author_response(resp.get("response", ""))
        agent_test = bundle["agent_test"]
        reference = bundle["reference"]
        wrong_stubs = bundle["wrong_stubs"]
        eli5 = bundle["eli5"]
        author_cost = resp.get("cost_usd", 0.0)
        author_model_resolved = resp.get("model_resolved", author_model)
    elif source == "recorded-fallback":
        if not (reference_path and wrong_stub_paths and agent_test_path):
            raise ValueError("recorded-fallback derive needs reference_path, wrong_stub_paths, agent_test_path")
        agent_test = Path(agent_test_path).read_text()
        reference = Path(reference_path).read_text()
        wrong_stubs = []
        for p in wrong_stub_paths:
            p = Path(p)
            wrong_stubs.append({"label": p.stem, "why": "operator must-REJECT pick (recorded)",
                                "code": p.read_text()})
        eli5 = picks.get("eli5") or ("The WRONG version differs from the RIGHT one in exactly the behavior "
                                     "the operator named in the picks; the test catches that difference.")
        author_cost = 0.0
        author_model_resolved = author_model
    else:
        raise ValueError(f"unknown source: {source!r} (expected live-agent | recorded-fallback)")

    # Near-correct-mutant guard (record per stub; the gate folds it into `discriminates`).
    ref_exports = module_exports(reference)
    for ws in wrong_stubs:
        ws_exports = module_exports(ws["code"])
        ws["surface_match"] = (ws_exports == ref_exports) and bool(ref_exports)
        ws["reference_exports"] = sorted(ref_exports)
        ws["stub_exports"] = sorted(ws_exports)

    # Write the bundle files.
    test_file = out_dir / Path(test_path).name
    test_file.write_text(agent_test)
    ref_file = out_dir / Path(module).name
    ref_file.write_text(reference)
    stub_files = []
    for ws in wrong_stubs:
        sf = out_dir / f"wrong_stub_{ws['label']}{Path(module).suffix}"
        sf.write_text(ws["code"])
        stub_files.append({"label": ws["label"], "why": ws.get("why", ""), "file": sf.name,
                           "surface_match": ws["surface_match"],
                           "reference_exports": ws["reference_exports"], "stub_exports": ws["stub_exports"]})

    eli5_md = out_dir / "ELI5.md"
    eli5_md.write_text(
        f"# ELI5 — red-test mutation gate for phase `{phase}`\n\n"
        "## What the operator asked for\n- ACCEPT: " + "; ".join(picks.get("accept") or []) + "\n"
        "- REJECT: " + "; ".join(picks.get("reject") or []) + "\n\n"
        "## The three derived versions\n"
        f"- **agent test** (`{test_file.name}`): the failing acceptance test, written from the picks.\n"
        f"- **reference** (`{ref_file.name}`): the RIGHT version that satisfies every pick.\n"
        + "".join(f"- **wrong-stub** (`wrong_stub_{ws['label']}{Path(module).suffix}`): {ws.get('why','')}\n"
                  for ws in wrong_stubs)
        + f"\n## Why the wrong version is wrong (author's words)\n{eli5}\n\n"
        f"## What happens next\nAfter you APPROVE, the mutation gate plants each WRONG version (the test "
        f"MUST go red) and the REFERENCE (the test MUST go green). A test that can't tell them apart is "
        f"rejected as a fake oracle.\n")

    manifest = {
        "phase": phase,
        "module": module,
        "test_path": test_path,
        "source": source,
        "author": {"role": AUTHOR_RECIPE, "model": author_model, "model_resolved": author_model_resolved,
                   "source": source, "cost_usd": author_cost},
        "executor_model": executor_model,
        "reviewer_model": reviewer_model,
        "picks_file": str(picks_path),   # stored AS PASSED (repo-relative when invoked from the repo root)
        "author_inputs": {"spec": spec, "picks": picks, "example_table": example_table},
        "files": {"agent_test": test_file.name, "reference": ref_file.name, "wrong_stubs": stub_files},
        "eli5": eli5,
        "derived_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    (out_dir / "derive.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return manifest


# ---------------------------------------------------------------------------
# Throwaway scaffold + new-file diffs for the jest oracle
# ---------------------------------------------------------------------------
_SCAFFOLD_PKG = {
    "name": "redtest-gate-scaffold", "version": "0.0.0", "private": True,
    "scripts": {"test": "jest"}, "devDependencies": {"jest": "^29.7.0"},
}


def _git(args, cwd):
    return subprocess.run(["git", "-c", "user.email=redtest@local", "-c", "user.name=redtest-gate",
                           *args], cwd=str(cwd), capture_output=True, text=True)


def build_throwaway_scaffold(*, data_dir: Path, instance_id: str, test_path: str, test_content: str,
                             node_modules_src: Path | None = None) -> dict:
    """A FRESH minimal node/jest project as the gate instance — NOT a symlink to any shared project
    (a symlink would make jest_oracle_eval's copytree drag in node_modules). The repo commits ONLY the
    agent test at base; src/ is ABSENT (so the test is RED at base) and instance.json declares
    f2p_tests=[the agent test] so the oracle keeps its >=1-F2P guarantee. Optionally seeds node_modules
    (copied, not symlinked) so the per-call `npm install` is a near-noop / offline."""
    data_dir = Path(data_dir)
    inst_dir = data_dir / "instances" / instance_id
    repo = inst_dir / "repo"
    if repo.exists():
        shutil.rmtree(repo)
    (repo / "__tests__").mkdir(parents=True, exist_ok=True)
    (repo / ".gitignore").write_text("node_modules/\n.jestout.json\ncoverage/\n")
    (repo / "package.json").write_text(json.dumps(_SCAFFOLD_PKG, indent=2) + "\n")
    tp = repo / test_path
    tp.parent.mkdir(parents=True, exist_ok=True)
    tp.write_text(test_content)
    _git(["init", "-q"], cwd=repo)
    _git(["add", ".gitignore", "package.json", test_path], cwd=repo)
    _git(["commit", "-q", "-m", "scaffold: red acceptance test (src absent)"], cwd=repo)
    base = _git(["rev-parse", "HEAD"], cwd=repo).stdout.strip()
    if node_modules_src and Path(node_modules_src).exists() and not (repo / "node_modules").exists():
        shutil.copytree(node_modules_src, repo / "node_modules", symlinks=False)
    instance = {
        "instance_id": instance_id, "repo": f"local/{instance_id}", "base_commit": base,
        "language": "javascript", "problem_statement": "red-test mutation gate scaffold",
        "FAIL_TO_PASS": test_path, "f2p_tests": [test_path], "p2p_tests": [],
        "test_files": [test_path],
    }
    inst_dir.mkdir(parents=True, exist_ok=True)
    (inst_dir / "instance.json").write_text(json.dumps(instance, indent=2) + "\n")
    return {"data_dir": str(data_dir), "instance_id": instance_id, "repo": str(repo), "base_commit": base}


def make_new_file_diff(*, repo: Path, rel_path: str, content: str, out_path: Path) -> Path:
    """Produce a valid git diff that CREATES `rel_path` with `content` on top of the scaffold base, then
    restore the repo to a clean state. The gate feeds this diff to jest_oracle_eval (which applies it,
    re-imposes the graded test from base, and runs jest)."""
    repo = Path(repo)
    target = repo / rel_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)
    _git(["add", rel_path], cwd=repo)
    diff = _git(["diff", "--cached", "HEAD", "--", rel_path], cwd=repo).stdout
    _git(["reset", "-q", "HEAD", rel_path], cwd=repo)
    if target.exists():
        target.unlink()
    Path(out_path).write_text(diff)
    return Path(out_path)


# ---------------------------------------------------------------------------
# gate — the mutation gate (jest_oracle_eval ×2) + advisory reviewer -> committed gate-log
# ---------------------------------------------------------------------------
def run_mutation_gate(*, derive_manifest: dict, bundle_dir: Path, work_dir: Path, jest_eval=None,
                      node_modules_src: Path | None = None) -> dict:
    """The mechanical proof. In a fresh throwaway scaffold, run jest_oracle_eval against EACH wrong-stub
    (expect resolved:false) and the reference (expect resolved:true). `discriminates` is true IFF the
    reference resolves, EVERY wrong-stub does NOT resolve, AND every wrong-stub has the identical module
    surface (a near-correct mutant — so discrimination proves behavior, not a load failure)."""
    jest_eval = jest_eval or relay.jest_oracle_eval
    # ABSOLUTE paths throughout: jest_oracle_eval runs `git -C <jestrepo> apply <diff>`, so a RELATIVE
    # diff path (or LFAH_DATA_DIR) resolves against the wrong cwd inside the oracle and silently fails
    # to apply (-> the reference looks unresolved). Resolve here so the oracle always sees absolute paths.
    work_dir = Path(work_dir).resolve()
    work_dir.mkdir(parents=True, exist_ok=True)
    data_dir = work_dir / "data"
    bundle_dir = Path(bundle_dir)              # where derive.json + the test/reference/stub files live

    module = derive_manifest["module"]
    test_path = derive_manifest["test_path"]
    files = derive_manifest["files"]
    instance_id = f"redtest-{derive_manifest['phase']}"

    test_content = (bundle_dir / files["agent_test"]).read_text()
    reference_content = (bundle_dir / files["reference"]).read_text()

    scaf = build_throwaway_scaffold(data_dir=data_dir, instance_id=instance_id, test_path=test_path,
                                    test_content=test_content, node_modules_src=node_modules_src)
    repo = Path(scaf["repo"])

    # The jest oracle reads LFAH_DATA_DIR/instances/<id>/{instance.json,repo}; LFAH_JEST_DOCKER=0 = host node.
    prev = {k: os.environ.get(k) for k in ("LFAH_DATA_DIR", "LFAH_JEST_DOCKER", "LFAH_JEST_WORKROOT")}
    os.environ["LFAH_DATA_DIR"] = str(data_dir)
    os.environ["LFAH_JEST_DOCKER"] = "0"
    os.environ["LFAH_JEST_WORKROOT"] = str(work_dir / "_runs")
    try:
        # reference -> expect resolved:true
        ref_diff = make_new_file_diff(repo=repo, rel_path=module, content=reference_content,
                                      out_path=work_dir / "reference.diff")
        ref_res = jest_eval(instance_id, ref_diff, "reference")
        reference = {"resolved": bool(ref_res.get("resolved")), "rc": ref_res.get("rc"),
                     "reimpose_rc": ref_res.get("reimpose_rc")}

        # each wrong-stub -> expect resolved:false
        wrong_all = []
        for ws in files["wrong_stubs"]:
            stub_content = (bundle_dir / ws["file"]).read_text()
            wdiff = make_new_file_diff(repo=repo, rel_path=module, content=stub_content,
                                       out_path=work_dir / f"wrong_{ws['label']}.diff")
            wres = jest_eval(instance_id, wdiff, f"wrong-{ws['label']}")
            wrong_all.append({"label": ws["label"], "why": ws.get("why", ""),
                              "resolved": bool(wres.get("resolved")), "rc": wres.get("rc"),
                              "surface_match": bool(ws.get("surface_match")),
                              "reference_exports": ws.get("reference_exports"),
                              "stub_exports": ws.get("stub_exports")})
    finally:
        for k, v in prev.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    discriminates = (reference["resolved"] is True
                     and bool(wrong_all)
                     and all((w["resolved"] is False) and w["surface_match"] for w in wrong_all))
    representative = wrong_all[0] if wrong_all else {"resolved": None, "surface_match": False}
    return {"discriminates": discriminates,
            "mutants": {"wrong": representative, "wrong_all": wrong_all, "reference": reference}}


def review(*, derive_manifest: dict, gate_result: dict, bundle_dir: Path, reviewer_model: str = "sonnet",
           author_model: str = "opus", run_role=None, dry_run: bool = False) -> dict:
    """Advisory fresh-eyes reviewer (slice 1): a role whose model != the author model reads the test +
    reference + wrong-stubs + the operator's intent and returns a verdict. Advisory only — recorded, not
    blocking (blocking is slice 2)."""
    if reviewer_model == author_model:
        raise ValueError(f"reviewer model must differ from author model (both {reviewer_model!r})")
    run_role = run_role or relay.run_role
    bundle_dir = Path(bundle_dir)
    files = derive_manifest["files"]
    test_content = (bundle_dir / files["agent_test"]).read_text()
    reference_content = (bundle_dir / files["reference"]).read_text()
    picks = derive_manifest["author_inputs"]["picks"]
    stubs = "\n\n".join(f"### wrong-stub {ws['label']} (violates: {ws.get('why','')})\n"
                        f"```js\n{(bundle_dir / ws['file']).read_text()}\n```"
                        for ws in files["wrong_stubs"])
    prompt = (
        "You are a fresh-eyes reviewer of an agent-authored red test and its mutants. The operator's "
        "intent:\n"
        f"- ACCEPT: {picks.get('accept')}\n- REJECT: {picks.get('reject')}\n\n"
        f"## Acceptance test\n```js\n{test_content}\n```\n\n"
        f"## Reference implementation\n```js\n{reference_content}\n```\n\n"
        f"## Wrong-stubs\n{stubs}\n\n"
        f"## Mechanical gate result\n{json.dumps(gate_result['mutants'], indent=2)}\n\n"
        "Does the test faithfully capture the operator's ACCEPT/REJECT intent, and is it NOT gameable "
        "(e.g. it does more than merely `require()` the module)? End your reply with exactly one line:\n"
        "VERDICT: PASS    (test captures intent, not gameable)\n"
        "or\nVERDICT: CONCERN — <one-line reason>")
    spec_role = _author_recipe_spec(REVIEW_RECIPE)
    resp = run_role(spec=spec_role, model=reviewer_model, backend="cloud", user_prompt=prompt,
                    cwd=bundle_dir, max_turns=3, dry_run=dry_run)
    text = resp.get("response", "")
    m = re.search(r"VERDICT:\s*(PASS|CONCERN)", text, re.IGNORECASE)
    verdict = m.group(1).upper() if m else "UNCLEAR"
    return {"verdict": verdict, "model": reviewer_model, "advisory": True,
            "notes": text.strip()[-_REVIEWER_NOTES_MAXLEN:], "cost_usd": resp.get("cost_usd", 0.0)}


def reviewer_verdict_ok(verdict) -> bool:
    """A reviewer verdict is acceptable IFF it is exactly PASS (case-insensitive). CONCERN/UNCLEAR/None
    are non-PASS. The build path + the slice-2 CLI treat a non-PASS verdict as BLOCKING."""
    return isinstance(verdict, str) and verdict.strip().upper() == _REVIEWER_PASS


class GateRefusal(RuntimeError):
    """Raised when the mutation gate REFUSES a test (slice 2): either the test does not discriminate a
    wrong-stub from the reference, or — with reviewer-blocking on — the fresh-eyes reviewer did not PASS.
    Carries the written gate-log path so the paper trail survives the refusal."""

    def __init__(self, message: str, gate_log: dict | None = None):
        super().__init__(message)
        self.gate_log = gate_log


def _refusal_reason(gate_result: dict, rev: dict, *, block_on_reviewer: bool) -> str | None:
    """Return a human-readable refusal reason (naming the failing mutant) if the gate must REFUSE, else
    None. Discrimination is always blocking; the reviewer verdict blocks only when block_on_reviewer."""
    if not gate_result["discriminates"]:
        mutants = gate_result["mutants"]
        if not mutants.get("reference", {}).get("resolved"):
            return "reference did not resolve (the test fails even against the correct code)"
        offenders = [w["label"] for w in mutants.get("wrong_all", [])
                     if not (w["resolved"] is False and w["surface_match"])]
        if offenders:
            return (f"wrong-stub(s) {offenders} failed to discriminate (the test does not go RED "
                    f"against them, or they expose a different module surface)")
        return "the test did not discriminate (non-near-correct mutants)"
    if block_on_reviewer and not reviewer_verdict_ok(rev["verdict"]):
        return f"fresh-eyes reviewer verdict {rev['verdict']!r} is not PASS"
    return None


def gate(*, bundle_dir: Path, results_dir: Path, work_dir: Path, approved: bool = False,
         approval_path: Path | None = None, jest_eval=None, run_role=None,
         node_modules_src: Path | None = None, dry_run: bool = False,
         block_on_reviewer: bool = False) -> dict:
    """Step 2. Runs ONLY with a recorded approval. Runs the mutation gate + fresh-eyes reviewer and writes
    the committed gate-log results/AUTHORTEST-<phase>.json. Returns the gate-log dict (it does NOT raise on
    a non-discriminating test — the return value carries `discriminates`/`refused`/`refusal_reason`, and
    the CALLER decides whether to block). The build path uses `gate_phase_test`, which DOES raise.

    block_on_reviewer (slice 2): when True a non-PASS reviewer verdict counts toward `refused`/
    `refusal_reason` (and flips the reviewer record from advisory→blocking). Default False = the slice-1
    advisory-only behavior (verdict recorded, never blocks)."""
    bundle_dir = Path(bundle_dir)
    derive_manifest = json.loads((bundle_dir / "derive.json").read_text())

    approval = None
    if approval_path and Path(approval_path).exists():
        approval = json.loads(Path(approval_path).read_text())
    approved_ok = approved or bool((approval or {}).get("approved"))
    if not approved_ok:
        raise PermissionError(
            "gate refused: no recorded approval. Run `author-test derive`, surface the ELI5 to the "
            "operator, then re-run gate with --approved (or an approval.json carrying approved:true).")

    author_model = derive_manifest["author"]["model"]
    reviewer_model = derive_manifest.get("reviewer_model", "sonnet")
    executor_model = derive_manifest["executor_model"]

    # Fail-fast on a mis-configured reviewer (== author) BEFORE the expensive mutation gate runs. `review`
    # enforces the same invariant, but only after jest×2 — wasteful when the config is wrong up front.
    if reviewer_model == author_model:
        raise ValueError(f"reviewer model must differ from author model (both {reviewer_model!r})")

    gate_result = run_mutation_gate(derive_manifest=derive_manifest, bundle_dir=bundle_dir,
                                    work_dir=work_dir, jest_eval=jest_eval,
                                    node_modules_src=node_modules_src)
    rev = review(derive_manifest=derive_manifest, gate_result=gate_result, bundle_dir=bundle_dir,
                 reviewer_model=reviewer_model, author_model=author_model, run_role=run_role,
                 dry_run=dry_run)

    reason = _refusal_reason(gate_result, rev, block_on_reviewer=block_on_reviewer)
    gate_log = {
        "phase": derive_manifest["phase"],
        "module": derive_manifest["module"],
        "discriminates": gate_result["discriminates"],
        "mutants": gate_result["mutants"],
        "author": derive_manifest["author"],
        "executor_model": executor_model,
        "reviewer": {"verdict": rev["verdict"], "model": rev["model"],
                     "advisory": not block_on_reviewer, "blocking": block_on_reviewer,
                     "notes": rev["notes"]},
        "picks_file": derive_manifest["picks_file"],
        "author_inputs": derive_manifest["author_inputs"],
        "approval": approval or {"approved": True, "source": "--approved flag"},
        "gate_run": {"jest_docker": "0", "loop": "jest_oracle_eval x2 (wrong->false, reference->true)"},
        "refused": reason is not None,
        "refusal_reason": reason,
        "gated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    results_dir = Path(results_dir)
    results_dir.mkdir(parents=True, exist_ok=True)
    out = results_dir / f"AUTHORTEST-{derive_manifest['phase']}.json"
    out.write_text(json.dumps(gate_log, indent=2) + "\n")
    gate_log["_gate_log_path"] = str(out)
    return gate_log


# ---------------------------------------------------------------------------
# gate_phase_test — the build-run entrypoint: run the gate, RAISE on any refusal (#831 slice 2)
# ---------------------------------------------------------------------------
def gate_phase_test(*, picks: dict, reference: str, wrong_stubs: list, agent_test: str,
                    phase: str, module: str, test_path: str, work_dir: Path, results_dir: Path,
                    author_model: str = "opus", executor_model: str = "sonnet",
                    reviewer_model: str = "sonnet", jest_eval=None, run_role=None,
                    node_modules_src: Path | None = None, dry_run: bool = False) -> dict:
    """Wire the mutation gate into `build.run_phase`. Given a phase's AGENT-AUTHORED inputs (the operator
    picks + an already-authored reference/wrong-stubs/test carried INLINE in the build manifest), assemble
    a derive bundle (source='recorded-fallback' — no live author call, the build already has the artifacts),
    run the mutation gate + a BLOCKING fresh-eyes reviewer, and RAISE GateRefusal if the test does not
    discriminate OR the reviewer does not PASS. On success returns the gate-log dict. Reuses `derive` +
    `gate` (block_on_reviewer=True) — no duplicated jest×2 logic, no relay.py changes."""
    if not (picks and reference and wrong_stubs and agent_test):
        raise ValueError("gate_phase_test needs non-empty picks, reference, wrong_stubs, and agent_test")
    work_dir = Path(work_dir)
    bundle_dir = work_dir / "bundle"
    # Persist the inline inputs to temp files so the recorded-fallback derive can read them, exactly as the
    # standalone CLI path does (one assembly path, not two).
    inputs_dir = work_dir / "inputs"
    inputs_dir.mkdir(parents=True, exist_ok=True)
    picks_payload = dict(picks)
    picks_payload.setdefault("phase", phase)
    picks_payload.setdefault("module", module)
    picks_payload.setdefault("test_path", test_path)
    picks_path = inputs_dir / "picks.json"
    picks_path.write_text(json.dumps(picks_payload, indent=2) + "\n")
    ref_path = inputs_dir / "reference.js"
    ref_path.write_text(reference)
    test_in = inputs_dir / "agent_test.js"
    test_in.write_text(agent_test)
    stub_paths = []
    for i, ws in enumerate(wrong_stubs):
        label = (ws.get("label") if isinstance(ws, dict) else None) or f"stub{i}"
        code = ws.get("code") if isinstance(ws, dict) else ws
        if not code:
            raise ValueError(f"gate_phase_test: wrong_stub {label!r} has no code")
        # Name the file by the SANITIZED label so the recorded-fallback derive (which derives a stub's
        # label from its file STEM) keeps a matching label -> the gate run_id stays `wrong-<slug>`. The
        # raw label may contain `/` or other filename-unsafe chars (which would escape inputs_dir / break
        # the stem round-trip), so slugify before using it as a filename component.
        safe_label = _safe_label_slug(label, f"stub{i}")
        sp = inputs_dir / f"{safe_label}{Path(module).suffix}"
        sp.write_text(code)
        stub_paths.append(sp)

    derive(picks_path=picks_path, out_dir=bundle_dir, source="recorded-fallback",
           reference_path=ref_path, wrong_stub_paths=stub_paths, agent_test_path=test_in,
           author_model=author_model, executor_model=executor_model, reviewer_model=reviewer_model)
    log = gate(bundle_dir=bundle_dir, results_dir=results_dir, work_dir=work_dir / "gate",
               approved=True, jest_eval=jest_eval, run_role=run_role,
               node_modules_src=node_modules_src, dry_run=dry_run, block_on_reviewer=True)
    if log.get("refused"):
        raise GateRefusal(
            f"red-test mutation gate REFUSED phase {phase!r}: {log['refusal_reason']} "
            f"(gate-log: {log['_gate_log_path']})", gate_log=log)
    return log
