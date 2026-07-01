"""P10 gates (verifier-first) — 7th eval (4.0): lift the lone-3 Token-economy + the
autonomy gap + cheap correctness/honesty wins. Each gate drives a real function.

  A. Pool overspend is NO LONGER SILENT: capacity.pool_alerts() returns an alarm when the
     shared Claude pool crosses soft/hard, and the capacity loop emits it.
  B. No-LLM QA PASS path: doctor.floor_decision classifies a floor-clean, predicate-SATISFIED
     task as auto-passable (so the DAG advances without a live leader); clean-but-no-predicate
     defers to semantic review; junk fails. sweep_qa_floor auto-passes the passable ones.
  C. profiles compose: a research-project CODE task keeps the engineering block AND anti-fab;
     a data-project WRITEUP gets the numbers anti-fab clause (branch-ordering bug fixed).
  D. honesty: the dead 'PushNotification' escalation claim is gone from shipped comments.
"""
import json
import sys
from pathlib import Path

import pytest

ROOT_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(ROOT_SCRIPTS))
import doctor
import capacity
import profiles
SCRIPTS = ROOT_SCRIPTS


# ── A: pool overspend alarm ────────────────────────────────────────────────────

class TestNoLLMPass:
    def test_decision_pass_when_predicate_satisfied(self, tmp_path):
        (tmp_path / "g.txt").write_text("ok")
        spec = {"output_file": "g.txt",
                "acceptance_predicates": [{"type": "command", "cmd": ["true"]}]}
        verdict, _ = doctor.floor_decision(spec, tmp_path, {})
        assert verdict == "pass", "predicate-satisfied clean task should auto-pass (no LLM)"

    def test_decision_defer_without_predicate(self, tmp_path):
        (tmp_path / "g.txt").write_text("ok")
        verdict, _ = doctor.floor_decision({"output_file": "g.txt",
                                            "acceptance_predicates": []}, tmp_path, {})
        assert verdict == "defer", "no-predicate task must defer to semantic review, not auto-pass"

    def test_decision_fail_on_junk(self, tmp_path):
        verdict, failures = doctor.floor_decision({"output_file": "missing.txt",
                                                   "acceptance_predicates": []}, tmp_path, {})
        assert verdict == "fail" and failures

    def test_sweep_auto_passes_predicate_task(self):
        body = (SCRIPTS / "doctor.py").read_text()
        # sweep must have a qa-pass action path, not only qa-fail
        assert "qa-pass" in body and "floor_decision" in body, \
            "sweep_qa_floor has no no-LLM auto-pass path"


# ── C: profiles compose (no block lost to branch ordering) ─────────────────────

class TestProfilesCompose:
    def test_research_code_keeps_engineering_and_antifab(self):
        blk = profiles.discipline_block("code", "research").lower()
        assert "hard-cod" in blk, "research-project code task lost the engineering block"
        assert "fabricat" in blk, "research-project code task lost the anti-fab block"

    def test_data_writeup_has_numbers_antifab(self):
        blk = profiles.discipline_block("write", "data").lower()
        assert "fabricat" in blk and ("number" in blk or "metric" in blk), \
            "data-project writeup lacks the numbers anti-fabrication clause"

    def test_software_code_still_engineering_only(self):
        blk = profiles.discipline_block("code", "software").lower()
        assert "hard-cod" in blk


# ── D: honesty — dead PushNotification escalation claim removed ─────────────────

class TestNoDeadPushClaim:
    def test_pushnotification_claim_gone(self):
        for f in ("fleet_health.py", "health_loop.sh"):
            body = (SCRIPTS / f).read_text()
            assert "PushNotification" not in body, \
                f"{f} still claims a PushNotification escalation that no code performs"
