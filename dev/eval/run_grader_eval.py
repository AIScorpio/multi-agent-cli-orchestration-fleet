#!/usr/bin/env python3
"""Empirical grader-agreement eval (P4) — the QA-5 measurement instrument.

Runs grader.grade with a REAL workhorse over the labeled corpus in dev/eval/corpus/
and reports agreement vs the human labels. This is NOT a pytest gate (it needs live
CLIs and is non-deterministic) — run it by hand to measure how well the automated
second-opinion separates good from bad:

  python3 dev/eval/run_grader_eval.py

QA-5 honest framing: the goal is not "perfect judgment" but "the human is no longer
the ONLY gate" — measured here as agreement rate, not a hard pass/fail.
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
import grader  # noqa: E402

CORPUS = Path(__file__).resolve().parent / "corpus"


def main():
    items = [json.loads(f.read_text()) for f in sorted(CORPUS.glob("*.json"))]
    if not items:
        print("no corpus items found"); return
    agree = 0
    print(f"grading {len(items)} corpus items with the real workhorse grader…\n")
    for it in items:
        truth_ok = (it["label"] == "good")
        v = grader.grade(it["deliverable"], it["criteria"])   # default = real runner
        match = (v["ok"] == truth_ok)
        agree += match
        mark = "✓" if match else "✗"
        print(f"  {mark} label={it['label']:4s} grader_ok={v['ok']} "
              f"reasons={v.get('reasons')}")
    rate = agree / len(items)
    print(f"\nagreement: {agree}/{len(items)} = {rate:.0%}")
    print("(QA-5 = human no longer the only gate; this measures separation, not "
          "perfection.)")


if __name__ == "__main__":
    main()
