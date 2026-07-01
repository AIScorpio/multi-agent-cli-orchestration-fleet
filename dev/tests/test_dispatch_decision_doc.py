"""D0 — the queue-vs-detach dispatch decision rule is explicit AND co-located at the create-task
decision point (not buried in a late 'Long-running jobs' section)."""
from pathlib import Path

SKILL = Path(__file__).resolve().parents[2] / "SKILL.md"


def test_dispatch_decision_block_present_and_colocated():
    body = SKILL.read_text()
    low = body.lower()
    # the rule exists with the three tracks
    assert "dispatch decision" in low
    assert "queue parallel fan-out" in low or "fan-out" in low
    assert "queue task" in low and "detach" in low
    # the key criteria are named
    for kw in ("worker", "resource", "side-effect", "survive", "timeout"):
        assert kw in low, f"decision criteria missing keyword: {kw}"
    # co-located: appears within the '## Creating tasks' section (before the next top-level '## ')
    head = body.split("## Creating tasks", 1)[1]
    section = head.split("\n## ", 1)[0]
    assert "dispatch decision" in section.lower(), "decision rule not co-located with create-task"
