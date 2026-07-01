"""P4 QUALITY-WITHOUT-THE-HUMAN gates (verifier-first — written BEFORE the fix).

RED until P4 lands. DETERMINISTIC gates only (pure logic + injectable grader). The
REAL-LLM grader-agreement run is EMPIRICAL (needs live worker CLIs, non-deterministic)
— it is NOT in this stop-condition; it runs via dev/eval/run_grader_eval.py and is
documented as a manual step, exactly like P2's real launchd load.

Contract the implementation must deliver (do NOT weaken these):

  scripts/qa_floor.py — mechanical QA floor (no LLM)
    · artifact_ok(path) -> (bool, reason): output must exist, be a REGULAR FILE, and
      be non-empty. A directory with rc==0 → (False, ...) — kills the
      rc==0-on-a-directory false-success.
    · test_count_grew(before, after) -> bool
    · reconcile_files(declared, changed) -> (bool, extra): files changed but NOT
      declared in scope are flagged (extra non-empty → False).

  scripts/predicates.py — pluggable per-task acceptance predicates
    · eval_predicate(pred, root) -> bool, fail-safe to False. Types:
      scalar  {"type":"scalar","source":f,"path":dotpath,"op":">=","value":n}
      regex   {"type":"regex","source":f,"pattern":p}
      command {"type":"command","cmd":[...]}  (exit 0)

  scripts/grader.py — auto second-opinion (LLM via INJECTABLE runner)
    · grade(deliverable, criteria, runner=None) -> {"ok":bool,"reasons":[...],"raw":str}
      runner(prompt)->str. Parse JSON/YES-NO; malformed → fail-open
      {"ok":False,"reasons":["unparseable"...]} (never raises).

  scripts/profiles.py — project-type profiles + discipline injection
    · load_profile(root) -> str  (reads .fleet/profile.json {"profile":...};
      default "software").
    · discipline_block(task_type, profile="software") -> str
      software+code/test → hard-coding discipline; research/writing/review +
      research/write/review task → ANTI-FABRICATION (cite-every-claim) discipline.

  dev/eval/corpus/*.json (labeled good/bad) — present; harness math validated here,
  REAL grader agreement run empirically.
"""
import importlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
CORPUS = Path(__file__).resolve().parents[2] / "dev" / "eval" / "corpus"


def _mod(name):
    try:
        return importlib.import_module(name)
    except Exception as e:
        pytest.fail(f"P4 not implemented: module {name}.py missing ({e})")


# ── P4a: mechanical QA floor ──────────────────────────────────────────────────

class TestQAFloor:
    def test_directory_output_is_failure(self, tmp_path):
        qf = _mod("qa_floor")
        d = tmp_path / "outdir"
        d.mkdir()
        ok, reason = qf.artifact_ok(d)
        assert ok is False and reason            # the rc==0-on-a-directory killer

    def test_missing_output_is_failure(self, tmp_path):
        qf = _mod("qa_floor")
        ok, _ = qf.artifact_ok(tmp_path / "nope.txt")
        assert ok is False

    def test_empty_output_is_failure(self, tmp_path):
        qf = _mod("qa_floor")
        p = tmp_path / "e.txt"
        p.write_text("")
        ok, _ = qf.artifact_ok(p)
        assert ok is False

    def test_real_file_passes(self, tmp_path):
        qf = _mod("qa_floor")
        p = tmp_path / "g.txt"
        p.write_text("content")
        ok, _ = qf.artifact_ok(p)
        assert ok is True

    def test_test_count_must_grow(self, tmp_path):
        qf = _mod("qa_floor")
        assert qf.test_count_grew(5, 5) is False
        assert qf.test_count_grew(5, 7) is True

    def test_files_touched_reconcile_flags_undeclared(self, tmp_path):
        qf = _mod("qa_floor")
        ok, extra = qf.reconcile_files(["src/a.py"], ["src/a.py", "src/SECRET.py"])
        assert ok is False and "src/SECRET.py" in extra


# ── P4b: pluggable acceptance predicates ──────────────────────────────────────

class TestPredicates:
    def test_scalar_pass_and_fail(self, tmp_path):
        pr = _mod("predicates")
        (tmp_path / "m.json").write_text(json.dumps({"metrics": {"recall": 0.81}}))
        base = {"type": "scalar", "source": "m.json", "path": "metrics.recall"}
        assert pr.eval_predicate({**base, "op": ">=", "value": 0.75}, tmp_path) is True
        assert pr.eval_predicate({**base, "op": ">=", "value": 0.90}, tmp_path) is False

    def test_scalar_missing_path_is_false(self, tmp_path):
        pr = _mod("predicates")
        (tmp_path / "m.json").write_text(json.dumps({"metrics": {}}))
        assert pr.eval_predicate(
            {"type": "scalar", "source": "m.json", "path": "metrics.nope",
             "op": ">=", "value": 1}, tmp_path) is False

    def test_scalar_nonnumeric_is_false(self, tmp_path):
        pr = _mod("predicates")
        (tmp_path / "m.json").write_text(json.dumps({"x": "NaNish"}))
        assert pr.eval_predicate(
            {"type": "scalar", "source": "m.json", "path": "x", "op": ">=", "value": 1},
            tmp_path) is False

    def test_regex_present(self, tmp_path):
        pr = _mod("predicates")
        (tmp_path / "f.txt").write_text("all 12 tests passed")
        assert pr.eval_predicate(
            {"type": "regex", "source": "f.txt", "pattern": r"\d+ tests passed"},
            tmp_path) is True
        assert pr.eval_predicate(
            {"type": "regex", "source": "f.txt", "pattern": r"FAILED"}, tmp_path) is False

    def test_command_exit(self, tmp_path):
        pr = _mod("predicates")
        assert pr.eval_predicate({"type": "command", "cmd": ["true"]}, tmp_path) is True
        assert pr.eval_predicate({"type": "command", "cmd": ["false"]}, tmp_path) is False

    def test_missing_source_is_false(self, tmp_path):
        pr = _mod("predicates")
        assert pr.eval_predicate(
            {"type": "scalar", "source": "absent.json", "path": "a", "op": ">=", "value": 0},
            tmp_path) is False


# ── P4c: grader plumbing (LLM via injectable runner) ──────────────────────────

class TestGraderPlumbing:
    def test_pass_verdict(self):
        g = _mod("grader")
        v = g.grade("deliverable", ["c1"], runner=lambda p: '{"ok": true, "reasons": []}')
        assert v["ok"] is True

    def test_fail_verdict(self):
        g = _mod("grader")
        v = g.grade("deliverable", ["c1"],
                    runner=lambda p: '{"ok": false, "reasons": ["missing test"]}')
        assert v["ok"] is False and v["reasons"]

    def test_garbage_is_fail_open(self):
        g = _mod("grader")
        v = g.grade("deliverable", ["c1"], runner=lambda p: "lol not json")
        assert v["ok"] is False                  # never crash; default to not-ok
        assert "reasons" in v

    def test_runner_exception_is_fail_open(self):
        g = _mod("grader")
        def boom(p):
            raise RuntimeError("backend down")
        v = g.grade("deliverable", ["c1"], runner=boom)
        assert v["ok"] is False and "reasons" in v


# ── P4d: project-type profiles + discipline injection ─────────────────────────

class TestProfiles:
    def test_default_profile_is_software(self, tmp_path):
        pf = _mod("profiles")
        assert pf.load_profile(tmp_path) == "software"

    def test_load_profile_from_file(self, tmp_path):
        pf = _mod("profiles")
        (tmp_path / ".fleet").mkdir()
        (tmp_path / ".fleet" / "profile.json").write_text(json.dumps({"profile": "research"}))
        assert pf.load_profile(tmp_path) == "research"

    def test_software_code_block_has_hardcoding(self, tmp_path):
        pf = _mod("profiles")
        b = pf.discipline_block("code", "software").lower()
        assert "hard-cod" in b or "hardcod" in b

    def test_research_block_has_antifabrication(self, tmp_path):
        pf = _mod("profiles")
        b = pf.discipline_block("write", "research").lower()
        assert "fabricat" in b and ("cite" in b or "source" in b)


# ── grader-eval harness math (deterministic; REAL run is empirical) ───────────

class TestGraderEvalHarness:
    def _corpus(self):
        items = []
        for f in sorted(CORPUS.glob("*.json")):
            items.append(json.loads(f.read_text()))
        return items

    def test_corpus_has_good_and_bad(self):
        items = self._corpus()
        labels = [i["label"] for i in items]
        assert labels.count("good") >= 2 and labels.count("bad") >= 2
        for i in items:
            assert i.get("deliverable") and i.get("criteria") and i["label"] in ("good", "bad")

    def test_harness_agreement_with_reference_grader(self):
        # A perfect reference grader (returns each item's true label) must score 100%
        # agreement — this validates the corpus is well-formed and the agreement math
        # is correct. The REAL LLM grader replaces the reference in the empirical run.
        g = _mod("grader")
        items = self._corpus()
        agree = 0
        for it in items:
            truth_ok = (it["label"] == "good")
            runner = (lambda ok: (lambda p: json.dumps({"ok": ok, "reasons": []})))(truth_ok)
            v = g.grade(it["deliverable"], it["criteria"], runner=runner)
            if v["ok"] == truth_ok:
                agree += 1
        assert agree / len(items) >= 0.8
