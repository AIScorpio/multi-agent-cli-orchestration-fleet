#!/usr/bin/env python3
"""
Scaffold the FLEET multi-agent orchestration system into a target project.

  python3 init_workspace.py [target-dir]      # default: current directory

Creates <target>/.fleet/ with the queue (incl. drafts/), runtime scripts, and
agent registry, and wires up .gitignore + the autonomous-mode guard hook.
Mission-agnostic — the queue carries arbitrary tasks.

Coexists with the legacy single-project skill: a project may contain BOTH
.multiagent/ (legacy) and .fleet/ — nothing collides (different dirs, scripts,
process names, ports). New work should use .fleet/.

Idempotent: re-running refreshes runtime scripts but won't clobber existing
agent configs unless --force.
"""
import argparse, json, shutil
from pathlib import Path

SKILL_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS    = SKILL_ROOT / "scripts"
TEMPLATES  = SKILL_ROOT / "templates"

import sys
sys.path.insert(0, str(SCRIPTS))
import phases   # canonical phase-manifest module (Phase 5)

RUNTIME_SCRIPTS = [
    "schema.py", "orchestrator.py", "watcher.sh", "kanban_hub.py",
    "start.sh", "stop.sh", "derive_phases.py", "phase_deriver.sh",
    "capacity.py", "capacity_loop.sh", "registry.py", "doctor.py",
    "caretaker.sh", "supervisor_pass.sh", "supervisor_loop.sh", "install_supervisor.sh",
    "qa_notify.sh", "watchdog.py", "detach_run.py",
    "ledger.py", "fleet_health.py", "health_loop.sh",
    "qa_floor.py", "predicates.py", "grader.py", "profiles.py", "worktree.py",
    "jobs.py", "phases.py", "leader_heartbeat.sh", "autonomous.sh", "phase_link_check.py",
    "fleet_progress.py", "board_cards.py",
]
# install_supervisord.sh + fleet_supervisord.sh are GLOBAL (run from the skill,
# TCC-safe outside ~/Documents) — intentionally NOT deployed per-project.
EXECUTABLE = ("watcher.sh", "start.sh", "stop.sh", "phase_deriver.sh",
              "caretaker.sh", "capacity_loop.sh", "supervisor_pass.sh",
              "supervisor_loop.sh", "install_supervisor.sh", "qa_notify.sh",
              "health_loop.sh", "leader_heartbeat.sh", "autonomous.sh")
QUEUE_DIRS = ["queue/drafts", "queue/pending", "queue/claimed",
              "queue/completed", "queue/failed",
              "status/heartbeat", "status/logs", "status/pids", "status/progress",
              "status/liveness"]

GITIGNORE_BLOCK = """
# ── Fleet ephemeral coordination state (runtime, not source) ──
.fleet/queue/drafts/*
.fleet/queue/pending/*
.fleet/queue/claimed/*
.fleet/queue/completed/*
.fleet/queue/failed/*
.fleet/status/heartbeat/*
.fleet/status/logs/*
.fleet/status/pids/*
.fleet/status/progress/*
.fleet/status/liveness/*
.fleet/AUTONOMOUS_ON
!.fleet/queue/**/.gitkeep
!.fleet/status/**/.gitkeep
""".lstrip()

# Autonomous-mode Bash-discipline guard (PreToolUse hook). Inert until the
# .fleet/AUTONOMOUS_ON sentinel exists; fails open.
HOOK_SCRIPTS = ["autonomous_bash_guard.py", "qa_gate_stop.py"]   # tests live in dev/tests/
HOOK_CMD = 'python3 "$CLAUDE_PROJECT_DIR/.fleet/hooks/autonomous_bash_guard.py"'
STOP_HOOK_CMD = 'python3 "$CLAUDE_PROJECT_DIR/.fleet/hooks/qa_gate_stop.py"'

# Baseline PROJECT permissions for unattended operation. The guard hook only
# BLOCKS dangerous patterns — it cannot APPROVE anything; approval comes from
# this allow-list. A fresh project with no allow-list prompts on every command,
# which silently breaks "autonomous mode on" (observed 2026-06-11 on a new
# fleet project). So init installs the baseline BY CONSTRUCTION, paired with a
# deny-list for the genuinely dangerous/irreversible. Idempotent merge — never
# removes or overrides existing rules; skip entirely with --no-perms.
PERM_ALLOW = [
    "Bash(python3 *)", "Bash(pytest *)",
    # JS/TS dev runners (safe, specific — NOT a blanket `npx *` which can run any package)
    "Bash(npx tsc *)", "Bash(npx jest *)", "Bash(npx tsx *)",
    "Bash(npx vitest *)", "Bash(npx prisma *)", "Bash(npm test*)", "Bash(npm run *)",
    "Bash(./.fleet/*)", "Bash(bash .fleet/*)", "Bash(caffeinate *)",
    "Bash(ls *)", "Bash(ps *)", "Bash(pgrep *)", "Bash(pkill *)", "Bash(kill *)",
    "Bash(grep *)", "Bash(rg *)", "Bash(find *)", "Bash(wc *)",
    "Bash(awk *)", "Bash(sed *)", "Bash(sort *)", "Bash(uniq *)",
    "Bash(head *)", "Bash(tail *)", "Bash(cut *)", "Bash(shasum *)",
    "Bash(mkdir *)", "Bash(mv *)", "Bash(cp *)", "Bash(touch *)", "Bash(chmod *)",
    "Bash(sleep *)", "Bash(echo *)", "Bash(true)",
    "Bash(git add *)", "Bash(git commit *)", "Bash(git status*)",
    "Bash(git log*)", "Bash(git diff*)", "Bash(git branch*)",
    "Bash(codex exec *)", "Bash(opencode run *)", "Bash(kimi *)",
    "Bash(curl -s *http://127.0.0.1:*)",
]


def _perm_deny() -> list:
    from pathlib import Path as _P
    home = str(_P.home())
    return [
        "Bash(sudo *)", "Bash(rm -rf /*)", "Bash(rm -rf ~*)",
        f"Bash(rm -rf {home})", "Bash(git push*)",
        f"Read(//{home.lstrip('/')}/.ssh/**)",
        f"Read(//{home.lstrip('/')}/.aws/**)",
        f"Read(//{home.lstrip('/')}/.config/gcloud/**)",
    ]


def _wire_permissions(target: Path) -> None:
    """Merge the baseline allow/deny into <target>/.claude/settings.json and
    set defaultMode=acceptEdits if no mode is set. Never removes anything."""
    settings_path = target / ".claude" / "settings.json"
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        settings = json.loads(settings_path.read_text()) if settings_path.exists() else {}
    except (ValueError, OSError):
        print(f"  · could not parse {settings_path}; left permissions untouched")
        return
    perms = settings.setdefault("permissions", {})
    if not perms.get("defaultMode"):
        perms["defaultMode"] = "acceptEdits"
    allow = perms.setdefault("allow", [])
    deny = perms.setdefault("deny", [])
    added = 0
    for r in PERM_ALLOW:
        if r not in allow:
            allow.append(r)
            added += 1
    for r in _perm_deny():
        if r not in deny:
            deny.append(r)
            added += 1
    settings_path.write_text(json.dumps(settings, indent=2, ensure_ascii=False) + "\n")
    print(f"  · permissions baseline merged ({added} rules added, "
          f"mode={perms['defaultMode']}) — unattended runs won't stall on prompts")


def _wire_autonomous_hook(target: Path) -> None:
    """Copy the guard hook into <target>/.fleet/hooks/ and register it as a
    PreToolUse(Bash) hook in <target>/.claude/settings.json (idempotent merge,
    never clobbering existing settings or other hooks — including the LEGACY
    .multiagent guard, which may coexist)."""
    src = SCRIPTS / "hooks"
    if not src.is_dir():
        return
    hooks_dir = target / ".fleet" / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    for h in HOOK_SCRIPTS:
        if (src / h).exists():
            shutil.copy(src / h, hooks_dir / h)

    settings_path = target / ".claude" / "settings.json"
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        settings = json.loads(settings_path.read_text()) if settings_path.exists() else {}
    except (ValueError, OSError):
        print(f"  · could not parse {settings_path}; left hook UNregistered (wire it by hand)")
        return
    changed = False
    # PreToolUse(Bash) — prompt-free Bash guard
    pre = settings.setdefault("hooks", {}).setdefault("PreToolUse", [])
    if not any(".fleet/hooks/autonomous_bash_guard.py" in h.get("command", "")
               for entry in pre if isinstance(entry, dict)
               for h in entry.get("hooks", []) if isinstance(h, dict)):
        pre.append({"matcher": "Bash", "hooks": [{"type": "command", "command": HOOK_CMD}]})
        changed = True
        print("  · registered PreToolUse(Bash) fleet guard hook")
    # Stop — force the leader's semantic QA before idling (autonomous mode)
    stop = settings.setdefault("hooks", {}).setdefault("Stop", [])
    if not any(".fleet/hooks/qa_gate_stop.py" in h.get("command", "")
               for entry in stop if isinstance(entry, dict)
               for h in entry.get("hooks", []) if isinstance(h, dict)):
        stop.append({"hooks": [{"type": "command", "command": STOP_HOOK_CMD}]})
        changed = True
        print("  · registered Stop QA-gate hook (autonomous-mode leader QA forcing)")
    if changed:
        settings_path.write_text(json.dumps(settings, indent=2) + "\n")
    else:
        print("  · fleet hooks already registered")


def init_workspace(target: Path, force: bool, perms: bool = True) -> None:
    ma = target / ".fleet"

    for d in QUEUE_DIRS:
        (ma / d).mkdir(parents=True, exist_ok=True)
        (ma / d / ".gitkeep").touch()
    (ma / "agents").mkdir(parents=True, exist_ok=True)

    # Runtime scripts (always refreshed to the skill's canonical version)
    for s in RUNTIME_SCRIPTS:
        shutil.copy(SCRIPTS / s, ma / s)
    for sh in EXECUTABLE:
        (ma / sh).chmod(0o755)

    # Project-type profile (P5): scaffold default 'software'; profiles.py + the watcher
    # discipline injection read it. Don't clobber a user's chosen profile.
    prof = ma / "profile.json"
    if not prof.exists():
        prof.write_text(json.dumps({"profile": "software"}, indent=2) + "\n")
        print("  · scaffolded .fleet/profile.json (profile=software; edit for research/writing/data/ml)")

    # Phase manifest (Phase 5): scaffold the mission-AGNOSTIC 'awaiting_definition' stub so the
    # kanban + derive_phases always have a manifest to read. The mission-AWARE leader fills it
    # (phases.set_phases) once goal/scope is confirmed — from a predefined pipeline with
    # tailoring, or self-led with the human — or marks a flat project no_pipeline. Idempotent:
    # never clobbers a leader-filled manifest.
    if phases.init_manifest(target):
        print("  · scaffolded .fleet/phases.json (state=awaiting_definition; the leader fills the phases)")

    # Agent registry (don't clobber user edits unless --force)
    for cfg in (TEMPLATES / "agents").glob("*.json"):
        dest = ma / "agents" / cfg.name
        if dest.exists() and not force:
            print(f"  · kept existing {dest.relative_to(target)} (use --force to overwrite)")
        else:
            shutil.copy(cfg, dest)

    # .gitignore
    gi = target / ".gitignore"
    existing = gi.read_text() if gi.exists() else ""
    if ".fleet/queue/pending/*" not in existing:
        with open(gi, "a") as f:
            if existing and not existing.endswith("\n"):
                f.write("\n")
            f.write(GITIGNORE_BLOCK)
        print(f"  · updated {gi.relative_to(target) if gi.is_relative_to(target) else gi}")

    # Autonomous-mode enforcement hook (copy + register in settings.json)
    _wire_autonomous_hook(target)

    # Baseline permissions so autonomous mode is prompt-free by construction
    if perms:
        _wire_permissions(target)

    print(f"\n✓ Fleet workspace ready at: {ma}")
    print("\nNext steps:")
    print("  1. Verify model auth for the agents you'll use (codex / kimi login / opencode / claude).")
    print("  2. Bring the project stack up (multi-project-safe):")
    print("       ./.fleet/start.sh          # watchers + caretaker + deriver;")
    print("                                  # global kanban hub + capacity loop if absent")
    print("  3. Open the fleet kanban (ALL projects, one port, tabs):")
    print("       http://127.0.0.1:8788")
    print("  4. Have the leader create tasks:")
    print("       python3 .fleet/orchestrator.py create-task --help")
    print("     (add --hold to pre-author the next wave as caretaker-promoted drafts)")
    print("  5. Autonomous/overnight mode: enforce prompt-free Bash with")
    print("       touch .fleet/AUTONOMOUS_ON      (delete to disable)")
    print("  6. Headless leader passes (model auto-laddered):")
    print("       ./.fleet/supervisor_pass.sh     (cron/launchd; template in skill)")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Scaffold fleet orchestration into a project")
    ap.add_argument("target", nargs="?", default=".", help="Target project dir (default: cwd)")
    ap.add_argument("--force", action="store_true", help="Overwrite existing agent configs")
    ap.add_argument("--no-perms", action="store_true",
                    help="skip installing the baseline permission allow/deny rules")
    args = ap.parse_args()
    init_workspace(Path(args.target).resolve(), args.force, perms=not args.no_perms)
