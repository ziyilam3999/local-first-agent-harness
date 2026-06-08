"""Greenfield BUILD orchestrator tests (#649). The chain itself is stubbed (a tiny fake `lfah run` that
writes a canned result), so these exercise the BUILD plumbing — scaffold, per-phase red-test commit,
commit-on-SHIP base advance, stop-on-unresolved, summary — with no models / no GPU."""
import json
import subprocess
import sys
from pathlib import Path

from lfah import build

# A fake `lfah run`: reads --instance + --out, writes lfah-<iid>-c.json. Three env-driven stub modes:
#   STUB_FAIL    ids -> no impl written; local fails, no handoff field       -> phase halts the build.
#   STUB_HANDOFF ids -> impl written; local fails but a handoff field lands  -> #708 cloud-handoff path.
#   otherwise        -> impl written; local succeeds, no handoff field       -> ordinary local path.
_STUB = '''import json, os, sys
a = sys.argv
inst = a[a.index("--instance") + 1]; out = a[a.index("--out") + 1]
iid = json.load(open(inst))["instance_id"]
fail = set(filter(None, os.environ.get("STUB_FAIL", "").split(",")))
handoff_ids = set(filter(None, os.environ.get("STUB_HANDOFF", "").split(",")))
# Simulate the executor (local OR cloud-handoff) writing source into the project; a hard FAIL writes nothing.
repo = os.path.join(os.path.dirname(inst), "repo")
if iid not in fail:
    os.makedirs(os.path.join(repo, "src"), exist_ok=True)
    open(os.path.join(repo, "src", iid + ".txt"), "w").write("impl " + iid + "\\n")
handoff = ({"resolved": iid in handoff_ids, "model_resolved": "claude-sonnet-stub", "backend": "cloud"}
           if iid in handoff_ids else None)
os.makedirs(out, exist_ok=True)
json.dump({"instance_id": iid, "final_resolved": iid not in fail and iid not in handoff_ids,
           "verdict": "SHIP" if iid not in fail else "SHIP-CAPPED",
           "loop_signal": "both", "iterations": 1, "handoff": handoff,
           "telemetry": {"cost": {"chain_total_cost_usd": 0.0}}},
          open(os.path.join(out, f"lfah-{iid}-c.json"), "w"))
'''

_MANIFEST = {
    "project_name": "app", "language": "text",
    "phases": [
        {"id": "bp1", "title": "one", "test_file": "p1.test", "test_path": "__tests__/p1.test",
         "f2p": "__tests__/p1.test", "p2p": [], "problem_statement": "make p1 pass"},
        {"id": "bp2", "title": "two", "test_file": "p2.test", "test_path": "__tests__/p2.test",
         "f2p": "__tests__/p2.test", "p2p": ["__tests__/p1.test"], "problem_statement": "make p2 pass"},
    ],
}


def _setup(tmp_path):
    md = tmp_path / "manifest"; md.mkdir()
    (md / "p1.test").write_text("// red 1\n")
    (md / "p2.test").write_text("// red 2\n")
    stub = tmp_path / "stub_run.py"; stub.write_text(_STUB)
    return md, [sys.executable, str(stub)]


def _git_log(project):
    return subprocess.run(["git", "-C", str(project), "log", "--oneline"],
                          capture_output=True, text=True).stdout


def test_run_build_two_phases_advances_base(tmp_path):
    md, run_cmd = _setup(tmp_path)
    summary = build.run_build(manifest=_MANIFEST, project=tmp_path / "project", data=tmp_path / "data",
                              out=tmp_path / "out", manifest_dir=md, run_cmd=run_cmd, npm_install=False)
    assert summary["pipeline_complete"] is True
    assert summary["phases_shipped"] == 2
    assert summary["loop_signal"] == "both"
    log = _git_log(tmp_path / "project")
    assert "scaffold" in log and "phase bp1: SHIP" in log and "phase bp2: SHIP" in log
    assert (tmp_path / "out" / "BUILD-SUMMARY.json").exists()
    # bp2's base_commit is bp1's SHIP commit (the base advanced)
    inst1 = json.loads((tmp_path / "data" / "instances" / "bp1" / "instance.json").read_text())
    inst2 = json.loads((tmp_path / "data" / "instances" / "bp2" / "instance.json").read_text())
    assert inst1["language"] == "text" and inst1["f2p_tests"] == ["__tests__/p1.test"]
    assert inst2["base_commit"] != inst1["base_commit"]  # advanced
    assert summary["phases"][0]["committed"] is not None


def test_run_build_relative_paths_resolve(tmp_path, monkeypatch):
    """Regression: RELATIVE project/data/out must be resolved to absolute, else the per-phase
    `repo` symlink (target = project) resolves relative to the symlink's own dir and breaks. A
    real `lfah build` smoke surfaced this; the absolute-tmp_path tests did not."""
    md, run_cmd = _setup(tmp_path)
    monkeypatch.chdir(tmp_path)
    summary = build.run_build(manifest=_MANIFEST, project=Path("project"), data=Path("data"),
                              out=Path("out"), manifest_dir=md, run_cmd=run_cmd, npm_install=False)
    assert summary["pipeline_complete"] is True
    link = tmp_path / "data" / "instances" / "bp1" / "repo"
    assert link.is_symlink() and link.resolve() == (tmp_path / "project").resolve()  # link not broken


def test_scaffold_typescript_emits_ts_jest_and_tsconfig(tmp_path):
    """TS support (#672/#673): a typescript scaffold lays a ts-jest package.json + jest.config.js +
    a tsconfig.json with type-checking ON (isolatedModules false), all git-tracked. The oracle is
    unchanged — ts-jest grades .ts under the same `npx jest`."""
    proj = tmp_path / "tsproj"
    build.scaffold_project(proj, "typescript", npm_install=False)
    dev = json.loads((proj / "package.json").read_text())["devDependencies"]
    assert "ts-jest" in dev and "typescript" in dev and "@types/jest" in dev
    assert (proj / "jest.config.js").exists()
    tscfg = json.loads((proj / "tsconfig.json").read_text())
    assert tscfg["compilerOptions"]["isolatedModules"] is False   # types gate at test time
    tracked = subprocess.run(["git", "-C", str(proj), "ls-files"], capture_output=True, text=True).stdout
    assert all(f in tracked for f in ("package.json", "jest.config.js", "tsconfig.json"))


def test_scaffold_javascript_has_no_tsconfig(tmp_path):
    """Behavior-preserving: a plain javascript scaffold still emits only package.json (no TS files)."""
    proj = tmp_path / "jsproj"
    build.scaffold_project(proj, "javascript", npm_install=False)
    assert (proj / "package.json").exists()
    assert not (proj / "tsconfig.json").exists() and not (proj / "jest.config.js").exists()


def test_run_build_stops_on_unresolved_phase(tmp_path, monkeypatch):
    md, run_cmd = _setup(tmp_path)
    monkeypatch.setenv("STUB_FAIL", "bp2")   # bp1 ships, bp2 does not -> pipeline halts at bp2
    summary = build.run_build(manifest=_MANIFEST, project=tmp_path / "project", data=tmp_path / "data",
                              out=tmp_path / "out", manifest_dir=md, run_cmd=run_cmd, npm_install=False)
    assert summary["pipeline_complete"] is False
    assert summary["phases_shipped"] == 1
    log = _git_log(tmp_path / "project")
    assert "phase bp1: SHIP" in log and "phase bp2: SHIP" not in log  # bp2 never committed


def test_run_build_ships_on_cloud_handoff(tmp_path, monkeypatch):
    """#708: a phase the LOCAL tier did not resolve but the CLOUD HANDOFF tier did (final_resolved False,
    handoff field present, the cloud's files already on disk) must SHIP, commit, and advance the base —
    not halt the build. The win is attributed to the cloud-handoff tier (honest per-tier attribution),
    and the cloud's on-disk file is really committed (not a hollow --allow-empty ship)."""
    md, run_cmd = _setup(tmp_path)
    monkeypatch.setenv("STUB_HANDOFF", "bp2")   # bp1 ships local; bp2 ships via the cloud handoff
    summary = build.run_build(manifest=_MANIFEST, project=tmp_path / "project", data=tmp_path / "data",
                              out=tmp_path / "out", manifest_dir=md, run_cmd=run_cmd, npm_install=False)
    assert summary["pipeline_complete"] is True
    assert summary["phases_shipped"] == 2
    bp2 = summary["phases"][1]
    assert bp2["resolved"] is True and bp2["solved_by"] == "cloud-handoff"
    assert bp2["local_resolved"] is False and bp2["handoff_resolved"] is True
    assert bp2["handoff_model"] == "claude-sonnet-stub"   # solving model named (per-tier attribution)
    assert bp2["committed"] is not None
    log = _git_log(tmp_path / "project")
    assert "phase bp2: SHIP (cloud-handoff)" in log
    tracked = subprocess.run(["git", "-C", str(tmp_path / "project"), "ls-files"],
                             capture_output=True, text=True).stdout
    assert "src/bp2.txt" in tracked   # the cloud's on-disk file was actually committed


def test_scaffold_reuse_keeps_existing_project(tmp_path):
    """scaffold_project REUSES an existing greenfield project by default (returns True, no wipe);
    fresh=True re-scaffolds (returns False, wipes)."""
    proj = tmp_path / "p"
    assert build.scaffold_project(proj, "javascript", npm_install=False) is False   # created fresh
    (proj / "marker.txt").write_text("keep me\n")
    subprocess.run(["git", "-C", str(proj), "add", "marker.txt"], check=True)
    subprocess.run(["git", "-C", str(proj), "commit", "-q", "-m", "add marker"], check=True)
    assert build.scaffold_project(proj, "javascript", npm_install=False) is True    # REUSE: no wipe
    assert (proj / "marker.txt").exists()
    assert build.scaffold_project(proj, "javascript", npm_install=False, fresh=True) is False  # wipe
    assert not (proj / "marker.txt").exists()


def test_run_build_reuses_project_and_accumulates(tmp_path):
    """Operator 2026-06-08: successive builds must accumulate into the SAME folder, not wipe per run.
    Build manifest A (bp1) into ./project, then manifest B (bp9) into the SAME ./project: the default
    REUSES the project (builds bp9 on top of HEAD), so bp1's impl + SHIP commit survive and bp9 is added."""
    md, run_cmd = _setup(tmp_path)
    (md / "p9.test").write_text("// red 9\n")
    proj = tmp_path / "project"
    man_a = {"project_name": "app", "language": "text",
             "phases": [{"id": "bp1", "title": "one", "test_file": "p1.test", "test_path": "__tests__/p1.test",
                         "f2p": "__tests__/p1.test", "p2p": [], "problem_statement": "make p1 pass"}]}
    man_b = {"project_name": "app", "language": "text",
             "phases": [{"id": "bp9", "title": "nine", "test_file": "p9.test", "test_path": "__tests__/p9.test",
                         "f2p": "__tests__/p9.test", "p2p": ["__tests__/p1.test"],
                         "problem_statement": "make p9 pass"}]}
    sa = build.run_build(manifest=man_a, project=proj, data=tmp_path / "data", out=tmp_path / "outa",
                         manifest_dir=md, run_cmd=run_cmd, npm_install=False)
    assert sa["reused"] is False and sa["phases_shipped"] == 1
    bp1_commit = sa["phases"][0]["committed"]
    sb = build.run_build(manifest=man_b, project=proj, data=tmp_path / "data", out=tmp_path / "outb",
                         manifest_dir=md, run_cmd=run_cmd, npm_install=False)
    assert sb["reused"] is True and sb["phases_shipped"] == 1
    log = _git_log(proj)
    assert "phase bp1: SHIP" in log and "phase bp9: SHIP" in log     # bp1 survived; bp9 added on top
    assert log.count("scaffold: empty greenfield project") == 1      # scaffolded ONCE, not re-scaffolded
    tracked = subprocess.run(["git", "-C", str(proj), "ls-files"], capture_output=True, text=True).stdout
    assert "src/bp1.txt" in tracked and "src/bp9.txt" in tracked
    inst9 = json.loads((tmp_path / "data" / "instances" / "bp9" / "instance.json").read_text())
    # bp9 was built ON TOP of the reused project: bp1's SHIP commit is an ancestor of bp9's base.
    anc = subprocess.run(["git", "-C", str(proj), "merge-base", "--is-ancestor", bp1_commit,
                          inst9["base_commit"]])
    assert anc.returncode == 0


def test_run_build_fresh_wipes_existing_project(tmp_path):
    """--fresh forces a clean wipe + re-scaffold even when the project exists: the prior build's phase is gone."""
    md, run_cmd = _setup(tmp_path)
    proj = tmp_path / "project"
    man_a = {"project_name": "app", "language": "text",
             "phases": [{"id": "bp1", "title": "one", "test_file": "p1.test", "test_path": "__tests__/p1.test",
                         "f2p": "__tests__/p1.test", "p2p": [], "problem_statement": "make p1 pass"}]}
    build.run_build(manifest=man_a, project=proj, data=tmp_path / "data", out=tmp_path / "outa",
                    manifest_dir=md, run_cmd=run_cmd, npm_install=False)
    man_b = {"project_name": "app", "language": "text",
             "phases": [{"id": "bp2", "title": "two", "test_file": "p2.test", "test_path": "__tests__/p2.test",
                         "f2p": "__tests__/p2.test", "p2p": [], "problem_statement": "make p2 pass"}]}
    sb = build.run_build(manifest=man_b, project=proj, data=tmp_path / "data", out=tmp_path / "outb",
                         manifest_dir=md, run_cmd=run_cmd, npm_install=False, fresh=True)
    assert sb["reused"] is False
    tracked = subprocess.run(["git", "-C", str(proj), "ls-files"], capture_output=True, text=True).stdout
    assert "src/bp1.txt" not in tracked and "src/bp2.txt" in tracked   # wiped: bp1 gone, only bp2
    log = _git_log(proj)
    assert "phase bp1: SHIP" not in log and "phase bp2: SHIP" in log
