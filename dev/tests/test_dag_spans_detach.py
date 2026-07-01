"""D5 — the DAG/phase accounting spans both tracks: a queue task depending on `phase:N` (where
N's work is a DETACHED card) releases ONLY after the card is approved (approved→qa member;
pending/running/done→out; failed→ignored)."""
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
    (ma / "status").mkdir(parents=True)
    monkeypatch.setattr(doctor, "MA", ma)
    monkeypatch.setattr(doctor, "ROOT", tmp_path)
    monkeypatch.setattr(doctor, "QUEUE", q)
    monkeypatch.setattr(doctor, "ledger", None)
    monkeypatch.setenv("FLEET_AUTO_PHASE", "1")   # so a SATISFIED phase boundary releases (isolate D5 from attended-hold)
    return tmp_path, q


def _card(tmp, phase, status):
    (tmp / ".fleet" / "status" / "board_cards.json").write_text(
        json.dumps({"cards": [{"id": "d1", "title": "sweep", "phase": phase, "status": status}]}))


def _draft(q, tid, deps, phase="3"):
    (q / "drafts" / f"{tid}.json").write_text(
        json.dumps({"task_id": tid, "phase": phase, "depends_on": deps}))


def test_queue_dep_held_until_detach_card_approved(qproj):
    tmp, q = qproj
    _draft(q, "cons", ["phase:2"], phase="3")
    _card(tmp, phase="2", status="done")          # detached, not approved → phase 2 'out'
    assert doctor.resolve_dependencies(fix=True, quiet=True) == 0, "held while the card is unapproved"
    assert (q / "drafts" / "cons.json").exists()
    _card(tmp, phase="2", status="approved")      # approved → phase 2 'qa', no out
    assert doctor.resolve_dependencies(fix=True, quiet=True) == 1, "released once the card is approved"
    assert (q / "pending" / "cons.json").exists()


def test_failed_card_does_not_satisfy(qproj):
    tmp, q = qproj
    _draft(q, "cons", ["phase:2"], phase="3")
    _card(tmp, phase="2", status="failed")        # failed → ignored → phase 2 has no done member
    assert doctor.resolve_dependencies(fix=True, quiet=True) == 0
