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


def scaffold_project(project: Path, language: str, *, npm_install: bool = True) -> None:
    """Create an empty, git-init'd project with a runnable test harness (jest for javascript/typescript)."""
    language = (language or "").lower()
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
    _git(["commit", "-q", "-m", "scaffold: empty greenfield project"], cwd=project)
    if npm_install and language in _NODE_LANGS:
        _sh(["npm", "install", "--silent", "--no-audit", "--no-fund"], cwd=project)


def _default_run_cmd() -> list:
    """How to invoke the stock `lfah run` chain. Prefer the console script; fall back to `-m lfah.cli`."""
    exe = shutil.which("lfah")
    return [exe] if exe else [sys.executable, "-m", "lfah.cli"]


def run_phase(phase: dict, *, project: Path, data: Path, out: Path, manifest_dir: Path,
              language: str, env: dict, run_cmd: list) -> dict:
    """Lay the phase's RED test + commit (-> base), run the chain, and on SHIP commit the work (-> advance)."""
    pid = phase["id"]
    test_path = phase["test_path"]
    dst = project / test_path
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(manifest_dir / phase["test_file"], dst)
    _git(["add", test_path], cwd=project)
    _git(["commit", "-q", "-m", f"phase {pid}: red acceptance test ({test_path})"], cwd=project)
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
    proc = _sh([*run_cmd, "run", "--instance", str(inst_dir / "instance.json"),
                "--local", "--mode", "c", "--out", str(out)], env=env, check=False)
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
    return {"id": pid, "base": base, "resolved": shipped,
            "local_resolved": local_resolved, "handoff_resolved": handoff_resolved,
            "verdict": res.get("verdict"), "solved_by": who,
            "handoff_model": handoff.get("model_resolved") or handoff.get("model_requested"),
            "iterations": res.get("iterations"), "loop_signal": res.get("loop_signal"),
            "cost_usd": (res.get("telemetry") or {}).get("cost", {}).get("chain_total_cost_usd"),
            "committed": committed, "result_file": str(res_path)}


def run_build(*, manifest: dict, project: Path, data: Path, out: Path, manifest_dir: Path,
              loop_signal: str = "both", local_timeout_s: str = "900", jest_docker: str = "0",
              run_cmd: list | None = None, npm_install: bool = True) -> dict:
    """Scaffold + drive every phase; STOP at the first phase that cannot go green (it blocks the rest)."""
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

    scaffold_project(project, language, npm_install=npm_install)
    results, ok = [], True
    for phase in manifest["phases"]:
        r = run_phase(phase, project=project, data=data, out=out, manifest_dir=manifest_dir,
                      language=language, env=env, run_cmd=run_cmd)
        results.append(r)
        if not r["resolved"]:
            ok = False
            break
    summary = {"project": str(project), "loop_signal": loop_signal,
               "phases_total": len(manifest["phases"]),
               "phases_shipped": sum(1 for r in results if r["resolved"]),
               "pipeline_complete": ok and len(results) == len(manifest["phases"]),
               "phases": results}
    (out / "BUILD-SUMMARY.json").write_text(json.dumps(summary, indent=2) + "\n")
    return summary
