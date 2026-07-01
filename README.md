# multi-agent-cli-orchestration-fleet

**[▶ Live interactive demo](https://aiscorpio.github.io/multi-agent-cli-orchestration-fleet/)** —
a self-playing, ~2-minute simulated walkthrough of the dashboard (one hub, three
projects, a worker fleet claiming and clearing a wave of tasks). All data shown is
synthetic; source in [`demo/`](demo/).

A [Claude Code](https://docs.claude.com/en/docs/claude-code) **skill** that turns Claude into
the orchestrator and QA gate for a small fleet of heterogeneous, autonomous **external CLI
agents** — coordinated through a plain file-based task queue, with no server, database, or
message broker in the loop.

Claude (the "leader") writes task specs, reviews deliverables, and gates quality. External CLI
tools (e.g. `codex`, `kimi`, `opencode`, another `claude` instance) are "workers" that pick up
tasks, do the work, and write results back — each running as an ordinary subprocess on your
machine, driven entirely through their own command-line interfaces.

It scales to **multiple concurrent projects** on one machine, sharing one global capacity budget
and one status dashboard, without one project's backlog starving another.

## Why this exists

Running several CLI-based coding/research agents at once raises problems a single-agent setup
never has to solve:

- **Coordination without a server** — workers must claim work exactly once, survive crashes, and
  never double-execute a task, using nothing but the filesystem.
- **Quality control without trusting the worker** — a worker self-reporting "done" is not the
  same as the deliverable being correct; something has to gate that independently.
- **Shared, scarce token/quota budgets** — most CLI agent subscriptions are flat-rate with rolling
  usage windows. Two projects running the same account can silently starve each other if nothing
  tracks and arbitrates capacity globally.
- **Long-running work** — training runs, sweeps, and backfills can run for hours; they can't sit
  inside a single worker invocation without blocking that worker's slot and hitting CLI timeouts.

This skill's answer to all four is: **a crash-only file queue with atomic claiming, a
model-agnostic QA floor that runs whether or not a leader is present, a global capacity
registry, and a first-class "detached long job" track** — described in detail below.

## Architecture

```
PER-PROJECT (each project root, ".fleet/")         GLOBAL (machine-level, "~/.fleet/")
  queue/{drafts,pending,claimed,                     projects.json     ← registry (hub tabs)
         completed,failed}/                          capacity/<agent>.json ← token/quota windows
  watcher.sh ×N per agent (pidfile-scoped)           slots/<agent>/    ← concurrency caps
  phase_deriver.sh · caretaker.sh                    kanban_hub.py     ← ONE dashboard port,
  orchestrator.py (leader's CLI)                                        all projects as tabs
  supervisor_pass.sh (headless leader)               capacity_loop.sh  ← periodic capacity probe
```

**The split is deliberate.** Queues, watchers, logs, and phases are *project-scoped* — they live
and travel with the project. Token/quota budgets are *account-scoped* — shared by every project
running on the same machine — so capacity tracking, concurrency slots, and the dashboard are
*global singletons*. Conflating the two is what makes most single-project agent setups unable to
run twice on one machine.

**Process safety.** Liveness is tracked via pidfiles (`.fleet/status/pids/`), each verified as
"process alive AND command line matches" before anything is counted or killed — never a bare
`pgrep`/`pkill -f <name>`. This is what lets project A's `stop.sh` run without ever touching
project B's workers.

### Core mechanics

- **Atomic claiming.** A task moves `pending/ → claimed/` via `rename(2)`, which is atomic on a
  local filesystem — exactly one watcher instance wins a race for any given task, with no lock
  file and no coordinator process.
- **QA floor (no-LLM).** Every completed task is checked against a deterministic floor before any
  model ever reviews it: does the declared output artifact exist and is it non-trivial, do all
  declared acceptance predicates hold, does the write-scope match what was actually touched, did
  the test count grow (for code tasks that declare tests). A caretaker loop (`doctor.py`, run via
  cron/supervisor, no LLM involved) sweeps this continuously — so tasks with fully mechanical
  acceptance criteria auto-pass or auto-fail even while no leader session is active. A task
  with no declared predicates still defers to semantic review — the floor never claims novel
  correctness it can't check.
- **Two dispatch tracks.** Short, single-deliverable work goes through the **queue**
  (`create-task`, gated by the QA floor + leader `qa-pass`/`qa-fail`). Long-running work, or work
  that holds a scarce resource or produces a *run* rather than a single file, goes through
  **detach** (`detach_run.py` + `jobs.py register`, surfaced as a card on the dashboard, gated by
  a completion predicate + leader `approve-card`/`reject-card`). Each track has exactly one QA
  owner — there's no double-gating.
- **Capacity-aware scheduling.** A capacity registry tracks each agent's real usage telemetry
  (where the CLI exposes it) or reactive bump/drain (on an observed quota error), and gates new
  claims and reroutes tasks away from an exhausted agent to a configured fallback. Where an agent
  supports a reasoning-effort knob, a ladder degrades effort under pressure rather than refusing
  work outright.
- **Observability.** A single dashboard (`kanban_hub.py`, one port, tabs per project) renders
  every task/card's live status. Long detached jobs get per-card progress (`done/total`, ETA) when
  their runner opts in to reporting it, and a caretaker-maintained **liveness floor** (log
  size/mtime, or a real percentage from a count-style completion predicate) as an always-on
  fallback so a running card never looks silently dead even if its runner never calls the
  progress API.
- **Recovery, without a human.** Orphaned claims (pidfile says dead), stuck tasks (log hasn't
  moved past a staleness threshold), and dead detached-job watchdogs are all detected and repaired
  by the same no-LLM caretaker loop — restart-safe and idempotent.

### Honest limitations

- The queue's atomicity depends on a **local filesystem**. Never point `.fleet/` at
  NFS/SMB/iCloud-synced storage.
- There is no cross-machine worker pooling yet — running this on a second Mac gives you a
  **second, independent fleet**, not shared capacity with the first. A remote-execution extension
  (local queue, SSH-driven remote workers) is designed but not built.
  See `SKILL.md` → *Multi-Mac scaling*.
- A command-type acceptance predicate executes under the same trust domain as the QA check
  itself — treat predicates you author the same as any other code you'd run locally.
  For detached cards specifically, the no-LLM caretaker never executes a card's command
  predicate on its own; only a present leader's explicit `approve-card` does.
- Write-scope / changed-file tracking is only strictly enforced under git-worktree task
  isolation (opt-in); in a shared working tree without isolation it's advisory.
- The default supervisor loop survives quota cliffs and closed rate-limit windows but not a
  machine reboot, unless you additionally install the optional `launchd` template for
  reboot-survival.
- There's no dependency-pinned token/cost meter for CLIs that don't expose one — capacity control
  falls back to reactive bump/drain on an observed quota error for those agents.

## Requirements

- **macOS.** Process supervision (pidfile+cmdline verification), the optional reboot-survival
  path (`launchd`), and the health checks assume a Darwin host. Not tested on Linux/Windows.
- **Python 3**, standard library only — there is no `requirements.txt` to install; every script
  runs against a stock interpreter.
- **[Claude Code](https://docs.claude.com/en/docs/claude-code)**, since this ships as a Claude
  Code skill and Claude plays the orchestrator/leader role.
- One or more **external CLI agent tools**, each authenticated independently. The included
  routing templates (`templates/agents/*.json`) target four out of the box:
  - `codex` (OpenAI Codex CLI)
  - `kimi` (Kimi CLI)
  - `opencode` (OpenCode CLI)
  - a second, headless `claude` instance (Claude Code CLI, non-interactive `-p` mode)

  You don't need all four — start.sh accepts a subset (`./.fleet/start.sh kimi opencode`), and
  adding a new CLI-drivable agent is a matter of adding a routing JSON plus a matching `cli`
  invocation string.

## Installation

The skill's own scripts resolve shared/global code from a fixed path
(`~/.claude/skills/multi-agent-cli-orchestration-fleet/`), so it must be installed at exactly
that location — not a renamed directory.

```bash
git clone <this-repo-url> ~/.claude/skills/multi-agent-cli-orchestration-fleet
```

Then, for each project you want to run a fleet in:

```bash
cd /path/to/your/project
python3 ~/.claude/skills/multi-agent-cli-orchestration-fleet/scripts/init_workspace.py .
```

This scaffolds `.fleet/` in the project (queue directories, runtime scripts, the agent routing
registry, `.gitignore` entries, and an autonomous-mode guard hook).

1. Authenticate each CLI you intend to use (`codex`, `kimi login`, `opencode`, `claude`) —
   independently of this skill, per that tool's own auth flow.
2. Bring the project's stack up:
   ```bash
   ./.fleet/start.sh                    # all configured agents + caretaker + phase deriver
   ./.fleet/start.sh kimi opencode      # only the named agents
   KIMI_INSTANCES=4 ./.fleet/start.sh   # override per-agent instance counts
   ```
   The first project's `start.sh` on a machine also launches the **global dashboard**
   (`http://127.0.0.1:8788` by default) and the capacity loop; every subsequent project just
   registers and appears as a tab.
3. Stop a project's stack with `./.fleet/stop.sh` (add `--all` to also tear down the global
   dashboard/capacity loop, once no project needs it).

## Basic usage

Dispatch a short, single-deliverable unit of work through the queue:

```bash
python3 .fleet/orchestrator.py create-task \
  --phase <id> --type research|code|test|write|review \
  --assign codex|kimi|opencode|claude|any \
  --title "..." --description "self-contained task description" \
  --output-file "relative/path/to/deliverable" \
  --criteria "criterion 1" "criterion 2" \
  --context-files "path/a" "path/b"
```

Review and gate a completed task:

```bash
python3 .fleet/orchestrator.py list
python3 .fleet/orchestrator.py read-result <task-id>
python3 .fleet/orchestrator.py qa-pass <task-id> --reason "..."
python3 .fleet/orchestrator.py qa-fail <task-id> --reason "..."
```

For anything long-running (roughly an hour or more), holding an exclusive resource for a long
time, or producing a run/side-effect rather than one file, use the **detach** track
(`detach_run.py` + `jobs.py register`) instead of a queue task — see `SKILL.md` → *Long-running
jobs* for the full walkthrough, including the observability and approval flow.

The live dashboard at `http://127.0.0.1:8788` shows every project's queue and detached-job cards
in one place.

## Testing

```bash
cd ~/.claude/skills/multi-agent-cli-orchestration-fleet
python3 -m pytest dev/tests/
```

The test suite (verifier-first: every capability above was landed behind a failing test first)
covers the queue lifecycle, QA floor, capacity registry, detached-job machinery, dashboard
rendering, and recovery/GC paths.

## Project layout

```
SKILL.md              # full operational reference (read this for the complete picture)
scripts/               # runtime: orchestrator, watcher, QA floor, capacity, dashboard, doctor...
templates/agents/       # per-agent routing config (model, role, concurrency, fallback, ladder)
templates/launchd/      # optional reboot-survival supervisor template
reference/              # task-state-flow diagram + notes
dev/tests/               # pytest suite
```

## Status

This is a personal tool, evolved through real production use rather than designed upfront —
`SKILL.md` includes an honest, dated capability matrix (what's actually wired vs. designed-but-
not-built) rather than a marketing description. Read it before relying on any specific behavior
not covered above.
