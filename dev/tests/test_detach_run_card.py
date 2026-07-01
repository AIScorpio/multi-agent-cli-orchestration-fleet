"""Gate 2 (verifier-first) — detach_run learns --card so a detached job is named, can import
the framework's fleet_progress (via injected PYTHONPATH), and its card id survives a watchdog
restart (persisted in the job registry). The child command after `--` is taken verbatim, so the
child's OWN --card is never swallowed by the launcher.

Drives the real seams: _parse (argv split), _job_env (env construction the forked child inherits),
register_if_requested (registry payload). The fork/exec path itself is not unit-tested (pragma).
"""
import os
import sys
from pathlib import Path

import pytest

ROOT_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(ROOT_SCRIPTS))
import detach_run  # noqa: E402
import jobs        # noqa: E402


# ── --card parsing + child-command isolation ────────────────────────────────────

def test_card_parsed_and_child_cmd_clean():
    ns, cmd = detach_run._parse(
        ["--card", "c1", "--log", "/t/l", "--", "python", "job.py", "--card", "x"])
    assert ns.card == "c1"
    assert cmd == ["python", "job.py", "--card", "x"], "child's own --card must NOT be swallowed"


def test_card_is_optional_backcompat():
    ns, cmd = detach_run._parse(["--log", "/t/l", "--", "python", "job.py"])
    assert getattr(ns, "card", None) is None
    assert cmd == ["python", "job.py"]


# ── _job_env: FLEET_CARD_ID + PYTHONPATH the child inherits ──────────────────────

def test_job_env_prepends_fleet_to_pythonpath():
    env = detach_run._job_env({"PYTHONPATH": "/existing"}, "c1", "/proj/.fleet")
    assert env["FLEET_CARD_ID"] == "c1"
    assert env["PYTHONPATH"].split(os.pathsep)[0] == "/proj/.fleet"
    assert "/existing" in env["PYTHONPATH"]


def test_job_env_no_existing_pythonpath():
    env = detach_run._job_env({}, "c1", "/proj/.fleet")
    assert env["PYTHONPATH"] == "/proj/.fleet"


def test_job_env_without_card_still_sets_pythonpath():
    # PYTHONPATH is injected even without --card so ANY detached runner can import fleet_progress.
    env = detach_run._job_env({}, None, "/proj/.fleet")
    assert "FLEET_CARD_ID" not in env
    assert env["PYTHONPATH"] == "/proj/.fleet"


def test_job_env_does_not_mutate_input():
    base = {"PYTHONPATH": "/x"}
    detach_run._job_env(base, "c1", "/proj/.fleet")
    assert base == {"PYTHONPATH": "/x"}


# ── card id persisted in the registry (R5: a restart can re-export it) ───────────

def test_register_persists_card(tmp_path, monkeypatch):
    captured = {}
    monkeypatch.setattr(jobs, "register", lambda root, job: captured.update(job))
    detach_run.register_if_requested(str(tmp_path), "job1", ["python", "x.py"], lock=None,
                                     card="cardX")
    assert captured.get("card") == "cardX"
    assert captured.get("id") == "job1" and captured.get("cmd") == ["python", "x.py"]
