"""`searchslim install`: add the hooks to a Claude Code settings file.

PreToolUse (Bash, PowerShell) rewrites search commands, PostToolUse replaces
over-budget Grep/Glob results, compacts Bash/PowerShell test and build output
and, after Edit/Write/MultiEdit/NotebookEdit, forgets that file's shown lines;
PreCompact clears the session's memory of lines already shown (see session.py). PreToolUse keeps Grep|Glob in its matcher so
`SEARCHSLIM_GREP_MODE=deny` works without reinstalling.

Re-running install also rewrites an older searchslim hook command (e.g. one
PowerShell could not parse) to the current form.

Merges into existing settings (other hooks and keys are kept) and is
idempotent. `--user` targets ~/.claude/settings.json, otherwise
<project>/.claude/settings.json.
"""

from __future__ import annotations

import json
import os
import shlex
import sys
from pathlib import Path

MATCHER = "Bash|PowerShell|Grep|Glob"
# PostToolUse also sees file edits, which invalidate the session's shown lines of that file.
POST_MATCHER = MATCHER + "|Edit|Write|MultiEdit|NotebookEdit"
MARKER = "-m searchslim hook"


def hook_command(executable: str | None = None, windows: bool | None = None) -> str:
    """The hook's command line. Absolute interpreter, so the hook works
    whatever python is on PATH.

    On Windows, Claude Code runs hooks with Git Bash, or with PowerShell when
    Git Bash is missing. A quoted path followed by arguments is a parse error
    in PowerShell, so the path is written unquoted with `/` separators, which
    both shells accept. A path with spaces gets its 8.3 short form first.
    """
    executable = executable or sys.executable
    windows = os.name == "nt" if windows is None else windows
    if not windows:
        return f"{shlex.quote(executable)} -m searchslim hook"
    if " " in executable:
        executable = _short_path(executable)
    path = executable.replace("\\", "/")
    if " " in path:
        # Still spaces: PowerShell's call operator (see hook_entry's shell).
        return f"& '{path}' -m searchslim hook"
    return f"{path} -m searchslim hook"


def hook_entry() -> dict:
    command = hook_command()
    entry = {"type": "command", "command": command, "timeout": 30}
    if command.startswith("& "):
        entry["shell"] = "powershell"
    return entry


def _short_path(path: str) -> str:
    try:
        import ctypes

        buf = ctypes.create_unicode_buffer(1024)
        if ctypes.windll.kernel32.GetShortPathNameW(path, buf, len(buf)):
            return buf.value
    except Exception:
        pass
    return path


def settings_path(project: str | None, user: bool) -> Path:
    if user:
        return Path.home() / ".claude" / "settings.json"
    return Path(project or ".").resolve() / ".claude" / "settings.json"


# (event, matcher) pairs the hook is registered for.
EVENTS = (("PreToolUse", MATCHER), ("PostToolUse", POST_MATCHER), ("PreCompact", ""))


def _has_hook(entries: list) -> bool:
    return any(MARKER in h.get("command", "") for entry in entries for h in entry.get("hooks", []))


def install(path: Path) -> bool:
    """Add the hooks; return False if they were all already there."""
    settings = _load(path)
    hooks = settings.setdefault("hooks", {})
    changed = False
    current = hook_entry()
    for event, matcher in EVENTS:
        entries = hooks.setdefault(event, [])
        if _has_hook(entries):
            for entry in entries:
                ours = any(MARKER in h.get("command", "") for h in entry.get("hooks", []))
                if ours and matcher and entry.get("matcher") != matcher and _is_ours_matcher(entry.get("matcher", "")):
                    entry["matcher"] = matcher  # e.g. add PowerShell to an older Bash|Grep|Glob
                    changed = True
                for h in entry.get("hooks", []):
                    if MARKER in h.get("command", "") and _is_installed_form(h) and h != current:
                        h.clear()
                        h.update(current)
                        changed = True
            continue
        entry = {"hooks": [dict(current)]}
        if matcher:
            entry = {"matcher": matcher, **entry}
        entries.append(entry)
        changed = True
    if changed:
        _save(path, settings)
    return changed


def _is_ours_matcher(m: str) -> bool:
    # Only widen matchers an earlier install wrote, never a hand-edited one.
    return m in ("Bash|Grep|Glob", "Grep|Glob", MATCHER)


def _is_installed_form(h: dict) -> bool:
    # Only rewrite commands install wrote (`<python> -m searchslim hook`), not a
    # hand-written one such as this repo's `PYTHONPATH=... python3 -m ...`.
    cmd = h.get("command", "")
    return cmd.endswith(MARKER) and "=" not in cmd.split(MARKER)[0]


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
