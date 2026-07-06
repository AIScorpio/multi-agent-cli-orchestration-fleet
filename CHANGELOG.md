# Changelog — multi-agent-cli-orchestration-fleet

## 2026-07-07 — P27: `orchestrator override-fail <id> --reason` — leader-verified accept path for mechanical false-fails
- **Why (observed live 2026-07-06):** the auto-QA mechanical floor failed a CORRECT
  deliverable 4x because the leader-authored predicates were themselves defective
  (a BSD `grep -L` exit-code inversion — the predicate could only pass if the file
  CONTAINED the forbidden string — plus a predicate invoking `.venv/bin/python` inside
  an isolated worktree that has no venv). P26's `requeue` is the re-run path and would
  have driven the good work back into the same defective floor, burning a worker cycle;
  P25's `qa-pass --leader-verified` deliberately keeps the mechanical floor running, so
  it could not rescue it either. The leader had to hand-move both queue files — the
  exact bare-mv failure mode P26 had just eliminated for the requeue path.
- **Change:** `override-fail <id> --reason "..."` (FAILED tasks only) moves the spec AND
  the result sidecar to `completed/archive/`; the sidecar keeps the machine's verdict
  verbatim (`original_auto_status` + the error text) and layers the leader's rationale
  on top (`qa_status: leader-approved (false-fail override): <reason>`); the spec is
  stamped (`override_fail_at`, `override_fail_reason`). `--reason` is REQUIRED (enforced
  at the parser AND in the command body) — it replaces the mechanical verdict in the
  audit trail, so it must state what was actually verified. Missing sidecars are
  synthesized with a note. Ledger event `override-fail`; journal line. Non-failed states
  get state-specific hints (completed: nothing to override; use requeue for a fresh run).
- **Test:** `dev/tests/test_cmd_override_fail.py` (5 tests: both-files move + verdict
  layering; no-sidecar synthesis; reason required; completed refused; unknown id).
  Suite: 609 passed. SKILL.md orchestrator-commands table documents the command. skill

## 2026-07-05 — P26: `orchestrator requeue <id>` — formal failed→pending path
- **Why (observed live):** the framework had no command to requeue a FAILED task, so the
  leader used a bare `mv failed/<id>.json pending/` — which left the `.result.json`
  sidecar behind, and the kanban Failed column showed a long-resolved failure until the
  leader noticed and archived it by hand.
- **Change:** `requeue <id> [--reason "..."]` handles BOTH files: the spec moves to
  pending/ with transient state cleared (`claimed_by_pid`, `fail_reason`) and provenance
  stamped (`requeued_at`, optional `requeue_reason`); the failed result sidecar is
  ARCHIVED to `failed/archive/` (audit trail kept, live board cleared). Ledger event
  `requeue`. FAILED tasks only: a completed task is REFUSED with a pointer to `qa-fail`
  (requeueing finished work is the P24 duplicate-run failure mode); other states get a
  state-specific hint.
- **Test:** `dev/tests/test_cmd_requeue.py` (4 tests: spec+sidecar handling; no-sidecar
  requeue; completed refused; unknown id errors). Suite: 604 passed. SKILL.md
  orchestrator-commands table documents the command. skill

Auditable record of skill-layer evolutions (harness changes only, never task/content
changes). Forked 2026-06-11 from `multi-agent-cli-orchestration` (the legacy
single-project skill, which stays installed and untouched until fleet is proven —
then demise it).

## 2026-07-05 — P25: `qa-pass --leader-verified` — the attended leader's semantic-QA override
- **Why:** the framework's own contract says the leader is the FINAL semantic authority
  (the supervisor defers semantic+science QA to the leader; the grader exists to scale QA,
  not to overrule it) — yet a grader `ok=false` mechanically overrode an attended leader's
  verdict and burned retry lineages on deliverables the leader had personally verified
  (observed live 2026-07-05 during the P22 timeout fallout: one lineage reached retry 3/3).
- **Change:** `qa-pass --leader-verified` skips ONLY the semantic grader; the mechanical
  floor and acceptance predicates still run. `--reason` is REQUIRED with the flag (the
  leader's rationale is the audit trail that replaces the grader verdict) and is pinned in
  the verdict sidecar as `{"grader": {"ran": false, "leader_verified": true}}`. The flag is
  IGNORED in fallback mode (`FLEET_FALLBACK_QA=1`) — the supervisor is not the leader and
  must not skip semantic QA on its behalf (Fix B preserved). User-approved for the skill
  source on 2026-07-05 (was project-local pending that decision). Also carries the P22
  companion fix that was blocked from syncing earlier: grader qa-fail reasons kept to
  1500 chars (a 200-char cap hid the actual failing criterion behind the preamble).
- **Test:** `dev/tests/test_qa_pass_leader_verified.py` (4 tests: skips grader + sidecar
  audit record; reason required; fallback ignores the flag; grader still runs without the
  flag). Suite: 600 passed. SKILL.md orchestrator-commands table documents the flag.

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

## 2026-07-01 — agent-side self-notification for detached runs (in-session cron sentinel)

Detached long jobs are first-class for the HUMAN (card + `report()` → kanban %/log) but do NOT re-invoke
the LEADER on completion; session-bound notifiers (`run_in_background`, `Monitor`) die on a new turn /
compaction, leaving the leader blind for multi-hour/day runs between manual passes. The Long-running-jobs
section of SKILL.md now prescribes arming an IN-SESSION cron sentinel (`CronCreate`, `durable:false`) at
launch: ~hourly it re-enqueues a self-contained check prompt (absolute paths + card id) that reports
progress / alerts on nan or failure / does the leader QA on completion, then `CronDelete`s itself on any
terminal state. in-session (NOT durable) is mandatory for multi-project safety: durable crons share one
`.claude/scheduled_tasks.json` (concurrent-write race) and fire in any idle REPL (cross-project
mis-firing), whereas in-session crons are per-session memory — no shared file, fire only in their own
session (tradeoff: die on a full REPL restart, recovered from the on-disk board/queue truth).

ENFORCEMENT (2026-07-02): docs alone let the leader forget (bash watchers were used + silently killed),
so this is now enforced by a PostToolUse hook `scripts/hooks/detach_sentinel_reminder.py` — on every
`detach_run.py --card <id>` Bash launch it writes the exact `CronCreate(... durable:false ...)` call to
stderr (exit 2) so the leader arms the sentinel; NO-OP (exit 0) for every other command, fail-open on any
error. Register per project in `.claude/settings.json` under `PostToolUse` → `Bash`. Live in project-C
(hook file + settings.json registration confirmed).

GAP CLOSE (2026-07-02, verifier-first): the hook shipped with no test anywhere and wasn't wired into
`init_workspace.py`, so fresh/re-scaffolded projects would silently miss the registration (project-C
only had it because it was added by hand). Closed: (1) `dev/tests/test_detach_sentinel_reminder.py` —
8 tests, real-subprocess invocation (stdin JSON in, exit code + stderr out), covering the fire/no-op/
fail-open matrix plus a RED test asserting `init_workspace.py` registers the hook; (2) `init_workspace.py`
— `detach_sentinel_reminder.py` added to `HOOK_SCRIPTS`, `POST_HOOK_CMD` added, `_wire_autonomous_hook`
now merges a `PostToolUse` → `Bash` entry (idempotent, not gated on `AUTONOMOUS_ON`); (3) SKILL.md's
Long-running-jobs section now carries the "Agent-side self-notification" subsection this entry originally
claimed existed. Full suite 570 → 578, zero regressions.

## 2026-06-30 — generic observability FLOOR for long detached runs (caretaker liveness)

Root cause of the t3 miss: progress was "framework provides `report()`, each runner must call it" →
any forgotten runner is a silent blank card. Fixed structurally with a no-LLM FLOOR so a 20h+
detached card ALWAYS shows alive + coarse % even if its runner never calls `report()` — the same
"framework guarantees, never relies on per-caller discipline" principle already used for pure-A / the
Stop hook. Verifier-first; suite 562 → 570. Detach-only (queue floor explicitly declined — a queue
task is one opaque short external-CLI invocation with no internal report() site). No opt-in flag
(self-gating: only running detach cards with a log/done-predicate).

- **`doctor.sweep_liveness_floor()`** (caretaker `--fix` tick, after `sweep_card_floor`, wrapped
  fail-open so it can never break the rest of the tick): for each RUNNING card WITHOUT a per-card
  progress file (runner not reporting), writes `status/liveness/<id>.json` from (a) a count
  `done`-predicate → a REAL % for free (reuse of the watchdog completion metric), and/or (b) the
  card's log size + mtime age. Reads only; writes ONLY the new liveness file (single-writer); never
  touches queue/QA/board status. Path-guarded (`_safe_log_path` containment, like the hub).
- **`kanban_hub`**: render precedence `progress (runner) > cell_progress > liveness (floor) > plain`;
  `_liveness_suffix` shows `done/total · ~pct%` (if done-count) + a `● <size> · <age>` / `⚠ stalled`
  chip. Helpers `_human_size`/`_age_str`.
- **`gc_artifacts`**: status-scoped sweep now covers BOTH `status/progress` and `status/liveness`
  (never reap a running card's files; reap terminal/orphan when stale).
- **`init_workspace`**: `status/liveness` in QUEUE_DIRS + gitignored (existing projects also covered —
  `sweep_liveness_floor` mkdirs defensively).

Gates: test_liveness_floor.py (8: caretaker writes from log, real % from done-count, skips when
runner reporting, skips non-running, log-outside-root safe, hub renders fallback, progress wins, gc
status-scoped). Synced doctor.py + kanban_hub.py to A/B/C (md5-verified) + restarted the global
hub. Inert on 01/04 (no detach cards). Honest limit: floor gives liveness + coarse/real-%-if-count,
NOT semantic stage/eta (that still needs `report()`); a job with neither a `--log` nor a count
`done` shows nothing extra.

## 2026-06-28 — detached-task first-class observability (Gates 1–8, framework live)

Detached long-runs were observability second-class: no completion %, no granular log, no log in the
card drawer; the single `cell_progress.json` collided across concurrent runs. Re-evaluated the
enhancement proposal with a read-only audit workflow (51 findings / 50 confirmed) before coding —
which corrected the proposal on four counts (below). Verifier-first, RED→GREEN per gate; suite
509 → 562 (+53), zero regressions. Decisions: DP1 id-from-output-stem (not a single env var), DP2
helper in `.fleet/` + `detach_run` injects `PYTHONPATH`, DP3 pure-A **scoped to cards only**, DP4
snapshot drawer (no live poll).

- **Gate 1 — `fleet_progress.py`** (NEW): `report(done,total,*,card_id,output,stage,…)` → per-card
  `.fleet/status/progress/<id>.json`. id = card_id > `FLEET_CARD_ID` env > output stem (so per-cell
  `subprocess.run` children get distinct ids — the real fix for collisions, not env). Safe math
  (no div-by-zero on the 0% tick, pct/done clamped), throttle that never drops the terminal tick,
  fully fail-open, optional append-only `[progress]` log line.
- **Gate 2 — `detach_run.py`**: `--card` (child cmd after `--` untouched); pure `_job_env` exports
  `FLEET_CARD_ID` + prepends `.fleet` to `PYTHONPATH` (children inherit); pre-exec progress stub;
  card id persisted in the registry; "finalize on exit" REMOVED (execvp can't, R6).
- **Gate 3 — `kanban_hub.py`**: renders `stage · done/total · ~pct% · eta` per running card from its
  own progress file; no progress → title verbatim (back-compat); legacy `cell_progress.json` kept as
  fallback; card view dict carries `log`/`has_log`.
- **Gate 4 — card log**: `board-card --log` (getattr; absent → no key); `resolve_card_log` REPLACES
  the queue token guard — `.resolve()` then realpath-must-be-inside-the-card's-OWN-project-root,
  symlink-escape/`..`/cross-project all rejected; shared `_tail_bytes` (also bounds queue `read_log`,
  UTF-8-boundary safe); `/api/card-log`; drawer routes detached cards there (one-shot snapshot).
- **Gate 5 — caretaker pure-A, CARDS ONLY**: `qa_floor.evaluate_card(allow_command=False)` for the
  no-LLM sweep (cards are runner-writable → automaton never execs their command predicate); the
  leader's `approve-card` passes `allow_command=True`. **QUEUE unchanged** (its predicates are
  leader-authored via `create-task`; project-C has 60 `command` predicates in use that still auto-pass).
  `qa_backlog` now also counts done cards so a long leader absence alarms.
- **Gate 6 — `board_cards.py`** (NEW): `merge_write` — only listed ids touched, unknown keys
  (verdict/log) preserved, leader-terminal cards never downgraded by a stale runner rebuild (R4
  lost-update). `cmd_board_card` routes through it.
- **Gate 7 — GC status-scoped**: `gc_artifacts` never reaps a RUNNING card's progress; reaps only
  terminal/orphan progress when stale; qa-passed sidecars still exempt.
- **Gate 8 — init wiring**: `fleet_progress.py`+`board_cards.py` in `RUNTIME_SCRIPTS`,
  `status/progress` in `QUEUE_DIRS`, gitignored.

Audit corrections baked in: (a) root cause is id-propagation, not file count; (b) the drawer click
path already existed — only server-side resolution was missing; (c) the "reuse the existing tail
cap" was fiction (none existed → added); (e) the path guard must REPLACE not reuse `TASK_ID_RE`.
Synced framework to A/B/C (24 files, md5-verified) + restarted the global hub WITHOUT disturbing
a live `a-long-sweep` detached run. **Gate 9 (project-C runner adoption) LANDED 2026-06-28** after that run
finished (was deferred — per-cell `subprocess.run` children re-import runner code from disk, unsafe
mid-run): the project's legacy progress shim now ALSO delegates to `fleet_progress.report` (card_id pinned to the
cell stem) → t1/t2/e1 get per-card progress with ZERO runner edits, legacy `cell_progress.json` kept
for back-compat; `run_full_replication._write_board_cards` switched to `board_cards.merge_write` (no
more approved→done revert on rebuild); the one runner with no progress calls got per-item progress (set_output + tick +
optional `stage`). Smoke-verified: two cells → two distinct progress files (25%/65%), merge preserves
a leader approval, py_compile clean. Multi-Mac exposure of the card-log read stays onhold (hub is
127.0.0.1; the guard is in regardless).

## 2026-06-28 — detach QA aligned to queue + explicit dispatch decision (D0–D6)

Detached jobs had only a binary `approve-card` (no floor, no predicates, no completion gate, no
forcing) — a QA backdoor weaker than queue tasks; and the skill never gave the leader a clear
"queue vs detach" rule. Closed both, verifier-first; suite 494 → 509.

- **D0 — dispatch decision rule, co-located.** SKILL.md `## Creating tasks` now opens with a
  queue / queue-fan-out / detach decision table at the decision point (criteria: worker timeout,
  resource exclusivity, run-vs-file side-effect, crash-durability; shardable→fan-out). Gate
  `test_dispatch_decision_doc.py`.
- **D1 — cards carry machine-checkable acceptance.** New `orchestrator board-card` upsert
  (`--output`, `--predicate`, `--done`, `--type`, `--provenance`) writing board_cards.json.
- **D2 — `approve-card` is floor-gated + writes a verdict.** Runs `qa_floor.evaluate_card`
  (output artifact + predicates) FAIL-CLOSED before the leader's sign-off; records a structured
  verdict on the card. (Gates D1+D2: `test_detach_card_qa.py`.)
- **D3 — caretaker covers cards.** `doctor.sweep_card_floor` auto-REJECTS a `done` card whose
  floor fails (crashed/incomplete/predicate-fail), DEFERS the rest (NEVER auto-approves science).
  Wired into the `--fix` tick. Gate `test_caretaker_card_sweep.py`.
- **D4 — Stop hook covers cards.** `qa_gate_stop.py` also blocks idle while any card is
  `done · pending QA` (lists approve-card/reject-card). Same forcing guarantee as queue.
- **D5 — DAG spans both tracks.** `doctor._phase_member_states` counts cards (approved→qa,
  pending/running/done→out, failed→ignored) so a queue task depending on `phase:N` (detached
  work) releases only after the card is approved. Gate `test_dag_spans_detach.py`.
- **D6 — completion + provenance (the extra strengthening detach needs beyond queue).**
  `qa_floor.eval_done` (count/file_exists) gates `evaluate_card` → a card marked `done` but
  incomplete (count < expected) can't be approved; `--provenance` recorded in the verdict.

Net: each unit is single-owned (queue OR detach); both obey the same QA principles; detach gets
queue's floor/predicates/verdict + forcing + DAG, plus completion/provenance. Honest limit: the
dispatch choice and L4 semantic correctness remain leader judgment. Synced
orchestrator.py/qa_floor.py/doctor.py/hooks/qa_gate_stop.py to A/B/C.

## 2026-06-27 — caretaker defers to a present leader + Stop-hook forces leader QA

project-C's leader caught it: the no-LLM caretaker auto-passed a content/impl task on a weak
grep-marker predicate — no semantic review — silently doing the leader's non-delegable
semantic/science QA for it; and nothing forced a present-but-idle leader to QA at all. Two fixes
(verifier-first; suite 483 → 494):

- **Part 1 — caretaker auto-PASS is leader-ABSENCE-only.** `doctor.floor_decision` now returns
  `pass` ONLY when floor-clean AND predicates hold AND task is non-content AND the leader is
  ABSENT (`_leader_alive()` = fresh `.fleet/status/leader.heartbeat` within FLEET_LEADER_TTL).
  Leader present → everything DEFERS to the leader; content (research/write/review) defers even in
  absence (Fix-B consistency). Auto-FAIL of junk + recovery still always run. So a present
  leader's review is never bypassed. Gate `test_caretaker_defers_to_leader.py` (6).
- **Part 2 — Stop hook forces leader QA before idle.** New `hooks/qa_gate_stop.py`: while
  `AUTONOMOUS_ON` and there are completed tasks awaiting QA (result.json not yet in qa-passed/),
  it BLOCKS the stop (exit 2 + stderr listing them) so the leader can't idle past un-QA'd work;
  `qa-pass`/`qa-fail` drains them → block clears (self-terminating, fail-open). Registered as a
  `Stop` hook in `.claude/settings.json` via `_wire_autonomous_hook` (same pattern as the
  PreToolUse Bash guard); `qa_gate_stop.py` added to `HOOK_SCRIPTS`. Gate
  `test_qa_gate_stop_hook.py` (5). Part 1 makes the tasks BE there (not auto-passed away); Part 2
  forces the leader to act on them.

Honest limit: the hook forces ATTENTION, not quality (the review's correctness is still the
leader's judgment). Scoped to autonomous mode (attended → human decides). Synced doctor.py +
qa_gate_stop.py + Stop registration to A/B/C. NOTE: the Stop hook takes effect on the leader's
NEXT session start (settings.json is read then); Part 1 takes effect on the next caretaker tick.

## 2026-06-26 — qa-passed sidecars retained forever + kanban Approved from specs

Found a latent data/display bug: `doctor.gc_artifacts` reaped `qa-passed/*.result.json` +
`*.verdict.json` after 14 days (and a 500 cap), but the kanban built its "Approved" column FROM
`result.json` — so any project finished >14 days ago silently lost its whole Approved column. It
hit 04 hard (a weeks-old project: 242 result.json + 242 verdict.json gc'd overnight → board empty;
specs all intact) and 01 partially. NOT caused by the phase remap (timeline + gc never targets
specs). Per the user: keep a COMPLETE QA audit forever + board always complete. Two fixes:

- **doctor.gc_artifacts** — REMOVED `qa-passed/*.result.json` and `*.verdict.json` from the gc
  targets entirely (no age rule, no per-dir cap). Full retention forever — they're the durable
  QA-verdict audit + historical board record (tiny JSON; deliverables live in project dirs). gc
  still prunes logs/archives/heartbeats. Gate `test_gc_retains_qa_sidecars.py` (3).
- **kanban_hub.collect / collect_overview** — "Approved" now derived from the qa-passed SPECS
  (never gc'd), enriched by `result.json` when present → the board shows every qa-passed task even
  if a sidecar is missing. Gate `test_kanban_approved_from_specs.py` (2).
- **Recovery already applied**: regenerated `result.json` from specs for 04 (242) and 01 (33
  missing; 28 originals kept), marked `reconstructed:true` (the gc'd `verdict.json` rationales are
  unrecoverable — Time Machine only). Boards verified: 01=61, 04=242, 05=31 approved.

Suite 478 → 483. Synced doctor.py/kanban_hub.py to A/B/C; hub restarted.

## 2026-06-25 — roll out the phase-invariant to A/B + reconcile drift + health backstop

The "every task ↔ a defined pipeline phase" enforcement (schema `_pipeline_phase_keys` reject +
orchestrator `create-task` reject + `phase_link_check.py`) had been built/verified in project-C + the skill
but deliberately NOT synced to A/B (they had pre-existing phase drift). Per the handoff
(`PHASE_INVARIANT_HANDOFF_01_04.md`), reconciled the drift, then rolled out. Suite → 478.

- **Reconciled (best-effort remap to CURRENT phases, git-backed + audit-logged):**
  - **04** — 124 historical `qa-passed` orphans from a superseded scheme (`R1–R6`, bare `0,1,3,6,7,9`)
    remapped per task-title semantics: `0/1/3/R1/R2/R3 → F0` (foundation: env/data/benchmark/method),
    `R4 → P3b` (baselines), `R5 → P5` (scale-up capstone), `6/9/R6 → W` (manuscript), `7 → D`
    ("Phase-D prep"). Distribution F0=60, W=47, P3b=11, P5=5, D=1.
  - **01** — 1 orphan `task-feecb0fa` (`phase="debt"`, a "DEBT-1 … not RAG" task) → `sync`.
  - Audit JSON written to each `.fleet/status/phase_remap_audit_*.json` (reversible).
- **Synced** `schema.py` + `orchestrator.py` + `phase_link_check.py` from skill → 01/04 (Fix B
  `independent=_content_task` confirmed intact — no regression).
- **Health backstop**: `fleet_health.check_health` now emits an `orphan_phase` alert when a project
  with a pipeline has a task whose phase isn't defined (defense-in-depth on top of the create-time
  reject; NO-OP where no `phases.json`). Gate `test_phase_health_alert.py` (4). Synced `fleet_health.py`
  to A/B/C. Registered `phase_link_check.py` in `RUNTIME_SCRIPTS` (future inits deploy it).
- **Verified**: `phase_link_check` → "no orphans" on A/B/C; `py_compile` clean across the stack.

## 2026-06-21 — detached jobs are first-class on the kanban (per-unit cards)

project-C's leader hit it head-on: a long DETACHED run (a multi-day experiment sweep via detach_run.py) is NOT
a queue task, so its per-unit progress was invisible on the board — the human's global-oversight surface
had a real hole, and "running unattended" silently meant "untracked." Verifier-first; suite 463 → 465.

- **`kanban_hub.collect` now renders `.fleet/status/board_cards.json`** — a detached job writes one card
  per unit (`{id,title,phase,status: pending|running|done|failed}`) and the hub renders each as a card in
  its phase (running→in-progress, done→completed, failed→failed, pending→pending). These cards live ONLY in
  the in-memory board — the on-disk queue is **untouched**, so the watchers + no-LLM caretaker (which read
  the queue dirs) never see them and cannot interfere. General mechanism for ANY detached batch job.
- The job keeps `board_cards.json` current (writes at start + each unit transition) → the board shows
  per-unit progress in REAL TIME, not just "phase active." No missing/lagging status.
- Gate `test_detached_board_cards.py` (+2: per-unit render across all four states with the queue left
  empty; no-file no-op). Full suite 465 passed. `kanban_hub.py` synced byte-identical to skill/A/B/C.
- ALSO FIXED (footgun surfaced here): `active_when` only honored `process_alive` and *silently dropped*
  `file_exists`/`count` (asymmetric with `done_when`) — so a `file_exists` active_when looked broken with
  no error. Now symmetric: `active_when` accepts `process_alive | file_exists | count` (the latter two via
  the same evaluator as `done_when`), and an unsupported type WARNS once per (phase,type) instead of
  silent-dropping. Gate `test_active_when_types.py` (+5). Suite 465 → 470; `kanban_hub.py` re-synced
  byte-identical to skill/A/B/C and the global hub restarted to load it.

## 2026-06-19 — review tasks exempt from the groundedness grader (closes the true-leader path)

project-C's leader hit this during overnight review-QA: `cmd_qa_pass` auto-arms the groundedness grader
for the fabrication-prone content types (P16), but the hardcoded list lumped `review` in with
`research`/`write`. A `review` is a CRITIQUE, not a grounded-claims deliverable — it is
legitimately adversarial (FAIL/CONCERN findings) and may cite its own reproduction run that no
static source can ground — so the groundedness rubric FALSE-BOUNCES legitimate PR4 findings into
retry churn (observed: 6 consumed reviews bounced into 6 retries). Fix A & B fixed the FALLBACK
path (supervisor defers content); this closes the TRUE-LEADER path, which Fix B left "unchanged".
Verifier-first; suite 461 → 463.

- **`cmd_qa_pass`: split the conflated `_content_task`.** `_content_task` (research/write/review)
  still drives the fallback-deferral — a review is STILL deferred to the true leader when the fleet
  is in fallback. New `_grounded_task` (research/write ONLY) drives the groundedness grader. A
  review is NEVER groundedness-graded, even under explicit `FLEET_GRADER=1` (the rubric never fits a
  critique); its quality is judged by the true leader who consumes it (semantic QA = leader's).
- Net rule: groundedness grader ⊆ {research, write}; review = leader-judged + fallback-deferred +
  floor-passed; code/test = floor-only unless opt-in. `resolve_grader_model` and the fallback-defer
  are unchanged (review stays `_content_task`).
- Gate: `test_p16_antifab.py` +2 (review NOT grader-armed & passes-on-floor; review exempt under
  `FLEET_GRADER=1`); docstring corrected. Full suite 463 passed. `orchestrator.py` synced
  byte-identical to skill/A/B/C.

## 2026-06-18 — Fix C: scope the supervisor + heartbeat to UNATTENDED mode (closes the arm window)

project-C's leader found the arm gap: the supervisor was launched at fleet-init (`start.sh`) but the
heartbeat is armed later by the leader → a startup window where the supervisor false-judges the
live leader "dead" and runs in parallel. Root cause: the supervisor and the heartbeat had
different lifecycles. After a multi-turn design debate, the fix scopes BOTH to autonomous
(unattended) mode and co-launches them in one action — no window by construction. Suite 457 → 461.

- **`start.sh` no longer launches the supervisor by default** (`FLEET_SUPERVISOR:-1` → `:-0`). An
  ATTENDED fleet runs NO parallel QA actor. Opt in for a pure-headless `start.sh` deployment with
  `FLEET_SUPERVISOR=1`.
- **New `autonomous.sh on|off`** — run from the leader session. `on` co-launches, in one action:
  `AUTONOMOUS_ON` + a leader-heartbeat stamper **watching the leader pid** (found by walking the
  process tree for the claude binary — verified `start.sh`'s parent IS the claude process;
  `FLEET_LEADER_PID` overrides) + the supervisor loop (detached, idempotent, survives the leader).
  `off` tears both down. No window: never a supervisor up without a heartbeat.
- Net behavior: leader alive → heartbeat fresh → supervisor stands down (Fix A); leader dies
  mid-run → heartbeat stale → detached supervisor takes over, defers semantic+science (Fix B);
  attended → neither runs. Pure-headless (no session) → launchd template runs the supervisor
  directly (no heartbeat → runs). `leader_heartbeat.sh` unchanged (already pid-watching).
- Gate `test_autonomous_arm.py` (4, behavioral: co-launch, off-teardown, stamper-dies-with-leader,
  registration). SKILL.md continuity SOP + matrix rows updated. `autonomous.sh` in
  RUNTIME_SCRIPTS+EXECUTABLE; synced `start.sh`/`autonomous.sh` to A/B/C.

## 2026-06-18 — supervisor ↔ true-leader: stand-down + science-deferral (Fix A & B)

Resolved a real contradiction (caught during project-C's overnight run): the headless supervisor does
FULL semantic+science QA (its prompt: "QA EVERY completed task … qa-pass/qa-fail") with NO
leader-presence check, so it ran in parallel with the live autonomous leader — a less-contextual
second authority making terminal science verdicts. Meanwhile the no-LLM caretaker already
correctly DEFERS content (`floor_decision` → `defer` = "leave for the leader"). The supervisor's
philosophy was the inconsistent one. Verifier-first; suite 448 → 457.

- **Fix A — leader-presence gate.** `supervisor_pass.sh` now STANDS DOWN at the top when a fresh
  `.fleet/status/leader.heartbeat` exists (TTL `FLEET_LEADER_TTL`, default 1800s): the supervisor
  is the FALLBACK for leader-*absence*, not a parallel QA. Stale/absent → it runs (net intact).
  New `leader_heartbeat.sh` stamps the heartbeat (watch a pid + AUTONOMOUS_ON; or the autonomous
  loop touches it each iteration). Fail-SAFE: a missed stamp degrades to today's behavior, never a
  suppressed-while-dead net. Gate `test_supervisor_leader_gate.py` (4, behavioral).
- **Fix B — fallback defers science.** When it DOES run (`FLEET_FALLBACK_QA=1`, set by
  supervisor_pass.sh), `cmd_qa_pass` DEFERS content (research/write/review) deliverables to the
  true leader (QA-DEFER, leaves them in completed) instead of a terminal verdict — UNLESS
  mechanically predicate-defensible (floor passes), in which case it passes WITHOUT the grader
  (we don't trust the fallback's semantic verdict). Code/test + true-leader paths unchanged.
  Supervisor prompt step 2 updated to match. Gate `test_supervisor_fallback_defer.py` (5).

Net rule: **semantic+science QA is the true leader's exclusive domain, always**; the supervisor
keeps only the mechanical fleet alive until the leader returns. `leader_heartbeat.sh` registered
in RUNTIME_SCRIPTS+EXECUTABLE; synced supervisor_pass.sh/orchestrator.py/leader_heartbeat.sh to
A/B/C. Immediate overnight option (no code): start with `FLEET_SUPERVISOR=0` to keep the
supervisor off entirely while the leader does QA prompt-free.

## 2026-06-17 — Phase 5: leader-authored `phases.json` on an agnostic scaffold

Closes the unseeded-manifest gap (project-C showed no pipeline because nothing AUTHORS `phases.json`).
The split: fleet **init scaffolds** a mission-agnostic stub; the mission-aware **leader is the
SOLE author** of the phase definitions; `derive_phases.py` only syncs *status*. Verifier-first,
each sub-phase RED→GREEN; suite 428 → 448.

- **5.1 `scripts/phases.py`** (new, agnostic): `init_manifest` (idempotent stub, no-clobber) ·
  `set_phases`/`mark_no_pipeline` (state machine `awaiting_definition → defined | no_pipeline`) ·
  `effective_state` (back-compat: a manifest with no `state` field = `defined` if it has phases) ·
  `validate` (empty stub valid). Gate `test_phases_manifest.py` (7).
- **5.2 init** scaffolds `{"state":"awaiting_definition","phases":[]}` (idempotent — never
  clobbers a filled manifest); `phases.py` added to `RUNTIME_SCRIPTS`. Gate `test_phases_init_wiring.py` (3).
- **5.3 kanban** `collect` surfaces `phase_state`; `renderProgress` shows `⏳ awaiting leader
  definition` (awaiting), the collapsible pipeline (defined), nothing (no_pipeline), or the legacy
  by-phase view (no manifest file). Back-compat verified against 04/01. Gate `test_phases_kanban_state.py` (5).
- **5.4 nudge** `fleet_health` emits `phases_undefined` when `awaiting_definition` AND
  phase-tagged tasks exist (never for `defined`/`no_pipeline`/untagged; surfaces, never blocks).
  Gate `test_phases_nudge.py` (4).
- **5.5 SOP** in SKILL.md (+ a matching note in ai-research-pipeline): leader is SOLE author;
  a pipeline provides **only a template** the leader tailors; the pipeline never writes
  `phases.json`; `derive_phases` syncs status only. Gate `test_phases_sop.py`.
- **5.6 DROPPED per the user** — no pipeline-side generator script; the pipeline only *provides* the
  template, the leader writes.

Synced `phases.py`/`kanban_hub.py`/`fleet_health.py` to A/B/C; hub restarted; served PAGE
shows the awaiting banner; 04 reads `defined`. `init_workspace.py`/SKILL.md are skill-only.

## 2026-06-17 — kanban: collapsible phase pipeline

A long pipeline (e.g. 04's 14 phases) wrapped the `#progress` flex bar and pushed the task
board below the fold. `renderProgress` now renders the phase pipeline as a COLLAPSIBLE section:
a clickable `.phasetoggle` header, auto-collapsed by default when `phases.length > 6` (small
pipelines stay expanded; the no-manifest "by phase" fallback is unchanged). The collapsed
header summarizes the ACTIVE phase + progress (`▸ <title> — ▶ P<id> <name> · done/total`, or
`✓ all done` / `· next P<id>`). State persists per-project in `localStorage` (`fleetPhases:<id>`)
and is re-read each poll (the board re-renders every ~2s); the toggle re-renders for instant
feedback. Live task counts stay outside the collapsible block, always on the first screen.
Front-end only (no backend/data change — statuses already come from `phases_meta`); read-only
to all queues. Gate: `dev/tests/test_phase_collapse.py` (structural — JS UX verified by serving
+ visual). Full suite 428. (Q1 — project-C shows no pipeline because it has no `.fleet/phases.json`
manifest; left as-is per the user's call.)

## 2026-06-17 — fairness `held` count: nullglob made reserve agents (codex/claude) yield forever

Found via `bash -x` trace in project-C: claude/codex NEVER claimed their pinned tasks despite
free slots, gate=0, fair_slot_floor=1. ROOT CAUSE — `claim_one`'s per-project fairness branch
(claude|codex only) counted held claims with `held=$(ls "$CLAIMED"/${AGENT}--*.json | wc -l)`, but
`shopt -s nullglob` (set at claim_one top) collapses a NO-MATCH glob to nothing → bare `ls` lists
**CWD** (the project root, ~11 entries) → held=11 ≥ floor=1 → `return 1` (yield). The bug fires
precisely when held SHOULD be 0, so a reserve agent with 0 claims can NEVER start. kimi/opencode skip
the fairness branch → unaffected; an idle project never reaches `claim_one` → never exposes it (so
01/04 looked "fine, just idle"). FIX: count with `find "$CLAIMED" -maxdepth 1 -name "${AGENT}--*.json"`
(no glob expansion). Verified end-to-end (`bash -x`: held=0 → codex claims the probe). Test
`dev/tests/test_held_count_nullglob.py`. Synced scripts/ + 01 + 04 + 05. NOTE: a running watcher holds
the old code in memory — the fix applies on the next restart.

## 2026-06-17 — worktree.finalize copies back the FULL change set (not just output_file)

Fix for the P20×P12 interaction found in production (project-C Phase-3): a declared
`--write-scope` FORCES worktree isolation (P20), but `finalize()` copied only `output_file`
back to root (P12), stranding sibling files (e.g. the `test_*.py`) on the `fleet/<id>` branch.
A pytest acceptance-predicate run at root then failed (test missing) → `qa_floor.evaluate`
fail → the no-LLM caretaker sweep auto-qa-FAILED good multi-file deliverables. `finalize()`
now copies every committed changed file back (single writer per worktree → all changes are
this task's deliverable; output_file still included). Verified live (C2/M1/B1 module+sibling-
test both land at root, predicates pass) + regression `dev/tests/test_worktree_copyback.py`.
Synced scripts/ + project-A + project-B + project-C.

## 2026-06-17 — pipeline↔fleet boundary refactor (Phases 0–4, gated, verifier-first)

Cleaned the seam between `ai-research-pipeline` (research methodology = gate DEFINITION) and
this fleet (execution = gate ENFORCEMENT). Guiding rule: the fleet stays MISSION-AGNOSTIC —
it never learns research config; the pipeline emits predicates and the fleet enforces them.
Each phase shipped a RED gate first, implemented to GREEN, no test weakened. Fleet suite
413 → 420; pipeline suite (in ai-research-pipeline/scripts) 20 green.

FLEET-SIDE changes (synced to project-A, project-B, project-C):
- **Phase 2 — grader independence + quota-tolerant fallback.**
  `grader.resolve_grader_model(content_task)`: content tasks (research/write/review) default
  to a NON-leader judge (codex; FLEET_GRADER_MODEL overrides to kimi|opencode), since a
  same-model (claude) second pass isn't an independent honesty check. `grade(..., model=,
  independent=)` runs a model-pinned fallback CHAIN (`_grader_chain` → `_run_chain`): for
  content tasks the pool is codex → kimi → opencode (leader EXCLUDED), and `_is_verdict`
  skips any model whose reply isn't a parseable `{"ok":...}` verdict — so a codex quota stub
  (exit-0 usage-limit message) falls through to kimi/opencode instead of hard-stopping. The
  ACTUAL judging model is recorded; if all independent judges are down the grade fails CLOSED
  (bounce, never the leader, never rubber-stamp). `cmd_qa_pass` pins `grader.model` into
  `verdict.json`. Gate: `dev/tests/test_grader_independence.py` (8).
- **Phase 3 — attended phase transitions.** `doctor._crosses_phase_boundary` + the
  `FLEET_AUTO_PHASE` guard in BOTH release paths (`resolve_dependencies`, `release_dependents`):
  a satisfied `phase:<id>` boundary dep is HELD by default (the leader/human advances the
  phase, preserving the pipeline's G2/G3 checkpoints); concrete intra-phase task-id deps still
  auto-release. `FLEET_AUTO_PHASE=1` opts into autonomous crossing. Gate:
  `dev/tests/test_attended_phase.py` (2). Updated the pre-existing
  `test_dependencies.py::TestPhaseSugar` to assert the new default (held) + release under the
  env — an approved behavior change, not a weakening.
- The `scalar` predicate already reads a named JSON `source`, so the Phase-1 seam needed NO
  fleet-engine change.

PIPELINE-SIDE (in ~/.claude/skills/ai-research-pipeline — global skill, not per-project):
- **Phase 0** — reworded the "no subagent" rule to distinguish inline `Task()` (banned) from
  external fleet workers (allowed when a fleet runs; visible on kanban + durable verdict.json).
- **Phase 1** — `scripts/gate_predicates.py`: translates project-config.yaml thresholds into
  fleet `scalar` predicates (numeric only; boolean/judgment gates stay inline). The seam that
  makes "pipeline defines, fleet enforces" real while config stays single-source-of-truth.
- **Phase 4** — `scripts/fleet_detect.py` (`fleet_available()`): delegate to fleet workers only
  when a fleet is RUNNING (live pidfile), else run purely inline (graceful, never hard-fail);
  plus a SKILL.md delegation map (mechanizable→fleet, judgment→inline).

## 2026-06-17 — kanban alerts get an age window (resolved alerts age out)

Bug surfaced while debugging a stale `caretaker_dead` chip: `collect_overview` rendered the
last 20 lines of `alerts.jsonl` with NO time filter, and `emit_alerts` only ever appends —
it never clears resolved conditions. So a caretaker that came back up left its alert on the
board for 95 min. Root-cause is a producer/consumer seam, so both sides were checked:
`fleet_health.emit_alerts` re-emits every LIVE condition each tick with a fresh `ts` (no
dedup), and the health loop ticks every `FLEET_HEALTH_INTERVAL` (120s). Given that, the fix
is an age window at render: `collect_overview` now drops any alert whose `ts` is older than
`FLEET_ALERT_TTL` (default `3 × FLEET_HEALTH_INTERVAL` = 360s), reads a 500-line tail (a
chatty live alert can emit many lines/window; the client collapses identical (type,detail)
into one ×count chip), and caps the payload at 50. A live alert keeps a fresh `ts` and
survives; a resolved one stops being re-emitted and ages out. Malformed `FLEET_ALERT_TTL`
falls back to 360 (never blanks the board); a record with no `ts` is treated as ancient.
Gate: `dev/tests/test_alert_age_window.py` (4 tests, behavioral — calls `collect_overview`
against an isolated tmp `FLEET_HOME`). Suite 409 → 413. Synced kanban_hub.py to both projects.
KNOWN LIMITATION: if the health loop itself dies, live alerts stop being re-emitted and age
out within the TTL — the board goes quiet. Pull-based monitoring can't watch its own watcher;
that needs the launchd supervisor (out of scope here).

NOTE (operational): the kanban hub is a GLOBAL singleton. A per-project `stop.sh`/`start.sh`
does NOT restart it (start.sh only launches it if not already running; stop.sh leaves it up
for other projects). To load a `kanban_hub.py` change: `stop.sh --all && start.sh`, or restart
the hub directly. The `capacity_loop`/`health_loop` re-`python3` their logic each tick, so they
pick up code changes on their own — only the hub holds Python in memory.

## 2026-06-17 — opencode pinned to glm-5.2

GLM-5.2 shipped. The watcher launched `opencode run` with NO `--model`, so it fell to
opencode's config default — verified empirically as `zhipuai-coding-plan/glm-5.1` (the
agent self-reported "build · glm-5.1"). Pinned the worker to GLM-5.2 explicitly at the
launch seam: `watcher.sh:186` now passes `--model "${OPENCODE_MODEL:-zhipuai-coding-plan/glm-5.2}"`
(overridable by env, pinned by default — mirrors the `claude` worker idiom). Verified the
flag flips the self-report to "build · glm-5.2". Kept the same fact in sync everywhere it
appears: `grader.py` opencode runner, `kanban_hub.py` role label, `watcher.sh` header
comment, SKILL.md description + agent table. Synced watcher.sh/grader.py/kanban_hub.py to
both live projects (project-A, project-B).

## 2026-06-17 — P19/P20: strip the Claude token ESTIMATE; finish #1/#2/#4

Per the user: Claude has no token meter, so the whole Claude spend-estimate subsystem was
useless — removed it, kept only the real codex telemetry; and completed the three
half-built capabilities. Full suite 409; 7 runtime files synced both projects.

**P19 — strip Claude spend estimation (#3):** removed `record_spend`, `pool_used`,
`pool_alerts`, `project_spend`, `rotate_spend`, `_read_spend`, `_pool_gate`,
`CLAUDE_POOL_ROLES`/`CLAUDE_GATED`, the `POOL_*` ceilings and `spend.jsonl`; the worker
log-bytes/4 feed (watcher), the leader 12000-constant feed (supervisor), the
`emit_pool_alerts` loop tick, the hub `pool`/`by_project` collection+render, and the gc
spend rotation. `gate_level` no longer consults any pool. KEPT: codex rollout telemetry
gate, reactive bump/drain, `fair_slot_floor`. Deleted the obsolete pool/spend test classes
across 8 files; added `test_p19_codex_only_economy` asserting the machinery is gone and the
real signals still work.

**P20 — finish the half-built three:**
- **#1 autonomous worktree merge:** `cmd_qa_pass` now merges the task's `fleet/<id>` branch
  on a pass (covers the no-LLM sweep, which shells qa-pass); conflict → abort+keep+alert
  (P16). No more human-only `merge-task`.
- **#2 predicate producer:** the supervisor-pass prompt now instructs the leader to attach
  `--predicate` (machine-checkable acceptance) so the no-LLM auto-pass can actually fire.
- **#4 write-scope enforced:** a task that declares `write_scope` now FORCES worktree
  isolation → `changed_files` is accurate → reconcile HARD-fails at qa-pass (was advisory).

## 2026-06-16 — P18b: fix the sweep_qa_floor doc-drift (overstatement)

The `sweep_qa_floor` docstring still claimed "NEVER auto-PASSES" while the code has
auto-passed predicate-satisfied tasks since P10. Corrected the docstring to the real
fail/pass/defer (`floor_decision`) behavior. Synced; suite 405. (The P16 kanban_hub
null-byte was already fixed at the time — verified 0 null bytes in skill + both projects.)

## 2026-06-16 — P18: leader model = claude-opus-4-8 (fable-5 unavailable/too costly)

`capacity.LEADER_MODEL` `claude-fable-5` → **`claude-opus-4-8`**; updated the 4 leader test
assertions and the now-stale doc references (supervisor_pass.sh header, SKILL.md routing
table / ladder section / tier diagram). Dated CHANGELOG history left as-is. Full suite 405;
`pick claude-lead` → `claude-opus-4-8` confirmed in both live projects.

## 2026-06-16 — P17 (/goal): the 3 items the latest eval pinned, exact scope

Strict-scope round (gate-first, grep-all-callers, full suite 405 passing, synced). The
last eval's confirmed dead code + two real overstatements.

- **#6 — removed the dead `claude-lead` model ladder.** Deleted the `LEADER_MODEL_LADDER`
  indirection (rung never bumped → always rung 0 anyway); `pick(claude-lead)` now returns
  the single top model directly (override via config `leader_model`). Leader degrades by
  drain-to-reset, not a ladder — now stated in code, not a never-firing ladder. Updated
  the leader test from "ladders by rung" to "always top model". Removes the confirmed
  dead code that blocked 5/5.
- **#7 — grader judge is STRONG + content fail-CLOSED.** `_default_runner` defaults to
  `claude` (configurable `FLEET_GRADER_MODEL=claude|codex|opencode|kimi`, falls back) —
  no longer grading the highest-risk output with only opencode/kimi. For CONTENT tasks
  (research/write/review) a grader that can't run now FAILS CLOSED (bounce), not fail-open.
  (This also exposed two sloppy test mocks — `setdefault() or {...}` returned `True` not
  the dict — fixed.)
- **#4 — health_loop self-watched.** Added `health_loop` to `fleet_health.SINGLETONS` so a
  dead alert-pinger is itself flagged by the liveness check (a peer checker / the launchd
  supervisord catches its death).

## 2026-06-16 — P16: close the confirmed-fixable gaps (from the 4.0-eval review)

Implemented the items confirmed genuinely fixable after the LinkedIn/eval review (the
worktree-merge-as-inherent and retrieval-substrate items were dropped as not-real; the
token meter / re-login / semantic-truth residuals are external). New gates across
test_p16_*.py; full suite 397 passing. Every contract change grep-checked for callers +
round-trip tested.

1. **fair_slot bug fixed (my hardcode hypocrisy).** The skill's own CODE_BLOCK forbids
   hard-coded constants, yet `FLEET_TOTAL_SLOTS` was a magic `4` unaligned with the real
   cap. Now: `fair_slot_floor` never returns 0 (no permanent starvation under
   oversubscription); the CLI derives total slots from the agent's REAL
   `global_max_concurrent` (agents/<agent>.json); denominator counts only LIVE projects
   (`registry.touch`/`live_projects`, caretaker stamps last_seen).
2. **Strict teeth AUTO-DETECT (no forgotten flag).** Watcher: `track_changes` auto-on when
   a git repo; `track_tests` auto-on for software profile + pytest. `qa_floor.evaluate`
   ENFORCES a write-scope violation only under worktree isolation (accurate changed_files);
   shared-tree is advisory (no false-fail). Explicit env / FLEET_STRICT still win.
3. **Auth = reroute + alert, not 30-retry-then-FAIL.** An expired credential reroutes the
   task to a healthy fallback agent and raises an `auth_expired` alert (re-login is
   human-only); the DAG is no longer poisoned. Added `fleet_health --emit` for shell alerts.
4. **Alerts on every project tab + dedup.** `dedupAlerts`/`renderAlerts` in the hub render
   alerts on each project tab (scoped to that project), not just Overview — pull-based,
   no external channel.
5. **Worktree merge safety.** Dropped silent `-X theirs`; a real conflict aborts, KEEPS the
   branch (work preserved), and raises a `worktree_merge_conflict` alert.
6. **claude-lead ladder:** left as-is (intentional drain-to-reset behavior, tested) —
   documented "PRESENT, rarely fires"; ripping it out would break tested machinery.
7. **Anti-fab default-on for content tasks.** The grader auto-arms at `cmd_qa_pass` for
   research/write/review task types (fabrication-prone) even without FLEET_GRADER/STRICT.

## 2026-06-16 — P15: pin the WHY on the ACCEPT path (verdict sidecar)

A LinkedIn reviewer (and the eval) noted the asymmetry: a qa-FAIL pins its reason to disk
(`retry_reason` on the retry spec), but a qa-PASS recorded only the FACT — the acceptance
rationale lived in the leader's conversation and was lost to compaction. Closed:

- `cmd_qa_pass` now writes a durable `completed/qa-passed/<id>.verdict.json` sidecar:
  `{verdict, accepted_at, reason, judged_against (acceptance_criteria), predicates_enforced,
  grader {ran, ok, reasons}}`. The WHY a task was closed is now durable, symmetric with the
  reject path.
- `qa-pass --reason "<rationale>"` (optional; back-compat preserved via getattr) pins the
  leader's semantic reason; the no-LLM caretaker sweep auto-pass records its own reason
  ("no-LLM sweep — predicates satisfied, no semantic review") so an auto-accept is
  distinguishable from a reviewed one.
- `ledger` qa-pass event now carries the reason; `gc_artifacts` prunes the verdict sidecars.

4 gates in `dev/tests/test_p15_verdict.py`; full suite 381 passing.

## 2026-06-16 — P14.3: 11th eval — worktree heartbeat bug + model-ladder honesty

11th eval: 3.83 (Observability 4→3 on pull-only alerting; all others 4; the P14.2 fail-open
fixes held — not flagged). Two actionable items:

- **REAL bug (P9/P12 seam):** the stuck-sweep liveness check read `ROOT/output_file`, but
  `FLEET_WORKTREE=1` writes the output inside `.worktrees/<id>/` — so a quiet-but-writing
  worktree job got no heartbeat and could be wrongly killed. Fixed: the sweep now counts
  the freshest of `ROOT/output_file` AND `ROOT/.worktrees/<id>/output_file`. Gated.
- **Honesty:** the `claude-lead` MODEL ladder never fires in production (nothing bumps
  claude-lead; the supervisor DRAINS-to-reset then runs the TOP model on a cliff — there's
  no Claude intra-window telemetry to ladder on). Corrected the stale `supervisor_pass.sh`
  header comment and the SKILL.md matrix/ladder rows from "model ladder WIRED" to
  "PRESENT, rarely fires"; the codex EFFORT ladder is the one live spend-degradation path.

Full suite 377 passing. Known NOT-fixed (larger/by-design, reported to the user): pull-only
alert delivery (no push/email/webhook) — the reason Observability slipped to 3; registry
liveness; grader off by default on the autonomous sweep; worktree `-X theirs`/no-rebase.

## 2026-06-16 — P14.2: close the fail-open-gate class GENERALLY (10th eval)

10th eval: uniform 4.0, NO confirmed dead code on any dimension. It caught two more
instances of the same "silent fail-open on a gate" class — so this round closes the class
across ALL gate paths, not per-instance. 3 gates in `dev/tests/test_p14_failclosed.py`;
full suite 376 passing.

- **REAL bug:** `cmd_qa_pass` guarded the whole floor with `if spec:` — a missing spec →
  blanket QA-PASS with no checks and no alert. Now fail-CLOSED: bounce to qa-fail + alert.
- **Scope gate:** `doctor.claim_scope_conflict` fail-open (on a checker exception) now
  emits a `scope_gate_error` alert instead of silently dropping write-collision serialization.
- **Floor infra:** `qa_floor.evaluate` returned a silent PASS `(True, [])` if the predicates
  module couldn't import — now fail-CLOSED `(False, [...])`. (`_predicates_module()` indirection
  makes the path testable.)

After this, every "the gate couldn't run" path either fails CLOSED or fails open WITH an
alert — across `cmd_qa_pass`, `sweep_qa_floor`, `evaluate`, and the scope gate.

## 2026-06-15 — P14.1: 9th eval (uniform 4.0) — fix the one seam it caught

9th adversarial eval: **uniform 4.0 across all six dimensions** (QA moved 3→4; gain
survived falsification). It caught one genuine inconsistency in P14-C: the fail-open
alarm was added to `cmd_qa_pass` (leader path) but NOT to `doctor.sweep_qa_floor` (the
UNATTENDED caretaker path), which still did a bare `except: continue`. Fixed — the sweep
now emits a `qa_floor_error` alert on a floor-checker error, honoring the "fail-open is
NOT silent" guarantee on both paths. Gated; full suite 373 passing.

## 2026-06-15 — P14: 8th-eval findings — defaults, fail-open visibility, integration

8th eval landed 3.83 (Reliability/Parallelism/Token-economy/Generality/Observability 4,
QA 3). P14 closes the non-external findings. 11 integration gates in
`dev/tests/test_p14_defaults.py` + `test_p13_jobs`/`test_detach_run` updated; full suite
372 passing. Process note: every contract touched this round (the `detach_run._parse`
signature, the gc rotation path) was grep-checked for ALL callers FIRST and given a
round-trip test — the seam-mismatch class that caused the P6 regressions.

- **A. `detach_run.py --register-id` AUTO-REGISTERS** the launched job with the fleet, so
  the P13 recovery loop populates when you launch a long job through the fleet's own
  detacher (gives `jobs.register` a real non-CLI caller). (`_parse` now returns `(ns, cmd)`;
  the 3 existing `test_detach_run` cases were updated to the new signature.)
- **B. Unattended mode auto-enables strict teeth:** the watcher defaults `FLEET_STRICT=1`
  when `.fleet/AUTONOMOUS_ON` exists — the grader + change/test producers turn ON exactly
  for overnight runs, while attended runs stay permissive. An explicit env value still wins.
- **C. QA floor fail-open is NOT silent:** a floor-checker exception emits a `qa_floor_error`
  alert (alerts.jsonl + OS toast) instead of quietly degrading the gate to a blanket PASS.
- **D. `events.jsonl` rotation under a flock** (`ledger.rotate`) — gc previously did an
  un-flock'd RMW racing the O_APPEND ledger writers (the no-drop guarantee held for
  spend.jsonl only).
- **E. Worktree branch integration:** `worktree.merge` + `orchestrator merge-task <id>`
  merge a finished task's `fleet/<id>` branch into the current branch and prune it — the
  refactor/migration archetype's defining op, previously documented but uncalled.

## 2026-06-15 — P13: autonomous recovery of detached long jobs

`watchdog.py` already restarts ONE detached job (sole-restarter O_EXCL lock); `detach_run.py`
daemonizes — but nothing autonomous kept the WATCHDOG alive (the 7th eval's Reliability
ceiling note: "no automatic recovery of detached long jobs — operator-invoked only"). P13
adds the missing layer. New `scripts/jobs.py` + caretaker hook; 9 integration gates in
`dev/tests/test_p13_jobs.py`; full suite 363 passing.

- **Registry:** `jobs.py register/list/deregister` → `.fleet/jobs/<id>.json` (cmd, lock,
  cwd, done-predicate, crash-loop params).
- **Caretaker hook:** every `doctor --fix` tick calls `jobs.ensure_watchdogs(ROOT)`: a DONE
  job (done-predicate satisfied) is deregistered; a not-done job whose watchdog PID is dead
  gets its watchdog RELAUNCHED detached (stale lock from the dead watchdog removed first).
- **Single-restarter preserved:** the watchdog's O_EXCL lock still guarantees exactly one
  LIVE watchdog, so "never two concurrent GPU jobs" holds across relaunches. Two no-LLM
  levels: caretaker→watchdog→job(`--resume`). Fail-open throughout. `jobs.py` added to
  `RUNTIME_SCRIPTS`.

## 2026-06-15 — P12: per-task git-worktree ISOLATION (built, opt-in)

The #1 remaining 5/5 lever every recent eval named ("worktree isolation documented-not-
built") is now BUILT, opt-in via `FLEET_WORKTREE=1`. New `scripts/worktree.py` + watcher
wiring; 6 integration gates in `dev/tests/test_p12_worktree.py` (real temp-git-repo tests);
full suite 354 passing. Does NOT touch running projects (default off).

- `worktree.ensure(root, task_id, context_files)` — create/reuse `.worktrees/<task_id>` on
  branch `fleet/<task_id>` off HEAD; copy declared `context_files` in (visible even if
  uncommitted); return the path to run in.
- `worktree.finalize(root, task_id, output_file, status, context_files)` — on COMPLETED:
  copy the deliverable back to root (QA floor + DAG unchanged), commit the branch for the
  leader to merge, return the ACCURATE `changed_files` (single writer per worktree → no
  cross-task leakage, excluding the copied context files), remove the worktree.
- watcher: when `FLEET_WORKTREE=1`, runs the agent (and the pytest count probes) with cwd =
  the worktree; adopts the worktree's `changed_files`. Fail-open: non-git tree / git error
  → runs at root with no isolation. `worktree.py` added to `RUNTIME_SCRIPTS`.
- This makes `FLEET_WORKTREE=1` the precise mode for `write_scope` reconcile (the P8
  shared-tree leakage caveat is gone under isolation).

## 2026-06-15 — P11: one-flag strict posture (7th-eval cross-cutting finding)

The 7th eval's cross-cutting note: "defaults ship the system in its WEAKEST posture"
(grader + change/test producers all off). P11 adds ONE umbrella switch — `FLEET_STRICT=1`
— that turns the strict QA producers/gates on together (the unattended-trust posture),
leaving the safe permissive default unchanged. Integration-gated in
`dev/tests/test_p11_strict.py`; full suite 348 passing.

- `cmd_qa_pass` runs the grader when `FLEET_STRICT=1` (not only `FLEET_GRADER=1`).
- `watcher.sh` defaults `FLEET_TRACK_CHANGES` + `FLEET_TRACK_TESTS` to `FLEET_STRICT` (each
  still individually overridable).

(Note: the 8th adversarial eval this round was quota-blocked — all agents hit the Claude
session limit, exactly the shared-pool cliff P10's `pool_drained` alarm now flags. P11 is
verified by the local integration suite; the adversarial re-score is deferred to reset.)

## 2026-06-15 — P10: 7th-eval findings — lift the lone-3 Token-economy + autonomy

7th adversarial eval scored **4.0** (Reliability 5; Parallelism/QA/Generality/Observability
4; Token-economy held at 3). P10 targets the lone 3 + the autonomy gap. Integration-gated
in `dev/tests/test_p10_economy.py`; full suite 345 passing.

- **Shared-pool overspend is no longer SILENT (Token-economy).** `capacity.pool_alerts`
  raises `pool_soft`/`pool_drained` when the Claude pool crosses soft/hard; the global
  `capacity_loop` emits it to `alerts.jsonl` every tick (`emit_pool_alerts`). The pool is
  still estimate-driven (no Claude-side meter exists) — but a drain now ALARMS.
- **No-LLM QA PASS path (QA + autonomy).** `doctor.floor_decision` classifies a completed
  task: `fail` (floor violation → auto-qa-fail), `pass` (floor-clean AND declared
  `acceptance_predicates` all hold → **auto-qa-pass, advancing the DAG with no live
  leader**), or `defer` (clean but no machine acceptance → leave for the leader/grader).
  `sweep_qa_floor` acts on all three. Removes the "DAG stalls when the leader is drained".
- **profiles COMPOSE (Generality).** Discipline blocks no longer lost to branch ordering:
  a research-project CODE task now keeps the engineering block AND anti-fab; a data-project
  WRITEUP gets the numbers anti-fabrication clause (was plain anti-fab).
- **Honesty.** Removed the dead `PushNotification` escalation claim from `fleet_health.py`
  / `health_loop.sh` comments — a script cannot push; surfacing is the hub banner + OS
  toast (pull), and that is now what the docs say.

## 2026-06-15 — P9: 6th-eval findings — give QA deterministic teeth (3.83→target 4.0)

6th adversarial eval scored **3.83** (up from 3.5; five of six dimensions at 4, QA the
lone 3). P9 targets QA + the real bugs it surfaced. Integration-gated in
`dev/tests/test_p9_qa_teeth.py`; full suite 334 passing.

- **Deterministic QA firing (the QA lever).** QA's mechanical floor previously fired ONLY
  via the LLM supervisor's prose prompt — a task it skipped (e.g. during a leader drain)
  sat un-QA'd forever. New `doctor.sweep_qa_floor` runs the floor over `completed/` every
  caretaker tick (NO LLM) and auto-qa-fails floor-violating deliverables; never
  auto-PASSES (semantic acceptance stays with the grader/leader).
- **ONE shared floor.** Extracted `qa_floor.evaluate(spec, root, result)` (artifact +
  predicates + write-scope reconcile + test-count); `cmd_qa_pass` and the sweep both call
  it, so the mechanical teeth are identical whoever fires them.
- **`test_count_grew` WIRED (kills the dead-code-blocks-5/5 objection).** The floor fails
  a code/test task whose collected test count didn't grow, when the watcher reports it
  (producer: `pytest --co` before/after, opt-in `FLEET_TRACK_TESTS=1`).
- **Pool key consistency.** `supervisor_pass` now records leader spend under the SAME
  registry project id as the worker (was bare basename → split each project's attribution
  into two keys); the hub now renders `pool.by_project`.
- **`spend.jsonl` rotation under the capacity lock** (`capacity.rotate_spend`) — gc
  previously did an un-flock'd read-modify-write racing the locked appender (dropped spend).
- **Stuck-sweep liveness = log mtime OR `output_file` mtime** — a long-quiet but actively
  writing ETL/ML job is no longer killed (matches the SKILL.md claim that was false).

## 2026-06-15 — P8: 5th-eval findings — two cross-project SAFETY bugs + precision

5th adversarial eval scored **3.5** (up from 3.0; the P7 gains were independently
re-verified as real). It surfaced two latent bugs that violate the headline
multi-project-safety invariant — fixed first — plus precision/wiring gaps. All
integration-gated in `dev/tests/test_p8_safety.py`; full suite 322 passing.

- **SAFETY — cross-project SIGKILL.** `doctor._kill_match` anchored on `claimed/<agent>--
  <id>.json` with no project constraint, and `_kill_task_children` did a machine-global
  `pgrep -fl "claimed/"` — so project A's stuck-sweep could kill project B's worker with
  the same task_id. Both now anchor on THIS project's ABSOLUTE claimed path. Gated
  (`test_other_project_same_task_id_not_matched`).
- **SAFETY — hub bare pgrep.** `kanban_hub._process_alive` ran a bare `pgrep -f match`,
  lighting a phase from another project's identical process (violating the skill's own
  "no bare pgrep" rule). Now `pgrep -fl` + requires the matched cmdline to contain the
  project_root; `derive_phase_statuses` passes it through.
- **Write-scope verification now has a PRODUCER.** `reconcile_files` was a consumer of
  `changed_files` that nothing emitted (dead by construction). The watcher now emits
  `changed_files` (git before/after diff, opt-in `FLEET_TRACK_CHANGES=1`) so scope QA
  actually fires. Honest caveat: accurate under worktree-isolation/single-writer.
- **Scope overlap is now PATH-SEMANTIC.** `_scopes_overlap` replaced fnmatch-literal
  (which ignored `/` boundaries → over-/under-serialization) with `**`=cross-segment,
  `*`/`?`=within-segment matching. `a/*.py` no longer collides with `a/b/c.py`;
  `src/**` correctly contains `src/auth/login.py`.
- **Fairness covers codex.** `fair_slot_floor` was claude-only; codex (also quota-scarce)
  now yields over its fair share too. kimi/opencode stay unthrottled (abundant). SKILL.md
  fairness row corrected from the overstated plain "WIRED".
- **`project_spend` wired.** Surfaced as `pool.by_project` in the hub (was def-only).

## 2026-06-15 — P7: close the STRUCTURAL gaps (integration-gated)

The 4th eval said the remaining path past 3.0 is structural, not one-line wiring. P7
implements all six, each gated at a REAL entry point in `dev/tests/test_p7_structural.py`
(19 gates, gate-zero confirmed RED first). Full suite 312 passing.

1. **Autonomous QA / leader continuity ON by default.** New `supervisor_loop.sh` loops
   the headless leader pass; `start.sh` launches it per-project (default-on, `FLEET_SUPERVISOR=0`
   to disable), `stop.sh` reaps it, registered in `RUNTIME_SCRIPTS`/`EXECUTABLE`. It's a
   plain in-shell loop (NOT launchd) so it sidesteps the macOS TCC trap under ~/Documents.
   `FLEET_PASS_TOKENS` now defaults to 12000 so the leader spend feed is live out of the box.
   → out of the box the fleet QAs its own output, releases dependents, drains quota.
2. **Shared pool surfaced.** `kanban_hub` collects `pool_used` and renders a `claude pool`
   5h/wk-vs-cap line on the overview.
3. **Grader groundedness.** `grader.grade(..., sources=...)` injects the task's
   `context_files` as the ONLY admissible support and flags ungrounded claims/numbers;
   `cmd_qa_pass` feeds them when `FLEET_GRADER=1` → anti-fabrication beyond prompt-deep.
4. **Write-collision made real.** `create-task --write-scope` writer (`write_scope` had a
   reader but no producer); claim-time enforcement via `doctor.py --claimable` (exit 1 =
   overlap → serialize; fail-open otherwise) in `watcher.sh`; `reconcile_files` is now
   glob-aware and wired into the qa-pass floor (fires when the result reports `changed_files`).
5. **Durable observability + stall detection.** `orchestrator metrics` replays the event
   ledger (the reader it never had). `check_health` now detects `qa_backlog` (completed
   piling up un-QA'd) and `stalled` (pending work, no claimed, no live watcher); deadlocks
   escalate from a log line to `alerts.jsonl` + OS notification via `fleet_health.emit_alerts`.
6. **Floor fails CLOSED.** `cmd_qa_pass` no longer swallows ALL exceptions: a predicate
   that RAISES, an artifact-check error, or an empty `output_file` now block the pass;
   fail-open is scoped to the checker-module import only. `profiles.py` gains a data/ml
   discipline block (engineering + numbers anti-fabrication) for data/ML projects.

## 2026-06-15 — 4th adversarial eval (3.0, flat) + P6 regression fixes

19-agent adversarial eval (recon → 6 dim reviews → adversarial verify-each → 5
archetype-fit → synthesis) re-verified every P6 claim against source. Verdict: upheld
**3.0** (all six dimensions 3/5), unchanged from the 3rd eval — P6's wiring was real and
re-confirmed, but it shipped TWO fresh overstatements that cancelled the gain. Both fixed
here (regression-gated in `dev/tests/test_p6_wiring.py`; full suite 293 passed):

- **BUGFIX — worker spend feed was dead-in-effect.** `watcher.sh` writes `record_spend …
  claude-worker`, but `CLAUDE_POOL_ROLES` excluded `"claude-worker"`, so `pool_used`
  dropped every worker record and the shared-pool throttle read an empty pool. Added
  `"claude-worker"` to `CLAUDE_POOL_ROLES` (capacity.py:371) + gate
  `TestWorkerSpendCounted`.
- **BUGFIX — alerts banner ignored the real alert shape.** `fleet_health` emits
  `{type, detail, ts}`; the hub's `renderOverview` only read `a.msg/a.message/a.level`,
  so real alerts fell to a raw `JSON.stringify`. Render `a.type · a.detail` (+ local
  time) (kanban_hub.py) + gate `TestHubRendersRealAlertShape`.
- **DOC honesty.** Corrected SKILL.md: `qa_floor.test_count_grew`/`reconcile_files` are
  available helpers NOT on the default floor (only `artifact_ok` is wired); added a
  residual-risks note for the default-install gaps the eval flagged (no autonomous QA /
  supervisor loop by default; `project_spend`/`ledger.read` uncalled; `write_scope` has a
  reader but no `create-task` writer flag → collision is `output_file`-equality only).

Remaining gaps holding the score at 3.0 are now STRUCTURAL (default-off supervisor loop,
prompt-deep anti-fabrication, source-blind grader, coarse write-scope, no durable ledger
reader / stall detection) — tracked as the P7 backlog in dev/HARDENING_PLAN.md.

## 2026-06-15 — P6: finish the wiring + fix 3rd-eval bugs (integration-gated)

Closes the second layer of read-wired-but-write-starved / dead capabilities surfaced by
the 3rd adversarial eval (3.0). All changes gated by `dev/tests/test_p6_wiring.py` at the
real entry points; full suite 290 passed.

- **Predicate reachability (was NOT REACHABLE → WIRED):** `acceptance_predicates` is now a
  real `TaskSpec` field; `orchestrator create-task --predicate '<json>'` persists it; the
  `cmd_qa_pass` floor (wired P5) finally has predicates to enforce.
- **Stuck/orphan counter split:** orphan requeues bump `orphan_count` (cap `MAX_REQUEUE`),
  stuck requeues bump `stuck_count` (cap `MAX_STUCK`) — independent, so a restart-orphaned
  task is no longer failed prematurely on its first genuine stuck event. Two pre-existing
  tests that asserted the old merged behavior were corrected (not weakened) to the split.
- **Audit-log GC:** `gc_artifacts` now rotates/prunes `spend.jsonl` / `events.jsonl` /
  `alerts.jsonl` (drop if stale, else trim to last 5000 lines) — previously unbounded.
  `_read_spend` is tail-bounded (last 20k lines) so the hot pre-claim path stays cheap.
- **`release_dependents` takes the per-project lock** (cross-process safety on the qa-pass
  fast path, concurrent with the caretaker's resolve); fail-open if held.
- **Grader fail-CLOSED + opt-in:** `grader._parse` no longer rubber-stamps a bare
  `YES`/`PASS`/prose — only a valid JSON verdict with `ok=true` passes. `cmd_qa_pass` runs
  the grader ON TOP of the floor only when `FLEET_GRADER=1` (fail-open on infra error).
- **Per-project fairness + spend feed at claim (was DEAD → WIRED):** the claude worker
  consults `capacity.py fair_slot_floor <project>` before claiming and yields when it
  already holds its fair share of the shared Claude pool; on completion it feeds an
  estimated spend via `capacity.py record_spend` so `pool_used`/`gate_level` see worker
  draw, not just leader passes. New `registry.py id --root` for the stable project id.
- **Hub renders alerts:** the overview now shows a `fleet alerts` banner from
  `d.alerts` (collected from `~/.fleet/alerts.jsonl`) instead of leaving it file-only.

## 2026-06-13 — Stuck-detection BUGFIX: stale claim-mtime false-kill

**Same-day regression in the stuck-detection above.** First cut of `check_stuck_claims`
used `ref = log.stat().st_mtime if log.exists() else f.stat().st_mtime` — falling back
to the CLAIM-FILE mtime when no log yet. But `mv` preserves mtime, so a draft held ~56min
then promoted+claimed carries an ANCIENT claim mtime; in the window before the fresh
worker flushes its first log line, the fallback saw "frozen 56min > grace" and SIGKILLed
two actively-working tasks (S1 mid-NotebookLM-audit with a 58KB log, S2 mid-refactor).
The caretaker re-ran it every 60s → churn (task appearing in completed+failed at once).

**Fix:** heartbeat is the WORKER LOG mtime ONLY. `if not log.exists(): continue` (no log =
worker spinning up → never kill); never fall back to the claim mtime. A genuinely hung
task still has its banner log (frozen mtime) and is still caught; a starting/working task
is never killed. Synced to skill + project. Lesson: when a freshness heuristic can fall
back to an `mv`-preserved mtime, it WILL eventually read as ancient — pin the heuristic to
a signal the live process actually writes (the log), not to file metadata.

## 2026-06-13 — Stuck-task detection (hung child under a live watcher)

**Problem:** two `opencode` tasks sat "in progress" for 24min with frozen logs
(banner-only) and zero file output — hung children blocked on an unreachable backend.
NEITHER recovery path caught them: (a) `wait` sentinels fire only on a TERMINAL state,
which a hung task never reaches → sentinel waits to timeout; (b) `doctor.check_orphaned_claims`
only requeues when the AGENT (watcher) is DEAD — here the watcher was alive, only its
child hung, so the claim was deemed legitimate forever. A real gap: live-watcher +
hung-child is invisible to both.

**Fix:** `doctor.py` gains `check_stuck_claims` (+ `_kill_task_children`): a claim under
a LIVE watcher whose worker-log (`status/logs/task-<id>.log`) has been FROZEN past
`--stuck-grace` (default 900s, far beyond any single LLM call) is a hung child → kill it
(pgrep the claim path), requeue with `stuck_count`+1, and give up to `failed` after
`MAX_STUCK`=3 (so a persistently-down backend can't loop forever). Wired into `main()`
+ the module docstring (check 2b); the caretaker (runs `doctor --fix` every 60s) now
self-heals stuck claims with no LLM. SKILL.md: continuity-tier table + supervisor-pass
routine gain a "stuck-task sweep" step distinguishing COMPLETION (sentinels) from
STUCKNESS (progress/heartbeat), with the root-cause note that an all-workers-hang means
the backend is unreachable (VPN tug-of-war: a VPN that fixes Groq/NotebookLM can block
China-based GLM/Kimi) — stop + surface, don't churn.

Verified: syntax-checked, dry-run clean, synced to project `.fleet/doctor.py`.

## 2026-06-13 — Autonomous allowlist: read-only inspection verbs

**Problem:** during an autonomous run, `awk '{print $1}'` (and `sed`/`cut` field
extraction) surfaced an interactive permission prompt that stalled the unattended
loop — even though the guard hook was silent. Root cause is a SECOND, distinct prompt
source: Claude Code's NATIVE permission system flags `$N`/`$VAR` (`simple_expansion`),
which the guard hook (only blocks `$(...)`/backticks/redirects) does not cover, and
`PERM_ALLOW` did not list `awk`/`sed`/etc. so they prompted by default.

**Fix (both layers):**
- `init_workspace.py` `PERM_ALLOW` += `awk sed sort uniq head tail cut shasum` — the
  common read-only verification verbs, so prefix-matched invocations don't prompt.
- `SKILL.md` prompt-free table gains an `awk '{print $N}'` row + a "two distinct
  prompt sources" note distinguishing the guard hook from the native permission check;
  guidance: for inspection prefer `grep -oE` / the **Read tool**, never `awk $N`
  (a stray `$N` can still trip `simple_expansion` even when allowlisted).

Verified: synced to project `.fleet/`, project `settings.json` allowlist updated.

## 2026-06-11 — Fork: the multi-project FLEET version

### Why a fork
The legacy skill's data layer was already project-scoped, but its PROCESS layer was
global: `start.sh` counted watchers with a bare `pgrep -fc "watch.sh $agent"` and
`stop.sh` killed with `pkill -f "watch.sh"` / `"monitor.py"` / `"phase_deriver_loop.sh"`
— so a second project either failed to start workers or, worse, stopping one project
killed another's 12-day run. Namespaces are fully disjoint from the legacy skill
(`.fleet/` vs `.multiagent/`, `watcher.sh` vs `watch.sh`, `kanban_hub.py` vs
`monitor.py`, `phase_deriver.sh` vs `phase_deriver_loop.sh`, `qa_notify.sh` vs
`qa_event_monitor.sh`, port 8788 vs 8787) — verified: with a fleet stack running,
every legacy `pkill -f` pattern matches zero fleet processes.

### Added — multi-project process safety (P0)
- Watchers/deriver/caretaker launched by ABSOLUTE path; liveness via **pidfiles**
  (`.fleet/status/pids/`), each verified `pid alive AND cmdline matches` before
  counting (start) or killing (stop). No bare pgrep/pkill anywhere.
- `stop.sh` is project-scoped; `--all` additionally stops the global singletons.
- Verified live: two projects ran concurrently; stopping A left B + hub untouched.

### Added — global kanban hub (P1)
- `kanban_hub.py` — ONE port (8788) for ALL projects: Overview tab (per-project
  counts, hot items, global capacity line) + a full board tab per project.
- `registry.py` — `~/.fleet/projects.json` (atomic write + O_EXCL lock with stale-lock
  reaping); `start.sh` registers, `stop.sh` deregisters.

### Added — token-capacity-aware scheduling (P2)
- `capacity.py` — global registry `~/.fleet/capacity/<agent>.json`. codex probe
  parses `~/.codex/sessions/**/rollout-*.jsonl` `rate_limits` (REAL 5h/weekly
  used_percent + resets_at; verified against live data, including reset
  self-correction). Reactive `bump` (drain + ladder rung) for agents with no local
  quota API. Gate contract: exit 0 free / 1 soft (priority≤2 only) / 2 drained;
  **fail-open by construction** (missing file/crash reads healthy — incl. the
  `python3 <missing>` exits-2-looks-drained trap, guarded in both layers).
- `watcher.sh`: claim gate · global per-agent slots (mkdir-atomic,
  `~/.fleet/slots/`, caps N concurrent CLI calls across ALL projects) ·
  RATE_LIMIT_RE quota requeue with fallback-chain reroute (`rerouted_from` audit
  field; lame-duck semantics — in-flight work never killed) · post-codex-task probe.
- `capacity_loop.sh` — global 60s probe + drain-expiry loop.

### Added — ladders + leader continuity (P3)
- codex reasoning-effort ladder (xhigh→high→medium) from live used%; claude WORKER
  pinned to Sonnet by design; kimi/opencode pinned best (flat-rate).
- `supervisor_pass.sh` — headless leader pass, model picked from the leader ladder
  (fable-5→opus-4.8→sonnet-4.6 by reactive rung; quota error → rung+1, exit 75).
- `caretaker.sh` + `doctor.py` — no-LLM continuity: requeue orphaned claims (dead
  watcher + grace), reap stale pidfiles, **promote drafts** when a pool runs dry.
- `orchestrator.py`: `create-task --hold` (drafts queue) + `promote` command.
- Subagent fan-out hint injected into workhorse prompts (kimi/opencode
  `subagent_fanout: true`; reserves never; `FLEET_FANOUT=0` to disable). codex
  native fan-out flags were still under development at fork time.
- launchd template (`templates/launchd/`) for reboot-proof supervisor scheduling.

### Fixed — symlink path-form orphan (found by live smoke, locked by regression test)
`bash cd && pwd` returns LOGICAL paths (`/tmp/...`) while python `Path.resolve()`
returns PHYSICAL ones (`/private/tmp/...`); doctor's substring cmdline check thus
treated a LIVE watcher's pidfile as stale, reaped it, and `stop.sh` then orphaned
the process. Fix: `pwd -P` everywhere in bash + realpath-resolved script-token
comparison in `doctor._cmd_is_watcher`. Tests:
`test_symlinked_cmdline_still_counted`, `test_other_projects_watcher_not_counted`.

### Verification (the skill's own discipline)
- 121 tests pass (`pytest scripts/ scripts/hooks/`) — legacy suites ported
  (derive_phases, detach_run, watchdog, bash-guard, init hook wiring → `.fleet`)
  + new suites: capacity (18), registry (7), doctor (13), hub collect (7).
- Live E2E: two concurrent projects; real kimi task claim→slot→CLI→completed→
  qa-pass (deliverable verified byte-exact); hub APIs served both tabs; legacy
  non-interference; project-scoped stop; clean global teardown (`--all`).

## 2026-06-11 (later) — three fixes from first real multi-project day

### Fixed — capacity display semantics (kanban Overview)
Board showed bare "5h 9%" while the codex desktop app shows REMAINING — users read
contradiction where there was none (9% USED ≡ 91% remaining). Chips now say
"5h used N% · wk used N%"; values are EFFECTIVE (reset-passed windows read 0,
expired drains cleared) so stale snapshots can't contradict the CLIs.

### Fixed — agents without telemetry looked unmonitored
claude/kimi/opencode (and claude-lead) have no local quota API by design
(reactive-only); their absence from the capacity line read as "not monitored".
Overview now renders a dim "reactive-only (no quota events yet)" placeholder per
known agent.

### Fixed — autonomous mode stalled on prompts in fresh projects (systemic)
The guard hook only BLOCKS bad patterns; it never APPROVES — approval is the
permissions layer, and a fresh project had no defaultMode/allow-list, so
"autonomous mode on" still prompted on every command (observed live on a new
project; root cause: defaultMode unset + no fleet rules). `init_workspace.py` now
installs a baseline BY CONSTRUCTION: defaultMode=acceptEdits (only if unset),
~30 allow rules (python3/fleet scripts/worker CLIs/read-only shell/git-local),
deny list (sudo, rm -rf /, git push, credential reads). Idempotent merge, never
removes/overrides existing rules; opt out with --no-perms. Regression tests:
TestPermissionsBaseline (4 tests). Suite: 125 passed.

### Added — anti-hardcoding discipline for code deliverables (user-reported, systemic)
kimi AND opencode were both observed baking magic values (thresholds, paths, sizes)
into code deliverables. One uniform rule now lands in three layers: (1) watcher.sh
build_prompt injects a MANDATORY engineering-discipline block into every code/test
task prompt — parameterize via args/flags; constants live in ONE editable place
(config file / CONSTANTS block); task-given values are defaults to wire through,
not literals to scatter; (2) SKILL.md task-spec discipline tells the leader to make
it a checkable acceptance criterion; (3) SKILL.md QA heuristics add a hard-coded-
value scan with qa-fail (never hand-patch). Locked by test_watcher_prompt.py — 8
behavioral tests running the REAL bash build_prompt against fixture tasks (also
locks the subagent fan-out hint). Suite: 133 passed. NOTE: running watchers load
build_prompt at start — the new prompt applies after their next restart.

### Added — routing discipline: comparative advantage, not partitions (user-reported)
A leader pinned 6 code tasks to opencode while kimi×3 sat idle — the best_for label
("kimi = reading") was read as a partition, halving workhorse throughput. Three-layer
fix: (1) SKILL.md routing section rewritten — default to assigned_to:any for bulk
(the atomic claim race IS the load balancer); pin only for tier necessity / QA-bounce
continuity / genuine specialty contention; count idle instances before pinning a
batch; reserve tier framed as "irreversible data + load-bearing design, tell the user
first". (2) kimi/opencode templates re-worded: GENERAL-PURPOSE workhorses, labels =
comparative advantage under contention. (3) orchestrator create-task now prints a
warn-only ROUTING ADVISORY when pinning a workhorse past its live instance count
while the peer has live instances (pidfile-liveness based; never blocks). Locked by
test_orchestrator_routing.py (7 tests). Suite: 140 passed. orchestrator.py is
per-invocation → synced copies take effect immediately, no watcher restart needed.

### Added — install_supervisor.sh: leader continuity as ONE command (user-reported gap)
A live session hit its 5h limit; after reset NOTHING resumed — the launchd template
existed but was never installed ("design intent on paper"). New installer renders +
loads the plist per project (label from project name; interval hash-staggered
1500–2099s so multi-project leaders never wake together; --interval/--status/
--remove; idempotent; LAUNCH_AGENTS_DIR + --no-load for tests). SKILL.md upgrades
"arm continuity" to a MANDATORY pre-run checklist item: launchd installer is the
answer (survives resets + closed windows; blackout passes fail cheap with rung+1),
/loop in-session is the window-open fallback. Locked by test_install_supervisor.py
(7 tests).

### Hardened — watcher slot release on SIGTERM/SIGINT
EXIT-trap-on-SIGTERM semantics vary across bash versions; explicit TERM/INT traps
now guarantee slot release under stop.sh (stale-pid reaper remains the SIGKILL
backstop). Observed during inspection as a transient stale-slot window mid-restart
(self-healed). Suite: 147 passed.

### Fixed — restart-orphaned claims invisible to doctor (found by inspection loop)
A mid-flight stack restart killed the claiming watchers; fresh same-name watchers
came up, so doctor's agent-level heuristic ("agent has live watchers → claim is
legitimate") read 3 stranded claims as held — stuck 100+ minutes, would have been
forever. Fix: watcher stamps `claimed_by_pid` on every claim (best-effort jq edit
after the atomic mv); doctor now verifies the SPECIFIC claimer pid (alive AND
cmdline is this project's watcher for that agent — realpath-checked, pid-reuse
safe) with a short 120s grace, regardless of sibling instances. Agent-level
heuristic kept as fallback for unstamped/legacy claims; stamp stripped on requeue.
Locked by TestStampedClaims (4) + test_watcher_claim.py (4 behavioral tests of the
real bash claim_one: stamp, priority order, agent filter, any-pool). Suite: 155.
NOTE: stamping starts at each watcher's next restart; doctor fix is live within
one caretaker tick (per-invocation).

### Fixed — ladder semantics were backwards at window reset (user-caught)
Wrong: a quota cliff bumped the rung, and the rung outlived the reset (5h decay),
so the FIRST post-reset pass — the moment quota is at its fullest — ran a
DOWNGRADED model. Right: reset = full quota = TOP model immediately; degradation
is strictly INTRA-window as consumption climbs (codex: real used%; claude-lead:
no intra-window telemetry yet — future spend-ledger). Changes: (1) capacity.py —
an expired drain now snaps rung to 0 in effective() and clear-expired (drain
expiry == window reset by construction); (2) supervisor_pass.sh — on a limit hit
it PARSES the reset time from the error ("resets at H:MM am/pm" / "resets HH:MM",
fallback 1800s, clamp 6h) and drains until exactly then, NO rung bump for window
cliffs; blackout ticks gate-check first and skip without an API call; (3) SKILL.md
corrected + in-session `/loop` supervisor cron promoted to an ENGAGEMENT-START
leader duty (it is what makes the interactive session itself auto-resume after a
"session limit · resets at HH:MM" cliff — observed live that nothing resumes
without it). Tests: 3 new regressions (reset→top-rung, active-drain→rung kept,
clear-expired→rung reset) + parser verified on three message formats. Suite: 158.

### Fixed — single-file prompt dogma contradicted multi-file tasks (01.R leader report)
Template line 5 ("Modify ONLY that output_file. Do not touch any other file.")
contradicted every multi-file code task and the skill's own sibling-test QA rule
(the legacy SKILL.md had already fingered it: "the single-deliverable watcher
prompt is why" codex drops tests). Production evidence cut both ways: codex
fail-stopped on the contradiction (correct behavior, burned 5% of a window on a
BLOCKER report); opencode/kimi silently ignored the line across ~10 multi-file
tasks — i.e. the template was training workhorses that instructions are
decorative. Fix: the DESCRIPTION is now the single source of truth for the
authorized file set; output_file is the primary-deliverable anchor; agents must
list every touched file in their final summary (lands in the task log); QA
checks the manifest against the actual diff (new heuristic). SKILL.md task-spec
discipline updated (multi-file tasks enumerate their file scope in the
description); the codex-omits-test heuristic rewritten for the new template.
Locked by TestFileScopeAuthorization (4 behavioral tests incl. the dogma line's
absence across all 5 task types). Suite: 162 passed. Effective per watcher at
its next restart (A-I restarted now — idle; 01.R restarts after its in-flight
batch lands, per its leader's plan, picking up claimed_by_pid stamping too).

### Added — leader wake-up visibility on the kanban (user request)
The four wake-up mechanisms run invisibly; each project tab now shows a
"leader wake-up" strip: wait sentinels ×N (attributed by absolute path OR by
--task-id membership in the project's queue — relative cmdlines disambiguated),
qa_notify (path-attributed; arming with "$PWD" is what makes it visible),
durable cron (reads .claude/scheduled_tasks.json; in-memory /loop crons are NOT
externally observable — the strip says so honestly), launchd (plist + launchctl
loaded + interval), plus two outcome-level signals: caretaker liveness and
last-supervisor-pass age (log mtime — "installed" vs "actually ran").
pgrep/launchctl results cached 10–15s against the 2s UI poll. Locked by
test_hub_continuity.py (15 tests). Suite: 177 passed. First live readout
immediately earned its keep: project-A shows sentinels ×4 + qa_notify + 
caretaker armed, but launchd NOT installed and zero supervisor passes ever run —
i.e. its post-reset auto-resume currently rides on an invisible in-memory cron.

### Documented — CronCreate durable:true silently ignored by this runtime (3× verified)
project-A's leader attempted durable supervisor crons twice (correct recipe,
CronList showed [session-only], no scheduled_tasks.json at project or user level);
the skill session reproduced independently (explicit durable:true → tool result
says "Session-only (not written to disk)"). Conclusion: this Claude Code runtime
does not honor the durable flag — in-session crons are cliff-proof (post-reset
ticks resume) but NOT REPL-restart-proof; the only restart-proof layer is launchd
(install_supervisor.sh, install-on-demand convention unchanged). Harness-level
limitation — per the self-evolution rules ("cannot patch Claude Code itself") the
skill encodes the VERIFICATION RITUAL in SKILL.md instead: check CronList's
durable/session-only marker + scheduled_tasks.json existence; never assume.

### Changed — quota-scarce vs quota-abundant terminology (user correction, refined)
"Metered/flat-rate" framing was wrong twice over: all four subscriptions are
flat-rate monthly (price is not the axis), and BOTH claude and codex are bounded
by BOTH a 5h window AND a weekly window (not "claude=5h, codex=weekly"). SKILL.md
routing table + codex/claude templates now say quota-scarce (claude+codex, dual
windows; claude pool shared with leader) vs quota-abundant (kimi/opencode). UI:
the in-memory-cron caveat moved next to the durable-cron chip it qualifies
(was dangling after caretaker); strip label renamed "leader continuity";
caretaker chip labeled "(no-LLM floor)" to stop it reading as a 5th wake-up
mechanism. Suite re-run: 177 passed.

## 2026-06-13 — Allowlist: JS/TS dev runners
PERM_ALLOW gained npx tsc/jest/tsx/vitest/prisma + npm test/run — specific safe dev runners (NOT a blanket npx *). Repeated permission prompts on `npx tsx --test` stalled autonomous passes; the baseline only had pytest. JS/TS fleet projects now run their test suites prompt-free.

## 2026-06-15 — Task-level dependency DAG (parallel-by-default scheduling)

### Added — `depends_on` DAG so parallelism scales to true dependencies, not phases
Problem: phases were being used as scheduling BARRIERS (dispatch phase-by-phase),
which over-serializes — independent tasks in different phases waited needlessly.
But the queue was never the bottleneck: `claim_one` filters only on assigned_to +
priority, so `pending/` is already greedy/parallel. The serialization was the
LEADER holding whole phases back, because the system gave it no finer way to
express ordering. Fix — make ordering a task-level DAG; phases become display /
optional-checkpoint only:
- **schema:** `depends_on: [task_id | "phase:<id>"]` (validated list-of-str).
- **orchestrator:** `--depends-on`; a task with deps is AUTO-HELD in drafts (must
  not be claimable until satisfied); creation-time warnings for self-dep and
  unknown ids (phase: refs not id-checked; forward refs allowed).
- **doctor `resolve_dependencies()`** (runs every caretaker tick, no LLM): releases
  a held draft when ALL deps are satisfied = producer QA-passed AND its output_file
  exists (never trust `completed/`). `phase:N` satisfied when that phase has ≥1
  QA-passed task and none outstanding. **Write-safety, project-agnostic:** two
  dep-ready tasks declaring the SAME output_file are auto-serialized (release one,
  hold the other) — the one ordering inferable without project knowledge. Dead deps
  (producer in failed/) and unknown ids are SURFACED, never silently held forever.
- **Guard:** `promote_drafts` (low-water) now SKIPS drafts with unsatisfied deps —
  it can no longer bypass the DAG and release blocked work early.
- **SKILL.md:** new "Parallelism — parallel by DEFAULT, serialize only by declared
  necessity" doctrine; dispatch guidance rewritten from "phase by phase" to "author
  the wave up front with --depends-on, the resolver auto-releases". Per-project risk
  posture via slot caps / worktrees (framework supplies mechanism; project supplies
  the DAG).
Locked by test_dependencies.py (17) + create-task dep tests (4). Live E2E: a phase-3
task depending only on a phase-1 producer auto-released the instant the producer was
QA-passed — cross-phase concurrency realized. schema/orchestrator/doctor are
per-invocation → synced copies effective immediately, no watcher restart.

### KNOWN PRE-EXISTING FAILURE (not from this change) — flagged for decision
`TestStampedClaims` (2 tests) fail: `watcher.sh` still STAMPS `claimed_by_pid`
(line ~245) but the current `doctor.py check_orphaned_claims` was reverted to the
agent-level version that does NOT read it. Net effect: a restart-orphaned claim
under a live same-name watcher is no longer precisely requeued (the 100-min-stuck
bug the stamp was meant to fix). Half-reverted state — left untouched pending the
user's call (the doctor.py change was flagged intentional).

### Fixed — re-activated pid-precise orphan detection (closed the half-reverted gap)
The watcher stamped `claimed_by_pid` but `doctor.py` had been reverted to an
agent-level-only `check_orphaned_claims` that ignored it — a producer with no
consumer, leaving the restart-orphan-under-live-watcher case (the 100-min-stuck
bug) uncovered and `TestStampedClaims` red. Re-added the consumer as a LAYERED
check: (1) PID-PRECISE — a claim's `claimed_by_pid` whose `ps` cmdline is not this
project's watcher for that agent (dead pid → empty cmdline; reused pid →
non-watcher cmdline) is orphaned even under live same-name watchers, after a short
STAMP_GRACE (120s, the claim→stamp write sliver); (2) AGENT-LEVEL — unchanged
fallback for unstamped/legacy claims. One `ps` probe is both liveness and identity
(reuses realpath-hardened `_cmd_is_watcher`; no os.kill). This is the
highest-confidence orphan signal and uniquely covers a worker killed BEFORE its
first log flush — which the log-freeze `check_stuck_claims` skips (logless → can't
judge). The three recovery checks are now complementary, not redundant: dead-pid
(claimer gone) · agent-level (whole agent down) · log-freeze (claimer alive but
child hung). Stamp stripped on requeue. Tests: TestStampedClaims green again + 2
new (no-log dead-stamp requeue, unstamped→agent-level fallback). Suite: 200 passed.
doctor.py is per-invocation → live on sync; stamping resumes at each watcher's next
restart (agent-level fallback covers the interim, no regression window).

## 2026-06-15 (later) — Hardening plan authored (dev/HARDENING_PLAN.md)

A 19-agent adversarial evaluation scored the skill 3/5 "functional" (QA 2/5, all
others 3/5 after adversarial tempering) for complex long-range multi-type projects.
Two findings independently VERIFIED in source: (1) retry-identity break — qa-fail
mints a new task_id + archives to completed/archive/ but never rewrites downstream
depends_on and _dep_is_dead only checks failed/, so a consumer of a QA-failed
producer waits forever (introduced 2026-06-15 with the DAG); (2) hung-child detector
dead — doctor reads `task-{tid}.log` but watcher writes `{tid}.log` (double-prefix),
so check_stuck_claims never fires. Authored the hardening plan (dev/HARDENING_PLAN.md
— kept under dev/ so it stays out of an exported pack): a strictly SEQUENTIAL,
verifier-first, scratch-verified 5-phase program (P0 verified bugs → P1 integrity
floor → P2 self-supervision+event-ledger → P3 scheduling depth → P4 QA-without-the-
human) to reach 5/5 adversarial on all six dimensions, with explicit per-phase
runnable gates, the inter-phase dependency DAG, and the design constraints
(file-based / fail-open / config-gated / per-project lock scoping / TCC-safe launchd /
QA-5-as-asymptote). NO CODE CHANGED — spec only, each phase green-lit separately.

## 2026-06-15 (P0 executed) — verified-correctness fixes, verifier-first

Ran HARDENING_PLAN P0 under the protocol (gates written FIRST, watched fail, then
fixed → green; unit-isolated + scratch E2E; per-invocation sync, no restart needed).

- **Retry-identity break FIXED (the #1 eval finding).** `cmd_qa_fail` now (a) rewrites
  every downstream draft's `depends_on` old→new id (`_rewrite_downstream_deps`) so the
  DAG and the retry loop compose — a consumer of a bounced producer no longer waits
  forever; (b) enforces a retry cap: after `MAX_QA_FAIL=3` failures the producer is
  moved to `failed/` terminal (spec + result sidecar) instead of bouncing/burning
  quota forever; carries `qa_fail_count` across the lineage (new schema field).
  `doctor._dep_is_dead` now also treats an `archive/`-superseded id as dead (safety
  net for dangling/forward refs), with a live-copy override.
- **Hung-child detector FIXED.** `doctor.check_stuck_claims` read `task-{tid}.log`
  while the watcher writes `{tid}.log` (double `task-` prefix) → log never found →
  the detector never fired. Corrected to `{tid}.log`.
- **Tests (verifier-first, all 8 failed before the fix, green after):**
  `TestStuckChildLogFilename` (×2), `TestDeadDepArchive` (×3),
  `TestQaFailDependencyIntegrity` (rewrite / retry-cap-terminal / count-carried).
  Full suite **208 passed** (was 200). Scratch E2E confirmed the full loop:
  producer qa-fail → retry → downstream `depends_on` rewired → retry qa-pass →
  consumer auto-released. Synced (schema/orchestrator/doctor) to both live projects.

## 2026-06-15 (P1 executed) — integrity floor, verifier-first via /goal

Ran HARDENING_PLAN P1 under the verifier-first protocol (gates written + confirmed
RED in test_p1_integrity.py BEFORE the fix; driven to green by the native /goal loop
against the exact stop-condition `pytest test_p1_integrity.py . hooks/`).

- **Per-project doctor lock** (`doctor.try_acquire_project_lock`/`release_project_lock`):
  O_EXCL pidfile at `MA/status/doctor.lock` with stale-reap; the `--fix` pass skips the
  tick if held. PER-PROJECT by construction (never global → can't serialize cross-project
  throughput; never gates worker claims). Closes the overlapping-doctor double-release race.
- **Global capacity RMW lock** (`capacity._cap_lock`, flock on `$CAP_DIR/.lock`): bump/
  drain/clear_expired/probe_codex now wrap their WHOLE read-modify-write — cross-process
  (caretaker vs supervisor) atomicity, closing lost-drain → silent quota over-spend.
- **Byte-verified `_atomic_write`** (orchestrator): writes tmp, verifies size == expected,
  fsyncs, THEN renames; a short write / ENOSPC raises and leaves the destination intact
  (no more rename-over-on-full-disk corruption). watcher.sh result.json writes hardened
  to tmp-then-mv.
- **Timezone-correct `parse_reset_seconds`** (capacity) + wired into supervisor_pass.sh,
  replacing the tz-naive datetime.now() parse that mis-timed the post-reset resume.
- **GC/rotation** (`doctor.gc_artifacts`): prunes logs/heartbeats/archives/qa-passed-
  sidecars by age (14d) and per-dir count (500); wired into the caretaker `--fix` pass —
  closes unbounded growth (the multi-week disk-exhaustion time bomb). The dead per-PID
  `.hb` files are now GC-swept.
- **Terminal churn cap** (`MAX_REQUEUE=8`): a claim repeatedly orphaned by a flapping
  watcher goes to failed/ (with fail_reason) instead of re-queuing forever.
- Tests: test_p1_integrity.py (11 gates: lock ×3, churn cap ×2, GC ×2, byte-verify ×1,
  tz ×2, capacity atomicity ×1). Full suite **219 passed** (was 208). Flake-checked the
  concurrency gate ×5. NOTE: one self-authored gate (test_zone_changes_result) was a
  faulty/UNSATISFIABLE spec — both 3am-Shanghai and 3am-NY clamped to 6h → equal; CORRECTED
  to a STRONGER spec (pins sh==3600 exact at a now where Shanghai-3am is un-clamped), not
  weakened. Not yet synced to live projects — pending a quiet-window sync.

## 2026-06-15 — Repo hygiene: tests out of the deployable surface

Principle: scripts/ holds ONLY what the skill needs at runtime / deploys to projects;
everything not exported lives under dev/. Moved all 18 test_*.py (16 from scripts/ +
test_autonomous_bash_guard.py from scripts/hooks/) → dev/tests/. Added
dev/tests/conftest.py (puts scripts/ + scripts/hooks/ on sys.path) and dev/pytest.ini
(rootdir + .pytest_cache scoped to dev/). Repointed the sibling-script paths in the
script-reading tests (watcher/install_supervisor/p2) and the spec_from_file_location
loaders (init_workspace/detach_run/autonomous_bash_guard) to skill-root/scripts.
FIXED A REAL LEAK: init_workspace HOOK_SCRIPTS was deploying test_autonomous_bash_guard.py
into every project's .fleet/hooks/ — now deploys only the guard. Canonical run command
is now `cd dev && python3 -m pytest -q`. Suite unchanged: 219 passed + 12 P2 gates red.
Export surface (skill root + scripts/) now carries zero test source.

## 2026-06-15 (P2 executed) — self-supervision + audit, verifier-first via /goal

Ran HARDENING_PLAN P2 under the protocol (gates RED-first in dev/tests/test_p2_
observability.py; driven green by /goal against `cd dev && python3 -m pytest -q`).

- **Event ledger** (ledger.py, NEW): append-only JSONL audit trail at
  status/events.jsonl via O_APPEND single-write (atomic <PIPE_BUF; concurrency-safe),
  every line {ts,type,...}; read() replays it. Closes the audit-trail-free blackout.
  Wired into orchestrator qa-pass/qa-fail/qa-fail-terminal/promote and doctor
  resolver-release; watcher/capacity wiring left for inspection. Fail-open.
- **No-LLM liveness pinger** (fleet_health.py + health_loop.sh, NEW):
  check_health detects dead global singletons (hub/capacity_loop pidfile holder
  dead), dead per-project caretaker, and disk pressure (<500MB default);
  emit_alerts appends to ~/.fleet/alerts.jsonl + best-effort osascript notification.
  Scripts can't call PushNotification — the leader pass reads alerts.jsonl and does.
- **timeout wraps**: supervisor_pass.sh wraps the leader `claude -p`
  (SUPERVISOR_PASS_TIMEOUT=1800), caretaker.sh wraps `doctor.py`
  (CARETAKER_DOCTOR_TIMEOUT=120). Portable shim: real timeout → gtimeout → passthrough
  (never breaks the loop on a macOS box without coreutils).
- **TCC-safe whole-stack KeepAlive** (install_supervisord.sh + fleet_supervisord.sh,
  NEW, GLOBAL — not deployed per-project): one launchd agent (KeepAlive+RunAtLoad)
  whose runnable lives under ~/.claude (OUTSIDE ~/Documents|Desktop|Downloads — the
  reboot-loop landmine, asserted by test). Loop ensures every registered project's
  start.sh is up + runs a health sweep. --no-load/--remove/--status.
- init_workspace registers ledger.py/fleet_health.py/health_loop.sh in
  RUNTIME_SCRIPTS (+ health_loop.sh executable); supervisord pair stays global.
- Tests: dev/tests/test_p2_observability.py (12 gates: ledger ×4, health ×4,
  timeout ×2, supervisord plist ×2). Full suite 231 passed (was 219). Empirically
  verified: real plist plutil -lint OK + executable path TCC-safe. NOT synced to live
  projects yet (pending quiet-window sync); launchd supervisord built but NOT loaded
  (loading stays the user's opt-in call).

## 2026-06-15 (P3 executed) — scheduling depth, verifier-first via /goal

Ran HARDENING_PLAN P3 (gates RED-first in dev/tests/test_p3_scheduling.py; driven
green by /goal against `cd dev && python3 -m pytest -q`).

- **Incremental resolution** (doctor.dependents_index + release_dependents): a
  qa-pass now releases ONLY that producer's now-ready dependents (O(dependents), not
  an O(n) full-drafts rescan — verified by a _dep_satisfied call-count gate). Wired
  into orchestrator cmd_qa_pass (fast path; periodic resolve_dependencies stays the
  backstop). Fail-open.
- **Deadlock detection** (doctor.find_deadlocks): DFS surfaces dependency CYCLES
  among drafts + chains rooted in a terminal (failed/archive) dep. SURFACE-ONLY
  (returned + _say in doctor main) — never auto-releases. Closes the "cyclic graph
  holds silently forever" gap.
- **Write-scope collision** (doctor._scopes_overlap + schema.write_scope): the
  resolver now serializes two dep-ready tasks whose WRITE SCOPES overlap (globs via
  fnmatch both-directions), generalizing the exact-output_file collision. Backward
  compatible — default scope [output_file] keeps TestOutputCollisionSerialize green.
- **Unified Anthropic pool accounting + fairness** (capacity): record_spend /
  pool_used ({5h,week} summed across claude WORKER + claude-lead = ONE tracked pool,
  closing the per-role-bucket blind spot) / project_spend (attribution) /
  fair_slot_floor (per-project minimum-slot reservation; one project can't starve
  another). flock-guarded. Wiring (supervisor records claude-lead spend, watcher
  records worker spend + consults fair_slot_floor) left for inspection/sync.
- Tests: 15 P3 gates (incremental ×3, deadlock ×4, write-scope ×3, pool ×3,
  fairness ×2). Full suite 246 passed (was 231); flake-checked ×5; backward-compat
  confirmed. NOT synced to live projects yet (pending quiet-window sync).

## 2026-06-15 (P4 executed) — quality without the human, verifier-first via /ralph-loop

Ran HARDENING_PLAN P4 under /ralph-loop:ralph-loop (promise P4-GREEN; gates RED-first
in dev/tests/test_p4_quality.py). Four new fail-open runtime modules + watcher wiring:
- **qa_floor.py** — mechanical no-LLM floor: artifact_ok (exists + regular FILE +
  non-empty → kills the rc==0-on-a-directory false-success), test_count_grew,
  reconcile_files (changed-vs-declared scope).
- **predicates.py** — pluggable per-task acceptance predicates: scalar
  (dotpath→numeric→op), regex, command(exit 0); fail-safe False.
- **grader.py** — auto second-opinion: grade(deliverable,criteria,runner=None)→
  {ok,reasons,raw}; INJECTABLE runner (default = cheap workhorse headless); JSON/YES-NO
  parse; malformed OR runner-exception → fail-open ok=False (never rubber-stamps).
- **profiles.py** — .fleet/profile.json project type → discipline_block(task_type,
  profile): software code/test → hard-coding block; research/writing/review →
  ANTI-FABRICATION block. WIRED into watcher.sh build_prompt (profile-driven, with an
  inline code-block fallback when profiles.py is absent so existing prompt tests hold).
- Registered the four in init_workspace RUNTIME_SCRIPTS. Added dev/eval/corpus/ (labeled
  good/bad) + dev/eval/run_grader_eval.py (EMPIRICAL agreement run — not a pytest gate).
  SKILL.md QA section documents the floor + predicates + grader + profiles.
- Tests: 22 P4 gates (qa_floor ×6, predicates ×6, grader ×4, profiles ×4, harness ×2).
  Full suite 268 passed (was 246). REAL-LLM grader agreement is run via
  run_grader_eval.py (empirical), not gated. NOT synced to live projects yet.

### P0–P4 GATES GREEN — but production WIRING incomplete (corrected 2026-06-15)
Initial self-assessment claimed 5/5 on all six dimensions. A 19-agent adversarial
RE-EVAL (post-hardening) corrected this to ~2.6/functional. VERIFIED root cause:
the P3/P4 gates tested modules as PURE FUNCTIONS that pass, but the wiring into the
live paths was scoped 'inspection-level' and never written — so pool_used/record_spend/
fair_slot_floor (P3) and qa_floor/predicates/grader (P4) have ZERO production callers
(grep-confirmed). cmd_qa_pass does NOT call the floor; gate_level does NOT consult the
pool; start.sh does NOT launch health_loop. Only profiles.py is genuinely wired
(watcher.sh). The spine (atomic queue, churn-bounded recovery, DAG scheduler, codex
telemetry, anti-fabrication profile) is real and strong; the QA/pool/fairness/health
CAPABILITIES exist + are unit-tested but DO NOT RUN. Honest status: gates green,
capabilities unwired. P5 wires them (gating the INTEGRATION entry points, not pure
functions) + corrects this overstatement.

## 2026-06-15 (P5 executed) — WIRING the dead code, gated on integration

The re-eval's dominant finding was P3/P4 capabilities with ZERO production callers.
P5 wires them at the REAL entry points, and its gates drive those entry points
(cmd_qa_pass / gate_level / start.sh / the kill path / init_workspace) — not pure
functions — so dead-code can't recur silently.
- **QA floor WIRED into cmd_qa_pass:** before passing, runs qa_floor.artifact_ok +
  predicates.eval_predicate on the spec's acceptance_predicates; any failure → auto
  qa-fail (with reason), never reaches qa-passed/. Good output still passes (backward
  compat). Fail-closed on a bad artifact, fail-open on a broken checker.
- **Shared-pool throttle WIRED into gate_level:** claude/claude-lead now honor
  pool_used vs POOL_5H_LIMIT/POOL_WEEK_LIMIT (worst-of with the codex-style signal);
  workhorses unaffected. FEEDER wired: supervisor_pass records claude-lead spend
  (config-gated by FLEET_PASS_TOKENS — off by default since the absolute ceiling is
  account-specific). watcher/fair_slot_floor reservation noted for follow-up.
- **Health pinger launched by default:** start.sh starts health_loop.sh as a global
  singleton; kanban_hub /api/projects now surfaces recent alerts.jsonl.
- **Ledger completed:** watcher.sh now logs claim/complete/reroute (was 5 of 8).
- **Kill-path hardened:** doctor._kill_match (anchored + re.escape'd) filters pgrep
  hits so a prefix-sibling task_id can't be killed; kills by process GROUP.
- **init_workspace scaffolds .fleet/profile.json** (default software).
- Tests: 10 P5 integration gates (qa-floor-at-qa-pass ×3, predicate ×2, pool-gate ×2,
  health-launch ×1, kill-match ×1, profile-scaffold ×1). Full suite 278 passed (was
  268). Scratch E2E confirmed pool gate drains claude / not kimi + profile scaffold.
  NOT synced to live projects yet.
