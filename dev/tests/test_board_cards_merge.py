"""Gate 6 (verifier-first) — board_cards.merge_write makes a runner's full-board rebuild safe
against the leader's concurrent approve-card (R4 lost-update). Merge semantics: only the listed
ids are touched; unknown keys (log/verdict) on a touched card are preserved; and a leader-terminal
card (approved/qa-passed) is NEVER downgraded by a (possibly stale) runner update.
"""
import argparse
import json
import sys
from pathlib import Path

import pytest

ROOT_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(ROOT_SCRIPTS))
import board_cards   # noqa: E402
import orchestrator  # noqa: E402


@pytest.fixture
def proj(tmp_path, monkeypatch):
    ma = tmp_path / ".fleet"
    for d in ("queue/completed/qa-passed", "status"):
        (ma / d).mkdir(parents=True)
    monkeypatch.setattr(orchestrator, "MA", ma)
    monkeypatch.setattr(orchestrator, "ROOT", tmp_path)
    monkeypatch.setattr(orchestrator, "QUEUE", ma / "queue")
    return tmp_path, ma


def _cards(ma):
    return json.loads((ma / "status" / "board_cards.json").read_text())["cards"]


def _seed(ma, cards):
    (ma / "status" / "board_cards.json").write_text(json.dumps({"cards": cards}))


# ── unit: merge semantics ────────────────────────────────────────────────────────

def test_preserves_unknown_keys_on_touched_card(proj):
    tmp, ma = proj
    _seed(ma, [{"id": "c1", "status": "running", "log": "/p/run.log",
                "verdict": {"verdict": "approved"}}])
    board_cards.merge_write(tmp, [{"id": "c1", "status": "done", "title": "t"}])
    c = _cards(ma)[0]
    assert c["title"] == "t" and c["log"] == "/p/run.log" and c["verdict"]["verdict"] == "approved"


def test_no_downgrade_of_terminal_card(proj):
    tmp, ma = proj
    _seed(ma, [{"id": "c1", "status": "approved", "verdict": {"verdict": "approved"}}])
    board_cards.merge_write(tmp, [{"id": "c1", "status": "done"}])     # stale runner rebuild
    assert _cards(ma)[0]["status"] == "approved", "must NOT revert a leader-approved card"


def test_normal_transition_applies(proj):
    tmp, ma = proj
    _seed(ma, [{"id": "c1", "status": "running"}])
    board_cards.merge_write(tmp, [{"id": "c1", "status": "done"}])
    assert _cards(ma)[0]["status"] == "done"


def test_only_listed_cards_touched(proj):
    tmp, ma = proj
    _seed(ma, [{"id": "a", "status": "approved", "verdict": {"v": 1}},
               {"id": "b", "status": "running"}])
    board_cards.merge_write(tmp, [{"id": "b", "status": "done"}])
    by = {c["id"]: c for c in _cards(ma)}
    assert by["a"]["status"] == "approved" and by["a"]["verdict"] == {"v": 1}
    assert by["b"]["status"] == "done"


def test_creates_new_card(proj):
    tmp, ma = proj
    _seed(ma, [])
    board_cards.merge_write(tmp, [{"id": "new", "status": "pending", "title": "t"}])
    assert _cards(ma)[0]["id"] == "new" and _cards(ma)[0]["status"] == "pending"


# ── integration: leader approval survives a runner rebuild (the R4 scenario) ──────

def test_runner_rebuild_preserves_leader_approval(proj):
    tmp, ma = proj
    (tmp / "r.json").write_text("results")
    orchestrator.cmd_board_card(argparse.Namespace(
        id="c1", title="sweep", phase="P2", status="done", output="r.json",
        predicate=None, type="research", done=None, provenance=None, log="/p/run.log"))
    orchestrator.cmd_approve_card(argparse.Namespace(card_id="c1", reason="correct vs paper"))
    assert _cards(ma)[0]["status"] == "approved"

    # a runner now rebuilds the board knowing only id/status/title (R4 bug scenario)
    board_cards.merge_write(tmp, [{"id": "c1", "status": "done", "title": "sweep"}])
    c = _cards(ma)[0]
    assert c["status"] == "approved", "runner rebuild must NOT revert the leader's approval"
    assert c["verdict"]["verdict"] == "approved", "leader verdict must survive"
    assert c["log"] == "/p/run.log"
