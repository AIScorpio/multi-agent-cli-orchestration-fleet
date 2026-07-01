"""Generic observability FLOOR for long DETACHED runs — the framework guarantees a running card
shows "alive + coarse %" even if its runner NEVER calls fleet_progress.report() (the t3-class gap).
The no-LLM caretaker writes status/liveness/<id>.json from the card's done-count predicate (a REAL
%, for free) and/or its log size/mtime. It stays out of the way when the runner IS reporting
(progress file present), only covers RUNNING cards, is path-guarded, and writes ONLY the new
liveness file (single-writer; never touches queue/QA/board status). Detach-only (no queue floor).
"""
import json
import sys
import time
from pathlib import Path

import pytest

ROOT_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(ROOT_SCRIPTS))
import doctor       # noqa: E402
import kanban_hub   # noqa: E402


# ── caretaker side: doctor.sweep_liveness_floor ─────────────────────────────────

@pytest.fixture
def dproj(tmp_path, monkeypatch):
    ma = tmp_path / ".fleet"
    (ma / "status").mkdir(parents=True)
    monkeypatch.setattr(doctor, "MA", ma)
    monkeypatch.setattr(doctor, "ROOT", tmp_path)
    return tmp_path, ma


def _board(ma, cards):
    (ma / "status" / "board_cards.json").write_text(json.dumps({"cards": cards}))


def _live(ma, cid):
    p = ma / "status" / "liveness" / f"{cid}.json"
    return json.loads(p.read_text()) if p.exists() else None


def test_caretaker_writes_liveness_from_log(dproj):
    tmp, ma = dproj
    (tmp / "run.log").write_text("loading model...\nitem 1 done\nitem 2 done\n")
    _board(ma, [{"id": "j", "status": "running", "log": "run.log"}])
    doctor.sweep_liveness_floor(quiet=True)
    rec = _live(ma, "j")
    assert rec and rec["log_size"] > 0 and "log_age_s" in rec, rec


def test_liveness_real_pct_from_done_count(dproj):
    tmp, ma = dproj
    (tmp / "results.json").write_text(json.dumps({"items": list(range(6))}))   # 6 of 10
    _board(ma, [{"id": "j", "status": "running",
                 "done": {"type": "count", "source": "results.json", "path": "items",
                          "op": ">=", "value": 10}}])
    doctor.sweep_liveness_floor(quiet=True)
    rec = _live(ma, "j")
    assert rec and rec["done"] == 6 and rec["total"] == 10 and rec["pct"] == 60, rec


def test_liveness_skips_when_runner_is_reporting(dproj):
    tmp, ma = dproj
    (tmp / "run.log").write_text("x" * 50)
    (ma / "status" / "progress").mkdir(parents=True, exist_ok=True)
    (ma / "status" / "progress" / "j.json").write_text(
        json.dumps({"card": "j", "done": 5, "total": 10, "pct": 50}))
    _board(ma, [{"id": "j", "status": "running", "log": "run.log"}])
    doctor.sweep_liveness_floor(quiet=True)
    assert _live(ma, "j") is None, "floor must stay out when the runner is reporting (enrichment owns it)"


def test_liveness_skips_non_running(dproj):
    tmp, ma = dproj
    (tmp / "run.log").write_text("x")
    _board(ma, [{"id": "j", "status": "done", "log": "run.log"}])
    doctor.sweep_liveness_floor(quiet=True)
    assert _live(ma, "j") is None


def test_liveness_log_outside_root_safe(dproj):
    tmp, ma = dproj
    secret = tmp.parent / "secret.txt"
    secret.write_text("TOPSECRET")
    _board(ma, [{"id": "j", "status": "running", "log": str(secret)}])   # outside root, no done
    doctor.sweep_liveness_floor(quiet=True)
    assert _live(ma, "j") is None, "no safe signal (log outside root) → write nothing"


# ── hub side: render liveness as a FALLBACK (progress > cell_progress > liveness > plain) ──

def _kproj(tmp_path):
    ma = tmp_path / ".fleet"
    for d in ("queue/pending", "queue/claimed", "queue/completed/qa-passed", "queue/drafts",
              "queue/failed", "status/pids", "status/logs", "status/progress", "status/liveness"):
        (ma / d).mkdir(parents=True)
    return ma


def _ctitle(board, cid):
    return [c["title"] for c in board["claimed"] if c["task_id"] == cid][0]


def test_hub_renders_liveness_when_no_progress(tmp_path):
    ma = _kproj(tmp_path)
    (ma / "status" / "board_cards.json").write_text(
        json.dumps({"cards": [{"id": "L", "title": "job", "status": "running"}]}))
    (ma / "status" / "liveness" / "L.json").write_text(
        json.dumps({"card": "L", "log_size": 2048, "log_age_s": 12}))
    t = _ctitle(kanban_hub.collect(tmp_path), "L")
    assert "●" in t and t != "job", f"floor should surface a live card: {t!r}"


def test_hub_progress_wins_over_liveness(tmp_path):
    ma = _kproj(tmp_path)
    (ma / "status" / "board_cards.json").write_text(
        json.dumps({"cards": [{"id": "R", "title": "job", "status": "running"}]}))
    (ma / "status" / "progress" / "R.json").write_text(
        json.dumps({"card": "R", "done": 50, "total": 100, "pct": 50}))
    (ma / "status" / "liveness" / "R.json").write_text(
        json.dumps({"card": "R", "log_size": 999, "log_age_s": 5}))
    t = _ctitle(kanban_hub.collect(tmp_path), "R")
    assert "50/100" in t and "●" not in t, f"enrichment must win over floor: {t!r}"


def test_liveness_gc_status_scoped(tmp_path, monkeypatch):
    ma = tmp_path / ".fleet"
    (ma / "status" / "liveness").mkdir(parents=True)
    (ma / "queue" / "completed" / "qa-passed").mkdir(parents=True)
    monkeypatch.setattr(doctor, "MA", ma)
    monkeypatch.setattr(doctor, "QUEUE", ma / "queue")
    monkeypatch.setattr(doctor, "LOGS", ma / "status" / "logs")
    (ma / "status" / "board_cards.json").write_text(
        json.dumps({"cards": [{"id": "R", "status": "running"}, {"id": "D", "status": "done"}]}))
    (ma / "status" / "liveness" / "R.json").write_text("{}")
    (ma / "status" / "liveness" / "D.json").write_text("{}")
    doctor.gc_artifacts(now=time.time() + 10 ** 9)
    assert (ma / "status" / "liveness" / "R.json").exists(), "running card liveness must survive gc"
    assert not (ma / "status" / "liveness" / "D.json").exists(), "terminal stale liveness should be reaped"
