"""Board always complete: the kanban derives "Approved" from the qa-passed SPECS (never gc'd),
enriched by result.json when present — so the board shows every qa-passed task even if its
result.json sidecar is missing."""
import json
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import kanban_hub  # noqa: E402


def _proj(tmp):
    for d in ("queue/pending", "queue/claimed", "queue/completed/qa-passed",
              "queue/drafts", "queue/failed", "status"):
        (tmp / ".fleet" / d).mkdir(parents=True)
    return tmp


def test_approved_includes_resultless_spec(tmp_path):
    p = _proj(tmp_path)
    qa = p / ".fleet" / "queue" / "completed" / "qa-passed"
    # spec WITHOUT a result.json (the gc-pruned scenario)
    (qa / "t1.json").write_text(json.dumps({"task_id": "t1", "title": "A", "phase": "P1",
                                            "assigned_to": "kimi"}))
    # spec WITH result.json (should enrich agent/completed_at)
    (qa / "t2.json").write_text(json.dumps({"task_id": "t2", "title": "B", "phase": "P2",
                                            "assigned_to": "any"}))
    (qa / "t2.result.json").write_text(json.dumps({"task_id": "t2", "agent": "codex",
                                                   "completed_at": "2026-01-01T00:00:00Z"}))
    d = kanban_hub.collect(tmp_path)
    ids = {a["task_id"] for a in d["approved"]}
    assert ids == {"t1", "t2"}, "board must show ALL qa-passed specs, even result-less ones"
    assert d["counts"]["approved"] == 2
    assert d["phase_counts"].get("P1") == 1 and d["phase_counts"].get("P2") == 1
    t2 = next(a for a in d["approved"] if a["task_id"] == "t2")
    assert t2["agent"] == "codex", "should enrich agent from result.json when present"


def test_resultless_only_still_shows(tmp_path):
    p = _proj(tmp_path)
    qa = p / ".fleet" / "queue" / "completed" / "qa-passed"
    for i in range(5):
        (qa / f"t{i}.json").write_text(json.dumps({"task_id": f"t{i}", "title": f"T{i}",
                                                   "phase": "F0", "assigned_to": "kimi"}))
    d = kanban_hub.collect(tmp_path)
    assert d["counts"]["approved"] == 5, "all result-less qa-passed specs must show"
    assert d["phase_counts"].get("F0") == 5
