import json
import subprocess
import sys

from searchslim.install import MARKER, POST_MATCHER, hook_command, install, uninstall


def test_install_merges_and_is_idempotent(tmp_path):
    path = tmp_path / ".claude" / "settings.json"
    path.parent.mkdir()
    other = {"matcher": "Edit", "hooks": [{"type": "command", "command": "echo hi"}]}
    path.write_text(json.dumps({"model": "x", "hooks": {"PreToolUse": [other]}}))
    assert install(path) is True
    assert install(path) is False
    data = json.loads(path.read_text())
    assert data["model"] == "x"
    pre = data["hooks"]["PreToolUse"]
    assert pre[0] == other
    assert pre[1]["matcher"] == "Bash|PowerShell|Grep|Glob" and MARKER in pre[1]["hooks"][0]["command"]


def test_uninstall_keeps_other_hooks(tmp_path):
    path = tmp_path / "settings.json"
    install(path)
    assert uninstall(path) is True
    assert uninstall(path) is False
    assert json.loads(path.read_text()) == {}


def test_install_cli_on_a_project(tmp_path):
    out = subprocess.run([sys.executable, "-m", "searchslim", "install", str(tmp_path)], capture_output=True, text=True)
    assert out.returncode == 0 and "added to" in out.stdout
    assert (tmp_path / ".claude" / "settings.json").exists()


def test_install_registers_post_tool_use_for_grep_and_glob(tmp_path):
    path = tmp_path / "settings.json"
    install(path)
    post = json.loads(path.read_text())["hooks"]["PostToolUse"]
    assert post[0]["matcher"] == POST_MATCHER and MARKER in post[0]["hooks"][0]["command"]
    assert "Edit|Write" in POST_MATCHER


def test_windows_command_parses_in_powershell_and_bash():
    # PowerShell rejects `'C:\\x\\python.exe' -m ...` (quoted path, then arguments).
    assert hook_command(r"C:\Python314\python.exe", windows=True) == "C:/Python314/python.exe -m searchslim hook"
    spaced = hook_command(r"C:\Program Files\Python\python.exe", windows=True)
    assert spaced == "& 'C:/Program Files/Python/python.exe' -m searchslim hook"
    assert hook_command("/usr/bin/python3", windows=False) == "/usr/bin/python3 -m searchslim hook"


def test_reinstall_fixes_an_old_windows_command(tmp_path, monkeypatch):
    import searchslim.install as inst

    path = tmp_path / "settings.json"
    old = "'C:\\Python314\\python.exe' -m searchslim hook"
    path.write_text(json.dumps({"hooks": {"PreToolUse": [{"matcher": "Bash|Grep|Glob", "hooks": [{"type": "command", "command": old, "timeout": 30}]}]}}))
    monkeypatch.setattr(inst, "hook_command", lambda: "C:/Python314/python.exe -m searchslim hook")
    assert install(path) is True
    hooks = json.loads(path.read_text())["hooks"]
    commands = [h["command"] for ev in hooks.values() for e in ev for h in e["hooks"]]
    assert commands == ["C:/Python314/python.exe -m searchslim hook"] * 3
    assert install(path) is False


def test_reinstall_keeps_hand_written_commands(tmp_path):
    path = tmp_path / "settings.json"
    mine = 'PYTHONPATH="$CLAUDE_PROJECT_DIR/src" python3 -m searchslim hook'
    path.write_text(json.dumps({"hooks": {"PreToolUse": [{"matcher": "Bash|Grep|Glob", "hooks": [{"type": "command", "command": mine}]}]}}))
    install(path)
    assert json.loads(path.read_text())["hooks"]["PreToolUse"][0]["hooks"][0]["command"] == mine


def test_reinstall_widens_an_old_post_tool_use_matcher(tmp_path):
    path = tmp_path / "settings.json"
    install(path)
    settings = json.loads(path.read_text())
    settings["hooks"]["PostToolUse"][0]["matcher"] = "Grep|Glob"  # written by 0.4 and earlier
    path.write_text(json.dumps(settings))
    assert install(path) is True
    assert json.loads(path.read_text())["hooks"]["PostToolUse"][0]["matcher"] == POST_MATCHER


def test_reinstall_adds_edit_tools_to_a_0_6_post_tool_use_matcher(tmp_path):
    path = tmp_path / "settings.json"
    install(path)
    settings = json.loads(path.read_text())
    settings["hooks"]["PostToolUse"][0]["matcher"] = "Bash|PowerShell|Grep|Glob"  # 0.5 - 0.6
    path.write_text(json.dumps(settings))
    assert install(path) is True
    hooks = json.loads(path.read_text())["hooks"]
    assert hooks["PostToolUse"][0]["matcher"] == POST_MATCHER
    assert hooks["PreToolUse"][0]["matcher"] == "Bash|PowerShell|Grep|Glob"
