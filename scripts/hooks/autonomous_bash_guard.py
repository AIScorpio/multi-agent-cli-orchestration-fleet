#!/usr/bin/env python3
"""PreToolUse hook: enforce prompt-free Bash discipline during autonomous mode.

Rationale (multi-agent-cli-orchestration-fleet skill, "Prompt-free Bash discipline"):
in an unattended/overnight run the permission setup auto-approves only literal
command-style Bash. Commands containing command substitution (`$(...)` OR
back-quotes), brace expansion, output redirection, a leading `cd`, or an inline
`python -c` with a `#` comment fall through to an interactive permission prompt
and STALL the loop. Back-quotes are doubly nasty: they are NOT neutralized by
double quotes, so markdown code spans in a quoted --description get executed and
silently corrupt the task spec. Docs alone did not stop the leader from slipping,
so this hook BLOCKS such commands and returns the rewrite hint — enforcement.

Contract:
- Reads the PreToolUse JSON event on stdin (tool_name, tool_input.command).
- Only acts on the Bash tool, and ONLY while the sentinel
  `$CLAUDE_PROJECT_DIR/.fleet/AUTONOMOUS_ON` exists (the on/off switch).
- On a violation: writes the reason to stderr and exits 2 (PreToolUse "block"),
  which feeds the reason back to Claude to rewrite the command.
- FAILS OPEN: any parsing/scan error, missing sentinel, or non-Bash tool -> exit 0
  (allow). A buggy guard must never wedge the whole loop.

Allowed (NOT blocked): `>>` append, `<<`/`<<-` heredocs, pipes `|`, `&&`, `;`,
`$?`, and any of the above tokens when they appear inside quotes or a heredoc
body (so log appends with data like `graph>abc` or `0.41->0.11` are fine).
"""
import sys
import os
import re
import json


def _mask_heredoc(cmd: str) -> str:
    """Blank heredoc bodies (newlines preserved) so their data contents — which
    may legitimately contain `>`, `$(`, backticks, etc. — don't trip the scanner.
    """
    s = list(cmd)
    n = len(s)
    for m in re.finditer(r"<<-?\s*([\"']?)(\w+)\1", cmd):
        tag = m.group(2)
        # body starts after the newline that ends the line containing the <<TAG
        nl = cmd.find("\n", m.end())
        if nl == -1:
            continue
        body_start = nl + 1
        # find terminating line == tag (optionally indented for <<-)
        term = re.search(r"(?m)^[ \t]*" + re.escape(tag) + r"[ \t]*$", cmd[body_start:])
        body_end = body_start + term.start() if term else n
        for i in range(body_start, min(body_end, n)):
            if s[i] != "\n":
                s[i] = " "
    return "".join(s)


def _mask(cmd: str) -> str:
    """Return cmd with heredoc bodies AND quoted spans replaced by spaces.

    Newlines are preserved so line structure is unchanged. Both quote kinds are
    blanked here — correct for tokens that quotes DO neutralize (redirection,
    `cd`, the `python -c` skeleton). NOT used for command substitution, which
    double quotes do not neutralize — that goes through `_live_expansion`.
    """
    out = []
    quote = None
    for ch in _mask_heredoc(cmd):
        if quote:
            if ch == quote:
                quote = None
                out.append(ch)
            else:
                out.append(" " if ch != "\n" else "\n")
        else:
            if ch in ("'", '"'):
                quote = ch
                out.append(ch)
            else:
                out.append(ch)
    return "".join(out)


def _live_expansion(cmd: str):
    """Detect command substitution / parameter expansion the shell WOULD execute.

    SINGLE quotes make these literal; DOUBLE quotes do NOT — back-quotes,
    `$(...)`, and `${...}` all expand inside "". So `_mask` (which blanks both
    quote kinds) misses e.g. "$(whoami)" or a markdown code span `def f(*a)`
    inside a double-quoted --description (the latter silently corrupted a task
    spec on 2026-06-05). Heredoc bodies are pre-masked (treated as data).
    Backslash escapes the next char. Returns 'backtick' | 'dollar-paren' |
    'brace' | None.
    """
    s = _mask_heredoc(cmd)
    quote = None
    i, n = 0, len(s)
    while i < n:
        ch = s[i]
        nxt = s[i + 1] if i + 1 < n else ""
        if quote == "'":
            if ch == "'":
                quote = None
        elif quote == '"':
            if ch == "\\":
                i += 1
            elif ch == '"':
                quote = None
            elif ch == "`":
                return "backtick"
            elif ch == "$" and nxt == "(":
                return "dollar-paren"
            elif ch == "$" and nxt == "{":
                return "brace"
        else:
            if ch == "\\":
                i += 1
            elif ch == "'":
                quote = "'"
            elif ch == '"':
                quote = '"'
            elif ch == "`":
                return "backtick"
            elif ch == "$" and nxt == "(":
                return "dollar-paren"
            elif ch == "$" and nxt == "{":
                return "brace"
        i += 1
    return None


def scan(cmd: str):
    """Return a human reason string if cmd violates the discipline, else None."""
    masked = _mask(cmd)

    # Command substitution / parameter expansion ($(...), ${...}, back-quotes).
    # Routed through `_live_expansion`, NOT `masked`, because double quotes do not
    # neutralize these (only single quotes do). This catches "$(whoami)" and the
    # markdown code span `def f(*a, **k):` inside a double-quoted --description
    # that the shell executes — silently deleting the span and mangling the task
    # spec (observed 2026-06-05).
    expansion = _live_expansion(cmd)
    if expansion == "backtick":
        return ("backtick command substitution `...` (double quotes do NOT protect "
                "backticks) — for a task spec with code, write it to a file with the "
                "Write tool and pass it as a context_file; never inline backticks/code "
                "in shell args")
    if expansion == "dollar-paren":
        return "command substitution `$(...)` — run it as its own command, read the output, then act"
    if expansion == "brace":
        return "brace/parameter expansion `${...}` — avoid; break into steps"

    # A REAL `python -c` invocation (executable token + a `-c` flag) in the
    # command SKELETON. Heredoc bodies and quoted spans are masked, so the
    # words "python -c ... #" appearing as DATA (e.g. inside a log heredoc that
    # documents this very rule) do NOT trip it. A MULTI-LINE inline -c (newline
    # anywhere in the raw cmd) is blocked regardless of `#`: a multi-line quoted
    # -c script slips past the guard's earlier (#-only) rule yet still trips the
    # interactive permission prompt. A single-line `python -c "print(1)"`
    # (no newline anywhere in the command) is still allowed.
    if (re.search(r"python[0-9.]*\b[^|;&\n]*?(?:^|\s)-c(?:\s|[\"']|$)", masked)
            and ("\n" in cmd)):
        return ("multi-line inline `python -c` (newline in the command) — write a "
                ".py file and run `<venv>/bin/python script.py` instead "
                "(multi-line inline -c trips the interactive permission prompt)")

    if re.search(r"(?:^|[;&|]|&&)\s*cd\s", masked):
        return "`cd` — omit it and put the absolute path inside the command"

    # Redirection: allow `>>` (append) and `<<` (heredoc); block `2>`, `>|`, `>`.
    tmp = masked.replace(">>", "  ")          # neutralize append
    tmp = re.sub(r"<<-?", "  ", tmp)           # neutralize heredoc operators
    if re.search(r"\d*\s*>\s*\|", tmp) or re.search(r"\d+\s*>", tmp):
        return "stderr/forced redirection (`2>`, `>|`) — drop it; use the Read tool or let stderr print"
    if ">" in tmp:
        return "output redirection `>` — use the Write tool (note: `>>` append is allowed)"
    return None


def main() -> None:
    try:
        data = json.load(sys.stdin)
    except Exception:
        sys.exit(0)  # fail open

    tool = data.get("tool_name") or data.get("tool") or ""
    if tool != "Bash":
        sys.exit(0)

    cmd = ((data.get("tool_input") or {}).get("command") or "")
    if not cmd.strip():
        sys.exit(0)

    proj = os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
    sentinel = os.path.join(proj, ".fleet", "AUTONOMOUS_ON")
    if not os.path.exists(sentinel):
        sys.exit(0)  # not in autonomous mode -> allow everything

    try:
        reason = scan(cmd)
    except Exception:
        sys.exit(0)  # fail open on any scanner bug

    if reason:
        sys.stderr.write(
            "BLOCKED by autonomous-mode Bash discipline: " + reason + ".\n"
            "Rewrite the command (see the multi-agent skill: 'Prompt-free Bash "
            "discipline'). To disable enforcement, remove "
            ".fleet/AUTONOMOUS_ON.\n"
        )
        sys.exit(2)
    sys.exit(0)


if __name__ == "__main__":
    main()
