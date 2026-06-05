"""#631: git_diff() must capture NEW/untracked files, not just modifications to tracked files.

Plain `git diff` is blind to untracked files, so a fix that CREATES a file (e.g. a missing
`types/*.d.ts`) was captured as an EMPTY patch -- the engine then scored its own correct fix as a
failure and the evaluator verified an incomplete patch (the 2026-06-05 #601 dayjs-857/858/569
incident). git_diff() now stages (`git add -A`), reads the staged diff, and unstages so new files
appear as apply-able `new file mode` hunks while the working tree is left untouched.
"""
import subprocess
from pathlib import Path

import lfah.relay as relay


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True).stdout


def _init_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    (repo / "tracked.txt").write_text("line1\nline2\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")


def test_git_diff_captures_new_untracked_file(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)

    # executor-style edits: modify a tracked file AND create a NEW untracked file
    (repo / "tracked.txt").write_text("line1\nline2-modified\n")
    (repo / "types").mkdir()
    (repo / "types" / "new.d.ts").write_text("export const x: number;\n")

    # the BUG that motivated this fix: plain `git diff` omits the new file
    blind = subprocess.run("git diff", shell=True, cwd=str(repo),
                           capture_output=True, text=True).stdout
    assert "types/new.d.ts" not in blind, "precondition: plain git diff is blind to new files"

    diff = relay.git_diff(repo)
    # the fix: BOTH the modification and the new file are captured
    assert "tracked.txt" in diff
    assert "types/new.d.ts" in diff
    assert "new file mode" in diff
    assert "export const x: number;" in diff


def test_git_diff_leaves_working_tree_and_index_pristine(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "types").mkdir()
    (repo / "types" / "new.d.ts").write_text("export const x: number;\n")

    relay.git_diff(repo)

    # the new file must still be on disk (mixed reset must not touch the working tree)
    assert (repo / "types" / "new.d.ts").exists()
    # and the index must be back to HEAD: the file is untracked again, nothing staged
    status = _git(repo, "status", "--porcelain")
    assert "?? types/" in status, f"expected new file still untracked, got: {status!r}"
    staged = _git(repo, "diff", "--cached", "--name-only").strip()
    assert staged == "", f"expected empty index after git_diff(), got staged: {staged!r}"


def test_captured_new_file_diff_applies_to_fresh_base_checkout(tmp_path):
    """The captured patch must be `git apply`-able -- this is exactly the oracle's scoring path
    (jest_oracle_eval / oracle_eval do `git apply --whitespace=nowarn <patch>` on a fresh base copy)."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "tracked.txt").write_text("line1\nline2-modified\n")
    (repo / "types").mkdir()
    (repo / "types" / "new.d.ts").write_text("export const x: number;\n")

    diff = relay.git_diff(repo)
    patch = tmp_path / "cand.patch"
    patch.write_text(diff)

    fresh = tmp_path / "fresh"
    _init_repo(fresh)  # same base content (line1/line2 + committed)
    ap = subprocess.run(["git", "-C", str(fresh), "apply", "--whitespace=nowarn", str(patch)],
                        capture_output=True, text=True)
    assert ap.returncode == 0, f"git apply failed: {ap.stderr}"
    assert (fresh / "types" / "new.d.ts").read_text() == "export const x: number;\n"
    assert (fresh / "tracked.txt").read_text() == "line1\nline2-modified\n"


def test_git_diff_no_checkout_returns_empty(tmp_path):
    # behavior-preserving: a non-existent repo path -> "" (dry-run smoke / no checkout)
    assert relay.git_diff(tmp_path / "does-not-exist") == ""
