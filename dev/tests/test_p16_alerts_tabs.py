"""P16 item 4 — alerts are dedup'd and rendered on EVERY project tab (not just Overview),
so a per-project view isn't alert-blind. Pure in-hub (pull); no external channel needed.
"""
import sys
from pathlib import Path

import pytest

ROOT_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
SCRIPTS = ROOT_SCRIPTS


class TestHubAlertsTabsDedup:
    def test_dedup_and_per_tab_render_present(self):
        body = (SCRIPTS / "kanban_hub.py").read_text()
        assert "function dedupAlerts" in body, "no alert dedup"
        assert "function renderAlerts" in body, "no reusable alert renderer"
        # rendered on the project tab path, scoped to the current project
        assert "renderAlerts(document.getElementById(\"progress\")" in body, \
            "alerts not rendered on the per-project tab"
        # Overview still renders all alerts
        assert "renderAlerts(host, d.alerts, null)" in body

    def test_hub_module_imports_clean(self):
        # the file must still import/exec (the PAGE string mustn't break Python parsing)
        import importlib.util
        spec = importlib.util.spec_from_file_location("kanban_hub_p16", SCRIPTS / "kanban_hub.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        assert hasattr(mod, "collect_overview")
