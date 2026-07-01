"""D1 + D2 — detached board cards get machine-checkable acceptance + a floor-gated approve.

D1: `board-card` upserts a card carrying output + acceptance_predicates.
D2: `approve-card` runs the SAME mechanical floor (output artifact + predicates) FAIL-CLOSED —
    a card whose floor fails can't be approved — and on pass writes a structured verdict.
"""
import argparse
import json
import sys
from pathlib import Path

import pytest

ROOT_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(ROOT_SCRIPTS))
import orchestrator  # noqa: E402


@pytest.fixture
def proj(tmp_path, monkeypatch):
    ma = tmp_path / ".fleet"
    for d in ("queue/pending", "queue/completed/qa-passed", "status"):
        (ma / d).mkdir(parents=True)
    monkeypatch.setattr(orchestrator, "MA", ma)
    monkeypatch.setattr(orchestrator, "ROOT", tmp_path)
    monkeypatch.setattr(orchestrator, "QUEUE", ma / "queue")
    return tmp_path, ma


def _cards(ma):
    return json.loads((ma / "status" / "board_cards.json").read_text())["cards"]


def _upsert(id="c1", title="sweep", phase="P2", status="done", output=None, predicate=None, type=None):
    orchestrator.cmd_board_card(argparse.Namespace(
        id=id, title=title, phase=phase, status=status, output=output,
        predicate=predicate, type=type, done=None))


class TestBoardCardUpsert:  # D1
    def test_create_with_qa_fields(self, proj):
        tmp, ma = proj
        _upsert(output="results/sweep.json", predicate=['{"type":"command","cmd":["true"]}'],
                type="research")
        c = _cards(ma)[0]
        assert c["id"] == "c1" and c["output"] == "results/sweep.json" and c["status"] == "done"
        assert c["acceptance_predicates"][0]["cmd"] == ["true"]

    def test_upsert_updates_in_place(self, proj):
        tmp, ma = proj
        _upsert(status="running")
        _upsert(status="done", output="r.json")
        cards = _cards(ma)
        assert len(cards) == 1 and cards[0]["status"] == "done" and cards[0]["output"] == "r.json"


class TestApproveCardFloorGated:  # D2
    def test_approve_blocked_when_predicate_fails(self, proj):
        tmp, ma = proj
        (tmp / "r.json").write_text("x")
        _upsert(output="r.json", predicate=['{"type":"command","cmd":["false"]}'])
        with pytest.raises(SystemExit):
            orchestrator.cmd_approve_card(argparse.Namespace(card_id="c1", reason="ok"))
        assert _cards(ma)[0]["status"] == "done", "fail-closed: must NOT approve a floor-failing card"

    def test_approve_blocked_when_output_missing(self, proj):
        tmp, ma = proj
        _upsert(output="missing.json")            # no file on disk
        with pytest.raises(SystemExit):
            orchestrator.cmd_approve_card(argparse.Namespace(card_id="c1", reason="ok"))
        assert _cards(ma)[0]["status"] == "done"

    def test_approve_passes_floor_and_writes_verdict(self, proj):
        tmp, ma = proj
        (tmp / "r.json").write_text("results")
        _upsert(output="r.json", predicate=['{"type":"command","cmd":["true"]}'])
        orchestrator.cmd_approve_card(argparse.Namespace(card_id="c1", reason="correct vs paper"))
        c = _cards(ma)[0]
        assert c["status"] == "approved"
        assert c["verdict"]["reason"] == "correct vs paper"
        assert "predicates_enforced" in c["verdict"]


class TestCompletionAndProvenance:  # D6
    def _card_with_done(self, ma, tmp, items, value, provenance=None):
        (tmp / "results.json").write_text(json.dumps({"items": list(range(items))}))
        orchestrator.cmd_board_card(argparse.Namespace(
            id="c1", title="sweep", phase="P2", status="done", output="results.json",
            predicate=None, type="research",
            done=json.dumps({"type": "count", "source": "results.json", "path": "items",
                             "op": ">=", "value": value}),
            provenance=provenance))

    def test_incomplete_run_blocks_approve(self, proj):
        tmp, ma = proj
        self._card_with_done(ma, tmp, items=3, value=6)        # 3 < 6 → incomplete
        with pytest.raises(SystemExit):
            orchestrator.cmd_approve_card(argparse.Namespace(card_id="c1", reason="x"))
        assert _cards(ma)[0]["status"] == "done", "incomplete run must not be approvable"

    def test_complete_run_approves_with_provenance(self, proj):
        tmp, ma = proj
        self._card_with_done(ma, tmp, items=6, value=6, provenance="cfg@abc123")
        orchestrator.cmd_approve_card(argparse.Namespace(card_id="c1", reason="ok"))
        c = _cards(ma)[0]
        assert c["status"] == "approved"
        assert c["verdict"]["provenance"] == "cfg@abc123"
