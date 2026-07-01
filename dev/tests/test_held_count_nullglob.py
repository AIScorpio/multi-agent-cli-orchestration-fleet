#!/usr/bin/env python3
"""Regression: claim_one's per-project fairness `held` count must be nullglob-safe.

Bug: `held=$(ls "$CLAIMED"/${AGENT}--*.json | wc -l)` under `shopt -s nullglob` collapses a
no-match glob to nothing, so bare `ls` lists CWD → held = cwd-file-count ≥ floor → a reserve
agent (claude/codex) with 0 claims yields FOREVER and never starts. Fixed by counting with
`find` (no glob expansion).
"""
import subprocess
import tempfile
from pathlib import Path

WATCHER = Path(__file__).resolve().parent.parent.parent / "scripts" / "watcher.sh"


def test_watcher_uses_find_for_held_count():
    body = WATCHER.read_text()
    assert 'find "$CLAIMED" -maxdepth 1 -name "${AGENT}--*.json"' in body, "find-based fix absent"
    assert 'held=$(ls "$CLAIMED"/${AGENT}--*.json' not in body, "buggy ls-glob held count still present"


def test_find_count_is_nullglob_safe_vs_bare_ls():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "claimed").mkdir()
        for i in range(11):                       # cwd clutter a bare `ls` would miscount
            (root / f"f{i}.txt").write_text("x")
        old = subprocess.run(
            ["bash", "-c", f'cd {root}; shopt -s nullglob; ls "claimed"/codex--*.json 2>/dev/null | wc -l'],
            capture_output=True, text=True).stdout.strip()
        new = subprocess.run(
            ["bash", "-c", f'cd {root}; find "claimed" -maxdepth 1 -name "codex--*.json" 2>/dev/null | wc -l'],
            capture_output=True, text=True).stdout.strip()
        assert int(old) != 0, "expected the buggy ls-glob to mis-count (list cwd) under nullglob"
        assert int(new) == 0, "find-based count must be 0 when the agent holds no claims"


if __name__ == "__main__":
    test_watcher_uses_find_for_held_count()
    test_find_count_is_nullglob_safe_vs_bare_ls()
    print("PASS: held count is nullglob-safe (find), buggy ls-glob proven to mis-count")
