#!/usr/bin/env python3
"""Project-type profiles (P4) — make the framework adapt to non-software projects.

A project declares its type in .fleet/profile.json ({"profile": "research"|"writing"
|"review"|"software"|"data"|"ml"}); default "software". The profile selects which
discipline block the watcher injects per task type — so the framework's #1 guard for a
research/writing project (ANTI-FABRICATION) is applied by construction, not improvised.

  load_profile(root) -> str
  discipline_block(task_type, profile="software") -> str   (injected into the worker prompt)
"""
import json
from pathlib import Path

# Software code/test discipline — preserves the exact phrases the watcher prompt tests
# assert ("NO hard-coded values", "config").
CODE_BLOCK = """
Engineering discipline (MANDATORY for code deliverables):
- NO hard-coded values. Do not bake magic numbers, thresholds, paths, URLs, model
  names, dataset sizes, or credentials into function bodies.
- Parameterize instead: function arguments / CLI flags with sensible defaults.
- A value that must be a constant lives in ONE editable place — a config file
  (e.g. config.json/yaml) or a clearly-marked CONSTANTS block — referenced everywhere.
- Values given in the task description are DEFAULTS to wire through config/args, never
  literals to scatter through the code.
"""

# Anti-fabrication discipline for research/writing/review — the highest-risk failure
# mode (invented citations/numbers) for non-code work, otherwise the LEAST guarded path.
ANTIFAB_BLOCK = """
Anti-fabrication discipline (MANDATORY for research / writing / review deliverables):
- NEVER fabricate. Every factual claim, statistic, quote, or citation MUST trace to a
  source present in this task's context_files.
- Do NOT invent citations, DOIs, author names, dates, or numbers. If a needed source
  is not provided, say so explicitly — do not manufacture one.
- Cite the supporting source inline for each non-trivial claim; an unsupported claim is
  a defect, not a stylistic choice.
"""

# Data/ML discipline — the failure mode here is fabricated NUMBERS (metrics, p-values,
# row counts, accuracies) even in CODE deliverables, so this block carries BOTH the
# engineering discipline AND a numbers-anti-fabrication clause.
# Numbers-anti-fabrication clause — the data/ML failure mode (invented metrics/counts),
# applied to BOTH code and writeups in a data/ML project.
NUMBERS_BLOCK = """
Data / ML integrity (MANDATORY — this is a data/ML project):
- NEVER fabricate a number. Every metric, score, p-value, row/sample count, or result
  MUST trace to actually running the code on the actual data — never invented, recalled
  from memory, or copied from the task description as if it were an observed result.
- Report a value only after you have COMPUTED and OBSERVED it; if a run did not produce
  it, say so — do not fill the gap with a plausible figure.
- State the data/seed/config a reported number came from so it is reproducible.
"""
DATA_BLOCK = CODE_BLOCK + NUMBERS_BLOCK

_RESEARCH_PROFILES = {"research", "writing", "review", "academic", "docs"}
_RESEARCH_TASKS = {"research", "write", "review"}
_DATA_PROFILES = {"data", "ml", "datascience", "analytics"}


def load_profile(root) -> str:
    """Read .fleet/profile.json {"profile": ...}; default 'software'. Fail-safe."""
    try:
        p = Path(root) / ".fleet" / "profile.json"
        prof = json.loads(p.read_text()).get("profile")
        return prof if isinstance(prof, str) and prof else "software"
    except Exception:
        return "software"


def discipline_block(task_type: str, profile: str = "software") -> str:
    """The discipline text to inject for (task_type, profile). Empty when neither
    applies (e.g. a research task on a software profile that isn't code)."""
    # COMPOSE blocks so no discipline is lost to branch ordering (P10): a code task in a
    # research/data project keeps the engineering block AND gets anti-fab; a data writeup
    # gets anti-fab + the numbers clause.
    is_code = task_type in ("code", "test")
    parts = []
    if is_code:
        parts.append(CODE_BLOCK)                                  # engineering for any code
    if profile in _DATA_PROFILES:
        parts.append(NUMBERS_BLOCK)                               # numbers anti-fab (code + writeup)
        if not is_code:
            parts.append(ANTIFAB_BLOCK)                           # writeup: also general anti-fab
    elif profile in _RESEARCH_PROFILES or task_type in _RESEARCH_TASKS:
        parts.append(ANTIFAB_BLOCK)                               # research/writing: anti-fab
    return "".join(parts)


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Fleet project-type profiles")
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("block", help="print the discipline block for a task type")
    b.add_argument("task_type")
    b.add_argument("--root", default=".")
    args = ap.parse_args()
    if args.cmd == "block":
        print(discipline_block(args.task_type, load_profile(args.root)), end="")
