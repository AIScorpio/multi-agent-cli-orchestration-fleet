"""Q2 — the kanban phase pipeline is COLLAPSIBLE so a long pipeline (e.g. 14 phases) doesn't
push the task board below the fold. Collapsed by default when >6 phases; collapsed header
summarizes the ACTIVE phase + progress; state persists per-project in localStorage.

JS UX can't run under pytest, so this asserts the logic is PRESENT in the served PAGE and that
the hub module still imports clean (the PAGE string mustn't break Python parsing)."""
import importlib.util
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"


def test_collapse_logic_present():
    body = (SCRIPTS / "kanban_hub.py").read_text()
    assert ".phasetoggle" in body, "no clickable collapse-toggle style"
    assert 'localStorage.getItem("fleetPhases:" + CURRENT)' in body or 'fleetPhases:' in body, \
        "collapse state not persisted per-project in localStorage"
    assert "total > 6" in body, "missing the auto-collapse-when-many-phases default"
    # collapsed summary derives from the ACTIVE phase + done/total progress
    assert 'p.status === "active"' in body and 'p.status === "done"' in body, \
        "collapsed summary must derive from phase statuses (active + done)"
    assert "renderProgress(d)" in body, "toggle must re-render so the click gives instant feedback"


def test_hub_module_imports_clean():
    spec = importlib.util.spec_from_file_location("kanban_hub_q2", SCRIPTS / "kanban_hub.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert hasattr(mod, "collect_overview")
