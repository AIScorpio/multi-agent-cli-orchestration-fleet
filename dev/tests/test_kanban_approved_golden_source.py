"""Approved-count GOLDEN SOURCE — board column and Overview/tab must never diverge.

Observed live 2026-07-08 (project 05): board APPROVED column showed 54 (47 qa-passed
task specs + 7 leader-approved detached board cards) while the tab strip / Overview
showed 47 — collect_overview() counted only the specs and omitted the board cards,
despite a comment claiming consistency. Fix: _approved_board_cards(fleet_dir) is the
single shared source for "leader-approved detached cards"; collect() merges its dicts
into the APPROVED column and collect_overview() adds its length to approved_n.
"""
import json
import sys
from pathlib import Path

ROOT_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(ROOT_SCRIPTS))
import kanban_hub  # noqa: E402


def _mk_project(tmp_path, n_specs=3, cards=None):
    root = tmp_path / "proj"
    ma = root / ".fleet"
    for d in ("queue/pending", "queue/claimed", "queue/completed/qa-passed",
              "queue/failed", "queue/drafts", "status"):
        (ma / d).mkdir(parents=True)
    qap = ma / "queue/completed/qa-passed"
    for i in range(n_specs):
        (qap / f"task-{i}.json").write_text(json.dumps(
            {"task_id": f"task-{i}", "title": f"t{i}", "phase": "P1"}))
    if cards is not None:
        (ma / "status/board_cards.json").write_text(json.dumps({"cards": cards}))
    return root


def test_helper_counts_only_approved_statuses(tmp_path):
    cards = [
        {"id": "c1", "title": "approved card", "status": "approved", "phase": "P4"},
        {"id": "c2", "title": "qa-passed card", "status": "qa-passed", "phase": "P4"},
        {"id": "c3", "title": "still running", "status": "running"},
        {"id": "c4", "title": "done pending QA", "status": "done"},
        {"id": "c5", "title": "failed", "status": "failed"},
    ]
    root = _mk_project(tmp_path, n_specs=0, cards=cards)
    got = kanban_hub._approved_board_cards(root / ".fleet")
    assert [c["task_id"] for c in got] == ["c1", "c2"]
    assert all(c["detached"] for c in got)


def test_board_and_overview_formula_agree(tmp_path):
    """The regression: specs + approved cards must equal the board column count."""
    cards = [{"id": "c1", "title": "big detached run", "status": "approved", "phase": "P4"},
             {"id": "c2", "title": "another", "status": "qa-passed", "phase": "P4"}]
    root = _mk_project(tmp_path, n_specs=3, cards=cards)
    board = kanban_hub.collect(root)
    # exactly what collect_overview() computes for approved_n:
    qap = root / ".fleet/queue/completed/qa-passed"
    specs_n = sum(1 for f in qap.glob("*.json")
                  if not f.name.startswith(".")
                  and not f.name.endswith((".result.json", ".verdict.json")))
    overview_n = specs_n + len(kanban_hub._approved_board_cards(root / ".fleet"))
    assert board["counts"]["approved"] == overview_n == 5
    # approved cards must not leak into other columns
    ids = {c["task_id"] for c in board["approved"]}
    assert {"c1", "c2"}.issubset(ids)
    for col in ("pending", "claimed", "completed", "failed"):
        assert not {"c1", "c2"} & {c.get("task_id") for c in board[col]}


def test_noop_without_board_cards_file(tmp_path):
    root = _mk_project(tmp_path, n_specs=2, cards=None)
    assert kanban_hub._approved_board_cards(root / ".fleet") == []
    assert kanban_hub.collect(root)["counts"]["approved"] == 2
