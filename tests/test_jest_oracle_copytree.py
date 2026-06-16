"""Regression test for #972 — jest_oracle_eval copytree self-recursion.

The footgun: in real chain runs `jestrepo` resolves UNDER `src_repo` (the evaluator wrapper
`eval_patch_jest.sh` sets WORK="$PWD/.eval_patch_jest-<iid>-<ts>" with cwd == the canonical repo,
and `work_root = diff_path.parent / "jest-runs" / run_id`). A naive `shutil.copytree(src_repo, jestrepo)`
then copies the repo INTO its own descendant -> unbounded self-recursion -> Errno 63 "File name too long".

This test reproduces that exact layout (diff_path inside repo/, LFAH_JEST_WORKROOT UNSET) so `jestrepo`
is a descendant of `src_repo`, and asserts the copy terminates cleanly + the scratch dirs are NOT
re-copied into the destination. It is hermetic: docker is disabled (LFAH_JEST_DOCKER=0) and the jest
*run* is mocked (no node), but the `shutil.copytree` under test executes for real — that is the thing
being verified.
"""
import json
import os
import subprocess
from pathlib import Path

import lfah.relay as relay

_GIT_ENV = {
    "GIT_AUTHOR_NAME": "lfah-fixture", "GIT_AUTHOR_EMAIL": "fixture@lfah.local",
    "GIT_COMMITTER_NAME": "lfah-fixture", "GIT_COMMITTER_EMAIL": "fixture@lfah.local",
    "GIT_AUTHOR_DATE": "2026-01-01T00:00:00 +0000",
    "GIT_COMMITTER_DATE": "2026-01-01T00:00:00 +0000",
}


def _git(repo: Path, *args: str) -> str:
    env = dict(os.environ); env.update(_GIT_ENV)
    r = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, env=env)
    if r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed in {repo}: {r.stderr.strip()}")
    return r.stdout


SENTINEL = "RECURSION_REGRESSION_SENTINEL_972"
DEEP_SENTINEL = "DEEP_SCRATCH_SENTINEL_972"


def _build_data_root(tmp_path: Path, iid: str):
    """Build LFAH_DATA_DIR/instances/<iid>/{instance.json,repo} with a real git repo AND a
    pre-existing scratch nest mirroring the live footgun. Returns (data_root, repo, diff_path)."""
    data_root = tmp_path / "data"
    inst_dir = data_root / "instances" / iid
    repo = inst_dir / "repo"
    repo.mkdir(parents=True)

    # A real graded source file carrying a sentinel (must survive into jestrepo).
    (repo / "src").mkdir()
    (repo / "src" / "index.js").write_text(f"module.exports = '{SENTINEL}';\n")
    (repo / "package.json").write_text(json.dumps({"name": "fixt-972", "version": "1.0.0"}) + "\n")

    # Real git repo committed at base so the oracle's checkout/reset/clean path has a base_commit.
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base: 972 fixture")
    base_commit = _git(repo, "rev-parse", "HEAD").strip()

    # No test_files / f2p_tests declared -> legacy whole-suite-green grading; re-imposition no-ops.
    (inst_dir / "instance.json").write_text(json.dumps({
        "instance_id": iid, "language": "javascript", "base_commit": base_commit,
    }, indent=2) + "\n")

    # PRE-EXISTING scratch nest reproducing the live footgun. These are gitignored/untracked so the
    # `git clean -fdq` in the oracle would normally remove them from the COPY (not the source), but
    # the bug is during the COPY itself — copytree must skip these names while reading src_repo.
    ts = "20260616-000000"
    eval_scratch = repo / f".eval_patch_jest-{iid}-{ts}"
    deep_a = eval_scratch / "jest-runs" / "runX" / "jestrepo"
    deep_a.mkdir(parents=True)
    (deep_a / "DEEP_SENTINEL").write_text(DEEP_SENTINEL + "\n")
    deep_b = repo / "jest-runs" / "runY" / "jestrepo"
    deep_b.mkdir(parents=True)
    (deep_b / "DEEP_SENTINEL").write_text(DEEP_SENTINEL + "\n")

    # diff_path lives INSIDE repo/ (mirrors eval_patch_jest.sh: diff_path = WORK/patch.diff), so with
    # LFAH_JEST_WORKROOT UNSET, work_root = repo/.eval_patch_jest-.../jest-runs/<run_id> -> jestrepo is
    # a DESCENDANT of src_repo. An empty diff is a valid no-op per the code (baseline, unresolved).
    diff_path = eval_scratch / "patch.diff"
    diff_path.write_text("")
    return data_root, repo, diff_path


def _make_jest_run_mock(real_run):
    """Return a subprocess.run replacement: intercept ONLY the jest run (`sh -lc <npm/jest>`) and
    write a minimal valid jest report into jestrepo; pass every other call (git ...) through to real."""
    class _Done:
        def __init__(self, rc=0, stdout="", stderr=""):
            self.returncode, self.stdout, self.stderr = rc, stdout, stderr

    def fake_run(cmd, *args, **kwargs):
        # The jest invocation is the only call with cwd == jestrepo running `sh -lc <inner>`.
        is_jest = (isinstance(cmd, (list, tuple)) and len(cmd) >= 3
                   and cmd[0] == "sh" and cmd[1] == "-lc" and "jest" in cmd[2])
        if is_jest:
            cwd = kwargs.get("cwd")
            assert cwd, "jest run invoked without cwd (jestrepo)"
            # whole-suite-green report shape parsed by the legacy grading branch.
            report = {"success": True, "numFailedTests": 0, "numFailedTestSuites": 0,
                      "numTotalTests": 1, "testResults": [
                          {"name": "/work/src/index.test.js", "status": "passed",
                           "assertionResults": [{"title": "ok", "fullName": "ok",
                                                 "ancestorTitles": [], "status": "passed"}]}]}
            (Path(cwd) / ".jestout.json").write_text(json.dumps(report))
            return _Done(rc=0, stdout="mock jest ok", stderr="")
        return real_run(cmd, *args, **kwargs)

    return fake_run


def test_jest_oracle_copytree_no_self_recursion(tmp_path, monkeypatch):
    """Both-ends regression for #972. On the BUGGY relay (naive copytree) this errors with OSError/
    Errno 63 (recursion). On the FIXED relay (ignore_patterns) the copy terminates and the scratch
    dirs are absent from jestrepo while the real graded source + .git survive."""
    iid = "fixt-972__recursion-1"
    data_root, repo, diff_path = _build_data_root(tmp_path, iid)

    monkeypatch.setenv("LFAH_DATA_DIR", str(data_root))
    monkeypatch.setenv("LFAH_JEST_DOCKER", "0")
    monkeypatch.delenv("LFAH_JEST_WORKROOT", raising=False)  # force work_root under src_repo

    monkeypatch.setattr(relay.subprocess, "run", _make_jest_run_mock(subprocess.run))

    # (a) the call COMPLETES without OSError/Errno 63 (no recursion blow-up).
    res = relay.jest_oracle_eval(iid, diff_path, "run972")
    assert isinstance(res, dict) and "resolved" in res

    work_root = diff_path.parent / "jest-runs" / "run972"
    jestrepo = work_root / "jestrepo"
    assert jestrepo.is_dir(), "jestrepo was not produced"
    # confirm the bug's precondition really held: jestrepo IS a descendant of src_repo.
    assert str(jestrepo.resolve()).startswith(str(repo.resolve())), \
        "test misconfigured: jestrepo is not under src_repo, so it would not reproduce the bug"

    # (b) jestrepo must NOT contain any nested jest-runs/ or .eval_patch_jest-* dir (the scratch
    #     names that, if copied, are the self-recursion).
    nested = [p for p in jestrepo.rglob("*")
              if p.is_dir() and (p.name == "jest-runs" or p.name.startswith(".eval_patch_jest-"))]
    assert nested == [], f"scratch dirs leaked into jestrepo (recursion vector): {nested}"
    assert not (jestrepo / "DEEP_SENTINEL").exists()
    assert not any(jestrepo.rglob("DEEP_SENTINEL")), "deep scratch sentinel was copied into jestrepo"

    # (c) the real graded source + .git survived the copy.
    src_copy = jestrepo / "src" / "index.js"
    assert src_copy.is_file(), "graded source file missing from jestrepo"
    assert SENTINEL in src_copy.read_text(), "graded source sentinel content missing from jestrepo"
    assert (jestrepo / ".git").exists(), ".git missing from jestrepo (checkout/reset path would break)"
