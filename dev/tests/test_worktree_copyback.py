#!/usr/bin/env python3
"""Regression: worktree.finalize copies back ALL of a task's changed files
(module + sibling test_*.py), not just output_file.

Covers the P20(declared write_scope FORCES worktree) x P12(finalize copied only
output_file) interaction bug: a module+sibling-test deliverable had its test
stranded on the fleet/<id> branch, so a pytest acceptance-predicate run at root
(file missing) failed -> the no-LLM sweep auto-qa-FAILED good work. finalize now
copies the whole committed change set back.
"""
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "scripts"))
import worktree


def _git(d, *a):
    subprocess.run(["git", "-C", str(d), *a], capture_output=True)


def test_finalize_copies_all_changed_files_back_to_root():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        _git(root, "init", "-q")
        _git(root, "config", "user.email", "t@t")
        _git(root, "config", "user.name", "t")
        (root / "seed.txt").write_text("seed\n")
        _git(root, "add", "-A")
        _git(root, "commit", "-q", "-m", "init")

        tid = "task-copyback"
        wt = Path(worktree.ensure(root, tid, context_files=[]))
        assert str(wt) != str(root), "worktree not created — cannot exercise the path"

        code = wt / "experiments" / "code"
        code.mkdir(parents=True)
        (code / "mod.py").write_text("X = 1\n")
        (code / "test_mod.py").write_text("def test_x():\n    assert True\n")

        info = worktree.finalize(
            root, tid, "experiments/code/mod.py", "COMPLETED", context_files=[]
        )

        # output_file comes back (unchanged behaviour)
        assert (root / "experiments/code/mod.py").is_file()
        # the REGRESSION: the sibling test must ALSO come back to root
        assert (root / "experiments/code/test_mod.py").is_file(), (
            "sibling test not copied back — finalize regressed to output_file-only"
        )
        # changed_files reports both
        cf = set(info.get("changed_files") or [])
        assert "experiments/code/mod.py" in cf and "experiments/code/test_mod.py" in cf


if __name__ == "__main__":
    test_finalize_copies_all_changed_files_back_to_root()
    print("PASS: finalize copies module + sibling test back to root")
