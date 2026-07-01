"""P16 item 3 — auth failure should REROUTE to a healthy agent + ALERT the human
(re-login is interactive, human-only), NOT burn 30 retries on the same agent then FAIL
the task (which poisoned the DAG). Plus a generic `fleet_health --emit` so bash can alarm.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(ROOT_SCRIPTS))
SCRIPTS = ROOT_SCRIPTS


class TestFleetHealthEmit:
    def test_emit_writes_alert(self, tmp_path):
        env = dict(os.environ); env["FLEET_HOME"] = str(tmp_path)
        subprocess.run([sys.executable, str(SCRIPTS / "fleet_health.py"),
                        "--emit", "auth_expired", "kimi creds expired"],
                       env=env, capture_output=True)
        line = (tmp_path / "alerts.jsonl").read_text().strip()
        rec = json.loads(line)
        assert rec["type"] == "auth_expired" and "kimi" in rec["detail"]


class TestAuthReroutesNotFails:
    def test_requeue_auth_reroutes_and_alerts_no_fail(self):
        body = (SCRIPTS / "watcher.sh").read_text()
        # locate the requeue_auth function body
        start = body.index("requeue_auth() {")
        fn = body[start:body.index("\n}\n", start)]
        assert "emit_alert auth_expired" in fn, "auth failure must alert the human"
        assert "rerouted_from" in fn, "auth failure must reroute to a healthy agent"
        assert "marking FAILED" not in fn and "MAX_AUTH_RETRIES" not in fn, \
            "auth path must NOT burn retries then FAIL the task"
