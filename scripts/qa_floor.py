#!/usr/bin/env python3
"""Mechanical QA floor (P4) — the no-LLM checks that run BEFORE any human/grader
judgment, so the universal false-success modes are caught by construction.

These are pure, deterministic, fail-open helpers the supervisor QA routine calls:
the human/grader is no longer the ONLY gate.
"""
from pathlib import Path


def artifact_ok(path) -> tuple:
    """The deliverable must exist, be a REGULAR FILE, and be non-empty. A directory
    (the classic rc==0-on-a-directory false-success) or a missing/empty file fails.
    Returns (ok, reason)."""
    p = Path(path)
    try:
        if not p.exists():
            return False, f"output missing: {p}"
        if p.is_dir():
            return False, f"output is a directory, not a file: {p}"
        if not p.is_file():
            return False, f"output is not a regular file: {p}"
        if p.stat().st_size == 0:
            return False, f"output is empty: {p}"
        return True, ""
    except OSError as e:
        return False, f"output stat error: {e}"


def test_count_grew(before: int, after: int) -> bool:
    """A code deliverable must GROW the collected test total — a flat count means the
    new tests were never collected (the recurring embedded-vs-sibling bug)."""
    try:
        return int(after) > int(before)
    except (TypeError, ValueError):
        return False


def reconcile_files(declared, changed) -> tuple:
    """Files actually changed must be within the declared scope. `declared` entries may be
    GLOBS (e.g. 'src/**'); a changed path is in scope if it matches ANY declared pattern
    (exact strings match themselves, so this is backward compatible). Returns (ok, extra)
    where extra = changed files matching NO declared pattern (scope violation)."""
    import fnmatch
    pats = list(declared or [])
    extra = []
    for c in (changed or []):
        if not any(c == p or fnmatch.fnmatch(c, p) for p in pats):
            extra.append(c)
    return (not extra), extra


def _predicates_module():
    """Import the predicates checker (indirected so the import-failure path is testable)."""
    import predicates
    return predicates


def eval_done(done: dict, root) -> tuple:
    """Evaluate a COMPLETION predicate (jobs/card `done`: count / file_exists) → (ok, reason).
    Distinct from acceptance predicates (scalar/regex/command): this is the L1 completion gate
    (D6) — e.g. count of finished units >= expected — so a card marked 'done' but actually
    incomplete fails the floor. Unknown type → (True, '') (can't gate). Error → fail-CLOSED."""
    from pathlib import Path
    import json as _json
    try:
        t = done.get("type")
        if t == "file_exists":
            ok = (Path(root) / done.get("source", "")).exists()
            return (ok, "" if ok else f"completion file missing: {done.get('source')}")
        if t == "count":
            data = _json.loads((Path(root) / done.get("source", "")).read_text())
            obj = data[done["path"]] if done.get("path") else data
            if isinstance(obj, dict) and obj and all(isinstance(v, list) for v in obj.values()):
                n = sum(len(v) for v in obj.values())
            else:
                n = len(obj)
            op, val = done.get("op", ">="), done.get("value", 1)
            ok = {">=": n >= val, ">": n > val, "==": n == val, "=": n == val,
                  "<=": n <= val, "<": n < val}.get(op, False)
            return (ok, "" if ok else f"incomplete: count {n} {op} {val} is False")
        return (True, "")
    except Exception as e:
        return (False, f"completion check error: {e}")


def card_has_acceptance(card: dict) -> bool:
    """True iff a detached board card declares any machine-checkable acceptance (an output
    artifact and/or acceptance_predicates) — i.e. the mechanical floor can say something."""
    return bool(card.get("output") or card.get("acceptance_predicates"))


def has_command_predicate(card: dict) -> bool:
    """True if the card declares a command-type acceptance predicate. The no-LLM caretaker won't
    execute it (DP3 pure-A: cards are runner-writable), so a card that is otherwise floor-clean
    still NEEDS the present leader's approve-card — surface it, don't silently sit on it."""
    return any((p or {}).get("type") == "command"
               for p in (card.get("acceptance_predicates") or []))


def evaluate_card(card: dict, root, allow_command=False) -> tuple:
    """Mechanical floor for a DETACHED board card (D2/D3) → (ok, failures). Same primitives as
    evaluate(): the declared `output` (if any) must be a non-empty regular file, and each declared
    acceptance_predicate must pass. A card with NEITHER → floor-clean but no machine acceptance
    (caller defers to the leader). Fail-CLOSED on a checker error; fail-OPEN only if predicates
    can't import.

    DP3 pure-A: `allow_command` is False for the no-LLM caretaker (cards are runner-writable, so
    the unattended automaton must NOT execute their command predicates) and True only on the
    present leader's approve-card path. A command predicate skipped under allow_command=False is
    'unverifiable' — neither pass nor fail — and is left for the leader (see has_command_predicate)."""
    from pathlib import Path
    failures = []
    try:
        predicates = _predicates_module()
    except Exception as e:
        return False, [f"floor infra unavailable: predicates import failed ({e}) — fail-closed"]
    out = card.get("output")
    if out:
        try:
            ok, reason = artifact_ok(Path(root) / out)
        except Exception as e:
            ok, reason = False, f"artifact check raised: {e}"
        if not ok:
            failures.append(f"artifact: {reason}")
    for pred in (card.get("acceptance_predicates") or []):
        if (pred or {}).get("type") == "command" and not allow_command:
            continue                               # DP3: unattended automaton never execs a card command
        try:
            passed = predicates.eval_predicate(pred, Path(root))
        except Exception as e:
            failures.append(f"predicate raised (fail-closed): {pred}: {e}")
            continue
        if not passed:
            failures.append(f"predicate failed: {pred}")
    # D6: completion gate — a card marked 'done' but whose `done` predicate (e.g. count==expected)
    # doesn't hold is INCOMPLETE → floor fails (don't approve a partial run).
    done = card.get("done")
    if done:
        ok_d, reason_d = eval_done(done, root)
        if not ok_d:
            failures.append(f"completion: {reason_d}")
    return (not failures), failures


def evaluate(spec: dict, root, result=None) -> tuple:
    """The ONE mechanical QA floor (P9) → (ok, failures[list[str]]). No LLM. Shared by
    cmd_qa_pass (semantic gate adds the grader on top) and doctor.sweep_qa_floor (the
    deterministic no-LLM sweep over completed/). Fail-CLOSED on a checker error or an
    empty output_file; fail-OPEN ONLY if the predicates module can't import (can't gate).
      - artifact_ok: output exists, regular file, non-empty
      - acceptance_predicates: each must pass (a raising predicate is a FAILURE)
      - write_scope reconcile: when declared AND result reports changed_files
      - test_count_grew: when result reports test_count_before/after (code/test tasks)
    """
    from pathlib import Path
    result = result or {}
    failures = []
    try:
        predicates = _predicates_module()
    except Exception as e:
        # The checker module is unavailable → we CANNOT verify. Fail-CLOSED (P14.2): a
        # floor that can't run must NOT return a silent PASS — return a failure so the
        # caller bounces/alerts instead of letting unchecked work through.
        return False, [f"floor infra unavailable: predicates import failed ({e}) — fail-closed"]

    out = spec.get("output_file", "")
    if not out:
        failures.append("artifact: spec has an empty output_file")
    else:
        try:
            ok, reason = artifact_ok(Path(root) / out)
        except Exception as e:
            ok, reason = False, f"artifact check raised: {e}"
        if not ok:
            failures.append(f"artifact: {reason}")

    for pred in (spec.get("acceptance_predicates") or []):
        try:
            passed = predicates.eval_predicate(pred, Path(root))
        except Exception as e:
            failures.append(f"predicate raised (fail-closed): {pred}: {e}")
            continue
        if not passed:
            failures.append(f"predicate failed: {pred}")

    declared = spec.get("write_scope") or []
    changed = result.get("changed_files")
    if declared and changed:
        ok_s, extra = reconcile_files(declared, changed)
        if not ok_s:
            # ENFORCE (fail) only when the run was ISOLATED (worktree → one writer →
            # changed_files is accurate). In a SHARED tree a concurrent writer's files leak
            # into changed_files, so a violation there is advisory only (P16): never
            # false-fail honest work. The audit record (changed_files) is kept either way.
            if result.get("isolated"):
                failures.append(f"write-scope violation (wrote outside {declared}): {extra}")

    tcb, tca = result.get("test_count_before"), result.get("test_count_after")
    if tcb is not None and tca is not None:
        if not test_count_grew(tcb, tca):
            failures.append(
                f"test count did not grow ({tcb} → {tca}) — new tests not collected")

    return (not failures), failures
