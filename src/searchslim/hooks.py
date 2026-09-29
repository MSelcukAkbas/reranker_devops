"""Claude Code PreToolUse hook: route search output through searchslim.

  Bash       Plain rg/grep/fd/find commands are rewritten to
             `python -m searchslim run -- <cmd>` via `updatedInput`, so the
             command still runs as the Bash tool call, only its stdout is reduced.

  Grep/Glob  Built-in tool results cannot be rewritten by a hook, so the hook
             runs the equivalent `rg` itself. If the result already fits the
             budget it does nothing and the built-in tool runs normally. If not,
             it answers with `permissionDecision: deny` and puts the reduced
             result in the reason, which Claude Code hands to the model.

Every failure path (bad input, missing rg, timeout) returns no output so the
original tool call runs unchanged. `SEARCHSLIM=off` in the environment
disables the hook; `SEARCHSLIM_MAX_TOKENS` sets the budget.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from . import slim
from .models import Kind
from .rewrite import rewrite_command
from .rules import Config, estimate_tokens

RG_TIMEOUT_S = 20
GREP_REASON_HEADER = (
    "searchslim ran this search and reduced the output. This is the search "
    "result, not an error; do not retry the same call. Lines keep path:line "
    "anchors; the trailing [searchslim] note lists what was left out."
)


def config_from_env() -> Config:
    config = Config()
    value = os.environ.get("SEARCHSLIM_MAX_TOKENS", "")
    if value.isdigit():
        config.max_tokens = int(value)
    return config


def handle(event: dict, config: Config | None = None) -> dict | None:
    """Return the hook's JSON output, or None to let the tool run unchanged."""
    if os.environ.get("SEARCHSLIM", "").lower() == "off":
        return None
    config = config or config_from_env()
    tool = event.get("tool_name")
    tool_input = event.get("tool_input") or {}
    cwd = event.get("cwd") or os.getcwd()

    if tool == "Bash":
        new_command = rewrite_command(tool_input.get("command", ""))
        if not new_command:
            return None
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "updatedInput": {**tool_input, "command": new_command},
            }
        }

    if tool == "Grep":
        built = grep_to_rg(tool_input, cwd)
    elif tool == "Glob":
        built = glob_to_rg(tool_input, cwd)
    else:
        return None
    if built is None:
        return None
    argv, run_cwd, kind, postprocess = built

    raw = _run(argv, run_cwd)
    if raw is None:
        return None
    raw = postprocess(raw)
    if estimate_tokens(raw) <= config.max_tokens:
        return None  # small enough: let the real tool answer
    reduced = slim(raw, kind=kind, config=config)
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": f"{GREP_REASON_HEADER}\n\n{reduced.text}",
        }
    }


def grep_to_rg(tool_input: dict, cwd: str):
    """Build the rg call equivalent to a Claude Code Grep tool call."""
    pattern = tool_input.get("pattern")
    if not pattern:
        return None
    mode = tool_input.get("output_mode") or "files_with_matches"
    # Sorted output keeps results (and benchmarks) reproducible; rg is otherwise parallel.
    argv = ["rg", "--color=never", "--sort=path"]
    if tool_input.get("-i"):
        argv.append("-i")
    if tool_input.get("multiline"):
        argv += ["-U", "--multiline-dotall"]
    if tool_input.get("glob"):
        argv += ["--glob", tool_input["glob"]]
    if tool_input.get("type"):
        argv += ["--type", tool_input["type"]]

    if mode == "content":
        kind = Kind.CONTENT
        argv += ["--with-filename", "--no-heading"]
        # Line numbers are the evidence anchor; always ask for them.
        argv.append("--line-number")
        context = tool_input.get("context", tool_input.get("-C"))
        if context is not None:
            argv += ["-C", str(int(context))]
        for flag in ("-A", "-B"):
            if tool_input.get(flag) is not None:
                argv += [flag, str(int(tool_input[flag]))]
    elif mode == "count":
        kind = Kind.COUNT
        argv += ["--count", "--with-filename"]
    else:
        kind = Kind.PATHS
        argv.append("--files-with-matches")

    # Always name the path: with none, rg searches stdin when it is a pipe,
    # and a hook's stdin is the event JSON.
    path = tool_input.get("path")
    argv += ["-e", pattern, "--", path or "."]
    offset = int(tool_input.get("offset") or 0)
    limit = tool_input.get("head_limit")
    limit = int(limit) if limit else None

    def postprocess(raw: str) -> str:
        lines = raw.splitlines()
        if not path:
            lines = [ln[2:] if ln.startswith("./") else ln for ln in lines]
        lines = lines[offset:]
        if limit:
            lines = lines[:limit]
        return "\n".join(lines)

    return argv, cwd, kind, postprocess


def glob_to_rg(tool_input: dict, cwd: str):
    """Build an rg --files call listing what a Claude Code Glob call would match."""
    pattern = tool_input.get("pattern")
    if not pattern:
        return None
    base = Path(tool_input.get("path") or cwd)
    if not base.is_absolute():
        base = Path(cwd) / base
    argv = ["rg", "--files", "--sort=path", "--glob", pattern]

    def postprocess(raw: str) -> str:
        # Glob returns absolute paths.
        return "\n".join(str(base / p) for p in raw.splitlines() if p)

    return argv, str(base), Kind.PATHS, postprocess


def _run(argv: list[str], cwd: str) -> str | None:
    if shutil.which(argv[0]) is None:
        return None
    try:
        proc = subprocess.run(
            argv, cwd=cwd, stdin=subprocess.DEVNULL, capture_output=True, text=True, errors="replace", timeout=RG_TIMEOUT_S
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    # rg: 0 = matches, 1 = no matches, 2 = error.
    if proc.returncode not in (0, 1):
        return None
    return proc.stdout


def main() -> int:
    try:
        event = json.load(sys.stdin)
        out = handle(event)
    except Exception:  # never block the agent because of this hook
        return 0
    if out:
        json.dump(out, sys.stdout)
    return 0
