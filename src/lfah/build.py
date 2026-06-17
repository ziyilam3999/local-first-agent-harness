"""Greenfield BUILD orchestrator — drive a from-scratch app build as a sequence of test-first phases
through the stock lfah fix-chain, with NO engine changes (#648/#649).

A greenfield build reuses everything the bug-fix chain already has: each phase ships a RED acceptance
test (FAIL_TO_PASS), the executor makes it pass, the real test grades it. The only additions over a
single `lfah run` are (a) a project SCAFFOLD, (b) a per-phase loop that COMMITS on SHIP so the base
advances for the next phase, and (c) the `both` ship-gate by default (the phase's acceptance test is OUR
own AC, not ground truth, so ship requires the test green AND the independent evaluator satisfied — this
backstops a coverage-gap test; see #660).

The chain itself is invoked via the stock `lfah run` CLI per phase (so the both-gate, INFRA-SKIP handling,
telemetry, and faithfulness asserts are all inherited verbatim). `run_cmd` is injectable so the loop is
unit-testable with a stub runner (no models, no GPU).

Manifest schema (JSON):
  {
    "project_name": "my-app",
    "language": "javascript",          # "javascript"/"typescript" -> jest profile; omit/other -> pytest
    "phases": [
      { "id": "p1", "title": "...",
        "test_file": "phases/p1.test.js",          # source of the RED acceptance test (relative to manifest)
        "test_path": "__tests__/p1.test.js",        # where it lands in the project
        "f2p": "__tests__/p1.test.js",              # FAIL_TO_PASS id (file or file:testname)
        "p2p": ["__tests__/prev.test.js"],          # tests that must stay green
        "problem_statement": "Implement ... so the failing test passes. Do NOT modify tests." },
      ...
    ]
  }
"""
from __future__ import annotations
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

# A minimal Node/jest scaffold for `language: javascript`. Other languages scaffold only a git repo +
# the phase tests (the executor + the test_file bring their own deps/config).
_JS_PKG = {
    "name": "greenfield-app", "version": "0.0.0", "private": True,
    "scripts": {"test": "jest"}, "devDependencies": {"jest": "^29.7.0"},
}
# TypeScript scaffold (#672 verdict): ts-jest plugs into the SAME `npx jest` oracle, so the engine/oracle
# need NO change. Type-checking is ON (tsconfig `isolatedModules: false`) so a TYPE error fails the test
# run — a cheap extra correctness gate for the agent executor, beyond the acceptance test's own asserts.
# Tests stay CommonJS (module: commonjs) to dodge jest's ESM rough edge.
_TS_PKG = {
    "name": "greenfield-app", "version": "0.0.0", "private": True,
    "scripts": {"test": "jest"},
    "devDependencies": {
        "jest": "^30.0.0", "ts-jest": "^29.4.0", "typescript": "^5.4.0",
        "@types/jest": "^30.0.0", "@types/node": "^20.0.0",
    },
}
_TS_JEST_CONFIG = ("/** @type {import('ts-jest').JestConfigWithTsJest} */\n"
                   "module.exports = { preset: 'ts-jest', testEnvironment: 'node' };\n")
_TSCONFIG = {
    "compilerOptions": {
        "target": "ES2021", "module": "commonjs", "lib": ["ES2021"], "strict": True,
        "esModuleInterop": True, "skipLibCheck": True, "forceConsistentCasingInFileNames": True,
        "isolatedModules": False, "types": ["jest", "node"], "rootDir": ".", "outDir": "dist",
    },
    "include": ["src/**/*.ts", "__tests__/**/*.ts"],
}
_GITIGNORE = "node_modules/\n.jestout.json\n.eval_patch_jest-*\ncoverage/\ndist/\n.ai-workspace/\n"
_NODE_LANGS = ("javascript", "js", "typescript", "ts")
_TS_LANGS = ("typescript", "ts")


def _sh(cmd, cwd=None, env=None, check=True):
    r = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True)
    if check and r.returncode != 0:
        raise RuntimeError(f"cmd failed ({r.returncode}): {' '.join(map(str, cmd))}\n{r.stdout}\n{r.stderr}")
    return r


def _git(args, cwd, **kw):
    return _sh(["git", "-c", "user.email=greenfield@local", "-c", "user.name=greenfield-build",
                *args], cwd=cwd, **kw)


_SCAFFOLD_MSG = "scaffold: empty greenfield project"


def _looks_like_greenfield_project(project: Path) -> bool:
    """A REUSABLE lfah greenfield project: an existing git repo whose ROOT commit is the lfah scaffold. Keying
    off the scaffold's own root commit (not bare `.git`) means we never lay phase commits onto an UNRELATED
    repo that happens to live at --project — only projects lfah itself scaffolded are reused. Language-agnostic
    (a non-node scaffold has no package.json), so the scaffold marker commit is the signal."""
    if not (project / ".git").exists():
        return False
    roots = _sh(["git", "rev-list", "--max-parents=0", "HEAD"], cwd=project, check=False)
    root = (roots.stdout or "").strip().splitlines()
    if not root:
        return False
    subj = _sh(["git", "log", "-1", "--format=%s", root[-1]], cwd=project, check=False)
    return (subj.stdout or "").strip() == _SCAFFOLD_MSG


def scaffold_project(project: Path, language: str, *, npm_install: bool = True, fresh: bool = False) -> bool:
    """Ensure `project` is a runnable greenfield repo (jest harness for javascript/typescript).

    By DEFAULT this REUSES an existing project: if `project` already exists and is an lfah scaffold (a git repo
    whose root commit is the scaffold marker), it is kept and the build's phases are laid on top of HEAD — so
    successive `lfah build` runs accumulate into the SAME folder instead of each phase landing in a fresh one.
    Pass `fresh=True` to force a clean wipe + re-scaffold. Returns True if an existing project was reused, False
    if a fresh scaffold was created.
    """
    language = (language or "").lower()
    if project.exists() and not fresh and _looks_like_greenfield_project(project):
        # REUSE: keep the project + its git history; build new phases on top of HEAD. Only (re)install deps
        # when they're actually missing (e.g. a fresh clone of the repo) so the jest oracle can run.
        if npm_install and language in _NODE_LANGS and not (project / "node_modules").exists():
            _sh(["npm", "install", "--silent", "--no-audit", "--no-fund"], cwd=project)
        return True
    if project.exists():
        shutil.rmtree(project)
    project.mkdir(parents=True)
    (project / "__tests__").mkdir()
    (project / "src").mkdir()
    (project / ".gitignore").write_text(_GITIGNORE)
    tracked = [".gitignore"]
    if language in _TS_LANGS:
        (project / "package.json").write_text(json.dumps(_TS_PKG, indent=2) + "\n")
        (project / "jest.config.js").write_text(_TS_JEST_CONFIG)
        (project / "tsconfig.json").write_text(json.dumps(_TSCONFIG, indent=2) + "\n")
        tracked += ["package.json", "jest.config.js", "tsconfig.json"]
    elif language in ("javascript", "js"):
        (project / "package.json").write_text(json.dumps(_JS_PKG, indent=2) + "\n")
        tracked.append("package.json")
    _git(["init", "-q"], cwd=project)
    _git(["add", *tracked], cwd=project)
    _git(["commit", "-q", "-m", _SCAFFOLD_MSG], cwd=project)
    if npm_install and language in _NODE_LANGS:
        _sh(["npm", "install", "--silent", "--no-audit", "--no-fund"], cwd=project)
    return False


def _default_run_cmd() -> list:
    """How to invoke the stock `lfah run` chain. Prefer the console script; fall back to `-m lfah.cli`."""
    exe = shutil.which("lfah")
    return [exe] if exe else [sys.executable, "-m", "lfah.cli"]


def _phase_agent_inputs(phase: dict) -> dict | None:
    """A phase is AGENT-AUTHORED (so the mutation gate applies) iff it carries picks + reference +
    wrong_stubs (all present and non-empty). A human-supplied phase (NONE of those three keys) returns
    None and the gate is skipped — existing builds with plain `test_file`-only phases are untouched. The
    agent_test text comes from the phase's `test_file` content (the same file `run_phase` lays down).

    FAIL CLOSED on a PARTIALLY-specified agent phase (#831 review nit): a phase that carries ANY of
    picks/reference/wrong_stubs (clearly agent-authored intent) but NOT all three present-and-non-empty
    must REFUSE — e.g. an author that produced a test but `wrong_stubs: []` (zero valid mutants) is
    EXACTLY the bypass the gate exists to catch. Such a phase raises GateRefusal naming what's missing,
    rather than silently skipping the gate and committing an ungated RED test."""
    keys = ("picks", "reference", "wrong_stubs")
    present = [k for k in keys if phase.get(k)]
    if not present:
        return None   # human-supplied phase: NONE of the three -> gate skipped (unchanged)
    missing = [k for k in keys if not phase.get(k)]
    if missing:
        from . import authortest
        raise authortest.GateRefusal(
            f"partially-specified agent phase {phase.get('id')!r}: carries {present} but is missing or "
            f"has empty {missing} — an agent-authored phase needs all of picks/reference/wrong_stubs "
            f"present and non-empty (refusing rather than silently skipping the mutation gate)")
    return {"picks": phase["picks"], "reference": phase["reference"],
            "wrong_stubs": phase["wrong_stubs"]}


def gate_agent_authored_test(phase: dict, *, agent_test: str, gate_work: Path, results_dir: Path,
                             jest_eval=None, run_role=None,
                             node_modules_src: Path | None = None) -> dict | None:
    """If the phase is agent-authored, run the red-test mutation gate (authortest.gate_phase_test) BEFORE
    the test is committed. RAISES authortest.GateRefusal if the test does not discriminate a wrong-stub
    from the reference, or the (now BLOCKING) fresh-eyes reviewer does not PASS — which halts the phase.
    Returns the gate-log on PASS, or None when the phase is human-supplied (no agent inputs -> no gate)."""
    inputs = _phase_agent_inputs(phase)
    if inputs is None:
        return None
    from . import authortest
    return authortest.gate_phase_test(
        picks=inputs["picks"], reference=inputs["reference"], wrong_stubs=inputs["wrong_stubs"],
        agent_test=agent_test, phase=phase["id"], module=phase.get("module", "src/module.js"),
        test_path=phase["test_path"], work_dir=gate_work, results_dir=results_dir,
        author_model=phase.get("author_model", "opus"),
        executor_model=phase.get("executor_model", "sonnet"),
        reviewer_model=phase.get("reviewer_model", "sonnet"),
        jest_eval=jest_eval, run_role=run_role, node_modules_src=node_modules_src)


def run_phase(phase: dict, *, project: Path, data: Path, out: Path, manifest_dir: Path,
              language: str, env: dict, run_cmd: list, executor_recipe: str | None = None,
              planner_model: str = "opus", evaluator_model: str = "opus",
              executor_backend: str = "local", executor_model: str = "sonnet",
              gate_jest_eval=None, gate_run_role=None,
              gate_node_modules_src: Path | None = None) -> dict:
    """Lay the phase's RED test + commit (-> base), run the chain, and on SHIP commit the work (-> advance).

    For an AGENT-AUTHORED phase (carries picks/reference/wrong_stubs), the red-test mutation gate runs
    BEFORE the test is committed; a non-discriminating test or a non-PASS reviewer RAISES and halts the
    phase (#831 slice 2). `gate_jest_eval`/`gate_run_role` inject stubs for unit tests (no models/node)."""
    pid = phase["id"]
    test_path = phase["test_path"]
    dst = project / test_path
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(manifest_dir / phase["test_file"], dst)

    # GATE the agent-authored RED test BEFORE committing it (#831): prove it discriminates a wrong-stub
    # from the reference + the fresh-eyes reviewer PASSes, else GateRefusal halts the phase. Human-supplied
    # phases (no picks/reference/wrong_stubs) skip the gate entirely. On a refusal the RED test was already
    # copied to `dst` but is never committed — unlink it so a refused phase leaves no stray file on disk.
    from . import authortest
    try:
        gate_log = gate_agent_authored_test(
            phase, agent_test=dst.read_text(), gate_work=out / f"_gate-{pid}",
            results_dir=out / "results", jest_eval=gate_jest_eval, run_role=gate_run_role,
            node_modules_src=gate_node_modules_src)
    except authortest.GateRefusal:
        dst.unlink(missing_ok=True)   # no stray uncommitted RED test left behind
        raise

    _git(["add", test_path], cwd=project)
    # --allow-empty: on a REUSED project, re-laying a phase whose test file is unchanged stages nothing;
    # still record the phase-boundary commit so the base advances (and a re-run doesn't crash).
    _git(["commit", "-q", "--allow-empty", "-m", f"phase {pid}: red acceptance test ({test_path})"], cwd=project)
    base = _git(["rev-parse", "HEAD"], cwd=project).stdout.strip()

    # instance dir with a symlink repo -> the one evolving project (the jest oracle reads
    # LFAH_DATA_DIR/instances/<id>/repo, and the CLI edits instance_dir/repo — same dir via the symlink).
    inst_dir = data / "instances" / pid
    inst_dir.mkdir(parents=True, exist_ok=True)
    repo_link = inst_dir / "repo"
    if repo_link.is_symlink() or repo_link.exists():
        repo_link.unlink()
    repo_link.symlink_to(project)
    instance = {
        "instance_id": pid, "repo": f"local/{project.name}", "base_commit": base,
        "language": language, "problem_statement": phase["problem_statement"],
        "FAIL_TO_PASS": phase["f2p"], "f2p_tests": [phase["f2p"]],
        "p2p_tests": phase.get("p2p", []),
        "test_files": [phase["f2p"], *phase.get("p2p", [])],
    }
    (inst_dir / "instance.json").write_text(json.dumps(instance, indent=2) + "\n")

    # Run the chain. lfah exits 1 when ALL_FAITHFUL is false (still a valid result) -> never check=True
    # here, or a faithfulness flag would crash the whole build. Capture stdout to a per-phase log so a
    # failing phase is self-evident (the #641 lesson).
    # #954 P0: thread the optional executor-recipe override into the per-phase `lfah run` argv (held
    # constant across the build's phases for one A/B arm). Absent -> no flag -> behavior-preserving.
    recipe_argv = ["--executor-recipe", executor_recipe] if executor_recipe else []
    # #961: thread the planner/evaluator models into every phase's run (executor is --local, $0; these
    # two cloud roles carry the cost). Default "opus" matches `lfah run`'s default -> behavior-preserving.
    model_argv = ["--planner", planner_model, "--evaluator", evaluator_model]
    # #966: executor-backend seam. local (default) == historical `--local` (local executor + cloud fallback);
    # cloud runs the executor itself on `--executor <model>` (no local, no fallback) -> fast cells when a slow
    # local model is the bottleneck. The evaluator stays cloud either way, so executor != evaluator still holds.
    if executor_backend == "local":
        exec_argv = ["--local"]
    else:
        exec_argv = ["--executor-backend", "cloud", "--executor", executor_model]
    proc = _sh([*run_cmd, "run", "--instance", str(inst_dir / "instance.json"),
                *exec_argv, "--mode", "c", "--out", str(out), *model_argv, *recipe_argv], env=env, check=False)
    (out / f"{pid}.log").write_text((proc.stdout or "") + "\n--- STDERR ---\n"
                                    + (proc.stderr or "") + f"\n--- rc={proc.returncode} ---\n")
    res_path = out / f"lfah-{pid}-c.json"
    if not res_path.exists():
        raise RuntimeError(f"phase {pid}: no result JSON (rc={proc.returncode}); see {out / (pid + '.log')}")
    res = json.loads(res_path.read_text())
    # SHIP signal for the BUILD: the phase's acceptance test went green via EITHER tier of the lfah chain.
    # `final_resolved` is INTENTIONALLY the LOCAL-only honest result — relay keeps a cloud-handoff outcome in
    # a SEPARATE `handoff` field so the BENCHMARK can attribute wins per tier (do NOT fold it into relay's
    # final_resolved; #601 honesty depends on that separation). But for a BUILD the goal is a green phase, and
    # the local->cloud escalation is a designed lfah feature (LFAH_CLOUD_HANDOFF), not an external rescue — so
    # a handoff-resolved phase SHIPS too (#708). The cloud's files are already on disk (relay reset to base,
    # the cloud wrote the fix, git_diff left them untracked), so the `git add -A` below commits them. We record
    # per-tier resolution + the solving model so the summary stays honest about which tier produced the win.
    local_resolved = bool(res.get("final_resolved"))
    handoff = res.get("handoff") or {}
    handoff_resolved = bool(handoff.get("resolved"))
    shipped = local_resolved or handoff_resolved
    who = "cloud-handoff" if handoff_resolved else ("local" if local_resolved else "none")

    committed = None
    if shipped:
        _git(["add", "-A"], cwd=project)  # gitignore excludes node_modules/.ai-workspace; keep any new dir
        # --allow-empty: a SHIP always records a phase-boundary commit so the base advances, even in the
        # degenerate case where the chain resolved with no net diff (don't crash the whole build on it).
        _git(["commit", "-q", "--allow-empty", "-m",
              f"phase {pid}: SHIP ({who}) — {phase.get('title', test_path)}"], cwd=project)
        committed = _git(["rev-parse", "HEAD"], cwd=project).stdout.strip()
    # Per-phase manifest paper-trail of what the mutation gate used (#831 slice 2). Additive: human-supplied
    # phases carry None for all three + a null gate-log, so existing manifest consumers are unaffected.
    record = {"id": pid, "base": base, "resolved": shipped,
              "local_resolved": local_resolved, "handoff_resolved": handoff_resolved,
              "verdict": res.get("verdict"), "solved_by": who,
              "handoff_model": handoff.get("model_resolved") or handoff.get("model_requested"),
              "iterations": res.get("iterations"), "loop_signal": res.get("loop_signal"),
              "cost_usd": (res.get("telemetry") or {}).get("cost", {}).get("chain_total_cost_usd"),
              "committed": committed, "result_file": str(res_path),
              "picks": phase.get("picks"), "reference": phase.get("reference"),
              "wrong_stubs": phase.get("wrong_stubs"),
              "gate_log": (gate_log.get("_gate_log_path") if gate_log else None),
              "gate_discriminates": (gate_log.get("discriminates") if gate_log else None)}
    return record


def run_build(*, manifest: dict, project: Path, data: Path, out: Path, manifest_dir: Path,
              loop_signal: str = "both", local_timeout_s: str = "900", jest_docker: str = "0",
              run_cmd: list | None = None, npm_install: bool = True, fresh: bool = False,
              planner_model: str = "opus", evaluator_model: str = "opus",
              executor_backend: str = "local", executor_model: str = "sonnet",
              gate_jest_eval=None, gate_run_role=None, gate_node_modules_src: Path | None = None) -> dict:
    """Scaffold (or REUSE) the project + drive every phase; STOP at the first phase that cannot go green.

    By default an existing project is REUSED — the manifest's phases are built on top of its current HEAD, so
    successive builds accumulate into the SAME folder. Pass fresh=True to force a clean wipe + re-scaffold.

    `gate_jest_eval`/`gate_run_role` inject stubs for the per-phase red-test mutation gate (so an
    agent-authored phase can be unit-tested with no models/node); production leaves them None so the gate
    uses the real relay.jest_oracle_eval + relay.run_role.
    """
    language = str(manifest.get("language", "javascript")).lower()
    # Resolve to ABSOLUTE paths: the per-phase instance dir holds a symlink `repo` -> project, and a
    # relative target would resolve relative to the symlink's own location (broken link), plus the role
    # subprocess cwd must be absolute. (A relative-path smoke surfaced this; the unit test used abs tmp_path.)
    project = Path(project).expanduser().resolve()
    data = Path(data).expanduser().resolve()
    out = Path(out).expanduser().resolve()
    out.mkdir(parents=True, exist_ok=True)
    run_cmd = run_cmd or _default_run_cmd()

    env = dict(os.environ)
    env["LFAH_DATA_DIR"] = str(data)
    env["LFAH_JEST_DOCKER"] = jest_docker
    env["LFAH_LOOP_SIGNAL"] = loop_signal   # build's AC is OUR test, not ground truth -> require BOTH (#660)
    env.setdefault("LOCAL_ROLE_TIMEOUT_S", local_timeout_s)

    # #954 P0: a manifest-level executor-recipe override, applied to every phase (held constant per A/B arm).
    # Absent key -> None -> no override -> behavior-preserving for existing manifests.
    executor_recipe = manifest.get("executor_recipe")

    reused = scaffold_project(project, language, npm_install=npm_install, fresh=fresh)
    results, ok = [], True
    for phase in manifest["phases"]:
        r = run_phase(phase, project=project, data=data, out=out, manifest_dir=manifest_dir,
                      language=language, env=env, run_cmd=run_cmd, executor_recipe=executor_recipe,
                      planner_model=planner_model, evaluator_model=evaluator_model,
                      executor_backend=executor_backend, executor_model=executor_model,
                      gate_jest_eval=gate_jest_eval,
                      gate_run_role=gate_run_role, gate_node_modules_src=gate_node_modules_src)
        results.append(r)
        if not r["resolved"]:
            ok = False
            break
    summary = {"project": str(project), "loop_signal": loop_signal, "reused": reused,
               "phases_total": len(manifest["phases"]),
               "phases_shipped": sum(1 for r in results if r["resolved"]),
               "pipeline_complete": ok and len(results) == len(manifest["phases"]),
               "phases": results}
    (out / "BUILD-SUMMARY.json").write_text(json.dumps(summary, indent=2) + "\n")
    return summary
