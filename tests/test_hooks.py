import json
import shutil
import subprocess
import sys

import pytest

from searchslim.hooks import GREP_REASON_HEADER, grep_to_rg, handle
from searchslim.rules import Config, NOTE_PREFIX, estimate_tokens

needs_rg = pytest.mark.skipif(shutil.which("rg") is None, reason="rg not installed")


@pytest.fixture
def repo(tmp_path):
    for i in range(40):
        body = "\n".join(f"    value_{n} = compute(target, {n})" if n % 3 == 0 else f"    other_{n} = 0" for n in range(90))
        (tmp_path / f"mod{i:02d}.py").write_text(body + "\n")
    (tmp_path / "small.txt").write_text("one needle here\n")
    return tmp_path


def test_bash_search_is_rewritten_keeping_other_fields():
    out = handle({"tool_name": "Bash", "tool_input": {"command": "rg foo", "description": "search"}})
    upd = out["hookSpecificOutput"]["updatedInput"]
    assert upd["description"] == "search"
    assert upd["command"].endswith("-m searchslim run --max-tokens=2000 -- rg --with-filename --line-number foo")
    assert "permissionDecision" not in out["hookSpecificOutput"]


def test_bash_non_search_passes():
    assert handle({"tool_name": "Bash", "tool_input": {"command": "ls"}}) is None


def test_off_switch(monkeypatch):
    monkeypatch.setenv("SEARCHSLIM", "off")
    assert handle({"tool_name": "Bash", "tool_input": {"command": "rg foo"}}) is None


def test_grep_mapping():
    argv, _, _, post = grep_to_rg(
        {"pattern": "a.b", "output_mode": "content", "-i": True, "-C": 2, "glob": "*.py", "head_limit": 2, "offset": 1},
        "/r",
    )
    assert argv[:3] == ["rg", "--color=never", "--sort=path"]
    assert "-i" in argv and ["-C", "2"] == argv[argv.index("-C"):argv.index("-C") + 2]
    assert argv[-4:] == ["-e", "a.b", "--", "."]
    assert post("l1\nl2\nl3\nl4") == "l2\nl3"


@needs_rg
def test_small_grep_lets_builtin_tool_run(repo):
    event = {"tool_name": "Grep", "cwd": str(repo), "tool_input": {"pattern": "needle", "output_mode": "content"}}
    assert handle(event) is None


@needs_rg
def test_large_grep_is_answered_with_reduced_output(repo):
    event = {"tool_name": "Grep", "cwd": str(repo), "tool_input": {"pattern": "target", "output_mode": "content", "-C": 1}}
    out = handle(event, Config(max_tokens=800))["hookSpecificOutput"]
    assert out["permissionDecision"] == "deny"
    reason = out["permissionDecisionReason"]
    assert reason.startswith(GREP_REASON_HEADER)
    result = reason[len(GREP_REASON_HEADER):].strip()
    assert estimate_tokens(result) <= 800
    assert result.splitlines()[0].startswith("mod00.py:")
    assert result.splitlines()[-1].startswith(NOTE_PREFIX)


@needs_rg
def test_large_glob_is_reduced_to_absolute_paths(repo):
    event = {"tool_name": "Glob", "cwd": str(repo), "tool_input": {"pattern": "*.py"}}
    reason = handle(event, Config(max_tokens=100))["hookSpecificOutput"]["permissionDecisionReason"]
    paths = [ln for ln in reason.splitlines()[2:] if not ln.startswith(NOTE_PREFIX)]
    assert paths and all(p.startswith(str(repo)) for p in paths)


def test_hook_cli_never_fails_on_bad_input():
    proc = subprocess.run([sys.executable, "-m", "searchslim", "hook"], input="not json", capture_output=True, text=True)
    assert proc.returncode == 0
    assert proc.stdout == ""


@needs_rg
def test_hook_cli_searches_cwd_not_its_own_stdin(repo):
    event = json.dumps({"tool_name": "Grep", "cwd": str(repo), "tool_input": {"pattern": "target", "output_mode": "content"}})
    proc = subprocess.run([sys.executable, "-m", "searchslim", "hook"], input=event, capture_output=True, text=True)
    reason = json.loads(proc.stdout)["hookSpecificOutput"]["permissionDecisionReason"]
    assert "\nmod00.py:1:" in reason


def test_hook_cli_emits_json():
    event = json.dumps({"tool_name": "Bash", "tool_input": {"command": "fd -e py"}})
    proc = subprocess.run([sys.executable, "-m", "searchslim", "hook"], input=event, capture_output=True, text=True)
    assert json.loads(proc.stdout)["hookSpecificOutput"]["updatedInput"]["command"].endswith("run --max-tokens=2000 -- fd -e py")
