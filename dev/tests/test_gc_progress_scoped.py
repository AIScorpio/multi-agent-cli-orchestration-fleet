"""Gate 7 (verifier-first) — gc_artifacts is STATUS-SCOPED for per-card progress files: it must
NEVER reap a RUNNING card's progress (a multi-day run must keep its progress visible), only reap
progress for terminal cards (done/approved/failed) or orphans, and only when stale. Regression:
qa-passed result/verdict sidecars stay exempt (the prior 'emptied the board' incident).
"""
import json
import sys
import time
from pathlib import Path

import pytest

ROOT_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(ROOT_SCRIPTS))
import doctor  # noqa: E402

FAR = 10 ** 9


@pytest.fixture
def dproj(tmp_path, monkeypatch):
    ma = tmp_path / ".fleet"
    (ma / "status" / "progress").mkdir(parents=True)
    (ma / "queue" / "completed" / "qa-passed").mkdir(parents=True)
    monkeypatch.setattr(doctor, "MA", ma)
    monkeypatch.setattr(doctor, "QUEUE", ma / "queue")
    monkeypatch.setattr(doctor, "LOGS", ma / "status" / "logs")
    return tmp_path, ma


def _board(ma, cards):
    (ma / "status" / "board_cards.json").write_text(json.dumps({"cards": cards}))


def _prog(ma, cid):
    (ma / "status" / "progress" / f"{cid}.json").write_text("{}")


def test_running_progress_survives_done_reaped(dproj):
    tmp, ma = dproj
    _board(ma, [{"id": "R", "status": "running"}, {"id": "D", "status": "done"}])
    _prog(ma, "R")
    _prog(ma, "D")
    doctor.gc_artifacts(now=time.time() + FAR)
    assert (ma / "status" / "progress" / "R.json").exists(), \
        "a RUNNING card's progress must NEVER be reaped (even very old)"
    assert not (ma / "status" / "progress" / "D.json").exists(), \
        "a terminal (done) card's stale progress should be reaped"


def test_orphan_progress_reaped_only_when_stale(dproj):
    tmp, ma = dproj
    _board(ma, [])
    _prog(ma, "orphan")
    doctor.gc_artifacts(now=time.time())                 # fresh → kept
    assert (ma / "status" / "progress" / "orphan.json").exists()
    doctor.gc_artifacts(now=time.time() + FAR)           # stale → reaped
    assert not (ma / "status" / "progress" / "orphan.json").exists()


def test_qa_passed_sidecars_still_exempt(dproj):
    tmp, ma = dproj
    qp = ma / "queue" / "completed" / "qa-passed"
    (qp / "t.result.json").write_text("{}")
    (qp / "t.verdict.json").write_text("{}")
    doctor.gc_artifacts(now=time.time() + FAR)
    assert (qp / "t.result.json").exists() and (qp / "t.verdict.json").exists(), \
        "qa-passed sidecars are the durable audit trail — must stay exempt from gc"
