"""Tests for derive_phases – run with: pytest .fleet/test_derive_phases.py -q"""
import copy
import json
import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(__file__))
from derive_phases import derive_phases


def _meta(phases):
    return {"title": "Test Pipeline", "phases": list(phases)}


def _write_json(directory, filename, data):
    with open(os.path.join(directory, filename), 'w') as f:
        json.dump(data, f)


def test_pure_no_mutation():
    """derive_phases must NOT mutate the input meta dict."""
    original = _meta([
        {"id": "P0", "name": "Setup", "status": "pending",
         "done_when": {"type": "file_exists", "source": "setup.txt"}}
    ])
    snapshot = copy.deepcopy(original)
    with tempfile.TemporaryDirectory() as root:
        derive_phases(original, root, proc_alive=lambda m: False)
    assert original == snapshot


def test_missing_source_unchanged():
    """FAIL-SAFE: missing / unreadable / invalid source leaves status unchanged."""
    meta = _meta([
        {"id": "P0", "name": "Setup", "status": "pending",
         "done_when": {"type": "count", "source": "no_such_file.json",
                        "path": "items", "op": ">=", "value": 5}}
    ])
    with tempfile.TemporaryDirectory() as root:
        result = derive_phases(meta, root, proc_alive=lambda m: False)
    assert result["phases"][0]["status"] == "pending"


def test_no_predicate_untouched():
    """Phase with no done_when / active_when is left unchanged."""
    meta = _meta([
        {"id": "P0", "name": "Setup", "status": "active"},
        {"id": "P1", "name": "Build", "status": "pending"},
    ])
    with tempfile.TemporaryDirectory() as root:
        result = derive_phases(meta, root, proc_alive=lambda m: False)
    assert result["phases"][0]["status"] == "active"
    assert result["phases"][1]["status"] == "pending"


def test_count_done():
    """count predicate meeting threshold -> status 'done'."""
    meta = _meta([
        {"id": "P0", "name": "Setup", "status": "pending",
         "done_when": {"type": "count", "source": "data.json",
                        "path": "items", "op": ">=", "value": 3}}
    ])
    with tempfile.TemporaryDirectory() as root:
        _write_json(root, "data.json", {"items": [1, 2, 3]})
        result = derive_phases(meta, root, proc_alive=lambda m: False)
    assert result["phases"][0]["status"] == "done"


def test_count_active_partial():
    """count with partial progress (count>0 but below threshold) -> 'active'."""
    meta = _meta([
        {"id": "P0", "name": "Setup", "status": "pending",
         "done_when": {"type": "count", "source": "data.json",
                        "path": "items", "op": ">=", "value": 10}}
    ])
    with tempfile.TemporaryDirectory() as root:
        _write_json(root, "data.json", {"items": [1, 2, 3]})
        result = derive_phases(meta, root, proc_alive=lambda m: False)
    assert result["phases"][0]["status"] == "active"


def test_process_alive_predicate():
    """process_alive predicate -> status 'active' when process found."""
    meta = _meta([
        {"id": "P0", "name": "Server", "status": "pending",
         "active_when": {"type": "process_alive", "match": "my_server"}}
    ])
    with tempfile.TemporaryDirectory() as root:
        result = derive_phases(
            meta, root, proc_alive=lambda m: m == "my_server")
    assert result["phases"][0]["status"] == "active"


def test_file_exists_predicate():
    """file_exists predicate -> status 'done' when file exists and non-empty."""
    meta = _meta([
        {"id": "P0", "name": "Build", "status": "pending",
         "done_when": {"type": "file_exists", "source": "output.txt"}}
    ])
    with tempfile.TemporaryDirectory() as root:
        with open(os.path.join(root, "output.txt"), 'w') as f:
            f.write("done")
        result = derive_phases(meta, root, proc_alive=lambda m: False)
    assert result["phases"][0]["status"] == "done"


def test_idempotent():
    """Running derive_phases twice produces identical output.
    Predicate fields (done_when, gate_template) preserved verbatim.
    Title preserved.
    """
    meta = _meta([
        {"id": "P0", "name": "Experiments", "status": "pending",
         "done_when": {"type": "count", "source": "data.json",
                        "path": "results", "op": ">=", "value": 2},
         "gate_template": "completed {count}/{value} tasks"}
    ])
    with tempfile.TemporaryDirectory() as root:
        _write_json(root, "data.json", {"results": [1, 2, 3]})
        result1 = derive_phases(meta, root, proc_alive=lambda m: False)
        result2 = derive_phases(result1, root, proc_alive=lambda m: False)
    assert result1["title"] == "Test Pipeline"
    assert result1["phases"][0]["done_when"] == meta["phases"][0]["done_when"]
    assert result1["phases"][0]["gate_template"] == meta["phases"][0]["gate_template"]
    assert result1["phases"][0]["status"] == "done"
    assert result1["phases"][0]["gate"] == "completed 3/2 tasks"
    assert result1 == result2


def test_gate_template_missing_keys_default_zero():
    """Missing per-strength keys in gate_template default to 0."""
    meta = _meta([
        {"id": "P1", "name": "Seeds", "status": "pending",
         "done_when": {"type": "count", "source": "seeds.json",
                        "path": "per_strength_seed", "op": ">=", "value": 15},
         "gate_template": "{count}/{value} (mild {mild}, moderate {moderate}, aggressive {aggressive})"}
    ])
    with tempfile.TemporaryDirectory() as root:
        _write_json(root, "seeds.json",
                    {"per_strength_seed": {"mild": [1, 2, 3, 4, 5]}})
        result = derive_phases(meta, root, proc_alive=lambda m: False)
    gate = result["phases"][0]["gate"]
    assert gate == "5/15 (mild 5, moderate 0, aggressive 0)"


def test_evaluative_no_judge_file_not_done():
    """Judgment gate with NO judge-produced file -> NEVER done, NEVER auto-active.
    A judgment phase must not flip just because work happened; it needs a real eval."""
    meta = _meta([
        {"id": "QG", "name": "Quality gate", "status": "pending",
         "done_when": {"type": "evaluative", "source": "qg_result.json",
                        "path": "score", "op": ">=", "value": 75}}
    ])
    with tempfile.TemporaryDirectory() as root:
        result = derive_phases(meta, root, proc_alive=lambda m: False)
    assert result["phases"][0]["status"] == "pending"


def test_evaluative_passing_score_done():
    """Judge wrote a passing score -> done (a real evaluation occurred)."""
    meta = _meta([
        {"id": "QG", "name": "Quality gate", "status": "active",
         "done_when": {"type": "evaluative", "source": "qg_result.json",
                        "path": "score", "op": ">=", "value": 75}}
    ])
    with tempfile.TemporaryDirectory() as root:
        _write_json(root, "qg_result.json", {"score": 82})
        result = derive_phases(meta, root, proc_alive=lambda m: False)
    assert result["phases"][0]["status"] == "done"


def test_evaluative_failing_score_not_done_not_active():
    """Judge ran but score below threshold -> NOT done, and NOT auto-bumped to active
    (the count>0 partial-progress heuristic must not apply to judgment gates)."""
    meta = _meta([
        {"id": "QG", "name": "Quality gate", "status": "pending",
         "done_when": {"type": "evaluative", "source": "qg_result.json",
                        "path": "score", "op": ">=", "value": 75}}
    ])
    with tempfile.TemporaryDirectory() as root:
        _write_json(root, "qg_result.json", {"score": 60})
        result = derive_phases(meta, root, proc_alive=lambda m: False)
    assert result["phases"][0]["status"] == "pending"
