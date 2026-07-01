"""P16 item 1 — fix the fair_slot bug I shipped (and that violates the skill's OWN
no-hardcode rule): the floor must (a) NEVER be 0 (no permanent starvation under
oversubscription), (b) derive total slots from the agent's REAL cap
(agents/<agent>.json global_max_concurrent), not a magic 4, and (c) count only LIVE
projects in the denominator (registry liveness), not crashed/forgotten ones.
"""
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(ROOT_SCRIPTS))
import capacity
import registry
SCRIPTS = ROOT_SCRIPTS


class TestFloorNeverZero:
    def test_oversubscribed_floor_is_at_least_one(self):
        # 3 live projects, real cap 2 → nobody may be permanently starved (floor>=1)
        f = capacity.fair_slot_floor(["a", "b", "c"], 2)
        assert all(v >= 1 for v in f.values()), f"floor 0 starves a project: {f}"

    def test_normal_share_preserved(self):
        assert capacity.fair_slot_floor(["a"], 2) == {"a": 2}
        assert capacity.fair_slot_floor(["a", "b"], 2) == {"a": 1, "b": 1}


class TestRegistryLiveness:
    def test_touch_then_live(self, tmp_path, monkeypatch):
        monkeypatch.setattr(registry, "FLEET_HOME", tmp_path)
        monkeypatch.setattr(registry, "REG", tmp_path / "projects.json")
        monkeypatch.setattr(registry, "LOCK", tmp_path / "projects.json.lock")
        registry.add(str(tmp_path / "projA"), None)
        pid = registry.project_id(str(tmp_path / "projA"))
        registry.touch(str(tmp_path / "projA"), now=1000)
        assert pid in registry.live_projects(now=1000, max_age=300)
        assert pid not in registry.live_projects(now=1000 + 9999, max_age=300), \
            "a project not touched within max_age must drop out of the live set"


class TestCliDerivesRealCap:
    def test_fair_slot_uses_agent_cap_not_magic_4(self, tmp_path, monkeypatch):
        fh = tmp_path / "fh"
        proj = tmp_path / "proj"
        (proj / ".fleet" / "agents").mkdir(parents=True)
        (proj / ".fleet" / "agents" / "codex.json").write_text(
            json.dumps({"global_max_concurrent": 2}))
        import os as _os
        env = dict(_os.environ)
        env["FLEET_HOME"] = str(fh)
        # register this single project so it's the only live one in the denominator
        subprocess.run([sys.executable, str(SCRIPTS / "registry.py"), "add",
                        "--root", str(proj)], env=env, capture_output=True)
        pid = registry.project_id(str(proj))
        out = subprocess.run([sys.executable, str(SCRIPTS / "capacity.py"),
                              "fair_slot_floor", pid, "--agent", "codex",
                              "--agents-dir", str(proj / ".fleet" / "agents")],
                             env=env, capture_output=True, text=True)
        assert out.stdout.strip() == "2", \
            f"fair_slot must derive the codex cap (2), not magic 4; got {out.stdout!r}"


class TestWatcherPassesAgent:
    def test_watcher_calls_fair_slot_with_agent(self):
        body = (SCRIPTS / "watcher.sh").read_text()
        assert 'fair_slot_floor "$PROJECT_ID" --agent "$AGENT"' in body, \
            "watcher must pass --agent on the fair_slot_floor call (its real cap)"


class TestCaretakerTouchesRegistry:
    def test_doctor_tick_touches_registry(self):
        body = (SCRIPTS / "doctor.py").read_text()
        assert "registry.touch" in body, \
            "caretaker tick must touch the registry so live_projects stays fresh"
