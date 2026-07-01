"""P19 (#3) — Claude has no token meter, so STRIP all the useless Claude spend ESTIMATION
(spend.jsonl, worker log-bytes/4 feed, leader 12000 constant, pool gate/alerts, by_project)
and keep ONLY the real signals: codex rollout telemetry + reactive bump/drain.
"""
import sys
from pathlib import Path

import pytest

ROOT_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(ROOT_SCRIPTS))
import capacity
SCRIPTS = ROOT_SCRIPTS


class TestEstimateMachineryGone:
    @pytest.mark.parametrize("sym", [
        "record_spend", "pool_used", "pool_alerts", "project_spend", "rotate_spend",
        "_pool_gate", "_read_spend", "CLAUDE_POOL_ROLES", "POOL_5H_LIMIT",
        "POOL_WEEK_LIMIT", "POOL_SOFT_FRAC", "CLAUDE_GATED", "SPEND_FILE",
    ])
    def test_symbol_removed(self, sym):
        assert not hasattr(capacity, sym), f"dead Claude-estimate symbol still present: {sym}"

    def test_no_spend_refs_in_scripts(self):
        for f in ("capacity.py", "watcher.sh", "supervisor_pass.sh", "capacity_loop.sh",
                  "kanban_hub.py", "doctor.py"):
            body = (SCRIPTS / f).read_text()
            # (functional machinery only; "claude-worker" remains a legit agent-role name)
            for bad in ("record_spend", "pool_used", "pool_alerts", "spend.jsonl",
                        "FLEET_PASS_TOKENS", "emit_pool_alerts", "by_project"):
                assert bad not in body, f"{f} still references stripped estimate machinery: {bad}"


class TestRealSignalsKept:
    def test_codex_telemetry_gate_still_works(self, tmp_path, monkeypatch):
        monkeypatch.setattr(capacity, "CAP_DIR", tmp_path / "cap")
        import time
        now = int(time.time())
        capacity._save("codex", {"agent": "codex", "used_5h_pct": 96.0,
                                 "resets_at_5h": now + 3600})
        assert capacity.gate_level("codex") == 2          # telemetry drain still fires

    def test_reactive_bump_drain_still_works(self, tmp_path, monkeypatch):
        monkeypatch.setattr(capacity, "CAP_DIR", tmp_path / "cap")
        r = capacity.bump("kimi")
        assert r["drained_until"] > 0
        assert capacity.gate_level("kimi") == 2           # drained

    def test_fair_slot_floor_kept(self):
        assert hasattr(capacity, "fair_slot_floor")

    def test_claude_gate_is_telemetry_drain_only(self, tmp_path, monkeypatch):
        # with no pool machinery, a fresh claude agent reads healthy (no inert pool gate)
        monkeypatch.setattr(capacity, "CAP_DIR", tmp_path / "cap")
        assert capacity.gate_level("claude") == 0
