import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from kanban_hub import (
    _eval_done_when,
    _process_alive,
    derive_phase_statuses,
)


@pytest.fixture
def tmp_root(tmp_path):
    return tmp_path


def _phase(**kw):
    defaults = {"id": "P0", "name": "test", "status": "pending"}
    defaults.update(kw)
    return defaults


class TestEvalDoneWhen:
    def test_none_returns_false(self, tmp_root):
        assert _eval_done_when(None, tmp_root) is False

    def test_count_gte(self, tmp_root):
        src = tmp_root / "data.json"
        src.write_text(json.dumps({"rows": [1, 2, 3]}))
        dw = {"type": "count", "source": "data.json", "path": "rows",
              "op": ">=", "value": 3}
        assert _eval_done_when(dw, tmp_root) is True

    def test_count_lt(self, tmp_root):
        src = tmp_root / "data.json"
        src.write_text(json.dumps({"rows": [1, 2]}))
        dw = {"type": "count", "source": "data.json", "path": "rows",
              "op": ">=", "value": 3}
        assert _eval_done_when(dw, tmp_root) is False

    def test_count_ops(self, tmp_root):
        src = tmp_root / "data.json"
        src.write_text(json.dumps({"x": [1]}))
        for op, val, expect in [
            (">", 0, True), (">", 1, False),
            ("==", 1, True), ("==", 2, False),
            ("<=", 1, True), ("<=", 0, False),
            ("<", 2, True), ("<", 1, False),
        ]:
            dw = {"type": "count", "source": "data.json", "path": "x",
                  "op": op, "value": val}
            assert _eval_done_when(dw, tmp_root) is expect, f"{op} {val}"

    def test_count_nested_dict_of_lists(self, tmp_root):
        # per_strength_seed = {strength: [seed, ...]} must count items ACROSS all
        # lists (3x5=15), not the number of strength keys (3). This is the bug
        # that left P2 stuck "pending" after the 15-cell sweep finished.
        src = tmp_root / "data.json"
        src.write_text(json.dumps({"pss": {"mild": [1, 2, 3, 4, 5],
                                           "moderate": [1, 2, 3, 4, 5],
                                           "aggressive": [1, 2, 3, 4, 5]}}))
        dw = {"type": "count", "source": "data.json", "path": "pss",
              "op": ">=", "value": 15}
        assert _eval_done_when(dw, tmp_root) is True

    def test_count_nested_partial_below_gate(self, tmp_root):
        # mid-run: only 3 cells dumped across the strength lists -> below 15.
        src = tmp_root / "data.json"
        src.write_text(json.dumps({"pss": {"mild": [1, 2], "moderate": [1]}}))
        dw = {"type": "count", "source": "data.json", "path": "pss",
              "op": ">=", "value": 15}
        assert _eval_done_when(dw, tmp_root) is False

    def test_count_dict_of_nonlists_counts_keys(self, tmp_root):
        # a plain dict whose values are NOT lists still counts keys (back-compat).
        src = tmp_root / "data.json"
        src.write_text(json.dumps({"m": {"a": 1, "b": 2, "c": 3}}))
        dw = {"type": "count", "source": "data.json", "path": "m",
              "op": ">=", "value": 3}
        assert _eval_done_when(dw, tmp_root) is True

    def test_file_exists_true(self, tmp_root):
        (tmp_root / "marker.txt").write_text("ok")
        dw = {"type": "file_exists", "source": "marker.txt"}
        assert _eval_done_when(dw, tmp_root) is True

    def test_file_exists_false(self, tmp_root):
        dw = {"type": "file_exists", "source": "nosuch.txt"}
        assert _eval_done_when(dw, tmp_root) is False

    def test_bad_json_returns_false(self, tmp_root):
        src = tmp_root / "bad.json"
        src.write_text("NOT JSON")
        dw = {"type": "count", "source": "bad.json", "path": "x",
              "op": ">=", "value": 0}
        assert _eval_done_when(dw, tmp_root) is False

    def test_missing_source_returns_false(self, tmp_root):
        dw = {"type": "count", "source": "missing.json", "path": "x",
              "op": ">=", "value": 0}
        assert _eval_done_when(dw, tmp_root) is False


class TestProcessAlive:
    @patch("kanban_hub.subprocess.run")
    def test_alive(self, mock_run):
        # P8: _process_alive uses `pgrep -fl` (reads cmdlines) so it can scope by
        # project_root; with no project_root a returncode-0 match is alive.
        mock_run.return_value.returncode = 0
        mock_run.return_value.stdout = "123 myproc --flag"
        assert _process_alive("myproc") is True
        mock_run.assert_called_once_with(
            ["pgrep", "-fl", "myproc"], capture_output=True, text=True, timeout=5
        )

    @patch("kanban_hub.subprocess.run")
    def test_dead(self, mock_run):
        mock_run.return_value.returncode = 1
        assert _process_alive("myproc") is False

    @patch("kanban_hub.subprocess.run", side_effect=Exception("boom"))
    def test_exception_returns_false(self, mock_run):
        assert _process_alive("myproc") is False


class TestStaleActiveDowngrade:
    def test_manual_active_no_live_signal_downgrades_to_pending(self):
        phases = [_phase(id="P2", status="active")]
        derive_phase_statuses(phases, [], Path("/nonexistent"))
        assert phases[0]["status"] == "pending"

    @patch("kanban_hub._process_alive", return_value=True)
    def test_manual_active_with_live_process_stays_active(self, _mock):
        phases = [_phase(id="P2", status="active", active_when={
            "type": "process_alive", "match": "run_sweep"
        })]
        derive_phase_statuses(phases, [], Path("/nonexistent"))
        assert phases[0]["status"] == "active"

    def test_manual_active_with_claimed_task_stays_active(self):
        phases = [_phase(id="P2", status="active")]
        claimed = [{"task_id": "t1", "phase": "P2"}]
        derive_phase_statuses(phases, claimed, Path("/nonexistent"))
        assert phases[0]["status"] == "active"


class TestDependsOn:
    def test_blocked_when_dep_not_done(self):
        phases = [
            _phase(id="A", status="done"),
            _phase(id="B", status="pending", depends_on=["A", "C"]),
            _phase(id="C", status="pending"),
        ]
        derive_phase_statuses(phases, [], Path("/nonexistent"))
        assert phases[0]["status"] == "done"
        assert phases[1]["status"] == "blocked"
        assert phases[2]["status"] == "pending"

    def test_not_blocked_when_all_deps_done(self):
        phases = [
            _phase(id="A", status="done"),
            _phase(id="B", status="pending", depends_on=["A"]),
        ]
        derive_phase_statuses(phases, [], Path("/nonexistent"))
        assert phases[1]["status"] == "pending"

    def test_empty_depends_not_blocked(self):
        phases = [_phase(id="X", status="pending", depends_on=[])]
        derive_phase_statuses(phases, [], Path("/nonexistent"))
        assert phases[0]["status"] == "pending"


class TestPrecedence:
    def test_done_takes_priority(self, tmp_root):
        src = tmp_root / "data.json"
        src.write_text(json.dumps({"r": [1, 2, 3]}))
        phases = [_phase(id="P0", status="done", done_when={
            "type": "count", "source": "data.json", "path": "r",
            "op": ">=", "value": 3
        })]
        derive_phase_statuses(phases, [], tmp_root)
        assert phases[0]["status"] == "done"

    def test_manual_done_trusted(self):
        phases = [_phase(id="P0", status="done")]
        derive_phase_statuses(phases, [], Path("/nonexistent"))
        assert phases[0]["status"] == "done"


class TestBackwardCompat:
    def test_manual_pending_unchanged(self):
        phases = [_phase(id="P0", status="pending")]
        derive_phase_statuses(phases, [], Path("/nonexistent"))
        assert phases[0]["status"] == "pending"

    def test_manual_blocked_unchanged(self):
        phases = [_phase(id="P0", status="blocked")]
        derive_phase_statuses(phases, [], Path("/nonexistent"))
        assert phases[0]["status"] == "blocked"

    def test_manual_done_unchanged(self):
        phases = [_phase(id="P0", status="done")]
        derive_phase_statuses(phases, [], Path("/nonexistent"))
        assert phases[0]["status"] == "done"
