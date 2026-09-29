import json
import subprocess
import sys

from searchslim.install import MARKER, install, uninstall


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
    assert pre[1]["matcher"] == "Bash|Grep|Glob" and MARKER in pre[1]["hooks"][0]["command"]


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
