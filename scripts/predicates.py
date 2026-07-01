#!/usr/bin/env python3
"""Pluggable per-task acceptance predicates (P4) — first-class, machine-checkable
acceptance criteria (sibling to the phases.json done-predicates). Lets a code/data
task assert e.g. "metric >= X", "this string is present", or "this validator exits 0"
without an LLM. Fail-safe to False on any error (a broken predicate never passes).

  scalar   {"type":"scalar","source":"f.json","path":"a.b.c","op":">=","value":N}
  regex    {"type":"regex","source":"f.txt","pattern":"..."}
  command  {"type":"command","cmd":["pytest","-q"]}   # exit 0 == pass
"""
import json
import os
import re
import subprocess
from pathlib import Path

_OPS = {
    ">=": lambda a, b: a >= b, ">": lambda a, b: a > b,
    "<=": lambda a, b: a <= b, "<": lambda a, b: a < b,
    "==": lambda a, b: a == b, "=": lambda a, b: a == b,
    "!=": lambda a, b: a != b,
}


def _navigate(obj, dotpath):
    if not dotpath:
        return obj
    for key in dotpath.split("."):
        if isinstance(obj, dict):
            if key not in obj:
                return None
            obj = obj[key]
        elif isinstance(obj, list):
            try:
                obj = obj[int(key)]
            except (ValueError, IndexError):
                return None
        else:
            return None
    return obj


def eval_predicate(pred: dict, root) -> bool:
    """Evaluate one predicate against the project root. Fail-safe → False."""
    try:
        root = Path(root)
        ptype = pred.get("type")

        if ptype == "scalar":
            src = root / pred.get("source", "")
            data = json.loads(src.read_text())
            val = _navigate(data, pred.get("path", ""))
            if isinstance(val, bool) or not isinstance(val, (int, float)):
                return False                       # missing / non-numeric → fail
            fn = _OPS.get(pred.get("op", ">="))
            return bool(fn(val, pred.get("value"))) if fn else False

        if ptype == "regex":
            src = root / pred.get("source", "")
            return re.search(pred.get("pattern", ""), src.read_text()) is not None

        if ptype == "command":
            cmd = pred.get("cmd") or []
            if not cmd:
                return False
            r = subprocess.run(cmd, cwd=str(root), capture_output=True, timeout=300)
            return r.returncode == 0

        return False
    except Exception:
        return False
