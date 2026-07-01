#!/usr/bin/env python3
"""Global fleet project registry — which projects the kanban hub serves.

State: $FLEET_HOME/projects.json (FLEET_HOME default: ~/.fleet)
  {"projects": [{"id", "name", "root", "registered_at"}, ...]}

Written by start.sh (add) and stop.sh (remove); read by kanban_hub.py.
Atomic write (temp+rename) + O_EXCL lock with bounded retries, so concurrent
start.sh runs from different projects cannot corrupt the file.

  python3 registry.py add --root /abs/project [--name NAME]
  python3 registry.py remove --root /abs/project
  python3 registry.py list [--json]
"""
import argparse, hashlib, json, os, sys, time
from datetime import datetime, timezone
from pathlib import Path

FLEET_HOME = Path(os.environ.get("FLEET_HOME", Path.home() / ".fleet"))
REG = FLEET_HOME / "projects.json"
LOCK = FLEET_HOME / "projects.json.lock"


def project_id(root: str) -> str:
    return f"{Path(root).name}-{hashlib.sha1(root.encode()).hexdigest()[:8]}"


def _read() -> dict:
    try:
        return json.loads(REG.read_text())
    except Exception:
        return {"projects": []}


def _write(data: dict) -> None:
    FLEET_HOME.mkdir(parents=True, exist_ok=True)
    tmp = REG.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    tmp.rename(REG)


def _locked(fn):
    """Run fn() holding the O_EXCL lock; reap a stale lock (>30s) rather than
    deadlocking a crashed registrar forever."""
    FLEET_HOME.mkdir(parents=True, exist_ok=True)
    for _ in range(50):
        try:
            fd = os.open(LOCK, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            try:
                os.write(fd, str(os.getpid()).encode())
                os.close(fd)
                return fn()
            finally:
                try:
                    os.unlink(LOCK)
                except OSError:
                    pass
        except FileExistsError:
            try:
                if time.time() - LOCK.stat().st_mtime > 30:
                    LOCK.unlink()
                    continue
            except OSError:
                continue
            time.sleep(0.1)
    print("registry: could not acquire lock", file=sys.stderr)
    sys.exit(1)


def add(root: str, name: str | None) -> str:
    root = str(Path(root).resolve())
    pid = project_id(root)

    def _do():
        data = _read()
        for p in data["projects"]:
            if p["root"] == root:
                if name:
                    p["name"] = name
                _write(data)
                return
        data["projects"].append({
            "id": pid,
            "name": name or Path(root).name,
            "root": root,
            "registered_at": datetime.now(timezone.utc).isoformat()[:19] + "Z",
        })
        _write(data)

    _locked(_do)
    return pid


def remove(root: str) -> bool:
    root = str(Path(root).resolve())

    def _do():
        data = _read()
        before = len(data["projects"])
        data["projects"] = [p for p in data["projects"] if p["root"] != root]
        _write(data)
        return len(data["projects"]) < before

    return _locked(_do)


def projects() -> list:
    return _read()["projects"]


def touch(root: str, now: float | None = None) -> None:
    """Stamp a project's `last_seen` (P16 liveness) — called by the caretaker each tick so
    a crashed/forgotten project ages out of the fair-share denominator instead of
    permanently shrinking every survivor's slot share. Fail-open."""
    now = now if now is not None else time.time()
    root = str(Path(root).resolve())

    def _do():
        data = _read()
        for p in data["projects"]:
            if p["root"] == root:
                p["last_seen"] = int(now)
                _write(data)
                return
    try:
        _locked(_do)
    except SystemExit:
        pass


def live_projects(now: float | None = None, max_age: int = 300) -> list:
    """Project ids touched within `max_age` seconds (P16). A project with no `last_seen`
    yet is treated as live (just registered, caretaker hasn't ticked) so a fresh project
    isn't excluded; one whose last_seen is stale ages out."""
    now = now if now is not None else time.time()
    out = []
    for p in _read().get("projects", []):
        ls = p.get("last_seen")
        if ls is None or (now - ls) <= max_age:
            out.append(p.get("id"))
    return [x for x in out if x]


def main():
    ap = argparse.ArgumentParser(description="Fleet project registry")
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("add")
    a.add_argument("--root", required=True)
    a.add_argument("--name", default=None)
    r = sub.add_parser("remove")
    r.add_argument("--root", required=True)
    l = sub.add_parser("list")
    l.add_argument("--json", action="store_true")
    i = sub.add_parser("id", help="print the stable project id for a root")
    i.add_argument("--root", required=True)
    args = ap.parse_args()

    if args.cmd == "id":
        print(project_id(args.root))
        return
    if args.cmd == "add":
        pid = add(args.root, args.name)
        print(f"registered: {pid}")
    elif args.cmd == "remove":
        ok = remove(args.root)
        print("deregistered" if ok else "(was not registered)")
    elif args.cmd == "list":
        ps = projects()
        if args.json:
            print(json.dumps(ps, indent=2))
        else:
            for p in ps:
                print(f"  {p['id']:32s} {p['root']}")


if __name__ == "__main__":
    main()
