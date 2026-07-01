"""Gate 3 (verifier-first) — the kanban hub annotates each RUNNING detached card from its OWN
per-card progress file (status/progress/<id>.json), so two concurrent runs show distinct %s and
never cross-talk (the single cell_progress.json bug). A card with no progress file renders exactly
as today (title verbatim, no suffix, no crash); the old cell_progress.json stays a fallback.

Drives the real entry point kanban_hub.collect(root) and the on-disk files report() writes.
"""
import json
import sys
from pathlib import Path

import pytest

ROOT_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(ROOT_SCRIPTS))
import kanban_hub  # noqa: E402


@pytest.fixture
def proj(tmp_path):
    ma = tmp_path / ".fleet"
    for d in ("queue/pending", "queue/claimed", "queue/completed/qa-passed",
              "queue/drafts", "queue/failed", "status/pids", "status/logs",
              "status/progress"):
        (ma / d).mkdir(parents=True)
    return tmp_path, ma


def _cards(ma, cards):
    (ma / "status" / "board_cards.json").write_text(json.dumps({"cards": cards}))


def _progress(ma, cid, **fields):
    (ma / "status" / "progress" / f"{cid}.json").write_text(json.dumps(fields))


def _claimed_title(board, cid):
    for c in board["claimed"]:
        if c["task_id"] == cid:
            return c["title"]
    raise AssertionError(f"running card {cid} not in claimed: {[c['task_id'] for c in board['claimed']]}")


def test_two_running_cards_each_own_pct(proj):
    tmp, ma = proj
    _cards(ma, [{"id": "A", "title": "expA", "phase": "P2", "status": "running"},
                {"id": "B", "title": "expB", "phase": "P2", "status": "running"}])
    _progress(ma, "A", card="A", stage="sweepA", done=30, total=100, pct=30, eta_s=600, started_at=1.0, ts=2.0)
    _progress(ma, "B", card="B", stage="sweepB", done=70, total=200, pct=35, eta_s=300, started_at=1.0, ts=2.0)
    board = kanban_hub.collect(tmp)
    ta, tb = _claimed_title(board, "A"), _claimed_title(board, "B")
    assert "30/100" in ta and "~30%" in ta and "sweepA" in ta
    assert "70/200" not in ta, "card A must NOT carry card B's numbers (no cross-talk)"
    assert "70/200" in tb and "~35%" in tb and "sweepB" in tb
    assert "30/100" not in tb


def test_running_card_without_progress_is_verbatim(proj):
    tmp, ma = proj
    _cards(ma, [{"id": "C", "title": "plainexp", "phase": "P1", "status": "running"}])
    board = kanban_hub.collect(tmp)          # no progress file, no cell_progress
    assert _claimed_title(board, "C") == "plainexp"


def test_eta_none_not_rendered(proj):
    tmp, ma = proj
    _cards(ma, [{"id": "D", "title": "exp", "phase": "P1", "status": "running"}])
    _progress(ma, "D", card="D", stage=None, done=0, total=100, pct=0, eta_s=None, started_at=1.0, ts=2.0)
    t = _claimed_title(board=kanban_hub.collect(tmp), cid="D")
    assert "eta" not in t and "None" not in t and "inf" not in t
    assert "0/100" in t                      # progress still shown, just no eta


def test_cell_progress_fallback_still_works(proj):
    tmp, ma = proj
    # a card with NO per-card progress file but a matching legacy cell_progress.json
    _cards(ma, [{"id": "E", "title": "legacy", "phase": "P1", "status": "running"}])
    (ma / "status" / "cell_progress.json").write_text(json.dumps(
        {"cell": "E", "seed": 2, "seeds": 3, "items_done": 5, "items_total": 10}))
    t = _claimed_title(board=kanban_hub.collect(tmp), cid="E")
    assert "seed 2/3" in t, "legacy cell_progress fallback must still annotate"
