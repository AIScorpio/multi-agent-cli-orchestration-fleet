"""P2 SELF-SUPERVISION + AUDIT gates (verifier-first — written BEFORE the fix).

RED until P2 lands. Defines the machine-checkable contract; the system-integration
parts that pytest CANNOT cover (real launchd bootstrap, real OS-notification
delivery, reboot survival) are documented as empirical gate-zero / inspection steps
in the /goal prompt, NOT here.

Contract the implementation must deliver (do NOT weaken these tests):

  ledger.py (new) — append-only audit trail (closes the "audit-trail-free blackout")
    · append(ma, etype, **fields) -> None : append ONE json line to
      ma/status/events.jsonl via O_APPEND (atomic for small lines); every line has
      'ts' and 'type'. read(ma) -> list[dict].
    · orchestrator qa-pass/qa-fail and doctor resolver-release EMIT ledger events.

  fleet_health.py (new) — no-LLM liveness pinger
    · check_health(fleet_home, projects=None, free_bytes=None) -> list[alert]
      detects: dead global singleton (hub.pid / capacity_loop.pid holder dead),
      dead per-project caretaker, disk pressure (free_bytes below threshold).
      alert = {"type": ..., "detail": ...}.
    · emit_alerts(fleet_home, alerts) -> int : append to fleet_home/alerts.jsonl
      (+ best-effort OS notification via osascript; fail-open). A leader pass reads
      alerts.jsonl and PushNotifications (scripts can't call that tool directly).

  timeout wraps
    · supervisor_pass.sh wraps the leader `claude -p` in `timeout`.
    · caretaker.sh wraps `doctor.py` in `timeout`.

  install_supervisord.sh (new, global) — TCC-SAFE whole-stack KeepAlive
    · generates a launchd plist whose executable is OUTSIDE TCC-protected trees
      (~/Documents, ~/Desktop, ~/Downloads), KeepAlive=true, that supervises every
      registered project's stack. (--no-load + LAUNCH_AGENTS_DIR for tests.)
"""
import importlib
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"


def _mod(name):
    try:
        return importlib.import_module(name)
    except Exception as e:
        pytest.fail(f"P2 not implemented: module {name}.py missing ({e})")


# ── 1. event ledger ───────────────────────────────────────────────────────────

class TestEventLedger:
    def _ma(self, tmp_path):
        ma = tmp_path / ".fleet"
        (ma / "status").mkdir(parents=True)
        return ma

    def test_append_and_read(self, tmp_path):
        L = _mod("ledger")
        ma = self._ma(tmp_path)
        L.append(ma, "claim", task_id="t1", agent="kimi")
        L.append(ma, "complete", task_id="t1")
        L.append(ma, "qa-pass", task_id="t1")
        rows = L.read(ma)
        assert len(rows) == 3
        assert [r["type"] for r in rows] == ["claim", "complete", "qa-pass"]

    def test_every_line_has_ts_and_type(self, tmp_path):
        L = _mod("ledger")
        ma = self._ma(tmp_path)
        L.append(ma, "drain", agent="codex")
        line = (ma / "status" / "events.jsonl").read_text().strip().splitlines()[-1]
        d = json.loads(line)                       # valid JSON
        assert "ts" in d and d["type"] == "drain"

    def test_concurrent_appends_not_corrupted(self, tmp_path):
        L = _mod("ledger")
        ma = self._ma(tmp_path)
        N = 30
        bar = threading.Barrier(N)

        def w(i):
            bar.wait()
            L.append(ma, "reroute", i=i)
        ts = [threading.Thread(target=w, args=(i,)) for i in range(N)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        lines = (ma / "status" / "events.jsonl").read_text().strip().splitlines()
        assert len(lines) == N
        for ln in lines:                           # no interleaved/garbled line
            json.loads(ln)


class TestLedgerWiring:
    def _orch(self, tmp_path, monkeypatch):
        orch = _mod("orchestrator")
        ma = tmp_path / ".fleet"
        for d in ("queue/completed", "queue/pending", "queue/drafts", "status"):
            (ma / d).mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(orch, "MA", ma)
        monkeypatch.setattr(orch, "QUEUE", ma / "queue")
        return orch, ma

    def test_qa_pass_emits_ledger_event(self, tmp_path, monkeypatch):
        orch, ma = self._orch(tmp_path, monkeypatch)
        import argparse
        (ma / "queue" / "completed" / "t1.json").write_text(json.dumps(
            {"task_id": "t1", "title": "t", "phase": "1", "type": "code",
             "description": "d", "assigned_to": "any", "output_file": "o.txt",
             "acceptance_criteria": ["c"]}))
        (ma / "queue" / "completed" / "t1.result.json").write_text(json.dumps(
            {"task_id": "t1", "status": "COMPLETED"}))
        orch.cmd_qa_pass(argparse.Namespace(task_id="t1"))
        events = ma / "status" / "events.jsonl"
        assert events.exists(), "qa-pass emitted no ledger event"
        types = [json.loads(l)["type"] for l in events.read_text().splitlines() if l.strip()]
        assert any("qa" in t for t in types)


# ── 2. liveness pinger ────────────────────────────────────────────────────────

class TestHealthCheck:
    def test_dead_singleton_alerts(self, tmp_path):
        H = _mod("fleet_health")
        fh = tmp_path / "fleet_home"
        fh.mkdir()
        (fh / "hub.pid").write_text("999999")          # dead holder
        alerts = H.check_health(fh, projects=[], free_bytes=10**12)
        assert any(a["type"] == "singleton_dead" for a in alerts)

    def test_healthy_singleton_no_alert(self, tmp_path):
        H = _mod("fleet_health")
        fh = tmp_path / "fleet_home"
        fh.mkdir()
        (fh / "hub.pid").write_text(str(os.getpid()))  # alive (this process)
        (fh / "capacity_loop.pid").write_text(str(os.getpid()))
        alerts = H.check_health(fh, projects=[], free_bytes=10**12)
        assert not any(a["type"] == "singleton_dead" for a in alerts)

    def test_disk_pressure_alerts(self, tmp_path):
        H = _mod("fleet_health")
        fh = tmp_path / "fleet_home"
        fh.mkdir()
        (fh / "hub.pid").write_text(str(os.getpid()))
        (fh / "capacity_loop.pid").write_text(str(os.getpid()))
        alerts = H.check_health(fh, projects=[], free_bytes=1024)   # ~1KB free
        assert any(a["type"] == "disk_pressure" for a in alerts)

    def test_emit_alerts_appends(self, tmp_path):
        H = _mod("fleet_health")
        fh = tmp_path / "fleet_home"
        fh.mkdir()
        n = H.emit_alerts(fh, [{"type": "x", "detail": "y"}, {"type": "z", "detail": "w"}])
        assert n == 2
        lines = (fh / "alerts.jsonl").read_text().strip().splitlines()
        assert len(lines) == 2 and all(json.loads(l).get("type") for l in lines)


# ── 3. timeout wraps ──────────────────────────────────────────────────────────

class TestTimeoutWraps:
    def test_supervisor_pass_wraps_claude(self):
        body = (SCRIPTS / "supervisor_pass.sh").read_text()
        lines = [l for l in body.splitlines()
                 if "-p" in l and ("claude" in l.lower() or "CLAUDE_BIN" in l)
                 and not l.strip().startswith("#")]
        assert lines, "no claude -p invocation found"
        assert any("timeout" in l for l in lines), "leader claude -p not wrapped in timeout"

    def test_caretaker_wraps_doctor(self):
        body = (SCRIPTS / "caretaker.sh").read_text()
        lines = [l for l in body.splitlines()
                 if "doctor.py" in l and not l.strip().startswith("#")]
        assert lines and any("timeout" in l for l in lines), "doctor.py not wrapped in timeout"


# ── 4. TCC-safe whole-stack launchd supervisord ───────────────────────────────

class TestSupervisordPlist:
    TCC = ("/Documents/", "/Desktop/", "/Downloads/")

    def _run(self, agents_dir):
        script = SCRIPTS / "install_supervisord.sh"
        if not script.exists():
            pytest.fail("P2 not implemented: install_supervisord.sh missing")
        env = dict(os.environ, LAUNCH_AGENTS_DIR=str(agents_dir))
        return subprocess.run(["bash", str(script), "--no-load"],
                              capture_output=True, text=True, env=env, timeout=15)

    def test_plist_lints_and_keepalive(self, tmp_path):
        agents = tmp_path / "agents"
        r = self._run(agents)
        assert r.returncode == 0, r.stderr
        plists = list(agents.glob("*.plist"))
        assert len(plists) == 1
        body = plists[0].read_text()
        assert "<key>KeepAlive</key>" in body and "<true/>" in body
        lint = subprocess.run(["plutil", "-lint", str(plists[0])],
                              capture_output=True, text=True)
        assert lint.returncode == 0, f"plist failed plutil -lint: {lint.stdout}{lint.stderr}"

    def test_executable_outside_tcc_tree(self, tmp_path):
        agents = tmp_path / "agents"
        self._run(agents)
        body = next((tmp_path / "agents").glob("*.plist")).read_text()
        import re
        progs = re.findall(r"<string>(/[^<]+)</string>", body)
        # the script/executable the agent runs must NOT live in a TCC-protected tree
        runnable = [p for p in progs if p.endswith(".sh") or "/bin/" in p or p.endswith("supervisord")]
        assert runnable, "no runnable program path found in plist"
        for p in runnable:
            assert not any(t in p for t in self.TCC), \
                f"launchd would run a script in a TCC-protected tree: {p}"
