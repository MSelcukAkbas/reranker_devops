import json
import shutil
import subprocess
import sys

import pytest

from searchslim import Kind, parse
from searchslim.hooks import GREP_REASON_HEADER, grep_to_rg, handle, shape_output
from searchslim.rules import Config, NOTE_PREFIX, estimate_tokens

needs_rg = pytest.mark.skipif(shutil.which("rg") is None, reason="rg not installed")


@pytest.fixture
def repo(tmp_path):
    for i in range(40):
        body = "\n".join(f"    value_{n} = compute(target, {n})" if n % 3 == 0 else f"    other_{n} = 0" for n in range(90))
        (tmp_path / f"mod{i:02d}.py").write_text(body + "\n")
    (tmp_path / "small.txt").write_text("one needle here\n")
    return tmp_path


@needs_rg
def test_bash_search_is_rewritten_keeping_other_fields():
    out = handle({"tool_name": "Bash", "tool_input": {"command": "rg foo", "description": "search"}})
    upd = out["hookSpecificOutput"]["updatedInput"]
    assert upd["description"] == "search"
    assert upd["command"].endswith("-m searchslim run --max-tokens=7000 --trigger-tokens=1500 --rerank=lexical -- rg foo")
    assert "permissionDecision" not in out["hookSpecificOutput"]


def test_bash_non_search_passes():
    assert handle({"tool_name": "Bash", "tool_input": {"command": "ls"}}) is None


def test_off_switch(monkeypatch):
    monkeypatch.setenv("SEARCHSLIM", "off")
    assert handle({"tool_name": "Bash", "tool_input": {"command": "rg foo"}}) is None


def test_grep_mapping():
    argv, _, _, post, _ = grep_to_rg(
        {"pattern": "a.b", "output_mode": "content", "-i": True, "-C": 2, "glob": "*.py", "head_limit": 2, "offset": 1},
        "/r",
    )
    assert argv[:3] == ["rg", "--color=never", "--sort=path"]
    assert "-i" in argv and ["-C", "2"] == argv[argv.index("-C"):argv.index("-C") + 2]
    assert argv[-4:] == ["-e", "a.b", "--", "."]
    assert post("l1\nl2\nl3\nl4") == "l2\nl3"


@needs_rg
def test_small_grep_lets_builtin_tool_run(repo):
    event = {"hook_event_name": "PostToolUse", "tool_name": "Grep", "cwd": str(repo), "tool_input": {"pattern": "needle", "output_mode": "content"}}
    assert handle(event) is None


def post_text(result) -> str:
    """The reduced text inside a PostToolUse updatedToolOutput object."""
    out = result["hookSpecificOutput"]["updatedToolOutput"]
    if out.get("mode") in ("content", "count"):
        return out["content"]
    return "\n".join(out["filenames"])


@needs_rg
def test_large_grep_is_answered_with_reduced_output(repo):
    event = {"hook_event_name": "PostToolUse", "tool_name": "Grep", "cwd": str(repo), "tool_input": {"pattern": "target", "output_mode": "content", "-C": 1}}
    out = handle(event, Config(max_tokens=800))["hookSpecificOutput"]
    assert out["hookEventName"] == "PostToolUse" and "permissionDecision" not in out
    assert out["updatedToolOutput"]["mode"] == "content"
    result = out["updatedToolOutput"]["content"]
    assert out["updatedToolOutput"]["numLines"] == len(result.splitlines())
    assert estimate_tokens(result) <= 800
    assert result.splitlines()[0].startswith("mod00.py:")
    assert result.splitlines()[-1].startswith(NOTE_PREFIX)


@needs_rg
def test_large_glob_is_reduced_to_absolute_paths(repo):
    event = {"hook_event_name": "PostToolUse", "tool_name": "Glob", "cwd": str(repo), "tool_input": {"pattern": "*.py"}}
    reason = post_text(handle(event, Config(max_tokens=100)))
    paths = [ln for ln in reason.splitlines() if not ln.startswith(NOTE_PREFIX)]
    assert paths and all(p.startswith(str(repo)) for p in paths)


def test_hook_cli_never_fails_on_bad_input():
    proc = subprocess.run([sys.executable, "-m", "searchslim", "hook"], input="not json", capture_output=True, text=True)
    assert proc.returncode == 0
    assert proc.stdout == ""


@needs_rg
def test_hook_cli_searches_cwd_not_its_own_stdin(repo):
    event = json.dumps({"hook_event_name": "PostToolUse", "tool_name": "Grep", "cwd": str(repo), "tool_input": {"pattern": "target", "output_mode": "content"}})
    proc = subprocess.run([sys.executable, "-m", "searchslim", "hook"], input=event, capture_output=True, text=True)
    reason = post_text(json.loads(proc.stdout))
    # Every match is kept (lossless view); the 30 lines repeated in all 40 files are written once each.
    parsed = parse(reason, kind=Kind.CONTENT)
    assert len({(ln.path, ln.number) for ln in parsed.lines}) == 40 * 30
    assert any(ln.path == "mod00.py" and ln.number == 1 for ln in parsed.lines)
    assert len(reason) < len("".join(f"mod{i:02d}.py:{n + 1}:    value_{n} = compute(target, {n})\n" for i in range(40) for n in range(0, 90, 3))) / 2


def test_hook_cli_emits_json():
    event = json.dumps({"tool_name": "Bash", "tool_input": {"command": "find . -name '*.py'"}})
    proc = subprocess.run([sys.executable, "-m", "searchslim", "hook"], input=event, capture_output=True, text=True)
    assert json.loads(proc.stdout)["hookSpecificOutput"]["updatedInput"]["command"].endswith("run --max-tokens=7000 --trigger-tokens=1500 --rerank=lexical -- find . -name '*.py'")


@needs_rg
def test_rerank_can_be_turned_off(monkeypatch):
    monkeypatch.setenv("SEARCHSLIM_RERANK", "off")
    out = handle({"tool_name": "Bash", "tool_input": {"command": "rg foo"}})
    assert "--rerank" not in out["hookSpecificOutput"]["updatedInput"]["command"]


@needs_rg
def test_large_grep_is_ranked_by_default(repo):
    event = {"hook_event_name": "PostToolUse", "tool_name": "Grep", "cwd": str(repo), "tool_input": {"pattern": "target", "output_mode": "content"}}
    reason = post_text(handle(event, Config(max_tokens=800)))
    assert "ranked by relevance (lexical)" in reason


def test_bash_search_with_a_missing_tool_is_left_alone(monkeypatch):
    # The agent's shell may know the tool only as an alias (Claude Code's bundled rg).
    monkeypatch.setenv("PATH", "/nonexistent")
    assert handle({"tool_name": "Bash", "tool_input": {"command": "rg foo"}}) is None


@needs_rg
def test_single_file_grep_under_budget_lets_builtin_tool_run(tmp_path):
    # With a path on every line this file's matches would exceed the budget.
    name = "a_rather_long_directory_name/another_nested_directory/module.py"
    (tmp_path / name).parent.mkdir(parents=True)
    (tmp_path / name).write_text("\n".join(f"def f_{i}(): pass" for i in range(150)) + "\n")
    event = {"hook_event_name": "PostToolUse", "tool_name": "Grep", "cwd": str(tmp_path), "tool_input": {"pattern": "def ", "path": name, "output_mode": "content"}}
    assert handle(event, Config(max_tokens=1000)) is None


@needs_rg
def test_single_file_grep_over_budget_names_the_file(tmp_path):
    (tmp_path / "big.py").write_text("\n".join(f"def function_{i}(value): return value * {i}" for i in range(300)) + "\n")
    event = {"hook_event_name": "PostToolUse", "tool_name": "Grep", "cwd": str(tmp_path), "tool_input": {"pattern": "def ", "path": str(tmp_path / "big.py"), "output_mode": "content"}}
    reason = post_text(handle(event, Config(max_tokens=600)))
    body = reason.splitlines()
    assert str(tmp_path / "big.py") in body[-1]  # the note names the file
    assert body[0][0].isdigit()  # pathless N:text, as rg prints one file
    assert body[-1].startswith(NOTE_PREFIX)


@needs_rg
@pytest.mark.parametrize("mode", ["files_with_matches", "count"])
def test_grep_list_modes_are_reduced(repo, mode):
    for i in range(200):
        (repo / f"extra_module_with_long_name_{i:03d}.py").write_text("target\n")
    event = {"hook_event_name": "PostToolUse", "tool_name": "Grep", "cwd": str(repo), "tool_input": {"pattern": "target", "output_mode": mode}}
    reason = post_text(handle(event, Config(max_tokens=300)))
    assert reason.splitlines()[-1].startswith(NOTE_PREFIX)


@needs_rg
def test_glob_output_keeps_globs_shape(repo):
    event = {"hook_event_name": "PostToolUse", "tool_name": "Glob", "cwd": str(repo), "tool_input": {"pattern": "*.py"}}
    out = handle(event, Config(max_tokens=100))["hookSpecificOutput"]["updatedToolOutput"]
    assert set(out) >= {"filenames", "numFiles", "truncated", "durationMs"}
    assert out["truncated"] is True and out["filenames"][-1].startswith(NOTE_PREFIX)


@needs_rg
def test_single_file_grep_keeps_pathless_lines(repo):
    event = {"hook_event_name": "PostToolUse", "tool_name": "Grep", "cwd": str(repo), "tool_input": {"pattern": "target", "output_mode": "content", "path": "mod00.py"}}
    # 30 matches, ~35 chars each: fits 800 tokens without filenames, not with them.
    assert handle(event, Config(max_tokens=800)) is None


@needs_rg
def test_single_file_bash_search_keeps_pathless_lines(tmp_path):
    (tmp_path / "f.rs").write_text("fn a() {}\n")
    (tmp_path / "d").mkdir()
    (tmp_path / "d" / "g.rs").write_text("fn b() {}\n")

    def run(command):
        cmd = handle({"tool_name": "Bash", "cwd": str(tmp_path), "tool_input": {"command": command}})["hookSpecificOutput"]["updatedInput"]["command"]
        return subprocess.run(cmd, shell=True, cwd=tmp_path, capture_output=True, text=True).stdout

    # Anchor flags are chosen at run time: one file stays pathless, a directory gets paths.
    assert run("rg -n 'fn ' f.rs") == "1:fn a() {}\n"
    assert run("rg 'fn ' d") == "d/g.rs:1:fn b() {}\n"


@needs_rg
def test_pre_tool_use_leaves_grep_alone_so_it_is_not_a_hook_error(repo):
    # A denied call reaches the model as a "hook error"; reduce after the tool runs instead.
    event = {"hook_event_name": "PreToolUse", "tool_name": "Grep", "cwd": str(repo), "tool_input": {"pattern": "target", "output_mode": "content"}}
    assert handle(event, Config(max_tokens=800)) is None


@needs_rg
def test_deny_mode_is_still_available(repo, monkeypatch):
    monkeypatch.setenv("SEARCHSLIM_GREP_MODE", "deny")
    event = {"hook_event_name": "PreToolUse", "tool_name": "Grep", "cwd": str(repo), "tool_input": {"pattern": "target", "output_mode": "content"}}
    out = handle(event, Config(max_tokens=800))["hookSpecificOutput"]
    assert out["permissionDecision"] == "deny"
    assert "not an error; do not retry" in out["permissionDecisionReason"]


def test_post_tool_use_ignores_bash():
    assert handle({"hook_event_name": "PostToolUse", "tool_name": "Bash", "tool_input": {"command": "rg foo"}}) is None


GREP_RESPONSE = {"mode": "content", "numFiles": 40, "filenames": [], "content": "", "numLines": 0, "totalLines": 0}


def test_post_tool_use_reduces_the_tools_own_result(tmp_path):
    # Claude Code rejects a string (it validates against Grep's output schema), so the
    # tool_response object comes back with its fields replaced; no second search runs.
    content = "\n".join(f"src/m{i:02d}.js:{n}:const target_{n} = require('x')" for i in range(40) for n in range(1, 30))
    response = {**GREP_RESPONSE, "content": content, "numLines": len(content.splitlines()), "totalLines": 999}
    event = {"hook_event_name": "PostToolUse", "tool_name": "Grep", "cwd": str(tmp_path),
             "tool_input": {"pattern": "target", "output_mode": "content"}, "tool_response": response}
    out = handle(event, Config(max_tokens=500))["hookSpecificOutput"]["updatedToolOutput"]
    assert set(out) == set(response) and out["totalLines"] == 999 and out["numFiles"] == 40
    assert out["numLines"] == len(out["content"].splitlines()) < response["numLines"]
    raw = set(content.splitlines())
    assert all(ln in raw for ln in out["content"].splitlines()[:-1])
    assert out["content"].splitlines()[-1].startswith(NOTE_PREFIX)


def test_post_tool_use_small_result_is_left_alone(tmp_path):
    response = {**GREP_RESPONSE, "content": "a.js:1:target", "numLines": 1}
    event = {"hook_event_name": "PostToolUse", "tool_name": "Grep", "cwd": str(tmp_path),
             "tool_input": {"pattern": "target", "output_mode": "content"}, "tool_response": response}
    assert handle(event) is None


def test_post_tool_use_files_mode_and_glob_keep_their_shapes(tmp_path):
    names = [f"services/svc{k}/src/file_with_a_long_name_{i:03d}.js" for k in range(4) for i in range(80)]
    grep = {"mode": "files_with_matches", "numFiles": len(names), "filenames": names}
    event = {"hook_event_name": "PostToolUse", "tool_name": "Grep", "cwd": str(tmp_path),
             "tool_input": {"pattern": "x"}, "tool_response": grep}
    out = handle(event, Config(max_tokens=300))["hookSpecificOutput"]["updatedToolOutput"]
    assert out["mode"] == "files_with_matches" and out["numFiles"] == len(names)
    assert out["filenames"][:-1] == names[: len(out["filenames"]) - 1]
    assert out["filenames"][-1].startswith(NOTE_PREFIX) and "services/svc" in out["filenames"][-1]

    glob = {"filenames": [f"/abs/{n}" for n in names], "durationMs": 12, "numFiles": len(names), "truncated": False}
    event = {"hook_event_name": "PostToolUse", "tool_name": "Glob", "cwd": str(tmp_path),
             "tool_input": {"pattern": "**/*.js"}, "tool_response": glob}
    out = handle(event, Config(max_tokens=300))["hookSpecificOutput"]["updatedToolOutput"]
    assert set(out) == set(glob) and out["durationMs"] == 12 and out["truncated"] is True
    assert all(f.startswith("/abs/") for f in out["filenames"][:-1])


def test_shape_output_count_mode():
    out = shape_output("Grep", "count", {"mode": "count", "numFiles": 3, "filenames": [], "numMatches": 9}, "a:1\nb:2")
    assert out == {"mode": "count", "numFiles": 3, "filenames": [], "numMatches": 9, "content": "a:1\nb:2", "numLines": 2}


def test_post_tool_use_defaults_to_coverage_view(tmp_path, monkeypatch):
    monkeypatch.setenv("SEARCHSLIM_MAX_TOKENS", "500")
    monkeypatch.setenv("SEARCHSLIM_TRIGGER_TOKENS", "0")
    monkeypatch.delenv("SEARCHSLIM_VIEW", raising=False)
    content = "\n".join(f"src/m{i:02d}.js:{n}:const target_{n} = require('x')" for i in range(40) for n in range(1, 30))
    response = {**GREP_RESPONSE, "content": content, "numLines": len(content.splitlines())}
    event = {"hook_event_name": "PostToolUse", "tool_name": "Grep", "cwd": str(tmp_path),
             "tool_input": {"pattern": "target", "output_mode": "content"}, "tool_response": response}
    out = handle(event)["hookSpecificOutput"]["updatedToolOutput"]
    lines = out["content"].splitlines()
    assert lines[0].startswith(NOTE_PREFIX) and "40/40 matching files indexed" in lines[0]
    monkeypatch.setenv("SEARCHSLIM_VIEW", "notes")
    out = handle(event)["hookSpecificOutput"]["updatedToolOutput"]
    assert out["content"].splitlines()[-1].startswith(NOTE_PREFIX)
