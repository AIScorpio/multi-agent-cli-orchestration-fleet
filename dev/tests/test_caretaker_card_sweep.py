"""D3 — the no-LLM caretaker sweeps DETACHED cards: auto-REJECT a 'done' card whose floor fails
(crashed/incomplete/predicate-fail), DEFER a floor-clean card to the leader (NEVER auto-approve —
detached semantic/science QA is the leader's job)."""
import json
import sys
from pathlib import Path

import pytest

ROOT_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(ROOT_SCRIPTS))
import doctor  # noqa: E402


@pytest.fixture
def cproj(tmp_path, monkeypatch):
    ma = tmp_path / ".fleet"
    (ma / "status").mkdir(parents=True)
    monkeypatch.setattr(doctor, "MA", ma)
    monkeypatch.setattr(doctor, "ROOT", tmp_path)
    return tmp_path, ma


def _card(ma, status="done", output=None, preds=None):
    card = {"id": "d1", "title": "s", "phase": "P2", "status": status}
    if output:
        card["output"] = output
    if preds:
        card["acceptance_predicates"] = preds
    (ma / "status" / "board_cards.json").write_text(json.dumps({"cards": [card]}))


def test_floor_failed_card_flagged(cproj):
    tmp, ma = cproj
    _card(ma, output="missing.json")                       # output missing → floor fails
    assert "d1" in doctor.sweep_card_floor(fix=False, quiet=True)


def test_floor_clean_card_deferred_not_approved(cproj):
    tmp, ma = cproj
    (tmp / "r.json").write_text("x")
    _card(ma, output="r.json", preds=[{"type": "command", "cmd": ["true"]}])
    assert doctor.sweep_card_floor(fix=False, quiet=True) == [], "clean → defer (not flagged)"
    # and NEVER auto-approved
    assert json.loads((ma / "status" / "board_cards.json").read_text())["cards"][0]["status"] == "done"


def test_no_machine_acceptance_deferred(cproj):
    tmp, ma = cproj
    _card(ma)                                              # no output / predicates / done
    assert doctor.sweep_card_floor(fix=False, quiet=True) == []
