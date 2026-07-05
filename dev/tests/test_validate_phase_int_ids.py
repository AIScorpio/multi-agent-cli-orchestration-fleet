"""Regression — `_validate_phase` must accept phases.json ids that are INTS.

`phases.set_phases` accepts integer phase ids ({"id": 1, ...}); `_validate_phase`
previously assumed string ids and called `.startswith("P")` on them, crashing every
`create-task` with AttributeError on any project whose leader defined int ids
(observed live 2026-07-05: 11/11 create-task calls crashed). Ids must be coerced
to str before matching, and the error path must join them safely.
"""
import json
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import orchestrator  # noqa: E402


def _arm(tmp_path, monkeypatch, ids):
    fleet = tmp_path / ".fleet"
    fleet.mkdir()
    (fleet / "phases.json").write_text(json.dumps({
        "state": "defined",
        "phases": [{"id": i, "name": f"phase-{i}"} for i in ids],
    }))
    monkeypatch.setattr(orchestrator, "MA", fleet)


def test_int_ids_accept_matching_phase(tmp_path, monkeypatch):
    _arm(tmp_path, monkeypatch, [1, 2, 3])
    orchestrator._validate_phase(1)      # int arg
    orchestrator._validate_phase("2")    # str arg against int ids


def test_int_ids_reject_unknown_phase(tmp_path, monkeypatch):
    _arm(tmp_path, monkeypatch, [1, 2, 3])
    with pytest.raises(SystemExit):
        orchestrator._validate_phase(9)  # unknown → clean rejection, not AttributeError


def test_string_p_ids_still_accept_bare_number(tmp_path, monkeypatch):
    _arm(tmp_path, monkeypatch, ["P1", "P2"])
    orchestrator._validate_phase("1")    # bare-number form vs "P1"
    orchestrator._validate_phase("P2")
