"""Phase 5.2 — init scaffolds the mission-agnostic `awaiting_definition` stub.

Guarantees every project has a manifest (kanban/deriver never face a missing file), while the
mission-aware leader still owns the content. Idempotent — re-init must NOT clobber a filled
manifest. Gates the integration entry point (init_workspace), not a helper.
"""
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import init_workspace  # noqa: E402
import phases          # noqa: E402


def test_phases_py_registered_as_runtime():
    assert "phases.py" in init_workspace.RUNTIME_SCRIPTS, "phases.py must deploy to every .fleet/"


def test_init_scaffolds_awaiting_stub(tmp_path):
    init_workspace.init_workspace(tmp_path, force=False, perms=False)
    assert phases.effective_state(phases.load(tmp_path)) == "awaiting_definition"
    assert (tmp_path / ".fleet" / "phases.py").exists()


def test_reinit_does_not_clobber_filled_manifest(tmp_path):
    init_workspace.init_workspace(tmp_path, force=False, perms=False)
    phases.set_phases(tmp_path, [{"id": "P1", "name": "Lit"}], title="T")
    init_workspace.init_workspace(tmp_path, force=False, perms=False)   # re-run
    m = phases.load(tmp_path)
    assert phases.effective_state(m) == "defined" and len(m["phases"]) == 1, \
        "re-init must preserve the leader's filled manifest"
