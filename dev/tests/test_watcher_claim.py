"""Behavioral tests for watcher.sh claim_one() — atomic claim + ownership stamp.
Runs the REAL bash function extracted from the script (sourcing would start the
main loop)."""
import json
import re
import subprocess
from pathlib import Path

WATCHER = Path(__file__).resolve().parents[2] / "scripts" / "watcher.sh"


def _run_claim(tmp_path, tasks, agent="kimi"):
    pending = tmp_path / "pending"
    claimed = tmp_path / "claimed"
    pending.mkdir()
    claimed.mkdir()
    for t in tasks:
        (pending / f"{t['task_id']}.json").write_text(json.dumps(t))
    src = WATCHER.read_text()
    m = re.search(r"^claim_one\(\) \{.*?^\}", src, re.M | re.S)
    assert m, "claim_one() not found in watcher.sh"
    script = (f'AGENT="{agent}"\nPENDING="{pending}"\nCLAIMED="{claimed}"\n'
              + m.group(0)
              + '\necho "SHELLPID=$$"\nclaim_one "${1:-10}"\necho\n')
    r = subprocess.run(["bash", "-c", script, "bash"],
                       capture_output=True, text=True, timeout=10)
    shellpid = int(re.search(r"SHELLPID=(\d+)", r.stdout).group(1))
    claimed_path = r.stdout.strip().splitlines()[-1] if r.stdout.strip().splitlines()[-1].endswith(".json") else None
    return shellpid, claimed_path, pending, claimed


def _t(tid, assigned="kimi", prio=5):
    return {"task_id": tid, "assigned_to": assigned, "priority": prio}


def test_claim_stamps_owner_pid(tmp_path):
    shellpid, claimed_path, pending, claimed = _run_claim(tmp_path, [_t("a1")])
    assert claimed_path and claimed_path.endswith("kimi--a1.json")
    d = json.loads(Path(claimed_path).read_text())
    assert d["claimed_by_pid"] == shellpid          # ownership stamp present
    assert not list(pending.glob("*.json"))         # moved out of pending


def test_claim_respects_priority_order(tmp_path):
    _, claimed_path, *_ = _run_claim(
        tmp_path, [_t("low", prio=8), _t("hot", prio=1)])
    assert claimed_path.endswith("kimi--hot.json")


def test_claim_skips_other_agents(tmp_path):
    _, claimed_path, pending, _ = _run_claim(
        tmp_path, [_t("notmine", assigned="codex")])
    assert claimed_path is None                     # nothing eligible
    assert (pending / "notmine.json").exists()


def test_claim_takes_any_pool(tmp_path):
    _, claimed_path, *_ = _run_claim(tmp_path, [_t("a2", assigned="any")])
    assert claimed_path.endswith("kimi--a2.json")
