"""P12 gates (verifier-first) — OPT-IN git-worktree isolation (FLEET_WORKTREE=1), the
#1 remaining 5/5 lever named by every recent eval. Each agent task runs in its OWN git
worktree so parallel writers never collide in the shared tree; the queue stays at root.
Fail-open: not a git repo / git error → no isolation, the run still proceeds at root.

  ensure(root, task_id, context_files) -> worktree path on branch fleet/<task_id>, with
      the declared context_files copied IN (so the agent sees them even if uncommitted).
  finalize(root, task_id, output_file, status) -> on COMPLETED copies the deliverable BACK
      to root (so QA + the DAG see it), returns the accurate changed_files (single writer
      per worktree → no cross-task leakage, fixing the P8 caveat), and removes the worktree.
  Both fail-open (return root / empty) off a non-git tree.
  watcher.sh consults FLEET_WORKTREE and runs the agent in the worktree when on.
"""
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(ROOT_SCRIPTS))
import worktree
SCRIPTS = ROOT_SCRIPTS


def _git(root, *args):
    subprocess.run(["git", "-C", str(root), *args], capture_output=True, check=True)


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "proj"
    r.mkdir()
    _git(r, "init", "-q")
    _git(r, "config", "user.email", "t@t")
    _git(r, "config", "user.name", "t")
    (r / "seed.txt").write_text("seed")
    _git(r, "add", "-A")
    _git(r, "commit", "-q", "-m", "init")
    return r


class TestEnsure:
    def test_creates_isolated_worktree(self, repo):
        (repo / "ctx.md").write_text("CONTEXT-DATA")          # uncommitted context file
        wt = worktree.ensure(repo, "task-aaa", ["ctx.md"])
        assert wt and Path(wt).is_dir() and Path(wt) != repo
        assert (Path(wt) / "ctx.md").read_text() == "CONTEXT-DATA", \
            "context_files not copied into the worktree"
        # it is a real linked worktree on its own branch
        out = subprocess.run(["git", "-C", str(repo), "worktree", "list"],
                             capture_output=True, text=True).stdout
        assert "task-aaa" in out

    def test_fail_open_non_git(self, tmp_path):
        nongit = tmp_path / "plain"
        nongit.mkdir()
        assert worktree.ensure(nongit, "t1", []) == str(nongit), \
            "non-git tree must fall back to root (fail-open), not crash"


class TestFinalize:
    def test_copies_output_back_and_reports_changes(self, repo):
        wt = worktree.ensure(repo, "task-bbb", [])
        (Path(wt) / "out.txt").write_text("the deliverable")   # agent writes in worktree
        info = worktree.finalize(repo, "task-bbb", "out.txt", "COMPLETED")
        assert (repo / "out.txt").read_text() == "the deliverable", \
            "deliverable not copied back to root → QA/DAG can't see it"
        assert "out.txt" in (info.get("changed_files") or []), \
            "changed_files not reported from the worktree"
        # worktree cleaned up
        lst = subprocess.run(["git", "-C", str(repo), "worktree", "list"],
                             capture_output=True, text=True).stdout
        assert "task-bbb" not in lst

    def test_finalize_fail_open_non_git(self, tmp_path):
        nongit = tmp_path / "plain"
        nongit.mkdir()
        info = worktree.finalize(nongit, "t1", "o.txt", "COMPLETED")
        assert isinstance(info, dict)


class TestWatcherWired:
    def test_watcher_consults_worktree(self):
        body = (SCRIPTS / "watcher.sh").read_text()
        assert "FLEET_WORKTREE" in body and "worktree.py" in body, \
            "watcher does not run tasks in an isolated worktree when FLEET_WORKTREE=1"

    def test_worktree_in_runtime_scripts(self):
        body = (SCRIPTS / "init_workspace.py").read_text()
        assert "worktree.py" in body, "worktree.py not deployed (RUNTIME_SCRIPTS)"
