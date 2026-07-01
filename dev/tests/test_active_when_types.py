"""active_when symmetry: derive_phase_statuses must light a phase 'active' for active_when of type
file_exists / count (not only process_alive), and WARN — never silently drop — an unsupported type.
This was a real footgun: a file_exists active_when silently failed with no error, so a phase that
should have shown active stayed 'pending' on the board with no signal as to why."""
import sys
from pathlib import Path

ROOT_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(ROOT_SCRIPTS))
import kanban_hub


def test_file_exists_active_when_lights_phase(tmp_path):
    (tmp_path / "marker.txt").write_text("x")
    phases = [{"id": "P1", "active_when": {"type": "file_exists", "source": "marker.txt"}}]
    kanban_hub.derive_phase_statuses(phases, [], tmp_path)
    assert phases[0]["status"] == "active"


def test_file_exists_active_when_absent_stays_pending(tmp_path):
    phases = [{"id": "P1", "status": "pending",
               "active_when": {"type": "file_exists", "source": "nope.txt"}}]
    kanban_hub.derive_phase_statuses(phases, [], tmp_path)
    assert phases[0]["status"] == "pending"


def test_count_active_when_lights_phase(tmp_path):
    (tmp_path / "m.json").write_text('{"items": [1, 2, 3]}')
    phases = [{"id": "P1", "active_when":
               {"type": "count", "source": "m.json", "path": "items", "op": ">=", "value": 2}}]
    kanban_hub.derive_phase_statuses(phases, [], tmp_path)
    assert phases[0]["status"] == "active"


def test_process_alive_active_when_still_works(tmp_path):
    # the original supported type must still fire (no match alive -> not active)
    phases = [{"id": "P1", "status": "pending",
               "active_when": {"type": "process_alive", "match": "no_such_proc_xyz123"}}]
    kanban_hub.derive_phase_statuses(phases, [], tmp_path)
    assert phases[0]["status"] == "pending"


def test_unsupported_active_when_warns_not_silent(tmp_path, capsys):
    kanban_hub._WARNED_AW.clear()
    phases = [{"id": "P9", "status": "pending", "active_when": {"type": "bogus_type"}}]
    kanban_hub.derive_phase_statuses(phases, [], tmp_path)
    assert phases[0]["status"] == "pending"               # did not fire
    assert "bogus_type" in capsys.readouterr().err        # but WARNED (not silently dropped)
