---
name: multi-agent-cli-orchestration-fleet
description: Multi-PROJECT scaling version of multi-agent-cli-orchestration. Orchestrate heterogeneous external CLI agents (codex/gpt-5.5, kimi/k2.6, opencode/glm-5.2, claude/sonnet-4.6) across SEVERAL projects concurrently — per-project worker fleets and queues, ONE global kanban hub (tabs per project), token-capacity-aware scheduling (codex telemetry probe, quota reroute, model/effort ladders), and leader-continuity machinery (headless laddered supervisor passes + no-LLM caretaker). Use for multi-agent pipelines, parallel research/coding/testing delegation, or divide-and-conquer agent workflows — especially when more than one project runs at once. Mission-agnostic.
---

# Multi-Agent CLI Orchestration — FLEET (multi-project)

Claude is the **orchestrator and QA gate** (the leader, `claude-lead`). External CLI agents (codex, kimi, opencode, and a headless Sonnet `claude` worker) are **autonomous workers**. Coordination happens through a **file-based task queue** with atomic claiming — no servers, no message broker.

> **Lineage:** this is the multi-project successor of `multi-agent-cli-orchestration`
> (the legacy single-project skill). Both can be installed side by side — different
> project dirs (`.fleet/` vs `.multiagent/`), script names (`watcher.sh` vs `watch.sh`,
> `kanban_hub.py` vs `monitor.py`, `phase_deriver.sh` vs `phase_deriver_loop.sh`),
> and ports (8788 vs 8787) — the legacy skill's `pkill -f` patterns match NOTHING in
> a fleet stack (verified). New projects should use FLEET; demise the legacy skill
> once fleet has proven itself in production.
> Also distinct from `orchestrating-swarms` (internal Claude subagents via the Task tool).

## Architecture — two layers, deliberately split

```
PER-PROJECT (each project root)              GLOBAL (machine-level, ~/.fleet/)
  .fleet/queue/{drafts,pending,claimed,        projects.json   ← registry (hub tabs)
               completed,failed}               capacity/<agent>.json ← token windows
  watcher.sh ×N per agent (pidfile-scoped)     slots/<agent>/  ← concurrency caps
  phase_deriver.sh · caretaker.sh              kanban_hub.py   ← ONE port (8788), all
  orchestrator.py (leader's CLI)                                 projects as tabs
  supervisor_pass.sh (headless leader)         capacity_loop.sh ← codex probe (60s)
```

**The split rule:** queues, watchers, logs, phases are *project-scoped* (they follow
the project). Token quotas are *account-scoped* — shared by every project on the
machine — so capacity, concurrency slots, and the kanban hub are *global singletons*.
Getting this split wrong is what made single-project designs unable to run twice.

**Multi-project process safety (the P0 fix):** liveness is tracked via **pidfiles**
(`.fleet/status/pids/`), each verified `pid alive AND cmdline matches` before
counting or killing. No bare `pgrep/pkill -f <script-name>` anywhere — project A's
`stop.sh` cannot touch project B's watchers, and `start.sh` in B is never fooled by
A's running instances. Watchers are launched by **absolute path**, so cmdlines are
project-distinguishable.

## Capability status — what actually RUNS (read this before trusting a feature)

Honest matrix to prevent overstatement (a capability that exists + is unit-tested is
NOT the same as one that runs on the production path). Status as of 2026-06-17 (P19/P20):

| Capability | Status | Note |
|---|---|---|
| Atomic-rename claim · crash-only queue | **WIRED** | default, lock-free |
| Recovery: orphan (pid-precise) · stuck (log-mtime) · churn caps | **WIRED** | doctor, every caretaker tick |
| Per-project doctor lock · capacity flock RMW | **WIRED** | default |
| DAG: dep release · incremental release · deadlock surfacing · write-scope collision | **WIRED** | `resolve_dependencies` + `release_dependents` |
| Capacity: codex telemetry · reactive bump/drain · claim gate | **WIRED** | default |
| codex EFFORT ladder (telemetry-driven xhigh→high→medium) | **WIRED** | live, from rollout used% |
| claude-lead model selection | **WIRED (top model, no ladder)** | leader runs the TOP model and degrades via DRAIN-TO-RESET on a cliff; the dead never-bumped model ladder was REMOVED (P17), `pick(claude-lead)`→top model (config `leader_model` override) |
| QA floor (`qa_floor.evaluate`): artifact + predicates + write-scope + test-count | **WIRED** | ONE shared floor; runs at `cmd_qa_pass` AND as a deterministic no-LLM sweep (P9) |
| **Deterministic QA firing — no-LLM `sweep_qa_floor` over completed/** | **WIRED (default-on)** | caretaker tick auto-qa-FAILS floor violations AND auto-qa-PASSES floor-clean tasks whose declared `acceptance_predicates` all hold (P9/P10) — DAG advances without a live leader; clean-but-no-predicate still defers to semantic review |
| QA floor fails CLOSED on a predicate/artifact error or empty `output_file` | **WIRED** | fail-open ONLY if the checker modules can't import (P7) |
| Test-count growth (`test_count_grew`) enforced | **WIRED (opt-in producer)** | floor fails a code task whose collected test count didn't grow, when the watcher reports it (`FLEET_TRACK_TESTS=1`, P9) |
| Profiles: anti-fab / code / data·ml discipline injection (COMPOSED) | **WIRED** | watcher `build_prompt`; blocks compose so research-code keeps engineering+anti-fab, data-writeup gets numbers anti-fab (P7/P10) |
| GC: logs/heartbeat/archives/qa-passed | **WIRED** | `gc_artifacts` (age+count) |
| health pinger + `alerts.jsonl` write | **WIRED** | `start.sh` launches `health_loop` |
| **Autonomous QA / leader continuity — scoped to UNATTENDED mode** | **WIRED** | `./.fleet/autonomous.sh on` co-launches the supervisor + a leader-watching heartbeat (Fix C); the supervisor STANDS DOWN while the leader is alive (Fix A) and DEFERS semantic+science when it runs (Fix B). NOT launched by `start.sh` (attended = no parallel QA); pure-headless via launchd or `FLEET_SUPERVISOR=1` |
| ledger: claim/complete/reroute/qa-pass/qa-fail/promote/release | **WIRED** | `events.jsonl` |
| ledger READER (`orchestrator metrics`) | **WIRED** | replays `events.jsonl` into a summary (P7) |
| Accept-verdict sidecar — the WHY pinned next to status on PASS | **WIRED** | `qa-pass` writes `qa-passed/<id>.verdict.json` (criteria + predicates + grader + `--reason`); symmetric with `retry_reason` on fail; survives compaction (P15) |
| Stall detection: qa-backlog · pending-with-no-workers · deadlock | **WIRED** | `check_health` + deadlock→`emit_alerts` (P7) |
| Token economy = codex telemetry + reactive bump/drain ONLY | **WIRED** | codex rollout used% gates codex; an observed quota error bump/drains any agent. The Claude shared-pool ESTIMATE machinery (spend.jsonl, worker log-bytes/4 feed, leader 12000 constant, pool gate/alerts, by_project) was REMOVED — Claude has no token meter, so it measured nothing (P19) |
| launchd whole-stack KeepAlive (`install_supervisord`) | **OPT-IN** | reboot-survival; the in-shell `supervisor_loop` is the default |
| autonomous (unattended) mode | **OPT-IN** | `./.fleet/autonomous.sh on` — sets `AUTONOMOUS_ON` (prompt-free Bash + strict QA) AND arms the leader-watching heartbeat + supervisor fallback; `off` to disarm |
| Pluggable acceptance predicates | **WIRED** | `TaskSpec.acceptance_predicates`; `create-task --predicate '<json>'`; enforced at `cmd_qa_pass` floor (P6) |
| Write-scope collision serialization + ENFORCEMENT | **WIRED** | `create-task --write-scope` (P7); path-semantic overlap (P8); serialized at claim (`doctor --claimable`) AND draft-release; a declared write_scope now FORCES worktree isolation so `reconcile_files` is accurate and HARD-fails at qa-pass — not advisory (P8/P20). Non-git → advisory (can't isolate) |
| Second-opinion grader (`grader.grade`) + groundedness | **WIRED (strong judge)** | default judge = **claude** (`FLEET_GRADER_MODEL` overrides), not opencode/kimi; auto-arms for content tasks; **fail-CLOSED for content tasks** (judge down → bounce, not pass); fed `context_files` as SOURCES; bare YES/prose never passes (P7/P16/P17) |
| Alert-pinger self-watch | **WIRED** | `health_loop` is in `fleet_health.SINGLETONS` — a dead pinger is itself flagged (P17) |
| Per-project fairness (`fair_slot_floor`) | **WIRED** | claude AND codex yield over their fair share at claim; **floor never 0** (no starvation), total derived from the agent's REAL `global_max_concurrent` (no magic constant), denominator = LIVE projects only (`registry` last_seen) (P6/P8/P16) |
| Cross-project safety: claim-scoped kill · hub `process_alive` scoped | **WIRED** | `_kill_match`/`pgrep` anchored on the project's absolute claimed path; hub requires `project_root` in the matched cmdline (P8) |
| Per-task git-worktree isolation | **WIRED (opt-in `FLEET_WORKTREE=1`)** | `worktree.py` runs each task in `.worktrees/<id>` on `fleet/<id>`; copies context in, copies deliverable + accurate `changed_files` back, removes worktree; fail-open off a non-git tree (P12) |
| Strict teeth AUTO-DETECT | **WIRED** | `track_changes` auto-on when git repo; `track_tests` auto-on for software profile + pytest; reconcile ENFORCED only under worktree isolation (else advisory); `FLEET_STRICT=1` / `.fleet/AUTONOMOUS_ON` / explicit env override (P11/P14/P16) |
| Auth-expiry handling | **WIRED** | reroute the task to a healthy fallback agent + `auth_expired` alert (re-login is human-only); no more 30-retry-then-FAIL DAG poisoning (P16) |
| Anti-fab grader auto-arms for content tasks | **WIRED** | research/write/review auto-run the groundedness grader at `cmd_qa_pass` even without FLEET_GRADER (P16); code/test stays opt-in |
| Detached long-job auto-recovery | **WIRED (registry-driven)** | caretaker `ensure_watchdogs` keeps each REGISTERED job's watchdog alive (relaunch dead, reap done), single-restarter preserved; register via `jobs.py register` or `detach_run.py --register-id` (P13/P14) — empty registry = no-op |
| Gate fail-open is NOT silent (ALL gate paths) | **WIRED** | `cmd_qa_pass`, `sweep_qa_floor`, and the write-scope claim gate all alert (`qa_floor_error`/`scope_gate_error`) when a checker errors; a MISSING spec or an unimportable checker fail-CLOSED instead of passing (P14/P14.1/P14.2) |
| `events.jsonl` rotation under a flock | **WIRED** | `ledger.rotate` (no un-flock'd RMW dropping appends) (P14) |
| Worktree branch integration — AUTONOMOUS | **WIRED** | `cmd_qa_pass` auto-merges the task's `fleet/<id>` branch on a pass (covers the no-LLM sweep too); real conflict → abort + keep branch + alert; `orchestrator merge-task` still available manually (P12/P16/P20) |
| no-LLM auto-pass PRODUCER (predicates) | **WIRED** | the supervisor pass prompt instructs the leader to attach `--predicate` so the caretaker can auto-pass during a leader blackout (P20) |
| `spend.jsonl` rotation under the capacity lock | **WIRED** | `capacity.rotate_spend` (no un-flock'd RMW dropping appends) (P9) |
| Stuck-sweep liveness = log mtime **OR `output_file` mtime** | **WIRED** | a long-quiet but actively-writing job isn't killed (P9) |
| hub render of `alerts.jsonl` | **WIRED** | overview renders the alerts banner (`d.alerts`) (P6) |
| Audit-log GC (`spend`/`events`/`alerts.jsonl`) | **WIRED** | `gc_artifacts` rotates to last 5000 / drops if stale (P6); `spend` read is tail-bounded |

**Known residual risks** (tracked, honest): `predicates.command` executes under the QA trust
domain. **DP3 pure-A narrows this for DETACHED cards**: the no-LLM caretaker NEVER executes a
card's command predicate (cards are runner-writable) — only the present leader's `approve-card`
runs it (a command-only card otherwise defers to the leader and counts toward the `qa_backlog`
alert). QUEUE command predicates are leader-authored via `create-task` and still execute (e.g.
`pytest`). The opt-in grader's default runner also executes — keep `FLEET_GRADER` opt-in. **There is NO Claude-side token meter**, so the entire Claude
spend-ESTIMATE machinery was REMOVED (P19) rather than shown as if metered — token control
is now codex rollout telemetry (real used%) + reactive bump/drain on an observed quota
error, nothing estimated. `fair_slot_floor` is a soft cap (fail-open), not a hard reservation.
The no-LLM floor sweep auto-FAILS structural junk and auto-PASSES floor-clean tasks whose
declared `acceptance_predicates` all hold (P10), but a clean task with NO predicates still
defers to semantic review — so for anti-fabrication-critical work either declare predicates
or TURN THE GRADER ON (`FLEET_GRADER=1`); with neither, a plausible-but-wrong deliverable
waits for the leader rather than auto-passing. `reconcile_files` and `test_count_grew` have real producers now
(watcher, `FLEET_TRACK_CHANGES=1` / `FLEET_TRACK_TESTS=1`), accurate under git-worktree
isolation (`FLEET_WORKTREE=1`, now BUILT — P12) / single-writer; in a SHARED tree without
isolation a concurrent disjoint writer's files can leak into `changed_files`. All three
default OFF. Worktree mode accumulates per-task `fleet/<id>` branches (leader merges +
prunes) and assumes context is committed or in `context_files`. The default `supervisor_loop`
survives quota cliffs and closed windows but NOT a machine reboot — add
`install_supervisor.sh` (launchd) for reboot-survival. Token-economy is estimate-capped:
NO Claude-side token meter exists (external limit) — overspend is alarmed, not metered. See
dev/HARDENING_PLAN.md.

## Setup (once per project)

```bash
python3 ~/.claude/skills/multi-agent-cli-orchestration-fleet/scripts/init_workspace.py .
```

Scaffolds `.fleet/` (queue incl. `drafts/`, runtime scripts, agent registry), wires
`.gitignore` + the autonomous-mode guard hook. Then:

1. Ensure each agent CLI is authenticated (`codex`, `kimi login`, `opencode`, `claude`).
2. Bring the project stack up (multi-project-safe, idempotent — scales UP to target):
   ```bash
   ./.fleet/start.sh                    # all 4 agents + caretaker + deriver
   ./.fleet/start.sh kimi opencode      # only the named agents
   KIMI_INSTANCES=4 ./.fleet/start.sh   # override counts (or .fleet/fleet.json
                                        #   {"instances": {"kimi": 4}})
   ./.fleet/stop.sh                     # stop THIS project only
   ./.fleet/stop.sh --all               # + the global hub/capacity loop (last project out)
   ```
   The first `start.sh` also launches the **global kanban hub** → http://127.0.0.1:8788
   and the **capacity loop**; later projects just register and appear as tabs.

**Multi-instance workers (clones).** Workhorses launch multiple instances by default
(**kimi×3, opencode×3**; reserves codex×1, claude×1) — flat-rate clones are near-free
throughput. All instances share the agent's assignment name; the atomic claim
(`rename(2)`) guarantees exactly one winner per task. Clones only help when the
backlog is fanned out into independent tasks (`assigned_to: any` or the agent name) —
decompose work to exploit them. A **global per-agent slot cap**
(`global_max_concurrent` in `agents/*.json`; mkdir-atomic slots in `~/.fleet/slots/`)
bounds total concurrent CLI invocations across ALL projects, so N projects × K clones
can never stampede one account.

## Agent routing — capability × token cost × live capacity

Read `.fleet/agents/*.json` for the registry (now carries `fallback_agents`,
`global_max_concurrent`, `subagent_fanout`, and codex's `effort_ladder`).

> **The economic axis is QUOTA SCARCITY, not price** — all four subscriptions are
> flat-rate monthly ("metered/cheap" framing is wrong). **Quota-scarce:** claude
> AND codex, each bounded by BOTH a 5h window and a weekly window (and claude's
> pool is shared with the leader sessions). **Quota-abundant:** kimi/opencode —
> hard to exhaust in practice.

| Agent | Model | Tier | Route to it for |
|-------|-------|------|-----------------|
| **codex** | gpt-5.5, effort **auto-laddered** xhigh→high→medium by live capacity | **reserve** (quota-scarce: 5h + weekly windows, REAL telemetry) | Only tasks needing top-tier intelligence: hard algorithms, subtle correctness, tricky debugging |
| **kimi** | kimi-k2.6 (**pinned best**) | **workhorse** (quota-abundant) | Long-context research, literature synthesis, reading-heavy analysis. Internal **subagents ON** |
| **opencode** | glm-5.2 (**pinned via `--model zhipuai-coding-plan/glm-5.2`**, override `OPENCODE_MODEL`) | **workhorse** (quota-abundant) | Bulk coding, tests, review, structured output. Internal **subagents ON** |
| **claude** (worker) | Sonnet 4.6 (**pinned by design** — the ladder is for the LEADER, never this worker) | **reserve** (quota-scarce: 5h + weekly windows, pool SHARED with the leader!) | Careful method/algorithm code, scientific writing, nuanced analysis above glm/kimi but below codex top tier |
| **claude-lead** = YOU | interactive session; headless passes run the TOP leader model (claude-opus-4-8), no ladder — degrade by drain-to-reset | **leader** | Orchestration, task-spec authoring, QA, version control — *never claims tasks* |

**Token economy rule:** reserve Claude and codex for high-intelligence work; push bulk
to opencode/kimi. Spend *your* effort on **strong task scaffolding** (self-contained
description, exact context_files, checkable acceptance_criteria) so cheap workhorses
succeed without expensive retries.

**Routing = comparative advantage, NOT partitions (hard-won).** The `best_for`
labels say what each agent is *comparatively* better at under contention — they do
NOT mean "kimi can't code" (it can, well). A real failure mode: a leader pinned six
code tasks to opencode while kimi's three instances sat idle, because the label said
"kimi = reading" — halving throughput out of label dogma. The discipline:
- **Default to `assigned_to: "any"` for parallelizable bulk work.** The atomic claim
  race IS the load balancer — every idle workhorse instance pulls work with zero
  routing decisions from you. Pinning bypasses it; do so only for a reason:
  (a) tier necessity (codex/claude-level reasoning), (b) QA-bounce continuity (the
  retry returns to the originating agent automatically), (c) genuine specialty
  contention (e.g. a long-context synthesis you want on kimi *while* both are busy).
- **Before pinning a batch, count idle instances.** N pinned tasks on one workhorse
  while its peer idles = wasted flat-rate capacity. `create-task` now prints a
  routing advisory when you pin a workhorse past its live instance count while the
  peer has capacity (warn-only — it never blocks).
- A practical reserve-tier framing that works: codex/claude only for "irreversible
  data operations" and "load-bearing design" classes — and tell the user first;
  everything else goes to the workhorse pool with tighter specs + QA bounce-backs.

### Token-capacity-aware scheduling (how it actually works)

- **Signals.** codex: PROACTIVE — `~/.codex/sessions/**/rollout-*.jsonl` carries
  `rate_limits.primary{used_percent, resets_at}` (5h) + `.secondary` (weekly); the
  global `capacity_loop.sh` probes it every 60s (and the watcher re-probes right
  after each codex task). All other agents: REACTIVE — a rate-limit/quota failure
  signature in a worker log triggers `capacity.py bump` (drain + ladder rung+1).
  There is **no local "remaining" API for claude/kimi/opencode** — do not pretend
  otherwise; reactive + reset-expiry is the honest mechanism.
- **Claim gate.** Before claiming, a watcher checks `capacity.py gate <agent>`:
  `0` claim freely · `1` soft (used ≥80% → only priority ≤ `FLEET_SOFT_MAX_PRIO`=2
  tasks) · `2` drained (used ≥95% or active drain → claim nothing; `any` tasks flow
  to healthy agents automatically). **Fail-open everywhere:** missing data, crashed
  gate, missing capacity.py all read as healthy — no data is never a reason to stall.
- **Quota reroute.** When a CLI run fails on a quota signature: registry bumped;
  an `assigned_to: <this agent>` task is rewritten to the first healthy agent in its
  `fallback_agents` chain (audit field `rerouted_from`); an `any` task simply
  re-queues. **In-flight work is never killed** — lame-duck semantics: finish what
  you hold, claim nothing new (mid-task hand-offs would forfeit completed work).
- **Self-correction.** `used_pct` whose `resets_at` passed reads as 0; drains expire;
  reactive rungs decay after ~5h. Stale data cannot wedge the fleet.

### Model/effort ladders (degrade spend-rate, not availability)

- **codex (LIVE):** reasoning effort `xhigh→high→medium` picked per task from real
  rollout used% telemetry — effort is the bulk of reasoning-token spend, and degrading it
  is smoother than swapping models. Ladder in `agents/codex.json` (`effort_ladder`). This
  is the one ladder that genuinely degrades spend in production.
- **claude-lead — TOP model, NO ladder (P17/P18):** the headless passes run a single top
  leader model (**claude-opus-4-8**; override via `agents`/config `leader_model`). There is
  no model ladder — the dead never-bumped ladder was removed. The leader degrades by
  DRAIN-TO-RESET on a quota cliff: it drains until the window reset, then runs the top
  model again (a fresh window = full quota; degrading post-reset would be wrong, and
  there's no Claude intra-window telemetry to ladder on anyway).
- **kimi/opencode/claude-worker:** pinned. Degrading a flat-rate or deliberately-tiered
  agent wastes paid-for intelligence.

### Internal subagent fan-out (second-level scaling)

Workhorse prompts (kimi, opencode — `subagent_fanout: true`) automatically include a
hint to use their internal subagent/parallel-task capability on decomposable tasks,
then assemble results into the single `output_file`. This converts their under-used
flat-rate quota into wall-clock speedup — outer clones scale task *throughput*, inner
subagents scale *single-task latency*; the two are orthogonal. Reserve agents never
get the hint (it would multiply metered spend). codex's native fan-out
(`enable_fanout`/`child_agents_md` feature flags) was still under development at fork
time — revisit when stable. Disable globally with `FLEET_FANOUT=0`.

## Creating tasks

### Dispatch decision — queue vs queue-fan-out vs detach (decide FIRST)

Before dispatching a unit of work, pick exactly ONE track (single ownership → exactly one QA
owner, no cross-track double-QA):

| Choose | When | QA path |
|---|---|---|
| **queue task** (`create-task`) | short (finishes well within a worker invocation, < ~worker timeout) AND produces one QA-able deliverable file | the queue QA floor + leader `qa-pass`/`qa-fail` |
| **queue parallel fan-out** (N `create-task`) | the work splits into INDEPENDENT short units | one queue task per unit (max throughput, each fully QA'd) — prefer this over one big detach when units are independent |
| **detach** (`detach_run.py` + `jobs.py register` + a `board-card`) | long (exceeds a worker invocation, ~1h+) **OR** holds an exclusive scarce resource (GPU/IO) for long **OR** produces a *run / side-effect* (trained model, populated DB, multi-unit sweep) rather than one file **OR** must survive crashes/reboots | detached-card QA (completion gate + predicates + leader `approve-card`/`reject-card`) — see *Long-running jobs* |

Heuristic: *"write/fix/review/produce a file"* → queue; *"run/sweep/train/backfill/scan"* → detach.
A long job is NOT a queue task (it would block a worker slot and hit the CLI timeout). When unsure,
prefer queue (shorter, fully gated) and only detach when a long-run criterion above is genuinely met.
**Whichever track: semantic/science QA is the leader's non-delegable job — neither track auto-passes it.**

```bash
python3 .fleet/orchestrator.py create-task \
  --phase <id> --type research|code|test|write|review \
  --assign codex|kimi|opencode|claude|any \
  --title "..." --description "..." \
  --output-file "relative/path/to/deliverable" \
  --criteria "criterion 1" "criterion 2" ... \
  --context-files "path/a" "path/b" \
  --priority 1-10 [--depends-on <id|phase:N> ...] \
  [--predicate '<json>' ...] [--write-scope 'src/**' ...] [--hold]
```

**Task-spec discipline (this is the leverage point):**
- `description` must be self-contained — the agent only has the task file +
  context_files + the repo. It has no memory of your conversation.
- `acceptance_criteria` must be concrete and checkable — they are what you QA
  against, and they're injected so the agent knows the bar.
- `context_files` point at exactly what the agent needs. Don't over-stuff.
- `output_file` is the PRIMARY deliverable anchor. **The `description` is the
  single source of truth for the authorized file set**: a multi-file task (module
  + sibling test, code + schema/config) must ENUMERATE every in-scope file in the
  description — the watcher prompt tells agents that described scope is the law
  and anything outside it is off-limits, and requires them to list every file
  they touched in their final summary (lands in the task log; QA checks it).
  (The old "modify ONLY output_file" line is gone — it contradicted the sibling-
  test QA rule, made the most obedient agent fail-stop on the contradiction and
  taught the rest that instructions are decorative.)
- **For code/test tasks, demand parameterization in the criteria** — e.g. "no
  hard-coded constants: tunables are function args/CLI flags; fixed values live
  in an editable config, not in function bodies". The watcher auto-injects this
  engineering discipline into every code/test prompt (kimi and opencode were
  both observed baking magic values into deliverables), but the criterion makes
  it CHECKABLE at QA — prompt injection guides, acceptance criteria enforce.
- **`--hold` enqueues a DRAFT** (`queue/drafts/`, invisible to watchers) — promoted
  explicitly (`promote <id>`) or mechanically by the caretaker when a pool's live
  backlog drops below low-water. **Pre-author the next wave as drafts every healthy
  pass** — that is what keeps workers fed through a leader quota blackout.
- **`--depends-on <id|phase:N> …` is how you express ordering** (see Parallelism
  below). A task with deps is auto-held in drafts and auto-released the instant
  every dep is QA-passed — you do NOT hand-dispatch it.

## Parallelism — parallel by DEFAULT, serialize only by declared necessity

**This framework's stance: maximize task-level parallelism; phases are a display /
optional-checkpoint abstraction, NOT a scheduling barrier.** The queue is already
flat and greedy — a watcher claims anything eligible in `pending/` immediately, up
to the slot caps. So the only thing that serializes work is *what you hold back*.
Do not hold back by phase out of habit; that throttles throughput (a real failure:
a leader gated phase 4 behind all of phase 2 when P4 only needed one P2 output).

The discipline, for any project (the framework can't know your DAG — you declare it):
- **Author the whole wave up front, each task declaring its REAL upstreams via
  `--depends-on` (concrete task ids, or `phase:N` sugar = "all of phase N
  QA-passed").** No deps = runs now. The resolver (`doctor.py`, every caretaker
  tick, no LLM) releases each draft the moment its deps are satisfied — so
  independent tasks across different phases run concurrently, bounded only by true
  data dependencies, slot caps, and capacity. This is the "closed loop": map the
  DAG once, let the scaffold release.
- **A dependency is satisfied only when the producer is QA-passed AND its
  `output_file` exists** — never on mere `completed/`, so a consumer never starts
  against unreviewed or missing input.
- **Write safety is automatic and project-agnostic:** if two dep-ready tasks
  declare the SAME `output_file`, the resolver releases one and holds the other
  (serializes the collision) — the one ordering the framework *can* infer without
  knowing your project. Tasks that share a write target should depend on each
  other or be merged.
- **Under-declaring a dep is the main risk** (B silently reads A's file). The cost
  is bounded by crash-only recovery: a task hitting a missing input fails cleanly
  and is requeued, and QA catches semantic wrongness. Over-declaring just
  re-serializes — so declare the deps that are real, omit the rest, and lean
  parallel. Dead deps (producer in `failed/`) and unknown ids are SURFACED by the
  resolver / at `create-task`, never silently held forever.
- **Tune per project, not in the framework:** concurrency width is the global slot
  caps (`agents/*.json` / `~/.fleet/slots/`); a project with heavy shared-file
  state lowers width or uses git worktrees; a clean, independent-task project runs
  wide open. Same framework, different posture, expressed in config.

## Orchestrator commands

| Command | Purpose |
|---------|---------|
| `create-task ... [--depends-on …] [--hold]` | Enqueue a task; deps auto-hold + auto-release |
| `promote <id>` | Manually promote a held draft to pending |
| `status` | Queue counts (incl. drafts) + in-progress + recent results |
| `metrics` | Replay the event ledger (`events.jsonl`) into a summary — durable audit |
| `list [--state S]` | List tasks by state (incl. `drafts`) |
| `wait --task-id <id> [--timeout N]` | Block until a task finishes |
| `read-result <id>` | Status + acceptance criteria + deliverable (for QA) |
| `qa-pass <id>` | Accept a result (moves to completed/qa-passed) |
| `qa-pass <id> --leader-verified --reason "..."` | ATTENDED leader override (P25): skip the semantic grader when the leader PERSONALLY read the deliverable and checked its claims against sources — the mechanical floor + predicates still run; `--reason` is REQUIRED (it replaces the grader verdict in the sidecar); ignored in fallback mode (the supervisor must never use it) |
| `qa-fail <id> --reason "..."` | Reject → auto-creates a retry carrying the reason |
| `qa-fail <id> --reason "..." --no-retry` | Reject and close TERMINALLY with no retry — for read-only review/research tasks (see QA loop note) |
| `cancel <id>` | Cancel a pending/drafted task |
| `requeue <id> [--reason "..."]` | Requeue a FAILED task for a fresh run (P26): spec → pending (claim/failure state cleared, provenance stamped), failed result sidecar → failed/archive/ so the board stops showing a resolved failure. NEVER hand-`mv` a failed spec — that leaves the sidecar behind. Refuses completed tasks (requeueing finished work duplicates it; use `qa-fail` for a retry) |
| `override-fail <id> --reason "..."` | Overturn a FAILED task as leader-verified COMPLETED (P27): spec + result sidecar → completed/archive/, original auto-verdict preserved verbatim (`original_auto_status` + error text), leader rationale pinned as `qa_status`. For MECHANICAL-FLOOR FALSE-FAILS the attended leader has personally verified (defective predicate, missing worktree env) — requeueing those re-runs good work into the same defective floor. `--reason` REQUIRED (state what was verified: commands run, assertions checked). FAILED only |

Capacity (global): `python3 .fleet/capacity.py status|probe|gate <a>|bump <a>|pick <a>`.
Health: `python3 .fleet/doctor.py [--fix]` (watchers, orphaned claims, drafts, capacity, registry).

## QA loop (the leader's core responsibility)

**The leader's role is direction, safety, guardrails, and quality standards — NOT
doing the work.** When a deliverable falls short, **bounce it back to the originating
agent** and iterate until it meets the bar. **Never hand-edit an agent's deliverable —
not even for "trivial" issues.** Fixing it yourself wastes the expensive tier and
hides the defect from the agent that should learn to avoid it.

For every completed task:
1. `read-result <id>` — read the deliverable AND its acceptance criteria.
2. Judge against EACH criterion. For executable deliverables, actually RUN them.
3. Fully meets the bar → `qa-pass`. Otherwise → `qa-fail --reason "<specific,
   actionable gap>"` (auto-retry to the same agent). Repeat until the standard is met.
4. Only after QA do you integrate/commit. **Nothing reaches main unreviewed.**

**Don't `qa-fail` a read-only review/research task that did its job.** A `review`/`research`
task has no write-scope, so an auto-retry just re-runs the SAME analysis over the SAME unchanged
inputs — it fails identically and burns workers until `MAX_QA_FAIL`. Two cases:
- The review is itself **low-quality/wrong** → `qa-fail` (a retry can produce a better review). Fine.
- The review is **correct and flags a real defect** in what it audited (e.g. "p_star=0.97 ≠ paper
  0.95") → do NOT `qa-fail` it. The review SUCCEEDED. `qa-pass` it (or `qa-fail --no-retry` to close
  without a pointless re-review) and open a SEPARATE `code` task to FIX the defect it found.
Mixing these up is how a finished review ends up bouncing in a retry loop. `qa-fail` on a
review/research type now prints a hint reminding you of this; `--no-retry` ends the lineage cleanly.

**The caretaker auto-pass is NOT your semantic QA.** The no-LLM caretaker sweep auto-passes a task
the moment its `--predicate`s hold, writing a verdict that literally says `no semantic review`
(`grader.ran:false`). A predicate is a regression tripwire (a string exists / a number matches) — it
does NOT verify the implementation is correct against the spec/paper. Reading the deliverable and
judging correctness is the leader's non-delegable job; never treat a predicate-only auto-pass as QA'd.

**The human is no longer the ONLY gate — WIRED at the real entry points (P5).**
`cmd_qa_pass` now runs the mechanical floor + acceptance predicates BEFORE passing and
auto-bounces (qa-fail) on failure; `gate_level` consults the shared Claude pool;
`start.sh` launches the health pinger. (Verified by integration gates that drive the
real `cmd_qa_pass`/`gate_level`/`start.sh`, not pure functions — so these can't silently
become dead code again.)
- **`qa_floor.py` (mechanical, no-LLM):** `artifact_ok` (output exists, is a regular
  FILE, non-empty — kills the rc==0-on-a-directory false-success) runs on every qa-pass.
  The floor now **fails CLOSED** on a predicate/artifact-check error or an empty
  `output_file` (P7 — only an *import* failure of the checker modules is fail-open).
  `reconcile_files` (changed files within the declared `write_scope`, glob-aware) is wired
  into the floor and fires when the task declared a `--write-scope` AND the result reports
  `changed_files`. `test_count_grew` remains an available helper (no default caller).
- **`predicates.py` (pluggable acceptance checks):** put machine-checkable criteria on
  a task — `scalar` (dotpath→numeric→compare, e.g. "metric ≥ X"), `regex` (a string is
  present), `command` (a validator exits 0). Reachable end-to-end (P6): they are a
  `TaskSpec` field, emitted by `create-task --predicate '<json>'`, and enforced by the
  `cmd_qa_pass` floor. Sibling to the `phases.json` predicates.
- **`grader.py` (auto second-opinion, OPT-IN):** set `FLEET_GRADER=1` and `cmd_qa_pass`
  has a cheap workhorse grade the deliverable against its `acceptance_criteria` →
  `{ok, reasons}` ON TOP of the mechanical floor. Breaks the "quality survives only as
  fast as one leader reads" ceiling. **Fail-closed on a parseable verdict** (only valid
  JSON with `ok=true` passes — a bare `YES`/prose never rubber-stamps; P6), fail-open on
  grader *infra* error (never stalls QA on a crash). Off by default to keep the trust
  domain tight. Measure it with `dev/eval/run_grader_eval.py`
  against the labeled corpus; QA-5 = the human isn't the sole gate, **measured by
  agreement, not perfection**.
- **`profiles.py` (project-type adaptation):** `.fleet/profile.json {"profile": ...}`
  selects the discipline the watcher injects — software code/test → the hard-coding
  block; **research/writing/review → the ANTI-FABRICATION block** (every claim cites a
  source in context_files; never invent citations/numbers) — so a non-software
  project's #1 risk is guarded by construction, not improvised.

**QA heuristics (hard-won, carried over verbatim — they are tier-independent):**
- **The test count must GROW, not just pass.** A flat collected-test total means the
  new tests were never collected (usually `def test_` embedded in the module instead
  of a sibling `test_*.py`). Bounce it.
- **Tests live in sibling `test_<module>.py` files** — put this in every code task's
  acceptance criteria.
- **Verify the named artifact exists** — a green suite can hide a missing deliverable.
- **codex routinely ships the module but OMITS the sibling test.** Put the test
  file explicitly in the description's file scope + acceptance criteria (the
  prompt now supports multi-file scope); if an agent still omits it, don't
  hand-write it — `qa-fail`, or dispatch the test as its OWN task with
  `output_file = test_<module>.py`.
- **Check the agent's files-touched list against the actual diff.** The prompt
  requires every touched file to be listed in the final summary (in the task
  log). Touched-but-unlisted files, or files outside the described scope →
  `qa-fail` — scope discipline only holds if QA enforces it.
- **A green pytest proves the code RUNS, not that it's semantically correct.** Read
  the deliverable; the expensive correctness bugs are caught by reading.
- **Scan code deliverables for HARD-CODED values** — magic numbers/thresholds in
  function bodies, baked-in paths/URLs/model names, inline dataset sizes. Both
  kimi and opencode have shipped these. If a tunable is not an argument/flag and
  a constant is not in a config file or marked CONSTANTS block, `qa-fail` with
  the specific literals to lift — never patch it yourself.
- **A piped `pytest … | tail` reports *tail's* exit code.** READ the captured output
  (`N passed` vs `no tests ran`) before trusting a run.
- **Beware test doubles leaking into the real path.** Acceptance criteria must pin the
  real-path INVARIANT (e.g. "all arms share the SAME backbone in full mode"), require
  a test that the production factory returns the REAL component type, and run a
  MINIMAL REAL-MODE invocation before trusting an experiment runner — a green
  stub-only smoke never validates production.

**Event-driven QA that SURVIVES COMPACTION.** Arm one persistent watcher per project:
`bash .fleet/qa_notify.sh "$PWD"` via the harness **Monitor** tool (emits
`QA-PENDING <file>` per new completed/failed result), or one background
`orchestrator.py wait --task-id <id>` per in-flight task. These notifiers are
*session-bound* — compaction or a REPL restart silently kills them. The durable source
of truth is the **on-disk queue**. Therefore EVERY pass (and any post-compaction
resume) starts with:
1. **DRAIN** `completed/` — QA every finished task — and triage `failed/`.
2. **RE-ARM** the notifier if not running. **MULTI-PROJECT: check with the workspace
   path included — `pgrep -fl "qa_notify.sh $PWD"` — a bare `pgrep -f qa_notify`
   matches ANOTHER project's notifier and silently skips re-arming this one.**

## Leader continuity — surviving the leader's own quota cliff

Multi-project parallelism multiplies leader spend (N sessions, one Claude account);
a drained leader used to mean ALL projects lose QA + dispatch while workers keep
producing. The fleet splits the leader's duties by how much intelligence they need:

```
Tier 0  interactive session (you + best model)   direction, architecture, judgment
Tier 1  supervisor_pass.sh (headless claude -p)  QA + dispatch — TOP model
        claude-opus-4-8 (no ladder)                limit hit → drain until the
                                                   window resets, then resume at
                                                   the top model
Tier 2  caretaker.sh (NO LLM, 60s loop)          requeue orphaned claims, KILL+requeue
                                                   STUCK claims (hung child under a live
                                                   watcher — frozen worker-log), reap
                                                   stale pidfiles, PROMOTE DRAFTS when a
                                                   pool runs dry — workers stay fed
Tier 3  reset arrives                            Tier 1 resumes, drains QA backlog
```

**Scoped to UNATTENDED mode (Fix C — was default-on in P7).** The supervisor is the
leader-*absence* fallback, so it is **no longer launched by `start.sh`** — an ATTENDED fleet
runs NO parallel QA actor beside you. Arm it when you hand the project off, FROM your leader
session:

```bash
./.fleet/autonomous.sh on     # AUTONOMOUS_ON + leader heartbeat (watching YOU) + supervisor — one action
./.fleet/autonomous.sh off    # disarm both; back to attended
```

Co-launching the supervisor and the heartbeat in one action means there is never a supervisor
up without a heartbeat (**no startup window**). The heartbeat **watches the leader pid**:
- **leader alive → heartbeat fresh → every supervisor pass STANDS DOWN** (Fix A). Semantic+science
  QA stays YOURS; the supervisor does nothing while you're working.
- **leader dies mid-run → heartbeat goes stale → the (detached) supervisor TAKES OVER** — but in
  fallback mode it does only mechanical/predicate QA + dispatch and **DEFERS every semantic+science
  (research/write/review) verdict to you** (`QA-DEFER`, left in `completed/` for your return — Fix B).
  It never rubber-stamps your science. When you return and re-arm, it stands down again.

**Pure-headless** (no leader session at all): the launchd template launches `supervisor_loop.sh`
directly (no heartbeat → it runs), or `FLEET_SUPERVISOR=1 ./.fleet/start.sh`. The in-shell loop
survives quota cliffs and closed windows but NOT a machine reboot — for reboot-survival add launchd
below.

**ARM REBOOT-SURVIVAL BEFORE ANY LONG/UNATTENDED RUN.** The in-shell supervisor loop dies
with its terminal/login session; a reboot or logout stops it. For a true unattended run,
install ONE durable layer:

1. **launchd (THE answer — one command, no Claude window needed):**
   ```bash
   ./.fleet/install_supervisor.sh            # auto-staggered ~25–35 min cadence
   ./.fleet/install_supervisor.sh --status   # verify · --remove to uninstall
   ```
   On a limit hit the pass parses the reset time from the error ("resets at
   HH:MM") and drains until exactly then; blackout ticks skip instantly (no API
   call). **The first post-reset tick resumes at the TOP ladder model — a fresh
   window is full quota.** Degradation is strictly INTRA-window, driven by
   telemetry where it exists (codex used%); it is never a post-reset hangover.
   The interval is hash-staggered per project so leaders never wake together.
2. **`/loop` in-session — ARM IT AT ENGAGEMENT START, not just overnight:**
   `/loop 20m run the supervisor pass`. This is what makes the INTERACTIVE leader
   session itself auto-resume after a "session limit · resets at HH:MM" cliff:
   blackout-period fires fail harmlessly, and the first post-reset tick executes
   in-session (session's own model, quota now fresh) — QA + dispatch pull the
   pipeline forward with no human nudge, worst case one period late. Without an
   armed cron the session stays silent after reset until a human types — observed
   live; never rely on memory to arm it. Caveats: fires only while the window
   stays open and the REPL is idle; cron is session-only (7-day auto-expiry) —
   which is why launchd (option 1) is the durable default and the two compose.
   **`durable: true` caveat (verified 3× on 2026-06-11): some Claude Code
   runtimes SILENTLY IGNORE the CronCreate durable flag** — the job is created
   but marked session-only and no `.claude/scheduled_tasks.json` is written.
   Never assume it took: verify by (a) the CronCreate result / CronList saying
   "durable" vs "session-only", and (b) `scheduled_tasks.json` actually existing.
   On runtimes that ignore it, an in-session cron is CLIFF-proof but NOT
   restart-proof — the only restart-proof layer is launchd. This is a harness
   limitation, not a skill bug (the skill cannot patch Claude Code; it encodes
   the verification ritual instead).

A blackout's cost then degrades from "all projects stall" to "QA latency ≤ one
tick", because the caretaker keeps promoting pre-authored drafts throughout.
**Corollary: every healthy supervisor pass should leave the drafts queue stocked**
(`create-task --hold`).

## Keep the agents non-stop

Idle workers are wasted capacity. Maintain a backlog so every agent always has work:
- Front-load independent tasks; serialize ONLY genuinely dependent ones, via
  `--depends-on` (not by withholding whole phases — see Parallelism). The DAG
  resolver then keeps the maximum number of tasks runnable at all times.
- **codex must contribute too** — route the hardest tasks to it (contained by
  `codex exec -s workspace-write`, never `--dangerously-bypass-approvals-and-sandbox`).
- When a batch clears, dispatch the next (or let the caretaker promote your drafts).

## Status board — ONE hub, every project

`kanban_hub.py` (global singleton, port **8788**) serves ALL registered projects:
- **Overview tab:** per-project counts + hot items (in-progress, failed) + the global
  capacity line (per-agent used%, rungs, drains) — the "where is it red?" view.
- **Per-project tabs:** the full live board — Pending · In Progress · Done·QA ·
  Approved ✓ (cumulative) · Failed — plus the roster (leader + workers) and the
  auto-derived pipeline line from `.fleet/phases.json`.
- Projects register via `start.sh` / deregister via `stop.sh` (`registry.py` under
  the hood). The hub is read-only and binds 127.0.0.1.

`phases.json` works exactly as in the legacy skill (predicates `count` /
`file_exists` / `process_alive` / `evaluative`; deriver writes, hub reads;
**done › active › blocked › pending**). **Multi-project caveat:** every
`process_alive` `match` MUST include this project's absolute path (e.g. the absolute
script path of the detached run) — a bare script name lights up from another
project's identical process.

**The leader emits `phases.json` (mission-aware authoring on an agnostic scaffold).**
`init_workspace.py` scaffolds a uniform, mission-**agnostic** stub
`{"state":"awaiting_definition","phases":[]}` so the hub + `derive_phases.py` always have a
manifest to read. The mission-**aware** leader then **fills it** at the first planning turn,
once goal/scope is confirmed — via `phases.py`'s `set_phases(root, phases, title)` (each phase
min `{id, name}`; optional `order`/`done_when`/`gate`/`depends_on`) — either deriving from a
predefined long-range pipeline (e.g. `ai-research-pipeline`) **with tailoring to the confirmed
scope**, or self-led through dialogue with the human. A genuinely flat one-shot project calls
`mark_no_pipeline`. While `awaiting_definition`, the board shows `⏳ awaiting leader definition`
and `fleet_health` raises a `phases_undefined` nudge once phase-tagged tasks exist (it surfaces,
never blocks execution). The fleet code stays mission-agnostic — it reads only the generic
manifest + state; *what the phases mean* is the leader's judgment, not the fleet's.

**The leader is the SOLE author** of the phase *definitions* (the list / names / gates /
`done_when`). `derive_phases.py` only syncs each phase's *status* from ground truth — it never
adds, removes, or edits a phase. A predefined long-range task (e.g. `ai-research-pipeline`)
contributes **only a TEMPLATE** — its documented phases — which the leader reads and tailors;
**the pipeline never writes `phases.json` itself.** Generating and updating the manifest is the
leader's job, on the fleet's agnostic scaffold, full stop.

**Record hygiene:** `qa-fail` auto-archives the superseded original; archive resolved
one-off failures too, so the board shows only live + cumulative truth.

## Resilience

- **Atomic claim:** `mv pending/x.json → claimed/<agent>--x.json` (`rename(2)`) —
  exactly one winner under any number of concurrent watchers.
- **Auth self-heal:** 401/credential failures *pause-not-fail* — bounded re-queue +
  backoff (`MAX_AUTH_RETRIES`/`AUTH_BACKOFF`); work resumes when the user refreshes.
- **Quota self-heal:** rate-limit failures bump capacity + reroute (see above).
- **Orphaned claims:** the caretaker (doctor `--fix`) re-queues claims whose agent
  has NO live watcher after a grace period (default 900s), `stuck_count`+1 — a killed
  watcher can no longer strand its claim forever.
- **Machine reboot:** optional launchd plist running `start.sh` at load (RunAtLoad)
  resurrects the whole stack; the crash-only queue makes the restart lossless.

## Long-running jobs — launch DETACHED, never in session background

Any job longer than ~1h MUST run as a fully-detached daemon — NOT via session-bound
`run_in_background`. Use `scripts/detach_run.py` (double-fork + setsid → PPID=1) and
VERIFY detachment (`pgrep -fl <arg>` then `ps -o pid,ppid -p <pid>` → PPID 1). Make
the job checkpoint-and-resume. **Don't over-attribute deaths to the harness** — a
real case OOM-ed identically after detaching; diagnose, fix, verify on a fresh run.
For crash-prone long jobs use `scripts/watchdog.py` (single-restarter `O_EXCL` lock +
crash-loop guard + done-predicate). **GPU exclusivity is machine-global:** use one
well-known lock path (e.g. `~/.fleet/locks/gpu.lock`) so two projects can never race
into two concurrent GPU jobs.

**Autonomous recovery — REGISTER the job (P13).** Detached jobs live outside the task
queue, so register them once and the caretaker keeps their watchdog alive (no human, no
LLM in the recovery path):

```bash
python3 .fleet/jobs.py register --root "$PWD" --id sweep1 \
  --lock ~/.fleet/locks/gpu.lock --done-source experiments/results/sweep.json \
  --done-path per_strength_seed --done-op '>=' --done-value 6 \
  --max-crashes 3 --resume-arg --resume -- /abs/venv/bin/python sweep.py
```

Every `doctor --fix` tick (the caretaker, 60s) then runs `jobs.ensure_watchdogs`: if the
job is done → deregister; if its watchdog PID is dead and the job isn't done → relaunch the
watchdog DETACHED (stale lock from the dead watchdog removed first; the O_EXCL lock still
guarantees exactly one LIVE watchdog, so two concurrent GPU jobs remain impossible). So the
caretaker supervises the watchdog, the watchdog supervises the job, the job's `--resume`
continues — two no-LLM levels. The supervisor pass is still a useful observer (`jobs.py
list`), but recovery no longer waits on it.

**First-class observability — declare a card, report progress.** A detached run is otherwise a
black box on the board (queue tasks get status + log + %; detached used to get none). Make it
first-class:
- **Declare a card with its log:** `orchestrator board-card --id <id> --phase <P> --status running
  --output <result> --log <abs/run.log>` (+ `--predicate`/`--done` for QA). Clicking the card in the
  kanban drawer then shows a bounded tail of `--log` — path-guarded to the card's OWN project
  (resolve→inside-root, symlinks/`..`/cross-project rejected; the 8h log is tailed, never slurped).
- **Launch with `--card`:** `detach_run.py --card <id> --log run.log -- python job.py …` exports
  `FLEET_CARD_ID` and prepends the project's `.fleet/` to `PYTHONPATH`, so the runner AND its
  per-cell `subprocess.run` children can `import fleet_progress`.
- **Report from the inner loop:** `fleet_progress.report(done, total, output=<this cell's output>,
  stage="seed 2/3")`. The id is derived from the OUTPUT path stem → one file per cell at
  `.fleet/status/progress/<id>.json`, so concurrent cells NEVER collide — do not rely on a single
  `FLEET_CARD_ID` for per-cell ids. The kanban renders `stage · done/total · ~pct% · eta`; the call
  is fail-open and throttled but never drops the terminal (100%) tick.
- detach_run stamps a `started_at` stub but CANNOT finalize on exit (execvp replaces it) — the
  runner's `finally` writes the terminal state; doctor's status-scoped sweep cleans stale progress
  (NEVER a running card's). Board writes go through `board_cards.merge_write` so a runner's full
  rebuild never clobbers the leader's `approve-card` verdict (no terminal→done downgrade).

**Agent-side self-notification — arm a cron sentinel, don't rely on a bash watcher.** A
detached run is first-class for the HUMAN (card + kanban % + log) but does NOT re-invoke
the LEADER when it finishes — unlike queue tasks, which get a `wait` sentinel (see *Event-
driven fast path* below). A session-bound watcher (`run_in_background`, `Monitor`) is
killed silently on a new turn or compaction, so a multi-hour/day detached run leaves the
leader blind between manual passes. The fix: the moment you launch a card
(`detach_run.py --card <id> --log <log> -- …`), arm an in-session `CronCreate` sentinel:

```
CronCreate(cron="<off-:00 minute> */1 * * *", recurring=true, durable=false,
  prompt="[<id> sentinel] Check detached job <id>: grep <log> for the latest
  Iter/complete/nan/FAILED; ps the runner; cat .fleet/status/progress/<id>.json.
  If COMPLETE -> set the card done + do the leader semantic/science QA (vs the
  plan/paper); if nan/failure -> report; if running -> one-line progress.
  CronDelete this job on ANY terminal state.")
```

`durable: false` is MANDATORY here, not a stylistic choice — `durable:true` crons share
one `.claude/scheduled_tasks.json` across every project in the runtime (a concurrent-
write race) and fire in ANY idle REPL, not just the one that launched the job (cross-
project mis-firing). A per-session cron only fires in its own session and dies on a full
REPL restart — recoverable, since the on-disk truth (`board_cards.json`,
`.fleet/status/progress/<id>.json`) survives regardless. See the `durable` caveat above
(*Leader continuity*) for the harness-level verification ritual — some runtimes silently
ignore the flag entirely.

**Enforced, not just documented:** `detach_sentinel_reminder.py` (PostToolUse(Bash),
registered by `init_workspace.py`) fires this exact reminder — with the real card id and
log path substituted in — the instant a `detach_run.py --card` command runs, so the
leader cannot forget to arm it. It is a no-op on everything else and is NOT gated on
`AUTONOMOUS_ON`: the forgetting risk exists attended or not.

## Autonomous overnight run mode (unattended)

Combine: `caffeinate -dimsu -t <secs>` (sized to outlast the run) · prompt-free
permissions (installed by init: mode `acceptEdits` + baseline allow/deny) · a shared
brief (`.fleet/BRIEF.md`) every task carries as a context_file · **an armed
supervisor — run `./.fleet/install_supervisor.sh` (launchd, survives limit resets
and closed windows; `/loop 20m` only as the window-open fallback — see Leader
continuity)** · a status log (`.fleet/status/overnight_status.txt`).

**`FLEET_STRICT=1` — the unattended-trust posture (one switch).** The permissive default
ships the QA producers/gates OFF (so a casual run isn't slowed); for genuinely unattended
long-range work, export `FLEET_STRICT=1` before `start.sh` to turn them ON together: the
second-opinion grader at `cmd_qa_pass` (groundedness / anti-fabrication), plus the
`changed_files` (write-scope reconcile) and test-count-growth producers in the watcher.
Each remains individually overridable (`FLEET_GRADER` / `FLEET_TRACK_CHANGES` /
`FLEET_TRACK_TESTS`). Pair with a per-task `--predicate` (mechanical acceptance) so the
no-LLM floor sweep can also auto-PASS work and keep the DAG moving without the leader.

### Prompt-free Bash discipline (command-style only) — MANDATORY in autonomous mode

Scan every Bash command for `` $( `` · `` ${ `` · `` > `` · `` 2> `` · leading `cd` ·
multi-line `python -c` — if present, rewrite (pipes and `&&` are fine; `>>` append
and heredocs are fine):

| ❌ Triggers a prompt | ✅ Use instead |
|---|---|
| `ps -p $(pgrep -f foo)` | `pgrep -fl foo` |
| `kill $(pgrep -f foo)` | `pkill -f foo` (or read the PID, then `kill <pid>`) |
| `cat`/`tail`/`head` for inspection | the **Read tool** |
| `echo "x" > file` / `... 2> err` | the **Write tool** (or **Edit**) |
| `cmd 2>&1 \| grep x` | `cmd \| grep x` |
| `X=$(cmd); use $X` | run the first command, read output, then act |
| `cd /abs/dir && cmd` | put the absolute path inside `cmd` |
| multi-line `python -c "...#..."` | **Write** a `.py` file and run it |
| `awk '{print $1}'` / `sed`-extract for inspection | `grep -oE 'pat'`, or **Read** the file, or a `.py` script — the `$N` field-ref trips the NATIVE `simple_expansion` prompt (a Claude Code permission check, NOT the guard hook), and it prompts even mid-autonomous-run |

> **Two distinct prompt sources.** (a) The **guard hook** (`.fleet/hooks/`) blocks `$(...)`, backticks, `>`/`2>` — that's the table above through the `python -c` row. (b) **Claude Code's native permission system** prompts on patterns like `$N`/`$VAR` (`simple_expansion`) in `awk`/`sed`/inline shell *even when the guard is silent*. Defence in depth: `init_workspace.py`'s `PERM_ALLOW` allowlists the common read-only verbs (`grep ls ps pgrep wc find awk sed …`) so prefix-matched invocations don't prompt — but a stray `$N` can still surface, so for inspection prefer `grep -oE` / the **Read tool**, never `awk '{print $N}'`.

**ENFORCED, not just documented:** `init_workspace.py` installs a PreToolUse(Bash)
hook (`.fleet/hooks/autonomous_bash_guard.py`, registered in `.claude/settings.json`).
While `.fleet/AUTONOMOUS_ON` exists, violating commands are BLOCKED with the rewrite
hint. Quote/heredoc-aware; fails open. Toggle: `touch .fleet/AUTONOMOUS_ON` / delete.

### Supervisor-pass routine (each fire)

1. Read the shared brief, then `orchestrator.py status`.
2. **QA every completed task** (run tests; read piped output, not exit codes; read
   code for semantic bugs). Bounce shortfalls with `qa-fail`.
2b. **STUCK-TASK SWEEP — sentinels catch COMPLETION, never STUCKNESS.** A claimed task
   whose worker child hung (live watcher, but `opencode run`/`kimi -p` blocked on a
   backend stall or tool loop) NEVER reaches a terminal state, so its `wait` sentinel
   just sits until timeout and the claim looks "in progress" forever. For each claimed
   task verify PROGRESS, not just a live process: is `status/logs/task-<id>.log`
   growing and is the `output_file` being written? A worker-log frozen >~15min = hung.
   `doctor.py --fix` (run by the caretaker every 60s) now does this automatically
   (`check_stuck_claims`: kills the hung child + requeues, giving up to `failed` after
   `MAX_STUCK`) — but confirm it's happening; if the caretaker is down, run
   `python3 .fleet/doctor.py --fix` by hand. **Root-cause a stuck wave before blindly
   requeueing**: if ALL workers of a backend hang at once (frozen logs, banner-only
   output), the BACKEND is unreachable (e.g. a VPN that fixes Groq/NotebookLM can block
   China-based GLM/Kimi endpoints) — stop the workers and surface it, don't churn.
3. Author the next wave with `--depends-on` declaring real upstreams (parallel by
   default; the resolver auto-releases); dispatch by token economy; **re-stock the drafts
   queue** for the caretaker.
4. **SAFETY:** never two GPU jobs (global lock); real-mode smoke before trusting an
   experiment runner; RAM < ~75%; never fabricate; stay inside the workspace.
   Timeouts: wrap SHORT experiments in `timeout`; long resumable monitored runs get
   NO hard timeout (size caffeinate instead).
5. Surface FAILED tasks (esp. auth pauses) in the status log.
6. Append the timestamped note; **end the pass — do not idle-poll.**

### Event-driven fast path

Background Bash you launch (`run_in_background`) fires a `<task-notification>` on
exit — that wakes you mid-session. Worker-queue completions do NOT self-notify: arm
one background `orchestrator.py wait --task-id <id>` per in-flight task (generous
`--timeout`), QA the moment one fires, then arm the next. The cron is the safety
net; per-task `wait` sentinels are the fast path. Keep both.

## Version control & worktree isolation

By default all agents write into the same working tree; **the leader controls all
commits** (review diff → QA → commit).

**Per-task worktree isolation (P12, opt-in `FLEET_WORKTREE=1`) — BUILT, not just advised.**
With the flag on, the watcher runs each claimed task in its OWN git worktree
(`.worktrees/<task_id>` on branch `fleet/<task_id>`) via `worktree.py`, so concurrent
writers never collide in the shared tree. The queue stays at the workspace root; only the
agent's run cwd moves. Declared `context_files` are copied into the worktree (so the agent
sees them even when uncommitted). On COMPLETED, `worktree.py finalize` copies the
deliverable back to root (so the QA floor + DAG see it unchanged), commits the branch for
the leader to merge, reports the ACCURATE `changed_files` (single writer per worktree → no
cross-task leakage, which is why `FLEET_WORKTREE=1` is the precise mode for `write_scope`
reconcile), and removes the worktree. Fail-open: a non-git tree or any git error → the
task just runs at root with no isolation. Residual: the per-task `fleet/<task_id>` branches
accumulate — the leader merges + prunes them; the worktree mode assumes context is either
committed or in `context_files` (uncommitted root-only files outside `context_files` aren't
visible in the worktree).

## Multi-Mac scaling (designed, not yet built — P5)

The queue's atomicity holds only on a LOCAL filesystem — never put `.fleet/` on
NFS/SMB/iCloud. The correct extension: **queue stays on the server Mac; remote Macs
contribute execution only.** A watcher variant claims locally, then runs the CLI on
the remote over `ssh -o BatchMode` (rsync task+context out, rsync output_file back),
registered as e.g. `kimi-air` with its own routing entry. Claim semantics, auth/quota
self-heal, and the hub all work unchanged. Note: same accounts = no extra token
capacity — the win is compute/fault isolation (or true capacity with separate
accounts). Implement only after the single-Mac fleet is proven.

## Self-evolution — when you hit a systemic error, improve THIS skill

Treat **recurring failures as skill bugs, not just task bugs**. Classify every error
(one-off → fix the instance; systemic → fix the layer you control: `hooks/`,
`scripts/`, this SKILL.md, `templates/`). Discipline per change (non-negotiable):
(1) VERIFY the fix actually works; (2) add a sibling `test_*.py` (test count grows);
(3) SYNC both copies — the project's `.fleet/` AND this skill dir; (4) LOG it
(CHANGELOG.md) and tell the user; (5) keep it reversible; (6) NEVER disguise a
config/experiment/science change as a "harness fix".

## Files

```
.fleet/                                  (per project)
  schema.py            # task spec contract (validated; + rerouted_from)
  orchestrator.py      # leader's task I/O + QA + drafts (--hold / promote)
  watcher.sh           # per-agent watcher: gate → slot → claim → CLI → result
  start.sh / stop.sh   # pidfile-scoped lifecycle (stop --all = global too)
  kanban_hub.py        # (copy; the RUNNING hub is the global singleton)
  capacity.py          # token-capacity registry CLI (probe/gate/bump/pick)
  capacity_loop.sh     # (copy; runs globally) codex probe + drain expiry
  registry.py          # global project registry ops
  doctor.py            # health checks + mechanical fixes (orphans, drafts)
  caretaker.sh         # no-LLM continuity loop (doctor --fix every 60s)
  supervisor_pass.sh   # headless leader pass, model laddered by capacity
  supervisor_loop.sh   # loops supervisor_pass (autonomous QA, default-on; TCC-safe)
  phase_deriver.sh / derive_phases.py   # phases.json ground-truth deriver
  qa_notify.sh         # event-driven QA notifier (Monitor tool command)
  watchdog.py / detach_run.py           # long-job machinery
  agents/*.json        # routing registry (+ fallbacks, slots, ladders, fanout)
  queue/{drafts,pending,claimed,completed,failed}/
  status/{heartbeat,logs,pids}/
~/.fleet/                                (global)
  projects.json · capacity/ · slots/ · hub.pid · capacity_loop.pid · locks/
```

> Reference (skill repo, not deployed): `reference/multiagent-task-state-flow.md` —
> the full task lifecycle state machine (+ PNG). The fleet adds two edges: drafts →
> pending (promotion) and claimed → pending with reassignment (quota reroute).
