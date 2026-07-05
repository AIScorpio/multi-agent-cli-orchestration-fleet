"""Regression — grader judge timeout must scale with prompt size; sources must be capped.

Observed live 2026-07-05: content-task qa-pass builds a grounding prompt carrying WHOLE
context_files (papers >100KB). The old flat 180s subprocess timeout made every judge in
the chain time out → _run_chain returned '' → fail-closed auto-bounce on 6/6 qa-pass
calls, burning retry lineages on perfectly good deliverables.
"""
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import grader  # noqa: E402


def test_small_prompt_keeps_default_timeout(monkeypatch):
    monkeypatch.delenv("FLEET_GRADER_TIMEOUT", raising=False)
    assert grader._grader_timeout("x" * 1000) == 180


def test_large_prompt_gets_extended_timeout(monkeypatch):
    monkeypatch.delenv("FLEET_GRADER_TIMEOUT", raising=False)
    assert grader._grader_timeout("x" * 100_000) == 600


def test_env_override_wins(monkeypatch):
    monkeypatch.setenv("FLEET_GRADER_TIMEOUT", "900")
    assert grader._grader_timeout("x" * 100) == 900


def test_env_floor(monkeypatch):
    monkeypatch.setenv("FLEET_GRADER_TIMEOUT", "5")
    assert grader._grader_timeout("x") == 30


def test_sources_capped_with_marker(monkeypatch):
    monkeypatch.delenv("FLEET_GRADER_MAX_SOURCES", raising=False)
    big = "A" * 200_000
    prompt = grader._build_prompt("d", ["c"], sources=big)
    assert "SOURCES TRUNCATED FOR LENGTH" in prompt
    assert len(prompt) < 120_000


def test_small_sources_not_truncated():
    prompt = grader._build_prompt("d", ["c"], sources="tiny source")
    assert "TRUNCATED" not in prompt and "tiny source" in prompt
