"""Tests for the autonomous-mode Bash discipline guard (scan logic)."""
import importlib.util
import os

_HERE = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), "scripts", "hooks")
_spec = importlib.util.spec_from_file_location(
    "autonomous_bash_guard", os.path.join(_HERE, "autonomous_bash_guard.py")
)
guard = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(guard)
scan = guard.scan


# ---- ALLOWED (must return None) -------------------------------------------

def test_plain_command_allowed():
    assert scan("pgrep -fl run_adversarial_sweep") is None


def test_pipe_and_and_allowed():
    assert scan("pgrep -fl foo | grep -v zsh") is None
    assert scan("a && b") is None


def test_append_heredoc_with_redirect_chars_in_body_allowed():
    # the exact logging pattern used every supervisor pass — body has > and ->
    cmd = (
        "cat >> /tmp/log.txt <<'EOF'\n"
        "graph>abc REVERSAL; zero 0.41->0.11->0.008\n"
        "cross_encoder 2h10m; ratio a>b\n"
        "EOF"
    )
    assert scan(cmd) is None


def test_special_var_dollar_question_allowed():
    assert scan('echo "append exit $?"') is None


def test_redirect_char_inside_quotes_allowed():
    assert scan('echo "a > b and 2> c"') is None


def test_double_append_only_allowed():
    assert scan("printf 'x\\n' >> file.txt") is None


# ---- BLOCKED (must return a reason) ---------------------------------------

def test_command_substitution_blocked():
    assert scan("ps -p $(pgrep -f foo)") is not None


def test_brace_expansion_blocked():
    assert scan("echo ${HOME}/x") is not None


def test_stderr_redirect_blocked():
    assert scan("cmd 2>/dev/null") is not None
    assert scan("cmd 2> err.log") is not None


def test_truncating_redirect_blocked():
    assert scan("echo x > file.txt") is not None


def test_force_redirect_blocked():
    assert scan("echo x >| file.txt") is not None


def test_leading_cd_blocked():
    assert scan("cd /tmp && ls") is not None
    assert scan("ls; cd /tmp") is not None


def test_cd_substring_not_blocked():
    # 'abcd' or '--include' must not look like a `cd` command
    assert scan("ls abcd/") is None


def test_python_c_with_hash_comment_blocked():
    cmd = 'python -c "\nimport json  # load\nprint(1)\n"'
    assert scan(cmd) is not None


def test_python_c_single_line_no_hash_allowed():
    assert scan('python -c "print(1)"') is None


def test_log_heredoc_mentioning_python_c_as_data_allowed():
    # regression: a log-append heredoc whose BODY documents the rule must not
    # be mistaken for a real `python -c` invocation (false positive seen live)
    cmd = (
        "cat >> /tmp/log.txt <<'EOF'\n"
        "lesson: avoid inline python -c with a # comment in autonomous mode\n"
        "EOF"
    )
    assert scan(cmd) is None


# ---- main() wrapper: sentinel gate + exit codes ----------------------------

import io
import json as _json
import pytest


def _run_main(monkeypatch, command, sentinel_present, tool="Bash", tmp_path=None):
    proj = str(tmp_path)
    mad = os.path.join(proj, ".fleet")
    os.makedirs(mad, exist_ok=True)
    if sentinel_present:
        open(os.path.join(mad, "AUTONOMOUS_ON"), "w").close()
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", proj)
    payload = _json.dumps({"tool_name": tool, "tool_input": {"command": command}})
    monkeypatch.setattr("sys.stdin", io.StringIO(payload))
    with pytest.raises(SystemExit) as exc:
        guard.main()
    return exc.value.code or 0


def test_main_allows_when_sentinel_absent(monkeypatch, tmp_path):
    # bad command, but no sentinel -> not autonomous -> allow (0)
    assert _run_main(monkeypatch, "ps -p $(pgrep x)", False, tmp_path=tmp_path) == 0


def test_main_blocks_when_sentinel_present(monkeypatch, tmp_path):
    assert _run_main(monkeypatch, "ps -p $(pgrep x)", True, tmp_path=tmp_path) == 2


def test_main_allows_good_command_with_sentinel(monkeypatch, tmp_path):
    assert _run_main(monkeypatch, "pgrep -fl foo | grep -v zsh", True, tmp_path=tmp_path) == 0


def test_main_ignores_non_bash_tool(monkeypatch, tmp_path):
    assert _run_main(monkeypatch, "ps -p $(pgrep x)", True, tool="Read", tmp_path=tmp_path) == 0
