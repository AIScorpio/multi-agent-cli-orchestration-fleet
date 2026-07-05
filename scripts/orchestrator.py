#!/usr/bin/env python3
"""
Orchestrator CLI — the leader's interface to the fleet task queue.

Usage (the leader calls these via Bash tool):
  python3 .fleet/orchestrator.py status
  python3 .fleet/orchestrator.py create-task --phase 1 --type research \\
      --assign kimi --title "..." --description "..." \\
      --output-file "project/databases/result.md" \\
      --criteria "criterion 1" "criterion 2" \\
      --context-files "project/databases/qg0-report.md" [--hold]
  python3 .fleet/orchestrator.py promote <task-id>
  python3 .fleet/orchestrator.py list [--state pending|claimed|completed|failed|drafts]
  python3 .fleet/orchestrator.py read-result <task-id>
  python3 .fleet/orchestrator.py qa-pass <task-id>
  python3 .fleet/orchestrator.py qa-fail <task-id> --reason "..."
  python3 .fleet/orchestrator.py wait --task-id <id> [--timeout 300]
  python3 .fleet/orchestrator.py cancel <task-id>

DRAFTS (leader-continuity): `create-task --hold` enqueues into queue/drafts/
instead of pending/. Drafts are invisible to watchers until promoted — either
explicitly (`promote <id>`) or mechanically by the caretaker when the live
backlog runs low. Pre-authoring the next wave as drafts is what keeps workers
fed through a leader quota blackout.
"""
import argparse, json, os, sys, time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent))
from schema import TaskSpec
import ledger

MA   = Path(__file__).parent
ROOT = MA.parent
QUEUE = MA / "queue"
STATUS = MA / "status"
FLEET_HOME = Path(os.environ.get("FLEET_HOME", Path.home() / ".fleet"))


# ── Helpers ──────────────────────────────────────────────────────────────────

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _alert(atype: str, detail: str) -> None:
    """Emit a fleet alert (alerts.jsonl + OS toast) — used so a fail-OPEN in the QA floor
    is NEVER silent (P14): if the mechanical gate can't run, the operator must SEE that the
    gate degraded to a pass, not discover it after a regression shipped. Fail-open itself."""
    try:
        import fleet_health
        fleet_health.emit_alerts(FLEET_HOME, [{"type": atype, "detail": detail}])
    except Exception:
        pass

def _atomic_write(path: Path, content: str) -> None:
    """Write to a temp sibling, BYTE-VERIFY (written size == expected) and fsync,
    then rename. A short write / ENOSPC must NEVER replace the destination with a
    truncated file — on a full disk the old tmp-then-rename silently atomically
    swapped good queue state for garbage. On mismatch: drop the partial tmp and
    raise, leaving the destination untouched."""
    data = content.encode("utf-8")
    tmp = path.with_suffix(".tmp")
    tmp.write_text(content, encoding="utf-8")
    try:
        written = tmp.stat().st_size
    except OSError:
        written = -1
    if written != len(data):
        tmp.unlink(missing_ok=True)
        raise OSError(f"_atomic_write: short write to {tmp} "
                      f"({written} != {len(data)} bytes) — destination left intact")
    try:                                  # durability: flush bytes to disk before rename
        fd = os.open(str(tmp), os.O_RDONLY)
        os.fsync(fd)
        os.close(fd)
    except OSError:
        pass
    tmp.rename(path)

def _find_spec(state: str, task_id: str) -> Optional[Path]:
    """Find the task spec {task_id}.json in a state (never the .result.json sidecar).
    Handles the claimed/ prefix form {agent}--{task_id}.json."""
    d = QUEUE / state
    exact = d / f"{task_id}.json"
    if exact.exists():
        return exact
    for m in d.glob(f"*--{task_id}.json"):   # claimed: agent--task.json
        return m
    return None

def _find_result(task_id: str) -> Optional[Path]:
    """Find the result sidecar {task_id}.result.json in completed, then failed."""
    for state in ("completed", "failed"):
        p = QUEUE / state / f"{task_id}.result.json"
        if p.exists():
            return p
    return None

def _find_spec_any(task_id: str) -> Optional[tuple[str, Path]]:
    """Find the task spec across all states. Returns (state, path) or None."""
    for state in ("drafts", "pending", "claimed", "completed", "failed"):
        p = _find_spec(state, task_id)
        if p:
            return state, p
    return None


# ── Commands ─────────────────────────────────────────────────────────────────

def cmd_merge_task(args) -> None:
    """Integrate a worktree-isolated task's `fleet/<id>` branch into the current branch and
    prune it (P14). Use after qa-pass for a `FLEET_WORKTREE=1` task whose full change set
    (not just output_file) must land — the migration/refactor integration step."""
    try:
        import worktree
        ok = worktree.merge(ROOT, args.task_id)
    except Exception:
        ok = False
    if ok:
        print(f"✓ merged worktree branch fleet/{args.task_id} → current branch")
    else:
        print(f"✗ no merge for {args.task_id} (no branch / conflict / not a git repo)")
        sys.exit(1)


def cmd_metrics(_args) -> None:
    """Replay the event ledger (events.jsonl) into a summary — the durable READER the
    audit trail lacked (P7). Counts by event type + the last few events, so an unattended
    run is auditable from the CLI (and the ledger is no longer write-only)."""
    events = ledger.read(MA)
    print("=== Fleet Event Metrics ===\n")
    if not events:
        print("  (no events recorded yet — events.jsonl empty or absent)")
        return
    counts: dict = {}
    for e in events:
        counts[e.get("type", "?")] = counts.get(e.get("type", "?"), 0) + 1
    print(f"  total events: {len(events)}")
    for t, n in sorted(counts.items(), key=lambda kv: -kv[1]):
        print(f"    {t:12s} {n}")
    print("\n  recent:")
    for e in events[-8:]:
        print(f"    {e.get('type','?'):10s} {e.get('task_id','')} {e.get('status','')}")


def cmd_status(_args) -> None:
    """Print live queue status computed directly from the filesystem."""
    drafts    = sorted((QUEUE / "drafts").glob("*.json")) if (QUEUE / "drafts").is_dir() else []
    pending   = sorted((QUEUE / "pending").glob("*.json"))
    claimed   = sorted((QUEUE / "claimed").glob("*.json"))
    completed = sorted((QUEUE / "completed").glob("*.result.json"))
    failed    = sorted((QUEUE / "failed").glob("*.result.json"))

    print("=== Fleet Queue Status ===\n")
    print(f"  drafts:    {len(drafts)}   (held — invisible to watchers)")
    print(f"  pending:   {len(pending)}")
    print(f"  claimed:   {len(claimed)}   (in progress)")
    print(f"  completed: {len(completed)}")
    print(f"  failed:    {len(failed)}")

    if claimed:
        print("\n── In progress ──────────────────────────")
        for f in claimed:
            try:
                d = json.loads(f.read_text())
                agent = f.name.split("--", 1)[0]
                print(f"  [{agent}] {d.get('task_id','?')}  {d.get('title','')[:48]}")
            except Exception:
                pass

    recent = sorted(completed + failed, key=lambda p: p.stat().st_mtime, reverse=True)[:8]
    if recent:
        print("\n── Recent results ───────────────────────")
        for f in recent:
            try:
                d = json.loads(f.read_text())
                mark = "✓" if d.get("status") == "COMPLETED" else "✗"
                print(f"  {mark} {d.get('task_id','?')} [{d.get('agent','?')}] "
                      f"{d.get('status','?'):9s} {d.get('title','')[:40]}")
            except Exception:
                pass


def _validate_phase(phase: str) -> None:
    """REJECT (hard-fail) if --phase matches no phases.json pipeline phase. Every task MUST map to a
    defined pipeline phase — an orphan-phase task desyncs the kanban (the pipeline line goes pending +
    a junk bucket appears in the per-phase counts). This is the construction-time chokepoint:
    PREVENTION, not after-the-fact alerting. The TaskSpec schema enforces the same invariant for any
    non-CLI construction path. Accepts both the id form ("P4") and the bare number ("4"). A
    mission-agnostic project (no phases.json) is a NO-OP.
    """
    pf = MA / "phases.json"
    if not pf.exists():
        return  # mission-agnostic project: no pipeline to match against
    try:
        ids = [p.get("id") for p in json.loads(pf.read_text()).get("phases", []) if p.get("id")]
    except Exception:
        return
    sids = [str(i) for i in ids]  # phases.json ids may be ints (set_phases accepts them)
    nums = [s[1:] if s.startswith("P") else s for s in sids]
    if not ids or str(phase) in sids or str(phase) in nums:
        return
    print("=" * 64)
    print(f"ERROR: --phase '{phase}' matches NO pipeline phase in phases.json — REJECTED.")
    print("  Every task MUST map to a pipeline phase, else the kanban desyncs.")
    print(f"  Valid phase ids: {', '.join(sids)}")
    print("  Fix: use an existing phase, OR define a new phase in phases.json first, then re-create.")
    print("=" * 64)
    raise SystemExit(2)


WORKHORSES = ("kimi", "opencode")


def _live_instances(agent: str) -> int:
    """Count live watcher instances for an agent via this project's pidfiles."""
    n = 0
    for pf in (MA / "status" / "pids").glob(f"watcher-{agent}-*.pid"):
        try:
            os.kill(int(pf.read_text().strip()), 0)
            n += 1
        except (ValueError, OSError):
            pass
    return n


def _pinned_load(agent: str) -> int:
    """Tasks explicitly pinned to `agent` across pending + claimed."""
    n = 0
    for state in ("pending", "claimed"):
        for f in (QUEUE / state).glob("*.json"):
            if f.name.startswith("."):
                continue
            try:
                if json.loads(f.read_text()).get("assigned_to") == agent:
                    n += 1
            except Exception:
                continue
    return n


def _advise_routing(assign: str) -> None:
    """Warn-only saturation check: pinning a workhorse past its live instance
    count while its peer has idle capacity wastes flat-rate throughput (a real
    leader once queued 6 code tasks on opencode while kimi×3 sat idle — label
    dogma; both workhorses write code). Never blocks — labels are comparative
    advantage, the leader may have a reason."""
    if assign not in WORKHORSES:
        return
    peer = "opencode" if assign == "kimi" else "kimi"
    mine = _live_instances(assign)
    if mine == 0:
        return                      # no liveness info → stay quiet
    load = _pinned_load(assign) + 1  # +1 = the task being created now
    peer_live = _live_instances(peer)
    if load > mine and peer_live > 0:
        print("=" * 64)
        print(f"ROUTING ADVISORY: '{assign}' now carries {load} pinned task(s) on "
              f"{mine} live instance(s), while {peer_live} {peer} instance(s) are up.")
        print("  best_for labels are comparative advantage, NOT partitions —")
        print("  both workhorses write code. Prefer --assign any (the atomic claim")
        print(f"  race load-balances automatically) or spill this one to {peer}.")
        print("=" * 64)


def _all_known_task_ids() -> set:
    """Every task_id currently in any queue state (for dependency validation)."""
    ids = set()
    for state in ("drafts", "pending", "claimed", "completed", "failed"):
        d = QUEUE / state
        if not d.is_dir():
            continue
        for f in d.glob("*.json"):
            if f.name.startswith("."):
                continue
            try:
                ids.add(json.loads(f.read_text()).get("task_id", ""))
            except Exception:
                pass
        qa = d / "qa-passed"
        if qa.is_dir():
            for f in qa.glob("*.json"):
                try:
                    ids.add(json.loads(f.read_text()).get("task_id", ""))
                except Exception:
                    pass
    ids.discard("")
    return ids


def _validate_depends_on(deps: list, new_task_id: str) -> None:
    """Warn (never block) on the two declaration mistakes the framework CAN catch
    without knowing the project: a dep id that matches no known task, and a
    self-dependency. (Full cycle detection across drafts is done by the resolver,
    which surfaces a never-releasable set.) `phase:<id>` deps are not id-checked —
    they resolve against whatever carries that phase at release time."""
    if not deps:
        return
    concrete = [d for d in deps if not d.startswith("phase:")]
    if new_task_id in concrete:
        print(f"WARNING: task depends on ITSELF ({new_task_id}) — it will never release.")
    known = _all_known_task_ids()
    missing = [d for d in concrete if d != new_task_id and d not in known]
    if missing:
        print("=" * 64)
        print(f"WARNING: depends_on references unknown task id(s): {', '.join(missing)}")
        print("  A draft whose dep never reaches qa-passed is held forever.")
        print("  Forward refs are OK if you create the producer next; otherwise fix the id.")
        print("=" * 64)


def cmd_create_task(args) -> None:
    """Create and enqueue a new task (atomic write to pending/ or drafts/)."""
    _validate_phase(args.phase)
    _advise_routing(args.assign)
    deps = args.depends_on or []
    # Parse --predicate JSON strings into acceptance_predicates (machine-checkable
    # acceptance enforced at qa-pass). A malformed predicate is a hard error — better
    # to fail loudly at creation than to silently drop a quality gate.
    predicates = []
    for p in (getattr(args, "predicate", None) or []):
        predicates.append(json.loads(p) if isinstance(p, str) else p)
    task = TaskSpec(
        title=args.title,
        phase=args.phase,
        type=args.type,
        description=args.description,
        assigned_to=args.assign,
        output_file=args.output_file,
        acceptance_criteria=args.criteria,
        context_files=args.context_files or [],
        priority=args.priority,
        depends_on=deps,
        acceptance_predicates=predicates,
        write_scope=getattr(args, "write_scope", None) or [],
    )
    _validate_depends_on(deps, task.task_id)
    # A task WITH dependencies is held by construction — it must not be claimable
    # until its deps clear. The resolver (doctor) promotes it when satisfied.
    # Explicit --hold also holds (the leader pre-authoring a wave).
    state = "drafts" if (args.hold or deps) else "pending"
    (QUEUE / state).mkdir(parents=True, exist_ok=True)
    target = QUEUE / state / task.filename
    _atomic_write(target, task.to_json())
    if deps and not args.hold:
        held = f"  (HELD — blocked on {len(deps)} dep(s); auto-releases when satisfied)"
    elif args.hold:
        held = "  (HELD as draft — promote explicitly or let the caretaker)"
    else:
        held = ""
    print(f"✓ Task created: {task.task_id}{held}")
    print(f"  Title:      {task.title}")
    print(f"  Assigned:   {task.assigned_to}")
    print(f"  Phase:      {task.phase}")
    if deps:
        print(f"  Depends on: {', '.join(deps)}")
    print(f"  Output:     {task.output_file}")
    print(f"  File:       {target}")


def cmd_promote(args) -> None:
    """Promote a held draft into the live pending queue."""
    p = _find_spec("drafts", args.task_id)
    if not p:
        print(f"Task {args.task_id} not found in drafts")
        sys.exit(1)
    target = QUEUE / "pending" / p.name
    p.rename(target)
    print(f"✓ Promoted {args.task_id} → pending")
    _journal(f"PROMOTE  {args.task_id}")
    ledger.append(MA, "promote", task_id=args.task_id)


def cmd_list(args) -> None:
    """List tasks, optionally filtered by state."""
    states = [args.state] if args.state else ["drafts", "pending", "claimed", "completed", "failed"]
    for state in states:
        d = QUEUE / state
        files = sorted(d.glob("*.json")) if d.is_dir() else []
        if not files:
            continue
        print(f"\n── {state.upper()} ({len(files)}) ─────────────────────")
        for f in files:
            try:
                data = json.loads(f.read_text())
                print(
                    f"  {data.get('task_id','?'):20s} "
                    f"[p={data.get('priority',5)}] "
                    f"{data.get('assigned_to','?'):10s} "
                    f"{data.get('title','')[:50]}"
                )
            except Exception:
                print(f"  {f.name} (unreadable)")


def cmd_read_result(args) -> None:
    """Display a finished task: result sidecar + acceptance criteria + deliverable."""
    task_id = args.task_id
    result_file = _find_result(task_id)

    if not result_file:
        found = _find_spec_any(task_id)
        if found:
            state, path = found
            print(f"Task {task_id} is in state: {state} (not yet finished)")
            print(path.read_text())
        else:
            print(f"Task {task_id} not found in any queue state")
        return

    r = json.loads(result_file.read_text())
    print(f"\n{'═'*60}")
    print(f"Task:      {r.get('task_id')}")
    print(f"Title:     {r.get('title','')}")
    print(f"Status:    {r.get('status')}")
    print(f"Agent:     {r.get('agent')}")
    print(f"Exit code: {r.get('exit_code')}")
    print(f"Completed: {r.get('completed_at')}")
    print(f"Output:    {r.get('output_file')}")
    print(f"Log:       {r.get('log')}")

    # Acceptance criteria from the spec — what the leader QAs against
    spec = _find_spec("completed", task_id) or _find_spec("failed", task_id)
    if spec:
        try:
            crit = json.loads(spec.read_text()).get("acceptance_criteria", [])
            if crit:
                print(f"{'─'*60}\nAcceptance criteria (QA against these):")
                for i, c in enumerate(crit, 1):
                    print(f"  {i}. {c}")
        except Exception:
            pass

    # Deliverable = the output file the agent wrote
    print(f"{'─'*60}")
    out_path = ROOT / r.get("output_file", "")
    if out_path.exists():
        print(f"Deliverable ({r.get('output_file')}):\n")
        print(out_path.read_text())
    else:
        print(f"(output file not found: {r.get('output_file')})")
        log = Path(r.get("log", ""))
        if log.exists():
            print(f"\nLog tail:\n{log.read_text()[-1500:]}")


def cmd_qa_pass(args) -> None:
    """Mark a completed task as QA-passed — AFTER the mechanical QA floor passes.
    P5: the floor (artifact_ok + acceptance_predicates) runs at the real entry point
    so junk can't be passed; a failure auto-bounces via qa-fail. The floor fails
    CLOSED on a bad artifact, but fail-OPEN on an infra error (don't stall on a broken
    checker)."""
    result_file = _find_result(args.task_id)
    if not result_file or result_file.parent.name != "completed":
        print(f"No completed result found for {args.task_id}")
        sys.exit(1)

    spec = _find_spec("completed", args.task_id)

    # ── Mechanical QA floor (P5) — runs BEFORE the pass ──────────────────────
    failures = []
    grader_info = {"ran": False}          # P15: recorded in the accept-verdict sidecar
    if not spec:
        # No spec → the floor CANNOT run; a result with no task spec must NOT be
        # blanket-passed unchecked (P14.2 — the 10th eval's primary-gate hole). Fail-CLOSED
        # and alarm (not silent).
        _alert("qa_floor_error",
               f"{args.task_id}: completed result has NO task spec — cannot verify "
               f"acceptance; blanket-pass refused, bounced to qa-fail")
        cmd_qa_fail(argparse.Namespace(
            task_id=args.task_id,
            reason="QA floor — no task spec found; cannot verify acceptance (fail-closed)"))
        return
    if spec:
        try:
            sd = json.loads(spec.read_text())
        except Exception:
            sd = {}
        # The ONE mechanical floor (P9): artifact_ok + predicates + write-scope reconcile
        # + test-count growth, all fail-CLOSED on a checker error / empty output_file,
        # fail-OPEN only if the checker modules can't import. Shared verbatim with the
        # deterministic doctor.sweep_qa_floor so QA's mechanical teeth are identical
        # whether the leader or the no-LLM caretaker fires them.
        try:
            import qa_floor
            try:
                rd = json.loads(result_file.read_text())
            except Exception:
                rd = {}
            _ok, _failures = qa_floor.evaluate(sd, ROOT, rd)
            failures.extend(_failures)
        except Exception as e:
            # Fail-open (can't gate) — but NOT silent (P14): alarm so a degraded gate
            # that quietly lets work through is visible, not a hidden blanket PASS.
            _alert("qa_floor_error",
                   f"{args.task_id}: mechanical QA floor could not run ({e}) — "
                   f"PASSED WITHOUT the floor; check the checker modules")

        # ── Opt-in LLM grader (P6/P7) — OFF by default (FLEET_GRADER=1 to enable). ──
        # Semantic acceptance check ON TOP of the mechanical floor for tasks whose quality
        # a regex/command can't capture (research/writing). P7: the task's context_files
        # are fed as SOURCES so the grader can check GROUNDEDNESS (anti-fabrication), not
        # just plausibility. Fail-CLOSED on a parseable NO; fail-OPEN on grader infra error.
        # Auto-arm the grader for the FABRICATION-PRONE task types (research/write/review)
        # even without FLEET_GRADER/STRICT (P16): a content deliverable gets a groundedness
        # check against its sources by default; code/test stays grader-free unless enabled.
        _content_task = sd.get("type") in ("research", "write", "review")
        # The groundedness grader checks that a deliverable's factual claims trace to its SOURCES —
        # that rubric fits research/write OUTPUTS, but a `review` is a CRITIQUE (legitimately
        # adversarial; it may cite its own reproduction run that no static source can ground), so the
        # groundedness grader FALSE-BOUNCES legitimate findings into retry churn. NEVER groundedness-
        # grade a review: its quality is judged by the leader who consumes it (semantic QA = leader's).
        # NOTE: fallback-deferral below still keys on _content_task, so a review is STILL left for the
        # true leader when the fleet is in fallback mode — only the wrong-rubric grader is removed.
        _grounded_task = sd.get("type") in ("research", "write")
        # Fix B — FALLBACK deferral. When the true leader is GONE (FLEET_FALLBACK_QA=1, set by
        # supervisor_pass.sh), the fallback must NOT make a semantic+science verdict on a content
        # deliverable — that's the true leader's exclusive job (same 'defer' philosophy as the
        # no-LLM caretaker's floor_decision). Defer UNLESS the task is mechanically
        # predicate-defensible (the floor already verified its acceptance_predicates above).
        _fallback = os.environ.get("FLEET_FALLBACK_QA") == "1"
        # Leader override (attended only): the leader IS the final semantic authority — a
        # second-opinion judge exists to scale QA, not to overrule a leader who personally
        # verified the deliverable. Never honored in fallback mode: the supervisor is not
        # the leader (Fix B) and must not skip semantic QA on its behalf.
        _leader_verified = bool(getattr(args, "leader_verified", False)) and not _fallback
        if (getattr(args, "leader_verified", False)
                and not (getattr(args, "reason", None) or "").strip()):
            print("ERROR: --leader-verified requires --reason — the leader's verification "
                  "rationale is the audit trail that replaces the grader's verdict.")
            raise SystemExit(2)
        if not failures and _fallback and _content_task and not sd.get("acceptance_predicates"):
            print(f"↩ {args.task_id} deferred to the true leader (fallback mode: content task "
                  f"with no machine-checkable acceptance — semantic+science QA is the leader's)")
            _journal(f"QA-DEFER {args.task_id} (fallback: content, no predicates → leader)")
            try:
                ledger.append(MA, "qa-defer", task_id=args.task_id,
                              reason="fallback content task, no predicates → leader")
            except Exception:
                pass
            return
        # Opt-in / auto-armed grader. In fallback mode SKIP the grader for content tasks — we do
        # not trust the fallback's semantic verdict; predicate-defensible content already passed
        # the floor, everything else was deferred above.
        if _leader_verified and not failures:
            grader_info = {"ran": False, "leader_verified": True}
        if not failures and not _leader_verified and not (_fallback and _content_task) and (
                _grounded_task
                or ((os.environ.get("FLEET_GRADER") == "1"
                     or os.environ.get("FLEET_STRICT") == "1")
                    and sd.get("type") != "review")):
            try:
                import grader
                deliverable = (ROOT / sd.get("output_file", "")).read_text()
                src_parts = []
                for cf in (sd.get("context_files") or []):
                    try:
                        src_parts.append(f"# {cf}\n" + (ROOT / cf).read_text())
                    except Exception:
                        pass
                sources = "\n\n".join(src_parts) or None
                # P2(boundary): for content tasks the grader must be INDEPENDENT of the
                # leader's model (claude) — resolve a non-leader judge and record which model
                # judged, so verdict.json shows the honesty check was external.
                gmodel = grader.resolve_grader_model(_content_task)
                verdict = grader.grade(deliverable, sd.get("acceptance_criteria") or [],
                                       sources=sources, model=gmodel,
                                       independent=_content_task)
                grader_info = {"ran": True, "ok": bool(verdict.get("ok")),
                               "reasons": verdict.get("reasons", []),
                               "model": verdict.get("model", gmodel)}
                if not verdict.get("ok"):
                    # keep the grader's full complaint (a 200-char cap hid the actual
                    # failing criterion behind the preamble — observed live P22)
                    failures.append("grader: " + "; ".join(verdict.get("reasons", []))[:1500])
            except Exception as e:
                # For a CONTENT task (research/write/review) the grader IS the anti-fab
                # gate → fail-CLOSED: a judge that can't run must NOT pass the deliverable
                # (P17). For the explicit opt-in path on a non-content task, stay fail-OPEN
                # (don't stall) — the mechanical floor already gated it.
                if _grounded_task:
                    failures.append(f"grader infra error (content task, fail-closed): {e}")
    if failures:
        reason = "QA floor — " + "; ".join(failures)
        print(f"✗ {args.task_id} blocked by QA floor → auto-qa-fail: {reason}")
        cmd_qa_fail(argparse.Namespace(task_id=args.task_id, reason=reason))
        return

    qa_dir = QUEUE / "completed" / "qa-passed"
    qa_dir.mkdir(exist_ok=True)
    result_file.rename(qa_dir / result_file.name)
    if spec:
        spec.rename(qa_dir / spec.name)
    # ── Accept-verdict sidecar (P15) — pin the WHY next to the status, symmetric with the
    # reject path's retry_reason. The reason a task was CLOSED (the bar it was judged
    # against, the machine-checkable predicates it cleared, the grader's verdict, and the
    # leader's optional rationale) is now DURABLE on disk, so it survives compaction instead
    # of living only in the leader's conversation. Best-effort: never block the pass on it.
    try:
        reason = getattr(args, "reason", None) or ""
        verdict_doc = {
            "task_id": args.task_id,
            "verdict": "qa-passed",
            "accepted_at": _now(),
            "reason": reason,                       # leader / sweep rationale (free text)
            "judged_against": sd.get("acceptance_criteria", []),
            "predicates_enforced": sd.get("acceptance_predicates", []),
            "grader": grader_info,
        }
        _atomic_write(qa_dir / f"{args.task_id}.verdict.json",
                      json.dumps(verdict_doc, indent=2))
    except Exception:
        pass
    print(f"✓ {args.task_id} marked QA-PASSED (floor: clean)")
    _journal(f"QA-PASS  {args.task_id}"
             + (f"  reason: {getattr(args, 'reason', None)}" if getattr(args, "reason", None) else ""))
    ledger.append(MA, "qa-pass", task_id=args.task_id,
                  reason=(getattr(args, "reason", None) or ""))
    # Fast path: immediately release any dependents this producer just unblocked
    # (the periodic caretaker resolve is the backstop). Fail-open.
    try:
        import doctor
        released = doctor.release_dependents(args.task_id, fix=True, quiet=True)
        if released:
            print(f"  ↳ released {released} dependent(s) unblocked by {args.task_id}")
    except Exception:
        pass
    # Autonomous worktree integration (P20): if this task ran isolated, merge its
    # fleet/<id> branch into the working tree now — no human merge-task needed. No-ops when
    # there's no branch (non-worktree task / non-git); a real conflict aborts cleanly,
    # KEEPS the branch, and alerts (worktree.merge, P16). Runs on the no-LLM sweep too
    # (it shells qa-pass), so QA-passed work integrates without a live leader.
    try:
        import worktree
        if worktree.merge(ROOT, args.task_id):
            print(f"  ↳ merged worktree branch fleet/{args.task_id}")
    except Exception:
        pass


MAX_QA_FAIL = 3   # retries per lineage before a producer is declared terminally failed


def _rewrite_downstream_deps(old_id: str, new_id: str) -> int:
    """Repoint every held draft's depends_on from old_id → new_id. Consumers of a
    QA-failed producer are necessarily still in drafts/ (a released task already
    cleared its deps), so drafts/ is the complete rewrite scope. Without this the
    DAG and the retry loop don't compose: the consumer waits on an id that was
    archived and never reaches qa-passed (the P0 retry-identity break)."""
    d_dir = QUEUE / "drafts"
    if not d_dir.is_dir():
        return 0
    n = 0
    for f in d_dir.glob("*.json"):
        if f.name.startswith("."):
            continue
        try:
            d = json.loads(f.read_text())
        except Exception:
            continue
        deps = d.get("depends_on") or []
        if old_id in deps:
            d["depends_on"] = [new_id if x == old_id else x for x in deps]
            _atomic_write(f, json.dumps(d, indent=2))
            n += 1
    return n


def cmd_qa_fail(args) -> None:
    """Bounce a deliverable: create a retry (rewriting downstream deps), OR — once
    the lineage exceeds MAX_QA_FAIL — declare the producer terminally failed so
    its consumers surface as dead instead of bouncing forever."""
    spec = _find_spec("completed", args.task_id) or _find_spec("failed", args.task_id)
    if not spec:
        print(f"Task spec for {args.task_id} not found in completed or failed")
        sys.exit(1)

    orig = json.loads(spec.read_text())
    orig_task_id = orig.get("task_id", args.task_id)
    count = int(orig.get("qa_fail_count", 0)) + 1
    _ttype = orig.get("type")

    # ── --no-retry: close terminally WITHOUT a retry ────────────────────────
    # A read-only review/research task has no write-scope, so a retry just re-runs
    # the SAME analysis over the SAME unchanged inputs → it fails identically and
    # burns workers until MAX_QA_FAIL. When such a task correctly flags a REAL
    # defect in what it reviewed, the fix belongs in a SEPARATE code task, not a
    # re-review. --no-retry ends the lineage cleanly. (Default path unchanged, so
    # this is a pure no-op for every existing qa-fail caller that omits the flag.)
    if getattr(args, "no_retry", False):
        orig["qa_fail_count"] = count
        orig["fail_reason"] = f"QA-failed (no-retry): {args.reason}"
        (QUEUE / "failed").mkdir(parents=True, exist_ok=True)
        _atomic_write(QUEUE / "failed" / f"{orig_task_id}.json", json.dumps(orig, indent=2))
        _atomic_write(QUEUE / "failed" / f"{orig_task_id}.result.json", json.dumps(
            {"task_id": orig_task_id, "status": "FAILED", "title": orig.get("title", ""),
             "error": orig["fail_reason"], "completed_at": _now()}, indent=2))
        if spec.exists():
            spec.unlink()
        sc = spec.parent / f"{orig_task_id}.result.json"
        if sc.exists():
            sc.unlink()
        print(f"✗ {orig_task_id} closed as FAILED (no retry) — reason: {args.reason}")
        _journal(f"QA-FAIL-NORETRY  {orig_task_id}  reason: {args.reason}")
        ledger.append(MA, "qa-fail-noretry", task_id=orig_task_id, count=count)
        return

    # ── Discipline hint: re-reviewing a read-only task can't change its inputs ─
    if _ttype in ("review", "research"):
        print(f"ℹ️  {orig_task_id} is a read-only '{_ttype}' task; the retry below re-runs the SAME "
              f"analysis on unchanged inputs. If the review correctly found a real defect, prefer "
              f"qa-pass + a separate fix task, or re-run with --no-retry to close it.")

    # ── Retry-count cap: stop the infinite-bounce / quota-burn loop ──────────
    if count > MAX_QA_FAIL:
        orig["qa_fail_count"] = count
        orig["fail_reason"] = (f"QA-failed {count}x (> MAX_QA_FAIL={MAX_QA_FAIL}); "
                               f"last reason: {args.reason}")
        (QUEUE / "failed").mkdir(parents=True, exist_ok=True)
        _atomic_write(QUEUE / "failed" / f"{orig_task_id}.json", json.dumps(orig, indent=2))
        _atomic_write(QUEUE / "failed" / f"{orig_task_id}.result.json", json.dumps(
            {"task_id": orig_task_id, "status": "FAILED", "title": orig.get("title", ""),
             "error": orig["fail_reason"], "completed_at": _now()}, indent=2))
        # remove the superseded completed spec + sidecar (now terminal in failed/)
        if spec.exists():
            spec.unlink()
        sc = spec.parent / f"{orig_task_id}.result.json"
        if sc.exists():
            sc.unlink()
        print(f"✗ {orig_task_id} TERMINALLY FAILED after {count} QA failures "
              f"(cap {MAX_QA_FAIL}) — downstream consumers will surface as dead deps")
        _journal(f"QA-FAIL-TERMINAL  {orig_task_id}  after {count}x  reason: {args.reason}")
        ledger.append(MA, "qa-fail-terminal", task_id=orig_task_id, count=count)
        return

    orig.pop("task_id", None)
    orig.pop("created_at", None)
    orig["retry_of"]     = orig_task_id
    orig["retry_reason"] = args.reason
    orig["qa_fail_count"] = count

    retry = TaskSpec(**{k: v for k, v in orig.items()
                        if k in TaskSpec.__dataclass_fields__})
    target = QUEUE / "pending" / retry.filename
    _atomic_write(target, retry.to_json())

    # Archive the superseded original (spec + result sidecar) so the board
    # shows only live state — the retry now represents this work.
    archive = spec.parent / "archive"
    archive.mkdir(exist_ok=True)
    spec.rename(archive / spec.name)
    sidecar = spec.parent / f"{orig_task_id}.result.json"
    if sidecar.exists():
        sidecar.rename(archive / sidecar.name)

    # Compose with the DAG: repoint downstream drafts old → new id.
    rewired = _rewrite_downstream_deps(orig_task_id, retry.task_id)

    print(f"✓ Retry task created: {retry.task_id} (retry {count}/{MAX_QA_FAIL} of {orig_task_id})")
    print(f"  Reason: {args.reason}")
    print(f"  Archived superseded original {orig_task_id}")
    if rewired:
        print(f"  Rewired {rewired} downstream dependency(ies) {orig_task_id} → {retry.task_id}")
    ledger.append(MA, "qa-fail", task_id=orig_task_id, retry_of=retry.task_id, count=count)
    _journal(f"QA-FAIL  {orig_task_id}  →  retry {retry.task_id}  "
             f"(rewired {rewired})  reason: {args.reason}")


def cmd_wait(args) -> None:
    """Block until a task reaches completed or failed, or timeout expires."""
    task_id = args.task_id
    timeout = args.timeout
    start   = time.time()

    print(f"Waiting for task {task_id} (timeout: {timeout}s) ...")
    while time.time() - start < timeout:
        r = _find_result(task_id)
        if r:
            d = json.loads(r.read_text())
            if d.get("status") == "COMPLETED":
                print(f"✓ {task_id} completed")
                return
            print(f"✗ {task_id} failed (exit {d.get('exit_code')})")
            sys.exit(1)
        time.sleep(3)

    print(f"✗ Timeout after {timeout}s — {task_id} not finished")
    sys.exit(2)


def cmd_requeue(args) -> None:
    """Requeue a FAILED task for a fresh run (P26) — the formal replacement for the
    leader's bare `mv failed/<id>.json pending/`, which left the `.result.json` sidecar
    behind so the kanban Failed column showed a resolved failure forever (observed live
    2026-07-05). Handles BOTH files: the spec moves to pending/ (transient claim state
    cleared, provenance stamped) and the failed result sidecar is ARCHIVED to
    failed/archive/ — the record survives for the audit trail, off the live board.
    FAILED tasks only: a completed task must never be requeued (that duplicates finished
    work — the exact P24 failure mode); use qa-fail for a retry instead."""
    spec = _find_spec("failed", args.task_id)
    if not spec:
        where = _find_spec_any(args.task_id)
        if where:
            state = where[0]
            hint = {"pending": "already pending — nothing to do",
                    "drafts": "still a draft — promote it instead",
                    "claimed": "in progress — let it run (or let doctor handle a hang)",
                    "completed": "COMPLETED — requeueing finished work duplicates it "
                                 "(P24); use qa-fail to bounce it for a retry"}.get(state, state)
            print(f"✗ {args.task_id} is in {state}/: {hint}")
        else:
            print(f"✗ {args.task_id} not found in any queue state")
        sys.exit(1)
    d = json.loads(spec.read_text())
    d.pop("claimed_by_pid", None)
    d.pop("fail_reason", None)
    d["requeued_at"] = _now()
    if getattr(args, "reason", None):
        d["requeue_reason"] = args.reason
    base = spec.name.split("--", 1)[1] if "--" in spec.name else spec.name
    _atomic_write(QUEUE / "pending" / base, json.dumps(d, indent=2))
    spec.unlink()
    sidecar = QUEUE / "failed" / f"{args.task_id}.result.json"
    archived = ""
    if sidecar.exists():
        arch = QUEUE / "failed" / "archive"
        arch.mkdir(exist_ok=True)
        sidecar.rename(arch / sidecar.name)
        archived = " · failed result archived → failed/archive/"
    print(f"✓ Requeued {args.task_id} → pending{archived}")
    _journal(f"REQUEUE  {args.task_id}"
             + (f"  reason: {args.reason}" if getattr(args, "reason", None) else ""))
    try:
        ledger.append(MA, "requeue", task_id=args.task_id,
                      reason=(getattr(args, "reason", None) or ""))
    except Exception:
        pass


def cmd_cancel(args) -> None:
    """Cancel a pending or drafted task."""
    p = _find_spec("pending", args.task_id) or _find_spec("drafts", args.task_id)
    if not p:
        print(f"Task {args.task_id} not in pending/drafts (may already be claimed/done)")
        sys.exit(1)
    data = json.loads(p.read_text())
    data["cancelled"] = True
    data["cancelled_at"] = _now()
    _atomic_write(QUEUE / "failed" / p.name, json.dumps(data, indent=2))
    p.unlink()
    print(f"✓ Cancelled {args.task_id}")
    _journal(f"CANCEL   {args.task_id}")


# ── Detached board cards (D1/D2) ──────────────────────────────────────────────
# A detached job (detach_run.py) has no queue spec, so it surfaces as a board card. These
# helpers give it the SAME QA layering as a queue task: a card carries machine-checkable
# acceptance (output + acceptance_predicates), `approve-card` runs the shared mechanical floor
# FAIL-CLOSED before the leader's semantic sign-off, and the verdict is recorded on the card.

def _board_cards_path() -> Path:
    return MA / "status" / "board_cards.json"


def _load_board_cards() -> dict:
    p = _board_cards_path()
    if not p.exists():
        return {"cards": []}
    try:
        bc = json.loads(p.read_text())
    except Exception:
        bc = {"cards": []}
    bc.setdefault("cards", [])
    return bc


def _find_card(bc: dict, card_id: str):
    for c in bc.get("cards", []):
        if c.get("id") == card_id:
            return c
    return None


def cmd_board_card(args) -> None:
    """Upsert a DETACHED board card (D1). The detached runner (or the leader) declares the card
    WITH its machine-checkable acceptance — `--output <file>` and/or `--predicate '<json>'` — so
    the QA floor can actually verify it (not a grep marker). Creates board_cards.json if absent."""
    import board_cards
    preds = []
    for pj in (args.predicate or []):
        try:
            preds.append(json.loads(pj))
        except Exception as e:
            print(f"✗ bad --predicate JSON: {pj} ({e})")
            sys.exit(2)
    done = None
    if getattr(args, "done", None):
        try:
            done = json.loads(args.done)
        except Exception as e:
            print(f"✗ bad --done JSON: {args.done} ({e})")
            sys.exit(2)
    # Build a PARTIAL update (only the provided fields) and merge-write it (Gate 6) so a card's
    # existing leader-set fields (verdict/log) and a terminal status are never clobbered.
    update = {"id": args.id, "status": args.status, "at": _now()}
    if args.title:
        update["title"] = args.title
    if args.phase is not None:
        update["phase"] = str(args.phase)
    if args.output is not None:
        update["output"] = args.output
    if preds:
        update["acceptance_predicates"] = preds
    if args.type:
        update["type"] = args.type
    if done is not None:
        update["done"] = done
    if getattr(args, "provenance", None):
        update["provenance"] = args.provenance
    _log = getattr(args, "log", None)
    if _log is not None:
        update["log"] = _log
    bc = board_cards.merge_write(ROOT, [update])
    card = next((c for c in bc["cards"] if c.get("id") == args.id), {"status": args.status})
    print(f"board card {args.id} → {card.get('status')} (phase {card.get('phase', '')})")


def cmd_approve_card(args) -> None:
    """Leader QA-approve a detached board card → APPROVED column. D2: runs the shared mechanical
    floor (declared output artifact + acceptance_predicates) FAIL-CLOSED first — a card whose
    floor fails CANNOT be approved (fix it or reject-card) — then records a structured verdict on
    the card. The leader's semantic sign-off rides ON TOP of the mechanical gate, same as a queue
    task; predicates alone (e.g. a grep marker) never substitute for the leader's judgment."""
    bc = _load_board_cards()
    card = _find_card(bc, args.card_id)
    if card is None:
        print(f"Board card {args.card_id!r} not found (ids: {[c.get('id') for c in bc['cards']]})")
        sys.exit(1)
    try:
        import qa_floor
        # leader is PRESENT and explicitly approving → allowed to run the card's command predicate
        # (DP3 pure-A reserves command execution to this accountable, in-session path).
        ok, failures = qa_floor.evaluate_card(card, ROOT, allow_command=True)
    except Exception as e:
        ok, failures = False, [f"card floor could not run ({e}) — fail-closed"]
    if not ok:
        print(f"✗ cannot APPROVE {args.card_id} — detached QA floor failed (fix or reject-card):")
        for f in failures:
            print(f"   - {f}")
        _alert("qa_floor_error",
               f"approve-card {args.card_id} blocked by floor: {'; '.join(failures)[:200]}")
        sys.exit(1)
    card["status"] = "approved"
    card["at"] = _now()
    if args.reason:
        card["verdict_reason"] = args.reason
    card["verdict"] = {
        "verdict": "approved",
        "accepted_at": _now(),
        "reason": args.reason or "",
        "output": card.get("output", ""),
        "predicates_enforced": card.get("acceptance_predicates", []),
        "provenance": card.get("provenance", ""),
        "floor": "clean",
    }
    _atomic_write(_board_cards_path(), json.dumps(bc, indent=2))
    print(f"✓ Board card {args.card_id} APPROVED (detached QA, floor clean)"
          + (f" — {args.reason}" if args.reason else ""))
    _journal(f"APPROVE-CARD  {args.card_id}  {args.reason or ''}")
    ledger.append(MA, "approve-card", task_id=args.card_id, reason=args.reason or "")


def cmd_reject_card(args) -> None:
    """Leader QA-reject a detached board card → FAILED column. Rejection is always allowed (no
    floor gate — you can always reject)."""
    bc = _load_board_cards()
    card = _find_card(bc, args.card_id)
    if card is None:
        print(f"Board card {args.card_id!r} not found (ids: {[c.get('id') for c in bc['cards']]})")
        sys.exit(1)
    card["status"] = "failed"
    card["at"] = _now()
    if args.reason:
        card["verdict_reason"] = args.reason
    _atomic_write(_board_cards_path(), json.dumps(bc, indent=2))
    print(f"✗ Board card {args.card_id} REJECTED (detached QA)"
          + (f" — {args.reason}" if args.reason else ""))
    _journal(f"REJECT-CARD  {args.card_id}  {args.reason or ''}")
    ledger.append(MA, "reject-card", task_id=args.card_id, reason=args.reason or "")


def _journal(entry: str) -> None:
    journal = MA / "status" / "orchestrator-journal.md"
    line = f"- {_now()[:19]}Z  {entry}\n"
    with open(journal, "a") as f:
        f.write(line)


# ── Argument parser ──────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Fleet orchestrator CLI")
    sub    = parser.add_subparsers(dest="cmd", required=True)

    # status
    sub.add_parser("status", help="Show live dashboard")
    sub.add_parser("metrics", help="Replay the event ledger into a summary (audit)")
    mt = sub.add_parser("merge-task", help="Merge a worktree task's fleet/<id> branch + prune")
    mt.add_argument("task_id")

    # create-task
    ct = sub.add_parser("create-task", help="Create and enqueue a task")
    ct.add_argument("--phase",        required=True, help="Pipeline phase id from phases.json; warns at creation if it matches no phase id")
    ct.add_argument("--type",         required=True, choices=["research","code","test","write","review"])
    ct.add_argument("--assign",       required=True, choices=["kimi","codex","opencode","claude","any"])
    ct.add_argument("--title",        required=True)
    ct.add_argument("--description",  required=True)
    ct.add_argument("--output-file",  required=True, dest="output_file")
    ct.add_argument("--criteria",     required=True, nargs="+")
    ct.add_argument("--context-files",nargs="*", dest="context_files", default=[])
    ct.add_argument("--priority",     type=int, default=5, choices=range(1,11), metavar="1-10")
    ct.add_argument("--depends-on",   nargs="*", dest="depends_on", default=[],
                    metavar="DEP",
                    help="task ids and/or 'phase:<id>' this task waits on; held "
                         "in drafts until ALL are QA-passed, then auto-released. "
                         "Empty = runnable now (parallel by default).")
    ct.add_argument("--predicate",    nargs="*", dest="predicate", default=[],
                    metavar="JSON",
                    help="machine-checkable acceptance predicate(s) enforced at qa-pass, "
                         "each a JSON object: scalar/regex/command "
                         "(e.g. '{\"type\":\"command\",\"cmd\":[\"pytest\",\"-q\"]}')")
    ct.add_argument("--write-scope",  nargs="*", dest="write_scope", default=[],
                    metavar="GLOB",
                    help="path glob(s) this task may write (e.g. 'src/**'). Overlapping "
                         "writers are serialized at claim time AND draft-release. Empty = "
                         "[output_file] (a task that edits more than its output_file MUST "
                         "declare wider scope so collisions are caught).")
    ct.add_argument("--hold",         action="store_true",
                    help="enqueue as a HELD draft (queue/drafts/) — promoted "
                         "explicitly or by the caretaker when the backlog runs low")

    # promote
    pr = sub.add_parser("promote", help="Promote a held draft to pending")
    pr.add_argument("task_id")

    # list
    ls = sub.add_parser("list", help="List tasks")
    ls.add_argument("--state", choices=["drafts","pending","claimed","completed","failed"])

    # read-result
    rr = sub.add_parser("read-result", help="Show completed task result")
    rr.add_argument("task_id")

    # qa-pass
    qp = sub.add_parser("qa-pass", help="Mark result as QA passed")
    qp.add_argument("task_id")
    qp.add_argument("--reason", default=None,
                    help="acceptance rationale — pinned to disk in the verdict sidecar "
                         "(why the task was closed), so it survives compaction")
    qp.add_argument("--leader-verified", action="store_true",
                    help="ATTENDED leader override: the leader personally verified this "
                         "deliverable (read it, checked quotes/claims against sources) — "
                         "skip the second-opinion semantic grader. The mechanical floor "
                         "and acceptance predicates STILL run. Recorded in the verdict "
                         "sidecar. Ignored in fallback mode (the supervisor is not the "
                         "leader and must not override semantic QA).")

    # qa-fail
    qf = sub.add_parser("qa-fail", help="Mark result as QA failed and create retry")
    qf.add_argument("task_id")
    qf.add_argument("--reason", required=True)
    qf.add_argument("--no-retry", action="store_true",
                    help="Close the task as terminally failed WITHOUT spawning a retry. Use for "
                         "read-only review/research tasks whose retry would just re-run the same "
                         "analysis on unchanged inputs (a real defect they flagged belongs in a "
                         "separate fix task, not a re-review).")

    # wait
    wt = sub.add_parser("wait", help="Block until task completes")
    wt.add_argument("--task-id", required=True, dest="task_id")
    wt.add_argument("--timeout", type=int, default=300)

    # cancel
    cn = sub.add_parser("cancel", help="Cancel a pending/drafted task")
    cn.add_argument("task_id")

    # requeue — formal failed→pending path (P26); archives the failed result sidecar
    rq = sub.add_parser("requeue",
                        help="Requeue a FAILED task for a fresh run (spec → pending; "
                             "failed result sidecar → failed/archive/). FAILED only — "
                             "completed work is never requeued (use qa-fail for a retry).")
    rq.add_argument("task_id")
    rq.add_argument("--reason", default=None,
                    help="why it is being requeued (e.g. 'transient CLI failure') — "
                         "stamped on the spec and in the ledger")

    # approve-card / reject-card — leader QA verb for DETACHED board cards (no queue spec)
    ac = sub.add_parser("approve-card",
                        help="QA-approve a detached board card → APPROVED column")
    ac.add_argument("card_id")
    ac.add_argument("--reason", default=None,
                    help="acceptance rationale, recorded on the card (survives compaction)")
    rc = sub.add_parser("reject-card",
                        help="QA-reject a detached board card → FAILED column")
    rc.add_argument("card_id")
    rc.add_argument("--reason", default=None, help="rejection rationale, recorded on the card")
    bcd = sub.add_parser("board-card",
                         help="Upsert a detached board card (with machine-checkable acceptance)")
    bcd.add_argument("--id", required=True)
    bcd.add_argument("--title", default=None)
    bcd.add_argument("--phase", default=None)
    bcd.add_argument("--status", default="running",
                     help="pending|running|done|approved|failed")
    bcd.add_argument("--output", default=None, help="result file the QA floor checks (non-empty)")
    bcd.add_argument("--predicate", action="append", default=None,
                     help="machine-checkable acceptance (JSON; repeatable) — verified at approve-card")
    bcd.add_argument("--type", default=None, help="research|write|review|code|… (for QA routing)")
    bcd.add_argument("--done", default=None,
                     help="completion predicate JSON (e.g. count==expected) — D6")
    bcd.add_argument("--provenance", default=None,
                     help="reproducibility note: config hash / git sha / frozen-results pointer")
    bcd.add_argument("--log", default=None,
                     help="path (abs or project-relative) to the job's log; shown in the card drawer")

    args = parser.parse_args()
    {
        "status":      cmd_status,
        "metrics":     cmd_metrics,
        "merge-task":  cmd_merge_task,
        "create-task": cmd_create_task,
        "promote":     cmd_promote,
        "list":        cmd_list,
        "read-result": cmd_read_result,
        "qa-pass":     cmd_qa_pass,
        "qa-fail":     cmd_qa_fail,
        "wait":        cmd_wait,
        "cancel":      cmd_cancel,
        "requeue":     cmd_requeue,
        "approve-card": cmd_approve_card,
        "reject-card":  cmd_reject_card,
        "board-card":   cmd_board_card,
    }[args.cmd](args)


if __name__ == "__main__":
    main()
