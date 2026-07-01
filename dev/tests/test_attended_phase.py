"""Phase 3 — attended phase transitions.

The pipeline's checkpoint principle (G2/G3 need human judgment at phase boundaries) must
survive on the fleet. So the no-LLM sweep may release CONCRETE intra-phase deps autonomously,
but must NOT auto-cross a `phase:<id>` boundary unless FLEET_AUTO_PHASE=1. Default = attended.

Gates the integration entry point (doctor.resolve_dependencies), not a helper.
"""
import json
import sys
from pathlib import Path

import pytest

ROOT_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(ROOT_SCRIPTS))
import doctor  # noqa: E402


@pytest.fixture
def qproj(tmp_path, monkeypatch):
    ma = tmp_path / ".fleet"
    q = ma / "queue"
    for d in ("drafts", "pending", "claimed", "completed/qa-passed", "failed"):
        (q / d).mkdir(parents=True)
    monkeypatch.setattr(doctor, "MA", ma)
    monkeypatch.setattr(doctor, "ROOT", tmp_path)
    monkeypatch.setattr(doctor, "QUEUE", q)
    monkeypatch.setattr(doctor, "ledger", None)   # no audit writes in the test
    # phase 1 is DONE: one qa-passed member, nothing outstanding → phase:1 dep is satisfied
    (q / "completed/qa-passed" / "p1.json").write_text(
        json.dumps({"task_id": "p1", "phase": "1", "output_file": ""}))
    # a phase-2 draft that CROSSES the phase-1 boundary
    (q / "drafts" / "d_phase.json").write_text(
        json.dumps({"task_id": "d_phase", "phase": "2", "depends_on": ["phase:1"]}))
    # a phase-2 draft with a CONCRETE intra-edge dep (control — should always release)
    (q / "drafts" / "d_concrete.json").write_text(
        json.dumps({"task_id": "d_concrete", "phase": "2", "depends_on": ["p1"]}))
    return q


def _in(q, state, tid):
    return (q / state / f"{tid}.json").exists()


def test_phase_boundary_held_when_attended(qproj, monkeypatch):
    monkeypatch.delenv("FLEET_AUTO_PHASE", raising=False)
    doctor.resolve_dependencies(fix=True, quiet=True)
    assert _in(qproj, "drafts", "d_phase"), "phase-boundary draft must be HELD for attended advance"
    assert not _in(qproj, "pending", "d_phase")
    # concrete intra-phase dep still releases autonomously
    assert _in(qproj, "pending", "d_concrete"), "a concrete-dep draft must still auto-release"


def test_phase_boundary_released_with_auto(qproj, monkeypatch):
    monkeypatch.setenv("FLEET_AUTO_PHASE", "1")
    doctor.resolve_dependencies(fix=True, quiet=True)
    assert _in(qproj, "pending", "d_phase"), "FLEET_AUTO_PHASE=1 must let the boundary auto-cross"
