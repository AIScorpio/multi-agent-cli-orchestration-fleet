"""Phase 5.1 — generic phase-manifest module (fleet-agnostic).

Init writes an `awaiting_definition` stub; the mission-aware leader fills it (→ `defined`) or
marks a flat project `no_pipeline`. Gates the integration entry points (init_manifest /
set_phases / mark_no_pipeline / effective_state), incl. idempotent no-clobber + back-compat.
"""
import json
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import phases  # noqa: E402  (RED until module exists)


def test_init_writes_awaiting_stub(tmp_path):
    (tmp_path / ".fleet").mkdir()
    p = phases.init_manifest(tmp_path)
    assert p is not None
    assert json.loads(p.read_text())["state"] == "awaiting_definition"


def test_init_idempotent_no_clobber(tmp_path):
    (tmp_path / ".fleet").mkdir()
    phases.init_manifest(tmp_path)
    phases.set_phases(tmp_path, [{"id": "P1", "name": "x"}], title="T")
    assert phases.init_manifest(tmp_path) is None, "re-init must NOT clobber a filled manifest"
    m = phases.load(tmp_path)
    assert m["state"] == "defined" and len(m["phases"]) == 1


def test_set_phases_transitions_to_defined(tmp_path):
    (tmp_path / ".fleet").mkdir()
    phases.init_manifest(tmp_path)
    m = phases.set_phases(tmp_path, [{"id": "P1", "name": "Lit"}, {"id": "P2", "name": "Method"}],
                          title="Proj")
    assert m["state"] == "defined" and m["title"] == "Proj" and len(m["phases"]) == 2
    assert phases.effective_state(phases.load(tmp_path)) == "defined"


def test_set_phases_validates(tmp_path):
    (tmp_path / ".fleet").mkdir()
    with pytest.raises(ValueError):
        phases.set_phases(tmp_path, [{"id": "P1"}])   # missing name


def test_mark_no_pipeline(tmp_path):
    (tmp_path / ".fleet").mkdir()
    phases.init_manifest(tmp_path)
    phases.mark_no_pipeline(tmp_path)
    assert phases.effective_state(phases.load(tmp_path)) == "no_pipeline"


def test_effective_state_backcompat():
    # 04/01 predate the `state` field — phases present → defined
    assert phases.effective_state({"title": "T", "phases": [{"id": "a", "name": "b"}]}) == "defined"
    assert phases.effective_state({}) == "awaiting_definition"
    assert phases.effective_state({"phases": []}) == "awaiting_definition"
    assert phases.effective_state({"state": "no_pipeline", "phases": []}) == "no_pipeline"


def test_validate_empty_stub_ok_and_bad_shape_rejected():
    ok, _ = phases.validate(phases.STUB)
    assert ok, "the empty awaiting_definition stub must validate"
    ok, errs = phases.validate({"phases": [{"id": "", "name": ""}]})
    assert not ok and errs
