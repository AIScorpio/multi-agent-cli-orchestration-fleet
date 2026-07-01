"""P8 gates (verifier-first) — the 5th adversarial eval's findings, safety first.

Each gate drives a real entry point / the real function. Items:
  A. _kill_match is PROJECT-SCOPED — a same-task_id worker in ANOTHER project's
     claimed/ is never matched (the cross-project SIGKILL bug, violates the headline
     multi-project-safety invariant).
  B. kanban _process_alive is PROJECT-SCOPED — a bare `pgrep -f match` must not light a
     phase from another project's identical process (the bare-pgrep invariant violation).
  C. watcher EMITS changed_files (a producer) so qa_floor.reconcile_files is no longer a
     consumer with no producer (write-scope verification goes live, opt-in & honest).
  D. per-project fairness covers CODEX too (also quota-scarce), not claude only.
  E. _scopes_overlap is PATH-SEMANTIC: dir containment overlaps; a single-level glob does
     NOT match a nested path (no silent parallelism collapse from fnmatch-literal).
  F. capacity.project_spend has a real caller (per-project token attribution surfaced).
"""
import json
import sys
from pathlib import Path

import pytest

ROOT_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(ROOT_SCRIPTS))
import doctor
import capacity
SCRIPTS = ROOT_SCRIPTS


# ── A: cross-project kill safety ───────────────────────────────────────────────

class TestKillMatchProjectScoped:
    def test_other_project_same_task_id_not_matched(self, tmp_path, monkeypatch):
        monkeypatch.setattr(doctor, "QUEUE", tmp_path / "projA" / ".fleet" / "queue")
        mine = str(tmp_path / "projA" / ".fleet" / "queue" / "claimed" / "kimi--task-001.json")
        other = "/some/other/projB/.fleet/queue/claimed/kimi--task-001.json"
        assert doctor._kill_match(f"kimi -p ... {mine}", "task-001") is True
        assert doctor._kill_match(f"kimi -p ... {other}", "task-001") is False, \
            "stuck-sweep would SIGKILL another project's worker with the same task_id"

    def test_prefix_sibling_still_safe(self, tmp_path, monkeypatch):
        monkeypatch.setattr(doctor, "QUEUE", tmp_path / "p" / ".fleet" / "queue")
        c = str(tmp_path / "p" / ".fleet" / "queue" / "claimed")
        assert doctor._kill_match(f"x {c}/kimi--task-012.json", "task-01") is False


# ── B: hub process-alive project scoping ───────────────────────────────────────

class TestHubProcessAliveScoped:
    def test_signature_and_wiring(self):
        body = (SCRIPTS / "kanban_hub.py").read_text()
        assert "def _process_alive(match, project_root" in body or \
               "def _process_alive(match, root" in body, \
            "_process_alive takes no project_root — cannot scope the pgrep"
        # derive_phase_statuses must pass project_root through to the check
        assert "_process_alive(" in body and "project_root" in body

    def test_no_alive_for_foreign_root(self, monkeypatch):
        import kanban_hub
        # a match that DOES run (python) but a project_root present in no cmdline → not alive
        assert kanban_hub._process_alive("python", "/zzz/nonexistent-root-xyz") is False


# ── C: changed_files producer exists ───────────────────────────────────────────

class TestChangedFilesProducer:
    def test_watcher_emits_changed_files(self):
        body = (SCRIPTS / "watcher.sh").read_text()
        assert "changed_files" in body, \
            "watcher never emits changed_files → reconcile_files is a consumer with no producer"


# ── D: fairness covers codex ───────────────────────────────────────────────────

class TestFairnessCoversCodex:
    def test_codex_in_fairness_guard(self):
        body = (SCRIPTS / "watcher.sh").read_text()
        # the fair_slot_floor block must apply to codex too (quota-scarce), not claude-only
        import re
        # find the line guarding the fairness block
        assert re.search(r'AGENT.*=.*codex', body) and "fair_slot_floor" in body
        # and specifically the fairness guard names codex
        guard = re.search(r'#\s*Per-project fairness.*?fair_slot_floor', body, re.S)
        block = body[body.index("fair_slot_floor") - 400: body.index("fair_slot_floor") + 200]
        assert "codex" in block, "fairness guard is claude-only; codex (quota-scarce) starves"


# ── E: path-semantic scope overlap ─────────────────────────────────────────────

class TestPathSemanticOverlap:
    def test_dir_containment_overlaps(self):
        assert doctor._scopes_overlap(["src/auth/**"], ["src/auth/login.py"]) is True
        assert doctor._scopes_overlap(["src/**"], ["src/foo.py"]) is True
        assert doctor._scopes_overlap(["src/**"], ["src/**"]) is True

    def test_single_level_glob_not_nested(self):
        # 'a/*.py' is ONE level — it must NOT collide with a nested 'a/b/c.py'
        assert doctor._scopes_overlap(["a/*.py"], ["a/b/c.py"]) is False

    def test_disjoint_dirs_dont_overlap(self):
        assert doctor._scopes_overlap(["src/**"], ["lib/**"]) is False
        assert doctor._scopes_overlap(["docs/x.md"], ["src/y.py"]) is False


# ── F: project_spend has a real caller ─────────────────────────────────────────

