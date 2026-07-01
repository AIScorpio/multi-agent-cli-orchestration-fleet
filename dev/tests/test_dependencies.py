"""Tests for the task-level dependency DAG — the parallel-by-default scheduler.

Covers: resolver release on satisfied deps, qa-passed+output-exists semantics,
phase:<id> sugar, output-collision auto-serialization, dead-dep surfacing, and
the guard that the low-water promoter NEVER bypasses an unsatisfied dependency.
"""
import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import doctor


@pytest.fixture
def proj(tmp_path, monkeypatch):
    ma = tmp_path / ".fleet"
    for d in ("queue/drafts", "queue/pending", "queue/claimed",
              "queue/failed", "queue/completed/qa-passed", "status/pids"):
        (ma / d).mkdir(parents=True)
    monkeypatch.setattr(doctor, "MA", ma)
    monkeypatch.setattr(doctor, "ROOT", tmp_path)
    monkeypatch.setattr(doctor, "QUEUE", ma / "queue")
    monkeypatch.setattr(doctor, "PIDS", ma / "status" / "pids")
    monkeypatch.setattr(doctor, "FLEET_HOME", tmp_path / "fleet_home")
    return ma


def _spec(qdir, task_id, *, assigned="kimi", prio=5, depends_on=None,
          output_file="", phase="1", claimed_by=None):
    d = {"task_id": task_id, "title": task_id, "assigned_to": assigned,
         "priority": prio, "phase": phase, "output_file": output_file,
         "depends_on": depends_on or []}
    name = f"{claimed_by}--{task_id}.json" if claimed_by else f"{task_id}.json"
    p = qdir / name
    p.write_text(json.dumps(d))
    return p


def _qa_pass(proj, task_id, output_file="", phase="1", make_output=True, root=None):
    """Put a task in qa-passed AND (optionally) create its output file."""
    qa = proj / "queue" / "completed" / "qa-passed"
    _spec(qa, task_id, output_file=output_file, phase=phase)
    if make_output and output_file:
        out = (root or proj.parent) / output_file
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("done")


class TestRelease:
    def test_releases_when_single_dep_qa_passed(self, proj):
        _qa_pass(proj, "prod", output_file="out/a.txt")
        _spec(proj / "queue" / "drafts", "cons", depends_on=["prod"],
              output_file="out/b.txt")
        n = doctor.resolve_dependencies(fix=True, quiet=True)
        assert n == 1
        assert (proj / "queue" / "pending" / "cons.json").exists()
        assert not (proj / "queue" / "drafts" / "cons.json").exists()

    def test_holds_until_all_deps_met(self, proj):
        _qa_pass(proj, "p1", output_file="out/1.txt")
        # p2 not done yet
        _spec(proj / "queue" / "drafts", "cons", depends_on=["p1", "p2"])
        assert doctor.resolve_dependencies(fix=True, quiet=True) == 0
        assert (proj / "queue" / "drafts" / "cons.json").exists()
        # now p2 lands
        _qa_pass(proj, "p2", output_file="out/2.txt")
        assert doctor.resolve_dependencies(fix=True, quiet=True) == 1

    def test_dep_qa_passed_but_output_missing_holds(self, proj):
        # producer reviewed but its artifact was deleted → NOT satisfied
        _qa_pass(proj, "prod", output_file="out/gone.txt", make_output=False)
        _spec(proj / "queue" / "drafts", "cons", depends_on=["prod"])
        assert doctor.resolve_dependencies(fix=True, quiet=True) == 0
        assert (proj / "queue" / "drafts" / "cons.json").exists()

    def test_dep_with_no_output_file_satisfied_by_qa_alone(self, proj):
        _qa_pass(proj, "prod", output_file="")      # research task, no file artifact
        _spec(proj / "queue" / "drafts", "cons", depends_on=["prod"])
        assert doctor.resolve_dependencies(fix=True, quiet=True) == 1

    def test_dep_free_draft_ignored_by_resolver(self, proj):
        _spec(proj / "queue" / "drafts", "x", depends_on=[])
        assert doctor.resolve_dependencies(fix=True, quiet=True) == 0  # promoter's job
        assert (proj / "queue" / "drafts" / "x.json").exists()


class TestPhaseSugar:
    def test_phase_dep_satisfied_when_all_members_qa_passed(self, proj, monkeypatch):
        _qa_pass(proj, "a", output_file="o/a.txt", phase="1")
        _qa_pass(proj, "b", output_file="o/b.txt", phase="1")
        _spec(proj / "queue" / "drafts", "cons", depends_on=["phase:1"], phase="2")
        # Phase 3 (boundary): the phase:1 dep IS satisfied, but crossing a PHASE boundary is
        # ATTENDED by default → held; it releases only when FLEET_AUTO_PHASE=1 opts into
        # autonomous crossing. (The held-by-default path is also covered in test_attended_phase.)
        monkeypatch.delenv("FLEET_AUTO_PHASE", raising=False)
        assert doctor.resolve_dependencies(fix=True, quiet=True) == 0
        monkeypatch.setenv("FLEET_AUTO_PHASE", "1")
        assert doctor.resolve_dependencies(fix=True, quiet=True) == 1

    def test_phase_dep_blocks_while_member_outstanding(self, proj):
        _qa_pass(proj, "a", output_file="o/a.txt", phase="1")
        _spec(proj / "queue" / "pending", "b", phase="1")   # still outstanding
        _spec(proj / "queue" / "drafts", "cons", depends_on=["phase:1"], phase="2")
        assert doctor.resolve_dependencies(fix=True, quiet=True) == 0

    def test_unknown_phase_does_not_release(self, proj):
        _spec(proj / "queue" / "drafts", "cons", depends_on=["phase:99"])
        assert doctor.resolve_dependencies(fix=True, quiet=True) == 0


class TestOutputCollisionSerialize:
    def test_two_dep_ready_same_output_serialized(self, proj):
        _qa_pass(proj, "p", output_file="o/p.txt")
        # both consumers depend on p, both write the SAME file
        _spec(proj / "queue" / "drafts", "c1", depends_on=["p"],
              output_file="shared/x.txt", prio=1)
        _spec(proj / "queue" / "drafts", "c2", depends_on=["p"],
              output_file="shared/x.txt", prio=2)
        n = doctor.resolve_dependencies(fix=True, quiet=True)
        assert n == 1                                   # only ONE released this tick
        pend = list((proj / "queue" / "pending").glob("*.json"))
        assert len(pend) == 1
        assert (proj / "queue" / "pending" / "c1.json").exists()  # lower prio first
        assert (proj / "queue" / "drafts" / "c2.json").exists()   # held

    def test_collision_with_existing_pending_holds(self, proj):
        _qa_pass(proj, "p", output_file="o/p.txt")
        _spec(proj / "queue" / "pending", "live", output_file="shared/x.txt")
        _spec(proj / "queue" / "drafts", "c", depends_on=["p"],
              output_file="shared/x.txt")
        assert doctor.resolve_dependencies(fix=True, quiet=True) == 0
        assert (proj / "queue" / "drafts" / "c.json").exists()

    def test_distinct_outputs_both_release(self, proj):
        _qa_pass(proj, "p", output_file="o/p.txt")
        _spec(proj / "queue" / "drafts", "c1", depends_on=["p"], output_file="o/1.txt")
        _spec(proj / "queue" / "drafts", "c2", depends_on=["p"], output_file="o/2.txt")
        assert doctor.resolve_dependencies(fix=True, quiet=True) == 2


class TestDeadDep:
    def test_dead_dep_blocks_and_is_surfaced(self, proj, capsys):
        # producer is in failed/, nowhere recoverable
        _spec(proj / "queue" / "failed", "prod")
        _spec(proj / "queue" / "drafts", "cons", depends_on=["prod"])
        n = doctor.resolve_dependencies(fix=True, quiet=False)
        assert n == 0
        assert (proj / "queue" / "drafts" / "cons.json").exists()
        assert "dead dep" in capsys.readouterr().out

    def test_failed_but_also_retried_is_not_dead(self, proj):
        # producer failed once but a live copy is pending (retry in flight)
        _spec(proj / "queue" / "failed", "prod")
        _spec(proj / "queue" / "pending", "prod")
        _spec(proj / "queue" / "drafts", "cons", depends_on=["prod"])
        # not dead → just unmet; held quietly, no dead-dep claim
        assert doctor.resolve_dependencies(fix=True, quiet=True) == 0


class TestLowWaterDoesNotBypassDeps:
    def test_promoter_skips_dependency_gated_draft(self, proj):
        # backlog empty (below low-water) but the draft has an UNMET dep —
        # the low-water promoter must NOT release it.
        _spec(proj / "queue" / "drafts", "blocked", depends_on=["nope"])
        n = doctor.promote_drafts(low_water=2, fix=True, quiet=True)
        assert n == 0
        assert (proj / "queue" / "drafts" / "blocked.json").exists()

    def test_promoter_still_releases_depfree_draft(self, proj):
        _spec(proj / "queue" / "drafts", "free", depends_on=[])
        n = doctor.promote_drafts(low_water=2, fix=True, quiet=True)
        assert n == 1
        assert (proj / "queue" / "pending" / "free.json").exists()

    def test_promoter_releases_draft_with_satisfied_dep(self, proj):
        # a satisfied-dep draft is normally released by the resolver, but if the
        # promoter sees it, it must treat deps as satisfied (not block).
        _qa_pass(proj, "p", output_file="o/p.txt")
        _spec(proj / "queue" / "drafts", "c", depends_on=["p"], output_file="o/c.txt")
        n = doctor.promote_drafts(low_water=2, fix=True, quiet=True)
        assert n == 1


class TestReportMode:
    def test_report_mode_does_not_move(self, proj):
        _qa_pass(proj, "p", output_file="o/p.txt")
        _spec(proj / "queue" / "drafts", "c", depends_on=["p"])
        n = doctor.resolve_dependencies(fix=False, quiet=True)
        assert n == 0
        assert (proj / "queue" / "drafts" / "c.json").exists()


class TestDeadDepArchive:
    """P0: _dep_is_dead must also treat a SUPERSEDED (archived) producer as dead,
    so a dangling reference to a qa-failed-then-retried id surfaces instead of
    waiting forever. (The primary fix rewrites downstream deps; this is the safety
    net for manual/forward-ref mistakes.)"""

    def test_completed_archive_producer_is_dead(self, proj):
        (proj / "queue" / "completed" / "archive").mkdir(parents=True, exist_ok=True)
        _spec(proj / "queue" / "completed" / "archive", "old")
        assert doctor._dep_is_dead("old", {}) is True

    def test_failed_archive_producer_is_dead(self, proj):
        (proj / "queue" / "failed" / "archive").mkdir(parents=True, exist_ok=True)
        _spec(proj / "queue" / "failed" / "archive", "old")
        assert doctor._dep_is_dead("old", {}) is True

    def test_live_retry_not_dead(self, proj):
        # old superseded in archive BUT a live retry exists in pending under old id
        (proj / "queue" / "completed" / "archive").mkdir(parents=True, exist_ok=True)
        _spec(proj / "queue" / "completed" / "archive", "old")
        _spec(proj / "queue" / "pending", "old")
        assert doctor._dep_is_dead("old", {}) is False
