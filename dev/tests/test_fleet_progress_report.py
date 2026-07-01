"""Gate 1 (verifier-first) — fleet_progress.report() writes a PER-CARD progress file so
concurrent detached runs never collide (the single cell_progress.json bug), with safe
eta/pct math (no div-by-zero on the 0% start tick), id derived from the output path
(not an env var that can't reach per-cell subprocesses), throttled writes that never drop
the terminal tick, and a fail-open body that never raises into a runner's hot loop.

Every assertion drives the REAL entry point fleet_progress.report() + the on-disk file the
kanban hub reads — not a private helper.
"""
import json
import sys
from pathlib import Path

import pytest

ROOT_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(ROOT_SCRIPTS))
import fleet_progress  # noqa: E402


def _read(root, cid):
    return json.loads((root / ".fleet" / "status" / "progress" / f"{cid}.json").read_text())


@pytest.fixture
def proj(tmp_path):
    (tmp_path / ".fleet" / "status").mkdir(parents=True)
    return tmp_path


# ── per-card file + schema ──────────────────────────────────────────────────────

def test_writes_per_card_file(proj):
    fleet_progress.report(10, 100, card_id="c1", root=proj, stage="seed 1/3")
    d = _read(proj, "c1")
    assert d["card"] == "c1" and d["done"] == 10 and d["total"] == 100
    assert d["pct"] == 10 and d["stage"] == "seed 1/3" and d["unit"] == "items"
    assert isinstance(d["ts"], (int, float)) and isinstance(d["started_at"], (int, float))
    assert d["eta_s"] is not None and d["eta_s"] >= 0   # done>0 and <total → real eta


def test_two_cards_never_collide(proj):
    # the core regression: the OLD single cell_progress.json could only hold one cell.
    fleet_progress.report(30, 100, card_id="a", root=proj)
    fleet_progress.report(70, 200, card_id="b", root=proj)
    assert _read(proj, "a")["done"] == 30 and _read(proj, "a")["total"] == 100
    assert _read(proj, "b")["done"] == 70 and _read(proj, "b")["total"] == 200


# ── id derivation: output stem (+ env override), NOT a single env var ────────────

def test_id_from_output_stem_with_walkup(proj):
    sub = proj / "results"
    sub.mkdir()
    out = sub / "exp3__glm__set.json"
    fleet_progress.report(5, 10, output=str(out))          # no root, no card_id → walk up + stem
    assert _read(proj, "exp3__glm__set")["done"] == 5


def test_env_overrides_stem(proj, monkeypatch):
    monkeypatch.setenv("FLEET_CARD_ID", "envid")
    fleet_progress.report(1, 4, output=str(proj / "x.json"), root=proj)
    assert (proj / ".fleet" / "status" / "progress" / "envid.json").exists()


def test_explicit_card_id_beats_env(proj, monkeypatch):
    monkeypatch.setenv("FLEET_CARD_ID", "envid")
    fleet_progress.report(1, 4, card_id="explicit", root=proj)
    assert (proj / ".fleet" / "status" / "progress" / "explicit.json").exists()


# ── safe math: never raise on the 0% start tick / total=0 / overshoot ────────────

def test_zero_done_no_divzero(proj):
    fleet_progress.report(0, 100, card_id="z", root=proj)   # every runner's seed-start tick
    d = _read(proj, "z")
    assert d["eta_s"] is None and d["pct"] == 0


def test_zero_total_no_raise(proj):
    fleet_progress.report(5, 0, card_id="t", root=proj)
    d = _read(proj, "t")
    assert d["pct"] is None                                  # can't compute a percentage


def test_done_clamped_to_total(proj):
    fleet_progress.report(150, 100, card_id="cl", root=proj)
    d = _read(proj, "cl")
    assert d["done"] == 100 and d["pct"] == 100


# ── fail-open: never raise into the runner's hot path ───────────────────────────

def test_unresolvable_target_is_noop(tmp_path):
    # no root, no output, no .fleet anywhere up-tree → cannot resolve → silent no-op
    fleet_progress.report(1, 2)                              # must not raise


def test_unwritable_target_fails_open(proj):
    # progress path blocked by a regular file where the dir must be → mkdir fails
    bad = proj / ".fleet" / "status" / "progress"
    bad.write_text("not a dir")
    fleet_progress.report(1, 2, card_id="x", root=proj)     # must not raise


# ── throttle: collapse rapid ticks but NEVER drop the terminal (100%) tick ───────

def test_throttle_and_terminal(proj, monkeypatch):
    monkeypatch.setattr(fleet_progress, "_now", lambda: 1000.0)
    fleet_progress.report(10, 100, card_id="th", root=proj, throttle_s=5)
    assert _read(proj, "th")["done"] == 10

    monkeypatch.setattr(fleet_progress, "_now", lambda: 1001.0)   # within throttle window
    fleet_progress.report(20, 100, card_id="th", root=proj, throttle_s=5)
    assert _read(proj, "th")["done"] == 10, "non-terminal tick within throttle must be skipped"

    monkeypatch.setattr(fleet_progress, "_now", lambda: 1002.0)   # still within window, but terminal
    fleet_progress.report(100, 100, card_id="th", root=proj, throttle_s=5)
    assert _read(proj, "th")["done"] == 100, "terminal tick must write despite throttle"


def test_started_at_preserved_across_writes(proj, monkeypatch):
    monkeypatch.setattr(fleet_progress, "_now", lambda: 500.0)
    fleet_progress.report(0, 10, card_id="s", root=proj, throttle_s=0)
    monkeypatch.setattr(fleet_progress, "_now", lambda: 530.0)
    fleet_progress.report(5, 10, card_id="s", root=proj, throttle_s=0)
    d = _read(proj, "s")
    assert d["started_at"] == 500.0 and d["ts"] == 530.0


# ── E3: optional structured log line is APPENDED, never truncates the runner's log ─

def test_log_line_appends_not_truncates(proj):
    logf = proj / "job.log"
    logf.write_text("PRE-EXISTING RUNNER OUTPUT\n")
    fleet_progress.report(50, 100, card_id="lg", root=proj, log=str(logf), stage="fever")
    body = logf.read_text()
    assert "PRE-EXISTING RUNNER OUTPUT" in body, "must not truncate the runner's own log"
    assert "[progress]" in body and "50/100" in body
