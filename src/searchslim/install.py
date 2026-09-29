"""`searchslim install`: add the PreToolUse hook to a Claude Code settings file.

Merges into existing settings (other hooks and keys are kept) and is
idempotent. `--user` targets ~/.claude/settings.json, otherwise
<project>/.claude/settings.json.
"""

from __future__ import annotations

import json
import shlex
import sys
from pathlib import Path

MATCHER = "Bash|Grep|Glob"
MARKER = "-m searchslim hook"


def hook_command() -> str:
    # Absolute interpreter, so the hook works whatever python is on PATH.
    return f"{shlex.quote(sys.executable)} -m searchslim hook"


def settings_path(project: str | None, user: bool) -> Path:
    if user:
        return Path.home() / ".claude" / "settings.json"
    return Path(project or ".").resolve() / ".claude" / "settings.json"


def install(path: Path) -> bool:
    """Add the hook; return False if it was already there."""
    settings = _load(path)
    pre = settings.setdefault("hooks", {}).setdefault("PreToolUse", [])
    if any(MARKER in h.get("command", "") for entry in pre for h in entry.get("hooks", [])):
        return False
    pre.append({"matcher": MATCHER, "hooks": [{"type": "command", "command": hook_command(), "timeout": 30}]})
    _save(path, settings)
    return True


def uninstall(path: Path) -> bool:
    """Remove the hook; return False if it was not there."""
    settings = _load(path)
    pre = settings.get("hooks", {}).get("PreToolUse", [])
    kept, removed = [], False
    for entry in pre:
        hooks = [h for h in entry.get("hooks", []) if MARKER not in h.get("command", "")]
        removed |= len(hooks) != len(entry.get("hooks", []))
        if hooks:
            kept.append({**entry, "hooks": hooks})
    if not removed:
        return False
    settings["hooks"]["PreToolUse"] = kept
    if not kept:
        del settings["hooks"]["PreToolUse"]
    if not settings["hooks"]:
        del settings["hooks"]
    _save(path, settings)
    return True


def _load(path: Path) -> dict:
    if not path.exists():
        return {}
    text = path.read_text(encoding="utf-8").strip()
    return json.loads(text) if text else {}


def _save(path: Path, settings: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
