# Changelog — multi-agent-cli-orchestration-fleet

## 2026-07-05 — P24: caretaker sweeps duplicated COMPLETED work (TOCTOU race) — FIXED
- **Bug (systemic, observed live):** worker completion is a two-step transition (write
  `completed/<id>.result.json`, THEN move the spec out of `claimed/`) with no lock. The
  orphan sweep judged a claim in that sliver "orphaned" (its claimer pid was gone — the
  worker HAD finished) and requeued it to `pending/` → another worker re-claimed and
  REDID the whole task, overwriting a deliverable the leader had already QA'd
  (task-cac31e7a: a full LLM synthesis re-run). The stuck sweep had the same hole.
- **Fix:** new `doctor._finalize_if_completed` — before ANY requeue (orphan path via
  `_requeue_claim`, stuck path inline), if `completed/<id>.result.json` exists with
  `status == COMPLETED`, the claim is FINALIZED (spec → `completed/`, lingering child
  reaped) instead of requeued. FAILED/absent/unreadable results keep the normal requeue
  path. Synced to skill source and live project copy.
- **Test:** `dev/tests/test_doctor_finalize_completed_claim.py` (4 tests: finalize on
  COMPLETED; requeue on FAILED / no result / unreadable result). Suite: 596 passed.
- **Mitigation used live before the fix:** leader snapshotted the QA'd deliverable, let
  the duplicate run finish, compared versions, kept the better one.

## 2026-07-05 — P23: deriver crashed on prose `done_when`; added `glob_count` predicate
- **Bug (systemic, observed live):** a leader authored human-readable `done_when` strings
  (the schema wants predicate dicts); the count-fallback line `done_when.get('type')` sat
  OUTSIDE the try/except → the deriver crash-looped every tick → phases.json never gained
  statuses → the kanban pipeline stayed dark while tasks were visibly in progress.
- **Fix:** `_eval_predicate` tolerates non-dict predicates (returns not-done instead of
  raising); the fallback/gate lines guard `isinstance(done_when, dict)`.
- **Feature:** new predicate `{"type": "glob_count", "pattern": "analysis/phase1/*.md",
  "op": ">=", "value": 6}` — counts non-empty files matching a glob. "Phase done when its
  N deliverable files exist" was previously inexpressible (`count` reads one JSON file;
  `file_exists` checks one path). Partial progress (count > 0) marks the phase `active`;
  `gate_template` gets `{count}`/`{value}`.
- **Test:** `dev/tests/test_derive_phases_prose_and_glob.py` (5 tests). Deriver suite: 17 passed.

## 2026-07-05 — P22: grader judge timed out on grounded content prompts → fail-closed bounced good work
- **Bug (systemic, observed live):** content-task `qa-pass` builds a groundedness prompt
  embedding WHOLE `context_files` (papers >100KB); the flat 180s subprocess timeout made
  EVERY judge in the fallback chain time out → `_run_chain` returned `''` → fail-closed
  auto-qa-fail on 6/6 leader qa-pass calls, burning retry lineages (one reached 3/3) on
  deliverables the leader had verified as good.
- **Fix:** `_grader_timeout(prompt)` — `FLEET_GRADER_TIMEOUT` env wins (floor 30s), else
  180s for prompts <20k chars, 600s above; `_cap_sources` truncates the SOURCES block to
  `FLEET_GRADER_MAX_SOURCES` (default 80k chars, head+tail with an explicit elision marker
  instructing the judge not to flag claims whose support falls in the elided middle).
  Synced to skill source and live project copy.
- **Test:** `dev/tests/test_grader_timeout_scaling.py` (6 tests). Suite: 9 passed.

## 2026-07-05 — P21: `_validate_phase` crashed on integer phase ids
- **Bug (systemic, observed live):** `phases.set_phases` accepts int phase ids
  (`{"id": 1}`), but `orchestrator._validate_phase` assumed string ids and called
  `.startswith("P")` on each → `AttributeError: 'int' object has no attribute
  'startswith'`, crashing EVERY `create-task` on such projects (11/11 failed in the
  first affected project).
- **Fix:** coerce ids to `str` before the `P`-prefix normalization and match against
  the str forms; error message joins the str forms. Synced to both the skill source
  (`scripts/orchestrator.py`) and the live project copy.
- **Test:** `dev/tests/test_validate_phase_int_ids.py` (3 tests: int ids accept
  int/str args, unknown phase still cleanly rejects, legacy "P1" ids + bare-number
  form still work). Suite: 3 passed.
