"""Tests for capacity.py — the token-aware scheduling core."""
import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import capacity


@pytest.fixture(autouse=True)
def isolated_fleet_home(tmp_path, monkeypatch):
    monkeypatch.setattr(capacity, "FLEET_HOME", tmp_path)
    monkeypatch.setattr(capacity, "CAP_DIR", tmp_path / "capacity")
    yield tmp_path


def _write_cap(agent, **kw):
    d = {"agent": agent}
    d.update(kw)
    capacity._save(agent, d)


NOW = int(time.time())


class TestGate:
    def test_no_data_is_healthy_fail_open(self):
        assert capacity.gate_level("codex") == 0

    def test_soft_at_80(self):
        _write_cap("codex", used_5h_pct=82.0, resets_at_5h=NOW + 3600)
        assert capacity.gate_level("codex") == 1

    def test_drained_at_95(self):
        _write_cap("codex", used_5h_pct=96.0, resets_at_5h=NOW + 3600)
        assert capacity.gate_level("codex") == 2

    def test_weekly_window_drains(self):
        _write_cap("codex", used_week_pct=99.0, resets_at_week=NOW + 86400)
        assert capacity.gate_level("codex") == 2

    def test_reset_passed_self_corrects(self):
        # A stale 96% reading whose window already reset must read as healthy.
        _write_cap("codex", used_5h_pct=96.0, resets_at_5h=NOW - 10)
        assert capacity.gate_level("codex") == 0

    def test_drained_until_expiry(self):
        _write_cap("kimi", drained_until=NOW + 600)
        assert capacity.gate_level("kimi") == 2
        _write_cap("kimi", drained_until=NOW - 1)
        assert capacity.gate_level("kimi") == 0


class TestBump:
    def test_bump_drains_and_steps_rung(self):
        r1 = capacity.bump("claude-lead", cooldown=300)
        assert r1["rung"] == 1
        assert r1["drained_until"] > NOW
        r2 = capacity.bump("claude-lead", cooldown=300)
        assert r2["rung"] == 2

    def test_rung_decays_after_window(self):
        _write_cap("claude-lead", rung=2, rung_set_at=NOW - capacity.RUNG_DECAY_SECS - 10)
        assert capacity.effective("claude-lead")["rung"] == 0

    def test_window_reset_snaps_rung_to_top(self):
        # REGRESSION (user-caught): an expired drain == the window RESET ==
        # full quota. The first post-reset pick must be the TOP model, never a
        # downgraded hangover — degradation is intra-window only.
        _write_cap("claude-lead", rung=2, rung_set_at=NOW - 60,
                   drained_until=NOW - 5)          # drain just expired
        e = capacity.effective("claude-lead")
        assert e["rung"] == 0
        assert e["drained_until"] == 0
        assert capacity.pick("claude-lead") == "claude-opus-4-8"   # TOP rung

    def test_active_drain_keeps_rung(self):
        _write_cap("claude-lead", rung=1, rung_set_at=NOW - 60,
                   drained_until=NOW + 600)        # still inside the blackout
        assert capacity.effective("claude-lead")["rung"] == 1

    def test_clear_expired_also_resets_rung(self):
        _write_cap("codex", rung=2, rung_set_at=NOW - 60, drained_until=NOW - 5)
        capacity.clear_expired()
        d = capacity._load("codex")
        assert d["drained_until"] == 0 and d["rung"] == 0


class TestPick:
    def test_codex_effort_from_probe_data(self):
        _write_cap("codex", used_5h_pct=30.0, resets_at_5h=NOW + 3600,
                   source="rollout:r.jsonl", probed_at=NOW)
        assert capacity.pick("codex") == "xhigh"
        _write_cap("codex", used_5h_pct=70.0, resets_at_5h=NOW + 3600,
                   source="rollout:r.jsonl", probed_at=NOW)
        assert capacity.pick("codex") == "high"
        _write_cap("codex", used_5h_pct=90.0, resets_at_5h=NOW + 3600,
                   source="rollout:r.jsonl", probed_at=NOW)
        assert capacity.pick("codex") == "medium"

    def test_codex_effort_from_reactive_rung(self):
        _write_cap("codex", rung=1, rung_set_at=NOW, source="reactive", probed_at=NOW)
        assert capacity.pick("codex") == "high"

    def test_leader_always_top_model(self):
        # P17: the leader does NOT degrade by a model ladder — it runs the TOP model and
        # degrades via drain-to-reset on a cliff. pick is rung-independent.
        assert capacity.pick("claude-lead") == "claude-opus-4-8"
        _write_cap("claude-lead", rung=1, rung_set_at=NOW)
        assert capacity.pick("claude-lead") == "claude-opus-4-8"
        _write_cap("claude-lead", rung=9, rung_set_at=NOW)
        assert capacity.pick("claude-lead") == "claude-opus-4-8"

    def test_pinned_agents_return_empty(self):
        # kimi/opencode/claude-worker are pinned — no override signal.
        assert capacity.pick("kimi") == ""
        assert capacity.pick("opencode") == ""
        assert capacity.pick("claude") == ""

    def test_config_ladder_override(self):
        cfg = {"effort_ladder": [{"effort": "high", "below_pct": 101}]}
        _write_cap("codex", used_5h_pct=5.0, resets_at_5h=NOW + 3600,
                   source="rollout:r.jsonl", probed_at=NOW)
        assert capacity.pick("codex", cfg) == "high"


class TestRolloutParsing:
    ROLLOUT_LINE = json.dumps({
        "timestamp": "2026-06-06T08:20:27.997Z", "type": "event_msg",
        "payload": {"type": "token_count",
                    "info": {"total_token_usage": {"total_tokens": 24236}},
                    "rate_limits": {
                        "limit_id": "codex",
                        "primary": {"used_percent": 70.0, "window_minutes": 300,
                                    "resets_at": 1780752021},
                        "secondary": {"used_percent": 25.0, "window_minutes": 10080,
                                      "resets_at": 1781142558}}}})

    def test_parse_codex_rollout_finds_newest(self, tmp_path):
        f = tmp_path / "rollout-x.jsonl"
        old = self.ROLLOUT_LINE.replace('"used_percent": 70.0', '"used_percent": 10.0', 1)
        f.write_text(old + "\n"
                     + json.dumps({"type": "other", "rate_limits": None}) + "\n"
                     + self.ROLLOUT_LINE + "\n")
        rl = capacity.parse_codex_rollout(f)
        assert rl["primary"]["used_percent"] == 70.0   # newest (scanned from end)
        assert rl["secondary"]["used_percent"] == 25.0

    def test_parse_skips_garbage_lines(self, tmp_path):
        f = tmp_path / "rollout-y.jsonl"
        f.write_text('NOT JSON "rate_limits"\n' + self.ROLLOUT_LINE + "\n")
        assert capacity.parse_codex_rollout(f)["primary"]["resets_at"] == 1780752021

    def test_probe_codex_writes_registry(self, tmp_path):
        sessions = tmp_path / "sessions" / "2026" / "06" / "10"
        sessions.mkdir(parents=True)
        (sessions / "rollout-a.jsonl").write_text(self.ROLLOUT_LINE + "\n")
        r = capacity.probe_codex(tmp_path / "sessions")
        assert r["used_5h_pct"] == 70.0
        assert r["used_week_pct"] == 25.0
        assert r["resets_at_5h"] == 1780752021
        # registry file landed
        assert capacity._load("codex")["used_5h_pct"] == 70.0

    def test_probe_codex_no_sessions_dir(self, tmp_path):
        assert capacity.probe_codex(tmp_path / "nope") is None


class TestClearExpired:
    def test_clears_expired_drain(self):
        _write_cap("kimi", drained_until=NOW - 5)
        assert capacity.clear_expired() == 1
        assert capacity._load("kimi")["drained_until"] == 0
