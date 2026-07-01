"""Tests for install_supervisor.sh — plist rendering, stagger, remove.
Uses LAUNCH_AGENTS_DIR override + --no-load so launchctl is never touched."""
import os
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "install_supervisor.sh"


@pytest.fixture
def proj(tmp_path):
    """A fake project with .fleet/install_supervisor.sh in place."""
    ma = tmp_path / "MyProj" / ".fleet"
    ma.mkdir(parents=True)
    shutil.copy(SCRIPT, ma / "install_supervisor.sh")
    (ma / "install_supervisor.sh").chmod(0o755)
    return ma


def _run(ma, *args, agents_dir):
    env = dict(os.environ, LAUNCH_AGENTS_DIR=str(agents_dir))
    return subprocess.run(["bash", str(ma / "install_supervisor.sh"), "--no-load", *args],
                          capture_output=True, text=True, env=env, timeout=10)


def test_writes_plist_with_project_paths(proj, tmp_path):
    agents = tmp_path / "agents"
    r = _run(proj, agents_dir=agents)
    assert r.returncode == 0, r.stderr
    plists = list(agents.glob("com.fleet.supervisor.*.plist"))
    assert len(plists) == 1
    body = plists[0].read_text()
    ws = str(proj.parent.resolve())
    assert f"<string>{ws}</string>" in body                       # WorkingDirectory
    assert f"<string>{ws}/.fleet/supervisor_pass.sh</string>" in body
    assert "MyProj" in plists[0].name


def test_interval_staggered_in_band(proj, tmp_path):
    agents = tmp_path / "agents"
    _run(proj, agents_dir=agents)
    body = next(agents.glob("*.plist")).read_text()
    import re
    interval = int(re.search(r"<integer>(\d+)</integer>", body).group(1))
    assert 1500 <= interval < 2100                                # hash-staggered band


def test_explicit_interval_honored(proj, tmp_path):
    agents = tmp_path / "agents"
    _run(proj, "--interval", "1800", agents_dir=agents)
    assert "<integer>1800</integer>" in next(agents.glob("*.plist")).read_text()


def test_different_projects_get_different_labels(proj, tmp_path):
    other = tmp_path / "OtherProj" / ".fleet"
    other.mkdir(parents=True)
    shutil.copy(SCRIPT, other / "install_supervisor.sh")
    agents = tmp_path / "agents"
    _run(proj, agents_dir=agents)
    _run(other, agents_dir=agents)
    names = sorted(p.name for p in agents.glob("*.plist"))
    assert len(names) == 2 and names[0] != names[1]


def test_idempotent_rewrite(proj, tmp_path):
    agents = tmp_path / "agents"
    _run(proj, agents_dir=agents)
    _run(proj, "--interval", "1700", agents_dir=agents)
    plists = list(agents.glob("*.plist"))
    assert len(plists) == 1                                       # rewritten, not duplicated
    assert "<integer>1700</integer>" in plists[0].read_text()


def test_remove_deletes_plist(proj, tmp_path):
    agents = tmp_path / "agents"
    _run(proj, agents_dir=agents)
    assert list(agents.glob("*.plist"))
    env = dict(os.environ, LAUNCH_AGENTS_DIR=str(agents))
    subprocess.run(["bash", str(proj / "install_supervisor.sh"), "--remove"],
                   capture_output=True, text=True, env=env, timeout=10)
    assert not list(agents.glob("*.plist"))


def test_unknown_arg_fails(proj, tmp_path):
    r = _run(proj, "--bogus", agents_dir=tmp_path / "agents")
    assert r.returncode == 1
