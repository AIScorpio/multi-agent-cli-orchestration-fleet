"""P16 item 5 — worktree.merge must NOT silently clobber on a real conflict (was
`-X theirs`, which discarded one side). On a genuine conflict it aborts cleanly, keeps the
branch for resolution, and ALARMS — work is preserved, not lost.
"""
import subprocess
import sys
from pathlib import Path

import pytest

ROOT_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(ROOT_SCRIPTS))
import worktree


def _git(root, *a):
    subprocess.run(["git", "-C", str(root), *a], capture_output=True, check=True)


class TestMergeConflictSafe:
    def test_conflict_aborts_keeps_branch_and_alerts(self, tmp_path, monkeypatch):
        r = tmp_path / "repo"; r.mkdir()
        _git(r, "init", "-q"); _git(r, "config", "user.email", "t@t"); _git(r, "config", "user.name", "t")
        (r / "f.txt").write_text("base\n"); _git(r, "add", "-A"); _git(r, "commit", "-qm", "base")
        wt = worktree.ensure(r, "task-c", [])
        (Path(wt) / "f.txt").write_text("WORKTREE EDIT\n")          # branch edits the line
        worktree.finalize(r, "task-c", "f.txt", "COMPLETED", [])
        # main advances the SAME line differently → guaranteed conflict
        (r / "f.txt").write_text("MAIN MOVED\n"); _git(r, "add", "-A"); _git(r, "commit", "-qm", "main")
        alerts = {}
        import fleet_health
        monkeypatch.setattr(fleet_health, "emit_alerts",
                            lambda home, a: alerts.setdefault("a", a))
        ok = worktree.merge(r, "task-c")
        assert ok is False, "a real conflict must not report success"
        # branch preserved (not deleted), main not clobbered, alarm raised
        branch = subprocess.run(["git", "-C", str(r), "branch", "--list", "fleet/task-c"],
                                capture_output=True, text=True).stdout
        assert "fleet/task-c" in branch, "conflicting branch was dropped (work lost)"
        assert (r / "f.txt").read_text() == "MAIN MOVED\n", "main was clobbered"
        assert alerts.get("a"), "merge conflict was silent (no alert)"
