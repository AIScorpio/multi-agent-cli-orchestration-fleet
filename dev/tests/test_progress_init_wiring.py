"""Gate 8 (verifier-first) — the new framework modules are DEPLOYED to every project and the
per-card progress dir is scaffolded + gitignored. Without this, runners can't import fleet_progress
(detached job dies with a silent ImportError) and runtime progress JSON gets committed as source.

Drives the real init_workspace.init_workspace() entry, not just the constants.
"""
import sys
from pathlib import Path

import pytest

ROOT_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(ROOT_SCRIPTS))
import init_workspace as iw  # noqa: E402


def test_new_modules_in_runtime_scripts():
    assert "fleet_progress.py" in iw.RUNTIME_SCRIPTS
    assert "board_cards.py" in iw.RUNTIME_SCRIPTS


def test_progress_dir_in_queue_dirs():
    assert "status/progress" in iw.QUEUE_DIRS


def test_gitignore_block_ignores_progress():
    assert ".fleet/status/progress/*" in iw.GITIGNORE_BLOCK


def test_init_deploys_modules_and_scaffolds(tmp_path):
    iw.init_workspace(tmp_path, force=True, perms=False)
    ma = tmp_path / ".fleet"
    assert (ma / "fleet_progress.py").exists(), "fleet_progress.py not deployed → runner ImportError"
    assert (ma / "board_cards.py").exists(), "board_cards.py not deployed"
    assert (ma / "status" / "progress").is_dir(), "status/progress not scaffolded"
    assert ".fleet/status/progress/*" in (tmp_path / ".gitignore").read_text()
