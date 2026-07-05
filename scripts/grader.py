#!/usr/bin/env python3
"""Auto second-opinion grader (P4) — breaks the "quality survives only as fast as one
leader reads" ceiling. A cheap workhorse grades a deliverable against its
acceptance_criteria and returns a STRUCTURED verdict; the leader stays the final
authority but is no longer the ONLY gate.

grade(deliverable, criteria, runner=None) -> {"ok": bool, "reasons": [...], "raw": str}
  runner(prompt)->str is INJECTABLE (tests pass a fake; default spawns a workhorse).
  Parsing is fail-open: malformed output or a runner error → {"ok": False, ...} (a
  grader that can't judge must never rubber-stamp).
"""
import json
import os
import re
import subprocess


def _grader_timeout(prompt: str) -> int:
    """Judge-CLI timeout. FLEET_GRADER_TIMEOUT wins; else scale with prompt size — a
    grounded content-task prompt carries whole context_files (papers can be >100KB) and
    the old flat 180s made every judge time out → chain exhausted → fail-closed bounce
    (observed live 2026-07-05: 6/6 qa-pass calls auto-bounced with an EMPTY verdict)."""
    env = os.environ.get("FLEET_GRADER_TIMEOUT")
    if env:
        try:
            return max(30, int(env))
        except ValueError:
            pass
    return 180 if len(prompt) < 20_000 else 600


# Cap the SOURCES block so the grounding prompt stays judgeable in one CLI call.
_MAX_SOURCES_CHARS = 80_000


def _cap_sources(sources: str) -> str:
    limit = int(os.environ.get("FLEET_GRADER_MAX_SOURCES", _MAX_SOURCES_CHARS))
    if len(sources) <= limit:
        return sources
    half = limit // 2
    return (sources[:half]
            + "\n\n[... SOURCES TRUNCATED FOR LENGTH — the middle portion is elided. "
              "Judge groundedness on the included portions; do NOT flag a claim as "
              "fabricated merely because its support may fall in the elided middle ...]\n\n"
            + sources[-half:])


def _build_prompt(deliverable: str, criteria, sources=None) -> str:
    crit = "\n".join(f"- {c}" for c in (criteria or []))
    if sources:
        sources = _cap_sources(sources)
    # When SOURCES are supplied (P7: the task's context_files), the grader checks
    # GROUNDEDNESS — every factual claim/number/citation in the deliverable must be
    # supported by the sources — instead of judging mere plausibility. This is the
    # anti-fabrication teeth for research/data work.
    src_block = ""
    ground = ""
    if sources:
        src_block = f"\n\nSOURCES (the ONLY admissible support for factual claims):\n{sources}\n"
        ground = ("Additionally, set ok=false if ANY factual claim, statistic, citation, "
                  "or number in the deliverable is NOT supported by the SOURCES above "
                  "(fabrication / ungrounded claim). ")
    return (
        "You are a strict QA reviewer. Judge whether the DELIVERABLE satisfies EVERY "
        "acceptance criterion. " + ground + "Reply with ONLY a JSON object: "
        '{"ok": true|false, "reasons": ["..."]}. ok=true only if ALL criteria are met '
        "and nothing is fabricated.\n\n"
        f"ACCEPTANCE CRITERIA:\n{crit}{src_block}\n\nDELIVERABLE:\n{deliverable}\n"
    )


def _runner_cmds(prompt: str) -> dict:
    """The grader CLI per model. STRONG judges (claude/codex) first — the anti-fab verdict
    on the highest-risk output must NOT be left to the weakest models (P17)."""
    claude_bin = os.path.expanduser(os.environ.get("CLAUDE_BIN", "~/.local/bin/claude"))
    return {
        "claude":   [claude_bin, "-p", prompt, "--model", "claude-sonnet-4-6",
                     "--dangerously-skip-permissions"],
        "codex":    ["codex", "exec", "--skip-git-repo-check", "-s", "read-only", prompt],
        "opencode": ["opencode", "run", "--dangerously-skip-permissions",
                     "--model", os.environ.get("OPENCODE_MODEL", "zhipuai-coding-plan/glm-5.2"), prompt],
        "kimi":     [os.path.expanduser("~/.kimi-code/bin/kimi"), "-p", prompt],
    }


def resolve_grader_model(content_task: bool = False) -> str:
    """Pick the grader model. FLEET_GRADER_MODEL (codex|kimi|opencode|claude) wins. Else a
    CONTENT task (research/write/review) defaults to a NON-leader model (codex) so the
    honesty / anti-fabrication verdict is INDEPENDENT of the leader's own model (claude) —
    a same-model second pass reproduces the same blind spots. Code/test tasks keep claude.
    Set FLEET_GRADER_MODEL=kimi|opencode to conserve scarce codex quota (both are
    quota-abundant and still independent of the leader). This returns the FIRST-CHOICE model;
    grade(..., independent=True) then auto-falls-back codex → kimi → opencode if the first
    choice errors or hits its quota, so a codex cliff never hard-stops a content grade."""
    env = os.environ.get("FLEET_GRADER_MODEL")
    if env:
        return env
    return "codex" if content_task else "claude"


# Model pools for grader fallback. INDEPENDENT (content-task) grades use NON-leader models
# only — codex → kimi → opencode — so a codex quota cliff falls back to another INDEPENDENT
# judge and never silently to the leader (claude). If all three are down the grade fails
# CLOSED (the content task bounces; it is NOT rubber-stamped). Non-independent grades may use
# the full pool incl. claude.
_NONLEADER = ("codex", "kimi", "opencode")
_ALL = ("codex", "kimi", "opencode", "claude")


def _is_verdict(raw: str) -> bool:
    """True iff `raw` carries a parseable {"ok": ...} verdict (a real judgment, pass OR fail).
    A CLI error, empty reply, quota/usage-limit message, or plain prose is NOT a verdict — so
    the runner SKIPS it and falls through to the next model, instead of treating a quota stub
    as a failed grade (which would hard-stop the content task)."""
    try:
        d = json.loads(raw)
        if isinstance(d, dict) and "ok" in d:
            return True
    except Exception:
        pass
    m = re.search(r"\{.*\}", raw, re.S)
    if m:
        try:
            d = json.loads(m.group(0))
            return isinstance(d, dict) and "ok" in d
        except Exception:
            return False
    return False


def _grader_chain(model: str, independent: bool) -> list:
    """Ordered model fallback for a pinned grade: `model` first, then the rest of the pool.
    INDEPENDENT grades exclude the leader (claude) from the pool."""
    pool = list(_NONLEADER if independent else _ALL)
    return [model] + [m for m in pool if m != model]


def _run_chain(prompt: str, chain) -> tuple:
    """Run models in `chain` order; return (stdout, model) for the FIRST that yields a
    PARSEABLE verdict. A CLI error / empty reply / quota stub / non-verdict prose is skipped
    (so codex hitting its usage limit falls through to kimi → opencode). ('', None) if none
    produced a verdict → the caller fails closed."""
    cmds = _runner_cmds(prompt)
    for m in chain:
        cmd = cmds.get(m)
        if not cmd:
            continue
        try:
            r = subprocess.run(cmd, capture_output=True, text=True,
                               timeout=_grader_timeout(prompt))
        except Exception:
            continue
        if r.returncode == 0 and r.stdout.strip() and _is_verdict(r.stdout):
            return r.stdout, m
    return "", None


def _default_runner(prompt: str) -> str:
    """Legacy / no-model path: FLEET_GRADER_MODEL (default claude when unset), first non-empty
    reply (parsing is fail-closed downstream). Fail-open → ''."""
    chosen = os.environ.get("FLEET_GRADER_MODEL", "claude")
    order = [chosen] + [m for m in _ALL if m != chosen]
    cmds = _runner_cmds(prompt)
    for m in order:
        cmd = cmds.get(m)
        if not cmd:
            continue
        try:
            r = subprocess.run(cmd, capture_output=True, text=True,
                               timeout=_grader_timeout(prompt))
            if r.returncode == 0 and r.stdout.strip():
                return r.stdout
        except Exception:
            continue
    return ""


def _parse(raw: str) -> dict:
    # 1. whole-string JSON
    try:
        d = json.loads(raw)
        if isinstance(d, dict) and "ok" in d:
            return {"ok": bool(d["ok"]), "reasons": list(d.get("reasons", []))}
    except Exception:
        pass
    # 2. embedded JSON object
    m = re.search(r"\{.*\}", raw, re.S)
    if m:
        try:
            d = json.loads(m.group(0))
            if isinstance(d, dict) and "ok" in d:
                return {"ok": bool(d["ok"]), "reasons": list(d.get("reasons", []))}
        except Exception:
            pass
    # FAIL-CLOSED: only a valid JSON verdict with ok=true passes. A bare "YES"/"PASS"
    # or any prose is NOT a pass — a grader that can't be parsed must never
    # rubber-stamp (P6: removed the YES-prefix shortcut that the eval flagged).
    return {"ok": False,
            "reasons": [f"grader output not a valid JSON verdict (fail-closed): "
                        f"{raw.strip()[:120]}"]}


def grade(deliverable: str, criteria, runner=None, sources=None, model=None,
          independent=False) -> dict:
    """Grade a deliverable. Never raises; a grader that cannot judge returns ok=False.
    `sources` (optional): admissible support text (task context_files) → the grader
    additionally checks groundedness / flags fabrication (P7).
    `model` (optional): first-choice judging model. With no explicit `runner`, grading runs
    the model-pinned fallback CHAIN and records the ACTUAL model that produced the verdict —
    so a codex quota cliff that falls back to kimi/opencode is reflected truthfully (Phase 2).
    `independent` (content tasks): restrict the fallback pool to NON-leader models (the grade
    fails CLOSED rather than ever judging a content deliverable with the leader's own model)."""
    prompt = _build_prompt(deliverable, criteria, sources=sources)
    # Injected runner (tests) OR legacy no-model path: single shot; record the intended model.
    if runner is not None or model is None:
        run = runner or _default_runner
        recorded = model or os.environ.get("FLEET_GRADER_MODEL", "claude")
        try:
            raw = run(prompt) or ""
        except Exception as e:
            return {"ok": False, "reasons": [f"grader runner error: {e}"], "raw": "", "model": recorded}
        verdict = _parse(raw)
        verdict["raw"] = raw
        verdict["model"] = recorded
        return verdict
    # Model-aware path: quota-tolerant fallback chain; record the ACTUAL judging model.
    raw, used = _run_chain(prompt, _grader_chain(model, independent))
    verdict = _parse(raw)
    verdict["raw"] = raw
    verdict["model"] = used or model
    return verdict
