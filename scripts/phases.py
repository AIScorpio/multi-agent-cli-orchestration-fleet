#!/usr/bin/env python3
"""Generic phase-manifest authoring (Phase 5) — the fleet-AGNOSTIC half of leader-authored
phases.json.

Init writes a uniform, mission-agnostic stub `{"state":"awaiting_definition",...}`. The
mission-AWARE leader later FILLS it (`set_phases` → state `defined`) — deriving from a
predefined pipeline (e.g. ai-research-pipeline) with tailoring, or self-led via dialogue with
the human — or marks a flat one-shot project `no_pipeline`. Fleet scripts only ever read a
GENERIC manifest + a state string; they never learn what a phase MEANS.

Manifest shape:
  {"state": "awaiting_definition" | "defined" | "no_pipeline",
   "title": str | None,
   "phases": [{"id": str, "name": str, "order"?: int,
               "done_when"?: {...}, "gate"?: str, "depends_on"?: [...], "status"?: str}, ...]}

`derive_phases.py` updates phase STATUS from ground truth; this module owns the LIST + state.
"""
import json
from pathlib import Path

STATES = ("awaiting_definition", "defined", "no_pipeline")
STUB = {"state": "awaiting_definition", "title": None, "phases": []}


def _path(root) -> Path:
    return Path(root) / ".fleet" / "phases.json"


def load(root) -> dict:
    try:
        return json.loads(_path(root).read_text())
    except Exception:
        return {}


def effective_state(manifest: dict) -> str:
    """Back-compat: a manifest with no explicit `state` is `defined` if it has phases, else
    `awaiting_definition` (04/01 predate the state field)."""
    m = manifest or {}
    st = m.get("state")
    if st in STATES:
        return st
    return "defined" if m.get("phases") else "awaiting_definition"


def validate(manifest: dict):
    """Return (ok, errors). The empty `awaiting_definition` stub is VALID. Checks the state
    vocabulary and that every phase carries at least id + name."""
    if not isinstance(manifest, dict):
        return False, ["manifest is not an object"]
    errs = []
    st = manifest.get("state")
    if st is not None and st not in STATES:
        errs.append(f"bad state {st!r}")
    phases = manifest.get("phases", [])
    if not isinstance(phases, list):
        errs.append("phases is not a list")
    else:
        for i, p in enumerate(phases):
            if not isinstance(p, dict) or not p.get("id") or not p.get("name"):
                errs.append(f"phase[{i}] needs at least id + name")
    return (not errs), errs


def _write(root, manifest: dict) -> Path:
    p = _path(root)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")
    tmp.replace(p)            # atomic
    return p


def init_manifest(root):
    """Idempotent: write the `awaiting_definition` stub ONLY if absent. Returns the path if it
    created the stub, else None — NEVER clobbers a leader-filled manifest (safe to re-run)."""
    if _path(root).exists():
        return None
    return _write(root, dict(STUB))


def set_phases(root, phases, title=None) -> dict:
    """Leader fills the manifest: validate, set the phase list (+ title), flip state →
    `defined`. Preserves an existing title when `title` is None. Raises ValueError on invalid
    input (so a malformed fill never corrupts the manifest the deriver/hub read)."""
    phases = list(phases)
    if title is None:
        title = (load(root) or {}).get("title")
    manifest = {"state": "defined", "title": title, "phases": phases}
    ok, errs = validate(manifest)
    if not ok:
        raise ValueError("invalid phases manifest: " + "; ".join(errs))
    _write(root, manifest)
    return manifest


def mark_no_pipeline(root) -> dict:
    """Mark a flat one-shot project as having no pipeline — silences the awaiting-definition
    nudge while staying a valid manifest."""
    manifest = load(root) or {}
    manifest["state"] = "no_pipeline"
    manifest.setdefault("title", None)
    manifest.setdefault("phases", [])
    _write(root, manifest)
    return manifest
