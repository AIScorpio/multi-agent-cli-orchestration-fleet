"""Detached-job board cards: a DETACHED batch run (launched via detach_run.py, not a queue task)
writes .fleet/status/board_cards.json, and the hub renders each unit as a card in its phase —
WITHOUT writing to the on-disk queue, so the watchers and the no-LLM caretaker (which read the
queue dirs) are unaffected. Closes the kanban visibility gap for long detached runs: the human
oversight surface now shows per-unit progress in real time, not just "phase active"."""
import json, sys
from pathlib import Path

ROOT_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(ROOT_SCRIPTS))
import kanban_hub


def _proj(tmp_path):
    ma = tmp_path / ".fleet"
    for d in ("queue/pending", "queue/claimed", "queue/completed/qa-passed",
              "queue/failed", "queue/drafts", "status"):
        (ma / d).mkdir(parents=True)
    return ma


def test_board_cards_render_per_unit(tmp_path):
    ma = _proj(tmp_path)
    (ma / "status" / "board_cards.json").write_text(json.dumps({"phase": "P4", "cards": [
        {"id": "run1", "title": "running cell", "phase": "P4", "status": "running"},
        {"id": "done1", "title": "done cell", "phase": "P4", "status": "done", "at": "2026-06-21T00:00:00Z"},
        {"id": "pend1", "title": "pending cell", "phase": "P4", "status": "pending"},
        {"id": "fail1", "title": "failed cell", "phase": "P4", "status": "failed", "at": "2026-06-21T00:00:00Z"},
    ]}))
    b = kanban_hub.collect(tmp_path)
    assert any(c["task_id"] == "run1" for c in b["claimed"]), "running unit must be an in-progress card"
    assert any(c["task_id"] == "done1" for c in b["completed"]), "done unit must be a completed card"
    assert any(c["task_id"] == "pend1" for c in b["pending"]), "pending unit must be a pending card"
    assert any(c["task_id"] == "fail1" for c in b["failed"]), "failed unit must be a failed card"
    assert any(c.get("detached") and c["phase"] == "P4" for c in b["claimed"] if c["task_id"] == "run1")
    # CRITICAL: nothing written to the on-disk queue -> watchers + caretaker untouched.
    assert not list((ma / "queue" / "claimed").glob("*.json"))
    assert not list((ma / "queue" / "pending").glob("*.json"))


def test_no_board_cards_is_noop(tmp_path):
    _proj(tmp_path)
    b = kanban_hub.collect(tmp_path)
    assert b["claimed"] == [] and b["pending"] == [] and b["completed"] == [] and b["failed"] == []


def test_running_card_annotated_with_within_cell_progress(tmp_path):
    ma = _proj(tmp_path)
    (ma / "status" / "board_cards.json").write_text(json.dumps({"phase": "P4", "cards": [
        {"id": "t1__M__hotpotqa", "title": "T1 · M · hotpotqa", "phase": "P4", "status": "running"},
    ]}))
    (ma / "status" / "cell_progress.json").write_text(json.dumps({
        "cell": "t1__M__hotpotqa", "seed": 2, "seeds": 6, "items_done": 200, "items_total": 600}))
    b = kanban_hub.collect(tmp_path)
    card = next(c for c in b["claimed"] if c["task_id"] == "t1__M__hotpotqa")
    # seed 2/6 with 200/600 of it done -> (1 + 1/3)/6 = 22%
    assert all(s in card["title"] for s in ("seed 2/6", "200/600", "22%"))
