"""`searchslim install`: add the hooks to a Claude Code settings file.

PreToolUse (Bash|Grep|Glob) reduces search output; PreCompact clears the
session's memory of lines already shown (see session.py).

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


# (event, matcher) pairs the hook is registered for.
EVENTS = (("PreToolUse", MATCHER), ("PreCompact", ""))


def _has_hook(entries: list) -> bool:
    return any(MARKER in h.get("command", "") for entry in entries for h in entry.get("hooks", []))


def install(path: Path) -> bool:
    """Add the hooks; return False if they were all already there."""
    settings = _load(path)
    hooks = settings.setdefault("hooks", {})
    changed = False
    for event, matcher in EVENTS:
        entries = hooks.setdefault(event, [])
        if _has_hook(entries):
            continue
        entry = {"hooks": [{"type": "command", "command": hook_command(), "timeout": 30}]}
        if matcher:
            entry = {"matcher": matcher, **entry}
        entries.append(entry)
        changed = True
    if changed:
        _save(path, settings)
    return changed


def uninstall(path: Path) -> bool:
    """Remove the hooks; return False if none were there."""
    settings = _load(path)
    removed = False
    for event, _ in EVENTS:
        entries = settings.get("hooks", {}).get(event, [])
        kept = []
        for entry in entries:
            hooks = [h for h in entry.get("hooks", []) if MARKER not in h.get("command", "")]
            removed |= len(hooks) != len(entry.get("hooks", []))
            if hooks:
                kept.append({**entry, "hooks": hooks})
        if event in settings.get("hooks", {}):
            settings["hooks"][event] = kept
            if not kept:
                del settings["hooks"][event]
    if not removed:
        return False
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
