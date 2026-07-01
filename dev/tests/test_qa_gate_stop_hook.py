"""Part 2 — Stop hook forces the leader's QA before it idles (autonomous mode).
While AUTONOMOUS_ON and there are completed tasks awaiting QA (result.json not yet in qa-passed/),
the hook BLOCKS the stop (exit 2 + stderr). Otherwise (attended, or nothing pending) it exit-0s.
Plus: the hook is registered as a Stop hook by init_workspace."""
import json
import os
import subprocess
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
HOOK = SCRIPTS / "hooks" / "qa_gate_stop.py"


def _proj(tmp, autonomous=True, pending=True):
    comp = tmp / ".fleet" / "queue" / "completed"
    (comp / "qa-passed").mkdir(parents=True)
    if autonomous:
        (tmp / ".fleet" / "AUTONOMOUS_ON").touch()
    if pending:
        comp.joinpath("t1.json").write_text(json.dumps({"task_id": "t1", "title": "impl vs paper"}))
        comp.joinpath("t1.result.json").write_text(json.dumps({"task_id": "t1"}))
    return tmp


def _run(tmp):
    return subprocess.run([sys.executable, str(HOOK)], input="{}", text=True,
                          capture_output=True,
                          env=dict(os.environ, CLAUDE_PROJECT_DIR=str(tmp)))


def test_blocks_when_autonomous_and_pending(tmp_path):
    _proj(tmp_path, autonomous=True, pending=True)
    r = _run(tmp_path)
    assert r.returncode == 2, "must BLOCK idle while QA pending in autonomous mode"
    assert "qa-pass" in r.stderr.lower() and "t1" in r.stderr


def test_allows_when_not_autonomous(tmp_path):
    _proj(tmp_path, autonomous=False, pending=True)
    assert _run(tmp_path).returncode == 0, "attended mode → human decides, no block"


def test_allows_when_nothing_pending(tmp_path):
    _proj(tmp_path, autonomous=True, pending=False)
    assert _run(tmp_path).returncode == 0


def test_allows_when_already_qa_passed(tmp_path):
    tmp = _proj(tmp_path, autonomous=True, pending=True)
    comp = tmp / ".fleet" / "queue" / "completed"
    (comp / "qa-passed" / "t1.result.json").write_text((comp / "t1.result.json").read_text())
    assert _run(tmp).returncode == 0, "a qa-passed task is no longer pending"


def _card_proj(tmp, status):
    ma = tmp / ".fleet"
    (ma / "queue" / "completed" / "qa-passed").mkdir(parents=True)
    (ma / "status").mkdir(parents=True)
    (ma / "AUTONOMOUS_ON").touch()
    (ma / "status" / "board_cards.json").write_text(
        json.dumps({"cards": [{"id": "d1", "title": "sweep", "status": status}]}))
    return tmp


def test_blocks_on_detached_done_card(tmp_path):       # D4
    _card_proj(tmp_path, status="done")
    r = _run(tmp_path)
    assert r.returncode == 2, "a detached done·pending-QA card must block idle"
    assert "approve-card" in r.stderr and "d1" in r.stderr


def test_allows_when_detached_card_approved(tmp_path):  # D4
    _card_proj(tmp_path, status="approved")
    assert _run(tmp_path).returncode == 0, "an approved card is no longer pending"


def test_registered_as_stop_hook(tmp_path):
    sys.path.insert(0, str(SCRIPTS))
    import init_workspace
    assert "qa_gate_stop.py" in init_workspace.HOOK_SCRIPTS
    init_workspace.init_workspace(tmp_path, force=False, perms=True)
    settings = json.loads((tmp_path / ".claude" / "settings.json").read_text())
    stop = settings.get("hooks", {}).get("Stop", [])
    cmds = [h.get("command", "") for e in stop for h in e.get("hooks", [])]
    assert any("qa_gate_stop.py" in c for c in cmds), "init must register the Stop hook"
