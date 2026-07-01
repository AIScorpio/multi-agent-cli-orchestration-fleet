"""P16 item 2 — strict teeth AUTO-DETECT (not a forgotten manual flag):
  · watcher: track_changes auto-on when git; track_tests auto-on for software+pytest.
  · qa_floor.evaluate: a write-scope violation FAILS only under isolation (worktree →
    accurate changed_files); in a shared tree it's advisory (changed_files can leak), so
    honest work is never false-failed.
"""
import sys
from pathlib import Path

import pytest

ROOT_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(ROOT_SCRIPTS))
import qa_floor
SCRIPTS = ROOT_SCRIPTS


class TestWatcherAutoDetect:
    def test_watcher_autodetects_git_and_pytest(self):
        body = (SCRIPTS / "watcher.sh").read_text()
        assert "rev-parse --git-dir" in body and "FLEET_TRACK_CHANGES=1" in body, \
            "track_changes must auto-detect a git repo"
        assert "import pytest" in body and "load_profile" in body, \
            "track_tests must auto-detect software profile + pytest"


class TestReconcileEnforceOnlyIsolated:
    def test_violation_advisory_in_shared_tree(self, tmp_path):
        (tmp_path / "g.txt").write_text("x")
        spec = {"output_file": "g.txt", "write_scope": ["docs/**"]}
        # changed_files outside scope, but NOT isolated → advisory, must NOT fail
        ok, failures = qa_floor.evaluate(spec, tmp_path,
                                         {"changed_files": ["src/evil.py"], "isolated": False})
        assert ok, f"shared-tree scope violation false-failed honest work: {failures}"

    def test_violation_enforced_under_isolation(self, tmp_path):
        (tmp_path / "g.txt").write_text("x")
        spec = {"output_file": "g.txt", "write_scope": ["docs/**"]}
        ok, failures = qa_floor.evaluate(spec, tmp_path,
                                         {"changed_files": ["src/evil.py"], "isolated": True})
        assert not ok and any("scope" in f for f in failures), \
            "isolated worktree must ENFORCE write-scope (changed_files is accurate there)"
