"""Greenfield BUILD orchestrator tests (#649). The chain itself is stubbed (a tiny fake `lfah run` that
writes a canned result), so these exercise the BUILD plumbing — scaffold, per-phase red-test commit,
commit-on-SHIP base advance, stop-on-unresolved, summary — with no models / no GPU."""
import json
import subprocess
import sys
from pathlib import Path

from lfah import build

# A fake `lfah run`: reads --instance + --out, writes lfah-<iid>-c.json. `final_resolved` is True unless
# the instance_id is in STUB_FAIL (so we can drive a stop-on-unresolved case).
_STUB = '''import json, os, sys
a = sys.argv
inst = a[a.index("--instance") + 1]; out = a[a.index("--out") + 1]
iid = json.load(open(inst))["instance_id"]
fail = set(filter(None, os.environ.get("STUB_FAIL", "").split(",")))
# Simulate the executor writing source into the project (repo = instance_dir/repo symlink).
repo = os.path.join(os.path.dirname(inst), "repo")
if iid not in fail:
    os.makedirs(os.path.join(repo, "src"), exist_ok=True)
    open(os.path.join(repo, "src", iid + ".txt"), "w").write("impl " + iid + "\\n")
os.makedirs(out, exist_ok=True)
json.dump({"instance_id": iid, "final_resolved": iid not in fail, "verdict": "SHIP",
           "loop_signal": "both", "iterations": 1, "handoff": None,
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


def test_run_build_stops_on_unresolved_phase(tmp_path, monkeypatch):
    md, run_cmd = _setup(tmp_path)
    monkeypatch.setenv("STUB_FAIL", "bp2")   # bp1 ships, bp2 does not -> pipeline halts at bp2
    summary = build.run_build(manifest=_MANIFEST, project=tmp_path / "project", data=tmp_path / "data",
                              out=tmp_path / "out", manifest_dir=md, run_cmd=run_cmd, npm_install=False)
    assert summary["pipeline_complete"] is False
    assert summary["phases_shipped"] == 1
    log = _git_log(tmp_path / "project")
    assert "phase bp1: SHIP" in log and "phase bp2: SHIP" not in log  # bp2 never committed
