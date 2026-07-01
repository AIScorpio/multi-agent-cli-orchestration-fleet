"""Gate 4 (verifier-first) — a detached card's log is surfaced in the drawer, but card["log"]
is a FREE path written into board_cards.json (by a runner), so it CANNOT reuse the queue
read_log token guard. resolve_card_log REPLACES it: resolve the real path (following symlinks)
and require it to live inside the card's OWN project root, be a regular file, and be returned
as a BOUNDED tail (the 8h log must not be slurped whole). The card is looked up in its project's
own board_cards.json so a client cannot bind project=A to project=B's card.
"""
import argparse
import json
import os
import sys
from pathlib import Path

import pytest

ROOT_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(ROOT_SCRIPTS))
import orchestrator  # noqa: E402
import kanban_hub     # noqa: E402


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


# ── board-card --log (getattr: absent → no key, R7) ─────────────────────────────

def test_board_card_stores_log(proj):
    tmp, ma = proj
    orchestrator.cmd_board_card(argparse.Namespace(
        id="c1", title="t", phase="P1", status="running", output=None,
        predicate=None, type=None, done=None, provenance=None, log="logs/run.log"))
    assert _cards(ma)[0]["log"] == "logs/run.log"


def test_board_card_without_log_attr_no_key(proj):
    tmp, ma = proj
    orchestrator.cmd_board_card(argparse.Namespace(
        id="c2", title="t", phase="P1", status="running",
        output=None, predicate=None, type=None))     # NO log/done/provenance attrs → getattr
    assert "log" not in _cards(ma)[0]


# ── resolve_card_log: path containment (REPLACES TASK_ID_RE) ─────────────────────

def test_log_inside_root_ok(proj):
    tmp, ma = proj
    (tmp / "logs").mkdir()
    (tmp / "logs" / "run.log").write_text("HELLO-LOG\n")
    ok, val = kanban_hub.resolve_card_log({"log": "logs/run.log"}, tmp)
    assert ok and "HELLO-LOG" in val


def test_log_outside_root_denied(proj):
    tmp, ma = proj
    secret = tmp.parent / "secret.txt"
    secret.write_text("TOPSECRET")
    ok, val = kanban_hub.resolve_card_log({"log": str(secret)}, tmp)
    assert not ok and "TOPSECRET" not in val


def test_symlink_escape_denied(proj):
    tmp, ma = proj
    secret = tmp.parent / "escape.txt"
    secret.write_text("ESCAPED")
    (tmp / "logs").mkdir()
    os.symlink(secret, tmp / "logs" / "link.log")          # symlink INSIDE root → OUTSIDE
    ok, val = kanban_hub.resolve_card_log({"log": "logs/link.log"}, tmp)
    assert not ok and "ESCAPED" not in val, "must .resolve() before the containment check"


def test_dotdot_traversal_denied(proj):
    tmp, ma = proj
    (tmp.parent / "x.txt").write_text("DOTDOT")
    ok, val = kanban_hub.resolve_card_log({"log": "../x.txt"}, tmp)
    assert not ok


def test_non_regular_file_denied(proj):
    tmp, ma = proj
    (tmp / "adir").mkdir()
    ok, val = kanban_hub.resolve_card_log({"log": "adir"}, tmp)
    assert not ok


def test_no_log_field(proj):
    tmp, ma = proj
    ok, val = kanban_hub.resolve_card_log({}, tmp)
    assert not ok


# ── bounded tail (R2): never slurp the whole 8h log; UTF-8 boundary safe ─────────

def test_tail_bounds_large_log(proj):
    tmp, ma = proj
    (tmp / "big.log").write_bytes(b"OLD LINE\n" * 100000 + b"TAILMARKER\n")
    ok, val = kanban_hub.resolve_card_log({"log": "big.log"}, tmp)
    assert ok and "TAILMARKER" in val
    assert len(val) < 300000, "must return a bounded tail, not the whole file"


def test_tail_utf8_boundary_no_raise(proj):
    tmp, ma = proj
    (tmp / "u.log").write_text("·" * 200000 + "ENDMARK\n", encoding="utf-8")  # · = 2-byte UTF-8
    ok, val = kanban_hub.resolve_card_log({"log": "u.log"}, tmp)
    assert ok and isinstance(val, str) and "ENDMARK" in val


# ── cross-project binding: a card is resolved against ITS OWN project ────────────

def test_cross_project_binding_denied(tmp_path):
    a, b = tmp_path / "A", tmp_path / "B"
    for r in (a, b):
        (r / ".fleet" / "status").mkdir(parents=True)
    (b / ".fleet" / "status" / "board_cards.json").write_text(
        json.dumps({"cards": [{"id": "bx", "log": "/etc/hosts"}]}))
    (a / ".fleet" / "status" / "board_cards.json").write_text(json.dumps({"cards": []}))
    assert kanban_hub.read_card_log(a, "bx") == "(unknown card)"
