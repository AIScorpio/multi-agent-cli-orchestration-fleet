"""Gate 5 (verifier-first) — DP3 pure-A, scoped to DETACH CARDS only.

The no-LLM caretaker must NOT execute a board CARD's command predicate (cards are runner-writable,
widened by E1/E4) — that's a code-execution surface the unattended automaton must never touch. The
present leader's approve-card path DOES run it (allow_command=True). QUEUE tasks are UNCHANGED:
their acceptance_predicates are leader-authored via create-task (05 has 60 in use), so the queue
floor keeps executing them. A done card pending the leader is surfaced via the qa_backlog alert.
"""
import json
import sys
from pathlib import Path

import pytest

ROOT_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(ROOT_SCRIPTS))
import doctor      # noqa: E402
import qa_floor    # noqa: E402
import fleet_health  # noqa: E402

_WRITE_SENTINEL = "import sys; open(sys.argv[1], 'w').write('x')"


@pytest.fixture
def dproj(tmp_path, monkeypatch):
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


def _cmd_pred(sentinel):
    return {"type": "command", "cmd": [sys.executable, "-c", _WRITE_SENTINEL, sentinel]}


# ── the core security property: caretaker never execs a card command ─────────────

def test_caretaker_does_not_exec_card_command(dproj):
    tmp, ma = dproj
    _card(ma, status="done", preds=[_cmd_pred("SENTINEL")])
    flagged = doctor.sweep_card_floor(fix=True, quiet=True)
    assert not (tmp / "SENTINEL").exists(), \
        "no-LLM caretaker must NOT execute a card's command predicate (RCE surface)"
    assert "d1" not in flagged, "a command-only clean card is DEFERRED, not auto-rejected"
    card = json.loads((ma / "status" / "board_cards.json").read_text())["cards"][0]
    assert card["status"] == "done", "must neither auto-approve nor auto-reject"


def test_leader_approve_path_runs_command(dproj):
    tmp, ma = dproj
    card = {"id": "x", "status": "done", "acceptance_predicates": [_cmd_pred("SENT2")]}
    ok, failures = qa_floor.evaluate_card(card, tmp, allow_command=True)
    assert (tmp / "SENT2").exists(), "present leader (approve-card) MUST run the command"
    assert ok and not failures


def test_safe_predicate_failure_still_rejected(dproj):
    tmp, ma = dproj
    _card(ma, status="done", output="missing.json")        # artifact missing → safe floor fails
    assert "d1" in doctor.sweep_card_floor(fix=False, quiet=True)


# ── queue path is UNCHANGED — leader-authored command predicates still run ────────

def test_queue_floor_still_runs_command(dproj):
    tmp, ma = dproj
    (tmp / "o.txt").write_text("data")
    spec = {"output_file": "o.txt", "acceptance_predicates": [_cmd_pred("SENT3")]}
    qa_floor.evaluate(spec, tmp, {})
    assert (tmp / "SENT3").exists(), \
        "queue floor (leader-authored predicates) must still execute commands — queue is unchanged"


# ── visibility: done cards count toward the qa_backlog alert ──────────────────────

def test_qa_backlog_counts_done_cards(tmp_path, monkeypatch):
    root = tmp_path / "proj"
    (root / ".fleet" / "status").mkdir(parents=True)
    (root / ".fleet" / "queue" / "completed" / "qa-passed").mkdir(parents=True)
    (root / ".fleet" / "status" / "board_cards.json").write_text(
        json.dumps({"cards": [{"id": f"c{i}", "status": "done"} for i in range(3)]}))
    monkeypatch.setattr(fleet_health, "QA_BACKLOG_MAX", 2)
    alerts = fleet_health.check_health(tmp_path / "fh", [{"root": str(root)}])
    assert any(a.get("type") == "qa_backlog" for a in alerts), \
        "a pile of done detached cards must alarm so a long leader absence is visible"
