"""Complete-audit retention: the gc must NEVER reap qa-passed result.json/verdict.json — not by
the 14-day age rule, not by the per-dir cap. They are the durable QA audit trail + board record.
(Other gc targets — old logs etc. — must still be pruned, i.e. gc isn't disabled.)"""
import os
import sys
import time
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import doctor  # noqa: E402


def _setup(tmp, monkeypatch):
    ma = tmp / ".fleet"
    q = ma / "queue"
    logs = ma / "status" / "logs"
    (q / "completed" / "qa-passed").mkdir(parents=True)
    logs.mkdir(parents=True)
    monkeypatch.setattr(doctor, "MA", ma)
    monkeypatch.setattr(doctor, "QUEUE", q)
    monkeypatch.setattr(doctor, "LOGS", logs)
    return ma, q, logs


def _age(p):
    t = time.time() - 100 * 24 * 3600       # 100 days old
    os.utime(p, (t, t))


def test_gc_keeps_old_qa_sidecars(tmp_path, monkeypatch):
    _, q, _ = _setup(tmp_path, monkeypatch)
    qa = q / "completed" / "qa-passed"
    r = qa / "t1.result.json"; r.write_text("{}"); _age(r)
    v = qa / "t1.verdict.json"; v.write_text("{}"); _age(v)
    doctor.gc_artifacts(now=time.time())
    assert r.exists(), "old qa-passed result.json must be RETAINED (complete audit)"
    assert v.exists(), "old qa-passed verdict.json must be RETAINED (complete audit)"


def test_gc_no_cap_on_qa_sidecars(tmp_path, monkeypatch):
    _, q, _ = _setup(tmp_path, monkeypatch)
    qa = q / "completed" / "qa-passed"
    for i in range(550):                      # > GC_MAX_PER_DIR (500)
        (qa / f"t{i}.result.json").write_text("{}")
    doctor.gc_artifacts(now=time.time())
    assert len(list(qa.glob("*.result.json"))) == 550, "qa-passed sidecars must NOT be cap-pruned"


def test_gc_still_prunes_old_logs(tmp_path, monkeypatch):
    _, _, logs = _setup(tmp_path, monkeypatch)
    lg = logs / "old.log"; lg.write_text("x"); _age(lg)
    doctor.gc_artifacts(now=time.time())
    assert not lg.exists(), "gc must still prune old logs (gc not disabled, only qa-passed exempt)"
