"""Tests for detach_run._parse (the pure arg-splitting logic).

The fork/setsid path (_daemonize_and_exec) is not unit-tested — it replaces the
process image — but the arg parsing that decides WHAT gets exec'd is, so a typo in
the launcher contract is caught.
"""
import importlib.util
import os

import pytest

_HERE = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), "scripts")
_spec = importlib.util.spec_from_file_location("detach_run", os.path.join(_HERE, "detach_run.py"))
detach_run = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(detach_run)


def test_parse_basic_command_after_double_dash():
    # P14: _parse now returns (ns, cmd); log/cwd are ns attributes (register opts added).
    ns, cmd = detach_run._parse(["--log", "/tmp/x.log", "--", "python", "job.py"])
    assert ns.log == "/tmp/x.log"
    assert ns.cwd is None
    assert cmd == ["python", "job.py"]


def test_parse_with_cwd():
    ns, cmd = detach_run._parse(
        ["--log", "/tmp/x.log", "--cwd", "/work", "--", "/venv/bin/python", "run.py", "--seeds", "42"]
    )
    assert ns.cwd == "/work"
    assert cmd == ["/venv/bin/python", "run.py", "--seeds", "42"]


def test_parse_child_flags_not_consumed_by_launcher():
    # the child's own --log must reach the child, not be swallowed by the launcher
    _, cmd = detach_run._parse(["--log", "/tmp/o.log", "--", "tool", "--log", "child.log", "--cwd", "z"])
    assert cmd == ["tool", "--log", "child.log", "--cwd", "z"]


def test_parse_missing_command_errors():
    with pytest.raises(SystemExit):
        detach_run._parse(["--log", "/tmp/x.log"])
    with pytest.raises(SystemExit):
        detach_run._parse(["--log", "/tmp/x.log", "--"])


def test_parse_missing_log_errors():
    with pytest.raises(SystemExit):
        detach_run._parse(["--", "python", "job.py"])
