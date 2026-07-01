"""Part 1 — the no-LLM caretaker auto-PASS is a leader-ABSENCE continuity mechanism only.
It must never bypass a PRESENT leader's semantic review, and never auto-pass content
(research/write/review) even in absence (the leader's exclusive job). floor_decision:
  · leader ALIVE (fresh heartbeat)            → defer (leader does QA)
  · leader ABSENT + content                   → defer (science waits for the leader)
  · leader ABSENT + non-content + predicates  → pass (DAG continuity)
  · no predicates                             → defer
  · floor violation                           → fail
"""
import os
import sys
import time
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import doctor  # noqa: E402


@pytest.fixture
def proj(tmp_path, monkeypatch):
    ma = tmp_path / ".fleet"
    (ma / "status").mkdir(parents=True)
    monkeypatch.setattr(doctor, "MA", ma)
    monkeypatch.setattr(doctor, "ROOT", tmp_path)
    (tmp_path / "o.txt").write_text("deliverable")
    monkeypatch.delenv("FLEET_LEADER_TTL", raising=False)
    return tmp_path, ma


def _spec(ttype="code", preds=True):
    s = {"task_id": "t", "type": ttype, "output_file": "o.txt", "acceptance_criteria": ["c"]}
    if preds:
        s["acceptance_predicates"] = [{"type": "command", "cmd": ["true"]}]
    return s


def _heartbeat(ma, fresh=True):
    hb = ma / "status" / "leader.heartbeat"
    hb.write_text("x")
    if not fresh:
        old = time.time() - 10000
        os.utime(hb, (old, old))


def test_noncontent_predicates_leader_absent_pass(proj):
    tmp, ma = proj                                   # no heartbeat → leader absent
    assert doctor.floor_decision(_spec("code"), tmp)[0] == "pass"


def test_noncontent_predicates_leader_present_defer(proj):
    tmp, ma = proj
    _heartbeat(ma, fresh=True)
    assert doctor.floor_decision(_spec("code"), tmp)[0] == "defer", \
        "must NOT bypass a present leader's review"


def test_stale_heartbeat_is_absent_pass(proj):
    tmp, ma = proj
    _heartbeat(ma, fresh=False)                       # stale → leader gone → continuity
    assert doctor.floor_decision(_spec("code"), tmp)[0] == "pass"


def test_content_always_defers(proj):
    tmp, ma = proj                                   # leader absent, but content → defer
    assert doctor.floor_decision(_spec("research"), tmp)[0] == "defer"


def test_no_predicates_defer(proj):
    tmp, ma = proj
    assert doctor.floor_decision(_spec("code", preds=False), tmp)[0] == "defer"


def test_floor_violation_still_fails(proj):
    tmp, ma = proj
    (tmp / "o.txt").unlink()                          # missing artifact → floor fail
    assert doctor.floor_decision(_spec("code"), tmp)[0] == "fail"
