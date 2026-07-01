"""Behavioral tests for watcher.sh build_prompt() — runs the REAL bash function
(extracted from the script, since sourcing watcher.sh would start its main loop)
against fixture task files."""
import json
import re
import subprocess
from pathlib import Path

import pytest

WATCHER = Path(__file__).resolve().parents[2] / "scripts" / "watcher.sh"


def _build_prompt(task: dict, tmp_path, agent="kimi", agents_cfg: dict | None = None,
                  env_extra: str = "") -> str:
    task_file = tmp_path / "task.json"
    task_file.write_text(json.dumps(task))
    ma = tmp_path / ".fleet"
    (ma / "agents").mkdir(parents=True, exist_ok=True)
    if agents_cfg is not None:
        (ma / "agents" / f"{agent}.json").write_text(json.dumps(agents_cfg))
    src = WATCHER.read_text()
    m = re.search(r"^build_prompt\(\) \{.*?^\}", src, re.M | re.S)
    assert m, "build_prompt() not found in watcher.sh"
    script = (f'AGENT="{agent}"\nMA="{ma}"\n{env_extra}\n'
              + m.group(0)
              + f'\nbuild_prompt "{task_file}"\n')
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=10)
    assert r.returncode == 0, r.stderr
    return r.stdout


def _task(ttype):
    return {"task_id": "t1", "type": ttype, "title": "x", "description": "y",
            "assigned_to": "kimi", "output_file": "out.md",
            "acceptance_criteria": ["a"]}


class TestHardcodeDiscipline:
    def test_code_task_gets_discipline(self, tmp_path):
        out = _build_prompt(_task("code"), tmp_path)
        assert "NO hard-coded values" in out
        assert "config" in out

    def test_test_task_gets_discipline(self, tmp_path):
        out = _build_prompt(_task("test"), tmp_path)
        assert "NO hard-coded values" in out

    def test_research_task_skips_discipline(self, tmp_path):
        out = _build_prompt(_task("research"), tmp_path)
        assert "NO hard-coded values" not in out

    def test_write_task_skips_discipline(self, tmp_path):
        out = _build_prompt(_task("write"), tmp_path)
        assert "NO hard-coded values" not in out


class TestFanoutHint:
    def test_workhorse_gets_fanout_hint(self, tmp_path):
        out = _build_prompt(_task("research"), tmp_path,
                            agents_cfg={"subagent_fanout": True})
        assert "Parallelism" in out

    def test_no_fanout_without_flag(self, tmp_path):
        out = _build_prompt(_task("research"), tmp_path,
                            agents_cfg={"subagent_fanout": False})
        assert "Parallelism" not in out

    def test_fleet_fanout_env_disables(self, tmp_path):
        out = _build_prompt(_task("research"), tmp_path,
                            agents_cfg={"subagent_fanout": True},
                            env_extra="FLEET_FANOUT=0")
        assert "Parallelism" not in out


def test_core_prompt_always_present(tmp_path):
    out = _build_prompt(_task("code"), tmp_path)
    assert "context_files" in out
    assert "acceptance_criteria" in out
    assert "output_file" in out


class TestFileScopeAuthorization:
    """REGRESSION (production leader bug report, 2026-06-11): the old line
    'Modify ONLY that output_file. Do not touch any other file.' contradicted
    every multi-file code task AND the skill's own sibling-test QA rule. In
    production, codex fail-stopped on the contradiction (correct, but burned
    window) while workhorses silently ignored the line (training instruction-
    disobedience). The description is now the single source of truth for the
    authorized file set."""

    def test_single_file_dogma_line_is_gone(self, tmp_path):
        for ttype in ("code", "test", "research", "write", "review"):
            out = _build_prompt(_task(ttype), tmp_path)
            assert "Modify ONLY" not in out
            assert "Do not touch any other file" not in out

    def test_description_defines_authorized_file_set(self, tmp_path):
        out = _build_prompt(_task("code"), tmp_path)
        assert "PRIMARY deliverable" in out
        assert "authorized file set" in out
        assert "off-limits" in out

    def test_files_touched_manifest_required(self, tmp_path):
        out = _build_prompt(_task("code"), tmp_path)
        assert "list EVERY file you created or modified" in out

    def test_scope_rules_apply_to_all_task_types(self, tmp_path):
        # the scope authorization is type-independent (unlike the hardcode block)
        out = _build_prompt(_task("research"), tmp_path)
        assert "authorized file set" in out
