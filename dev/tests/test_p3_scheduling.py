"""P3 SCHEDULING-DEPTH gates (verifier-first — written BEFORE the fix).

RED until P3 lands. Contract the implementation must deliver (do NOT weaken these):

  doctor.py
    · dependents_index() -> {producer_id: [draft_task_id, ...]}
      reverse map (concrete deps only) built from drafts/.
    · release_dependents(producer_id, fix=True, quiet=True) -> int
      event-driven release: when `producer_id` becomes QA-passed, release ONLY its
      now-ready dependents — must NOT re-evaluate satisfaction for unrelated drafts
      (O(dependents), not O(all drafts)). Reuses the P0/P1 satisfaction + collision
      logic.
    · find_deadlocks() -> [[task_id, ...], ...]
      surfaces never-releasable draft sets: dependency CYCLES, and chains whose root
      dep is dead/missing (terminal). SURFACED only — never auto-released.
    · _scopes_overlap(scope_a, scope_b) -> bool
      write-scope intersection over globs (fnmatch both directions + equality).
      resolve_dependencies serializes two dep-ready tasks whose WRITE SCOPES overlap
      (scope = task['write_scope'] or [output_file]) — generalizes the exact-
      output_file collision (existing behavior must still hold).

  schema.py
    · write_scope: List[str] = []   (empty → resolver uses [output_file])

  capacity.py  (unified Anthropic pool accounting + per-project fairness)
    · record_spend(project, role, tokens, now=None) -> None
      append a spend record (claude-worker + claude-lead share ONE tracked pool).
    · pool_used(now=None) -> {"5h": int, "week": int}
      summed claude-pool tokens within each window (closes the per-role-bucket blind
      spot where each reads healthy while the shared window drains).
    · project_spend(now=None) -> {project: tokens}   (attribution)
    · fair_slot_floor(active_projects, total_slots) -> {project: floor}
      per-project minimum-slot reservation so one project can't starve another.
"""
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import doctor
import capacity
import schema


def _has(mod, name):
    fn = getattr(mod, name, None)
    if fn is None:
        pytest.fail(f"P3 not implemented: {mod.__name__}.{name} is missing")
    return fn


# ── fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def proj(tmp_path, monkeypatch):
    ma = tmp_path / ".fleet"
    for d in ("queue/drafts", "queue/pending", "queue/claimed", "queue/failed",
              "queue/completed/qa-passed", "status"):
        (ma / d).mkdir(parents=True)
    monkeypatch.setattr(doctor, "MA", ma)
    monkeypatch.setattr(doctor, "ROOT", tmp_path)
    monkeypatch.setattr(doctor, "QUEUE", ma / "queue")
    monkeypatch.setattr(doctor, "FLEET_HOME", tmp_path / "fleet_home")
    return ma


@pytest.fixture
def cap_home(tmp_path, monkeypatch):
    fh = tmp_path / "fleet_home"
    monkeypatch.setattr(capacity, "FLEET_HOME", fh)
    monkeypatch.setattr(capacity, "CAP_DIR", fh / "capacity")
    return fh


import json as _json


def _spec(qdir, tid, *, depends_on=None, output_file="", write_scope=None, phase="1"):
    d = {"task_id": tid, "title": tid, "assigned_to": "any", "priority": 5,
         "phase": phase, "output_file": output_file, "depends_on": depends_on or []}
    if write_scope is not None:
        d["write_scope"] = write_scope
    (qdir / f"{tid}.json").write_text(_json.dumps(d))


def _qa(proj, tid, output_file="", root=None):
    _spec(proj / "queue" / "completed" / "qa-passed", tid, output_file=output_file)
    if output_file:
        out = (root or proj.parent) / output_file
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("x")


# ── 1. incremental (event-driven) resolution ─────────────────────────────────

class TestIncrementalResolution:
    def test_dependents_index_maps_concrete_deps(self, proj):
        idx_fn = _has(doctor, "dependents_index")
        _spec(proj / "queue" / "drafts", "c1", depends_on=["p"])
        _spec(proj / "queue" / "drafts", "c2", depends_on=["p", "q"])
        _spec(proj / "queue" / "drafts", "c3", depends_on=["z"])
        idx = idx_fn()
        assert set(idx.get("p", [])) == {"c1", "c2"}
        assert idx.get("z", []) == ["c3"]

    def test_release_dependents_only_releases_ready_dependents(self, proj):
        rel = _has(doctor, "release_dependents")
        _qa(proj, "p", output_file="o/p.txt")
        _spec(proj / "queue" / "drafts", "c", depends_on=["p"], output_file="o/c.txt")
        _spec(proj / "queue" / "drafts", "other", depends_on=["unrelated"])
        n = rel("p", fix=True, quiet=True)
        assert n == 1
        assert (proj / "queue" / "pending" / "c.json").exists()
        assert (proj / "queue" / "drafts" / "other.json").exists()   # untouched

    def test_release_dependents_is_incremental(self, proj, monkeypatch):
        rel = _has(doctor, "release_dependents")
        _qa(proj, "p", output_file="o/p.txt")
        _spec(proj / "queue" / "drafts", "c", depends_on=["p"], output_file="o/c.txt")
        for i in range(25):                       # many UNRELATED drafts
            _spec(proj / "queue" / "drafts", f"u{i}", depends_on=[f"x{i}"])
        calls = {"n": 0}
        real = doctor._dep_satisfied
        def counting(dep, qa, idx):
            calls["n"] += 1
            return real(dep, qa, idx)
        monkeypatch.setattr(doctor, "_dep_satisfied", counting)
        rel("p", fix=True, quiet=True)
        # only the dependent's deps get satisfaction-checked — NOT all 26 drafts
        assert calls["n"] <= 3, f"not incremental: {calls['n']} satisfaction checks"


# ── 2. deadlock / cycle detection ─────────────────────────────────────────────

class TestDeadlockDetection:
    def test_cycle_detected(self, proj):
        fd = _has(doctor, "find_deadlocks")
        _spec(proj / "queue" / "drafts", "a", depends_on=["b"])
        _spec(proj / "queue" / "drafts", "b", depends_on=["a"])
        dl = fd()
        flat = {t for grp in dl for t in grp}
        assert {"a", "b"} <= flat

    def test_dead_root_chain_detected(self, proj):
        fd = _has(doctor, "find_deadlocks")
        _spec(proj / "queue" / "failed", "deadprod")          # terminal producer
        _spec(proj / "queue" / "drafts", "c", depends_on=["deadprod"])
        dl = fd()
        flat = {t for grp in dl for t in grp}
        assert "c" in flat

    def test_healthy_chain_not_flagged(self, proj):
        fd = _has(doctor, "find_deadlocks")
        _qa(proj, "p", output_file="o/p.txt")
        _spec(proj / "queue" / "drafts", "c", depends_on=["p"])      # resolvable
        assert all("c" not in grp for grp in fd())

    def test_find_deadlocks_does_not_release(self, proj):
        fd = _has(doctor, "find_deadlocks")
        _spec(proj / "queue" / "drafts", "a", depends_on=["b"])
        _spec(proj / "queue" / "drafts", "b", depends_on=["a"])
        fd()
        assert (proj / "queue" / "drafts" / "a.json").exists()       # surfaced, not moved
        assert not list((proj / "queue" / "pending").glob("*.json"))


# ── 3. write-scope collision over globs ───────────────────────────────────────

class TestWriteScopeCollision:
    def test_overlap_helper(self, proj):
        ov = _has(doctor, "_scopes_overlap")
        assert ov(["src/a.ts"], ["src/*"]) is True
        assert ov(["src/a.ts"], ["src/a.ts"]) is True
        assert ov(["src/a.ts"], ["lib/*"]) is False
        assert ov(["docs/x.md"], ["src/**"]) is False

    def test_overlapping_scopes_serialized(self, proj):
        _has(doctor, "_scopes_overlap")
        _qa(proj, "p", output_file="o/p.txt")
        _spec(proj / "queue" / "drafts", "c1", depends_on=["p"],
              output_file="src/a.ts", write_scope=["src/a.ts"])
        _spec(proj / "queue" / "drafts", "c2", depends_on=["p"],
              output_file="src/b.ts", write_scope=["src/*"])      # overlaps c1
        n = doctor.resolve_dependencies(fix=True, quiet=True)
        assert n == 1                                              # one held this tick
        assert len(list((proj / "queue" / "pending").glob("*.json"))) == 1

    def test_disjoint_scopes_both_release(self, proj):
        _qa(proj, "p", output_file="o/p.txt")
        _spec(proj / "queue" / "drafts", "c1", depends_on=["p"],
              output_file="src/a.ts", write_scope=["src/*"])
        _spec(proj / "queue" / "drafts", "c2", depends_on=["p"],
              output_file="docs/b.md", write_scope=["docs/*"])
        assert doctor.resolve_dependencies(fix=True, quiet=True) == 2


# ── 4. unified pool accounting + per-project fairness ─────────────────────────

class TestFairSlots:
    def test_floor_divides_among_active_projects(self, cap_home):
        fsf = _has(capacity, "fair_slot_floor")
        r = fsf(["projA", "projB"], 6)
        assert r["projA"] >= 1 and r["projB"] >= 1
        assert r["projA"] + r["projB"] <= 6
        assert min(r.values()) >= 6 // 2 - 1          # roughly balanced, no starvation

    def test_single_project_gets_all(self, cap_home):
        fsf = _has(capacity, "fair_slot_floor")
        assert fsf(["solo"], 6)["solo"] == 6
