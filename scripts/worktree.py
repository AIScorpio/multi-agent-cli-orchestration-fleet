#!/usr/bin/env python3
"""Opt-in git-worktree isolation for fleet workers (P12).

When FLEET_WORKTREE=1, each claimed task runs in its OWN git worktree
(`.worktrees/<task_id>` on branch `fleet/<task_id>`), so parallel writers never collide
in the shared working tree — the safe-heavy-parallelism posture the SKILL long described
but didn't implement. The fleet QUEUE stays at the workspace root; ONLY the agent's run
cwd moves into the worktree.

Flow (driven by watcher.sh):
  ensure(root, task_id, context_files) -> worktree path
      create/reuse the worktree + branch off HEAD, copy the declared context_files in
      (so the agent sees them even when uncommitted at root), return the path to run in.
  finalize(root, task_id, output_file, status) -> {branch, changed_files}
      on COMPLETED: copy the deliverable back to root (so the existing QA floor + DAG see
      it), commit the branch (full change set for the leader to merge), capture the
      ACCURATE changed_files (one writer per worktree → no cross-task leakage, which fixes
      the shared-tree caveat on P8's changed_files), then remove the worktree.

EVERYTHING fail-open: a non-git tree or any git error returns the root path / an empty
dict so the watcher proceeds WITHOUT isolation rather than stalling. Worktree mode is
opt-in precisely because it commits per-task branches and assumes a git repo.
"""
import os
import shutil
import subprocess
from pathlib import Path

WORKTREE_DIR = ".worktrees"
BRANCH_PREFIX = "fleet/"


def _git(root, *args, check=True):
    return subprocess.run(["git", "-C", str(root), *args],
                          capture_output=True, text=True, timeout=60, check=check)


def _is_git(root) -> bool:
    try:
        r = _git(root, "rev-parse", "--git-dir", check=False)
        return r.returncode == 0
    except Exception:
        return False


def ensure(root, task_id: str, context_files=None) -> str:
    """Create (or reuse) an isolated worktree for `task_id`; copy context_files in.
    Returns the worktree path, or str(root) on any failure (fail-open = no isolation)."""
    root = Path(root)
    try:
        if not _is_git(root):
            return str(root)
        wt = root / WORKTREE_DIR / task_id
        branch = BRANCH_PREFIX + task_id
        if not wt.exists():
            wt.parent.mkdir(parents=True, exist_ok=True)
            # new branch off current HEAD; -f reuses the branch name if it lingers
            r = _git(root, "worktree", "add", "-B", branch, str(wt), "HEAD", check=False)
            if r.returncode != 0 or not wt.is_dir():
                return str(root)                       # fail-open
        # copy declared context files in (they may be uncommitted at root)
        for cf in (context_files or []):
            src = root / cf
            if src.is_file():
                dst = wt / cf
                dst.parent.mkdir(parents=True, exist_ok=True)
                try:
                    shutil.copy2(src, dst)
                except Exception:
                    pass
        return str(wt)
    except Exception:
        return str(root)


def finalize(root, task_id: str, output_file: str, status: str, context_files=None) -> dict:
    """On COMPLETED: copy the deliverable back to root, commit the branch, return the
    accurate changed_files (EXCLUDING the context_files we copied in, so they don't read
    as task writes); always remove the worktree afterward. Fail-open → {}."""
    root = Path(root)
    info: dict = {}
    try:
        if not _is_git(root):
            return info
        wt = root / WORKTREE_DIR / task_id
        if not wt.is_dir():
            return info
        branch = BRANCH_PREFIX + task_id
        info["branch"] = branch
        if status == "COMPLETED":
            # accurate changed set (this worktree had a single writer — this task)
            _git(wt, "add", "-A", check=False)
            r = _git(wt, "status", "--porcelain", "--untracked-files=all", check=False)
            changed = [ln[3:] for ln in r.stdout.splitlines() if ln.strip()]
            # commit the full change set so the leader can merge the branch
            _git(wt, "commit", "-q", "-m", f"fleet task {task_id}", check=False)
            # but get the committed change set vs HEAD's parent for a clean list
            diff = _git(wt, "diff", "--name-only", "HEAD~1", "HEAD", check=False)
            committed = [x for x in diff.stdout.splitlines() if x.strip()]
            ctx = set(context_files or [])
            info["changed_files"] = [f for f in (committed or changed) if f not in ctx]
            # copy ALL of this task's changed files back to root (not just output_file).
            # Single writer per worktree → every changed file is this task's deliverable.
            # Previously only output_file was copied, stranding sibling files (e.g. the
            # test_*.py) on the branch — which broke pytest acceptance-predicates that run
            # at root AND the QA floor's view of multi-file deliverables. P20 makes a
            # declared write_scope FORCE worktree, so a module+sibling-test task hit exactly
            # this. Copy the whole committed change set back; output_file is included.
            for rel in (info.get("changed_files") or ([output_file] if output_file else [])):
                src = wt / rel
                if src.is_file():
                    dst = root / rel
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    try:
                        shutil.copy2(src, dst)
                    except Exception:
                        pass
        return info
    except Exception:
        return info
    finally:
        try:
            wt = Path(root) / WORKTREE_DIR / task_id
            if wt.is_dir():
                _git(root, "worktree", "remove", "--force", str(wt), check=False)
        except Exception:
            pass


def merge(root, task_id: str) -> bool:
    """Integrate a finished task's `fleet/<task_id>` branch into the current root branch and
    prune it (P14) — the migration archetype's defining operation, previously documented
    but uncalled. finalize() already copied the single output_file back; this brings the
    FULL committed change set onto the main line. Commits any working-tree state first so
    the merge has a clean index, then merges with -X theirs (the worktree is authoritative
    for this task) and deletes the branch. Returns True on a clean merge; aborts + False
    otherwise. Fail-open off a non-git tree."""
    root = Path(root)
    branch = BRANCH_PREFIX + task_id
    try:
        if not _is_git(root):
            return False
        # is the branch present?
        if _git(root, "rev-parse", "--verify", branch, check=False).returncode != 0:
            return False
        # commit any pending working-tree changes (e.g. the copied-back deliverable) so
        # the merge starts from a clean tree.
        _git(root, "add", "-A", check=False)
        _git(root, "commit", "-q", "-m", f"fleet pre-merge {task_id}", check=False)
        # P16: a REAL 3-way merge (no `-X theirs`, which silently discarded one side of an
        # overlapping hunk). On a genuine conflict, abort cleanly and ALARM — the work is
        # preserved on its branch for manual/leader resolution, never silently clobbered.
        r = _git(root, "merge", "--no-edit", branch, check=False)
        if r.returncode != 0:
            _git(root, "merge", "--abort", check=False)
            try:
                import fleet_health
                fleet_health.emit_alerts(Path(os.environ.get("FLEET_HOME",
                                         str(Path.home() / ".fleet"))), [{
                    "type": "worktree_merge_conflict",
                    "detail": f"{task_id}: fleet/{task_id} conflicts with current main — "
                              f"branch kept for manual resolution (not clobbered)"}])
            except Exception:
                pass
            return False
        _git(root, "branch", "-D", branch, check=False)
        return True
    except Exception:
        return False


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Fleet git-worktree isolation")
    sub = ap.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("ensure")
    e.add_argument("--root", required=True)
    e.add_argument("--task-id", required=True)
    e.add_argument("--context", nargs="*", default=[])
    f = sub.add_parser("finalize")
    f.add_argument("--root", required=True)
    f.add_argument("--task-id", required=True)
    f.add_argument("--output-file", default="")
    f.add_argument("--status", required=True)
    f.add_argument("--context", nargs="*", default=[])
    m = sub.add_parser("merge")
    m.add_argument("--root", required=True)
    m.add_argument("--task-id", required=True)
    args = ap.parse_args()
    if args.cmd == "ensure":
        print(ensure(args.root, args.task_id, args.context))
    elif args.cmd == "finalize":
        import json as _json
        print(_json.dumps(finalize(args.root, args.task_id, args.output_file,
                                   args.status, args.context)))
    elif args.cmd == "merge":
        print("merged" if merge(args.root, args.task_id) else "merge-failed")
