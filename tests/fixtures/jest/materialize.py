#!/usr/bin/env python3
"""Materialize the committed jest fixture SOURCES into a data-root the jest oracle reads.

Each fixture source dir (e.g. ``instanceA-sum/``) holds three plain, git-free things:
  - ``repo_src/``  the BUGGY project files (this is the base state)
  - ``fixed/``     the corrected file(s) that fix the bug
  - ``meta.json``  the static instance fields (no base_commit yet)

For each, this builds ``<data_root>/instances/<instance_id>/``:
  - ``repo/``         a real git repo committed at the buggy base (so the chain can
                      `git checkout <base_commit>` and `git diff` the executor's edits)
  - ``instance.json`` = meta.json + the resolved ``base_commit``
  - ``gold.patch``    the unified diff (repo_src -> fixed) GENERATED via `git diff`,
                      so it is guaranteed to `git apply` cleanly

Why generate gold.patch instead of committing a hand-written diff: a hand-authored
unified diff drifts from the source and may fail to apply; round-tripping through real
git is the only way to keep the gold patch and the sources in lockstep.

We deliberately do NOT commit a nested ``.git`` into the lfah repo; the repo is
git-inited here at materialize time from the plain ``repo_src/`` sources.

Usage (script):  python materialize.py <data_root>   # default: ./_materialized
Usage (test):    from materialize import materialize_all; ids = materialize_all(dest)
"""
import json
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
# Fixed identity + date so the base_commit is deterministic across machines/runs.
_GIT_ENV = {
    "GIT_AUTHOR_NAME": "lfah-fixture", "GIT_AUTHOR_EMAIL": "fixture@lfah.local",
    "GIT_COMMITTER_NAME": "lfah-fixture", "GIT_COMMITTER_EMAIL": "fixture@lfah.local",
    "GIT_AUTHOR_DATE": "2026-01-01T00:00:00 +0000",
    "GIT_COMMITTER_DATE": "2026-01-01T00:00:00 +0000",
}


def _git(repo: Path, *args: str) -> str:
    import os
    env = dict(os.environ)
    env.update(_GIT_ENV)
    r = subprocess.run(["git", "-C", str(repo), *args],
                       capture_output=True, text=True, env=env)
    if r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed in {repo}: {r.stderr.strip()}")
    return r.stdout


def fixture_dirs() -> list:
    """Every source fixture dir (has repo_src/ + fixed/ + meta.json)."""
    return sorted(p for p in HERE.iterdir()
                  if p.is_dir() and (p / "meta.json").exists() and (p / "repo_src").is_dir())


def materialize_one(src: Path, data_root: Path) -> str:
    meta = json.loads((src / "meta.json").read_text())
    iid = meta["instance_id"]
    dest = data_root / "instances" / iid
    if dest.exists():
        shutil.rmtree(dest)
    repo = dest / "repo"
    shutil.copytree(src / "repo_src", repo)

    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base: buggy state")
    base_commit = _git(repo, "rev-parse", "HEAD").strip()

    # Generate gold.patch by overlaying the fixed file(s), diffing, then reverting.
    for f in (src / "fixed").iterdir():
        shutil.copy2(f, repo / f.name)
    gold = _git(repo, "diff")
    (dest / "gold.patch").write_text(gold)
    _git(repo, "checkout", "--", ".")   # back to the buggy base

    inst = dict(meta)
    inst["base_commit"] = base_commit
    (dest / "instance.json").write_text(json.dumps(inst, indent=2) + "\n")
    return iid


def materialize_all(data_root: Path) -> list:
    data_root = Path(data_root)
    (data_root / "instances").mkdir(parents=True, exist_ok=True)
    return [materialize_one(src, data_root) for src in fixture_dirs()]


if __name__ == "__main__":
    root = Path(sys.argv[1]) if len(sys.argv) > 1 else (HERE / "_materialized")
    ids = materialize_all(root)
    print(f"materialized {len(ids)} jest instance(s) under {root}/instances:")
    for i in ids:
        print(f"  - {i}")
