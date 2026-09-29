"""Claude Code PreToolUse hook: route search output through searchslim.

  PowerShell `rg`, `Get-ChildItem -Recurse` and `Select-String` commands are
             wrapped the same way (see rewrite.rewrite_powershell); on Windows
             agents often search with the PowerShell tool instead of Grep.

  Bash       Plain rg/grep/fd/find commands are rewritten to
             `python -m searchslim run -- <cmd>` via `updatedInput`, so the
             command still runs as the Bash tool call, only its stdout is reduced.

  Grep/Glob  On PostToolUse the hook reduces the tool's own result
             (`tool_response`) when it is over budget and returns it as
             `updatedToolOutput` in the same object shape (see shape_output).
             Under budget it does nothing. Without a tool_response it runs the
             equivalent `rg` itself.
             PreToolUse does nothing for them by default: a denied call reaches
             the model as a "hook error", which made agents search again.
             `SEARCHSLIM_GREP_MODE=deny` restores the old PreToolUse answer
             (deny with the reduced result as the reason) for Claude Code
             versions without `updatedToolOutput`.

Every failure path (bad input, missing rg, timeout) returns no output so the
original tool call runs unchanged. `SEARCHSLIM=off` in the environment
disables the hook; `SEARCHSLIM_MAX_TOKENS` sets the budget and
`SEARCHSLIM_TRIGGER_TOKENS` (default 6000) the size above which output is reduced.
`SEARCHSLIM_RERANK=lexical|claude|off` picks the ranking (default lexical), with
the intent taken from the session transcript.

Searches in one session share a memory of lines already shown (session.py), so
an over-budget search does not re-send them. On `PreCompact` that memory is
cleared, since compaction drops the earlier results from the agent's context.
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
from .rewrite import rewrite_command, rewrite_powershell
from .rules import DEFAULT_TRIGGER_TOKENS, NOTE_PREFIX, Config, estimate_tokens
from .session import SessionStore, enabled as session_enabled

RG_TIMEOUT_S = 20
# Lexical ranking kept more critical evidence than rules alone at every budget
# on the benchmark set (e.g. 20/22 at 1200 tokens vs 16/22 at 2000), so it is on
# by default. SEARCHSLIM_RERANK=off gives the plain rules mode.
DEFAULT_RERANK = "lexical"
GREP_REASON_HEADER = (
    "searchslim reduced this search output. Lines keep path:line anchors; the "
    "trailing [searchslim] note lists what was left out."
)
GLOB_REASON_HEADER = (
    "searchslim reduced this file list. The trailing [searchslim] note counts "
    "the paths left out, by directory."
)
# Only in deny mode, where the result arrives as a blocked call.
DENY_NOTE = " This is the search result, not an error; do not retry the same call."


def config_from_env() -> Config:
    config = Config(trigger_tokens=DEFAULT_TRIGGER_TOKENS)
    value = os.environ.get("SEARCHSLIM_MAX_TOKENS", "")
    if value.isdigit():
        config.max_tokens = int(value)
    value = os.environ.get("SEARCHSLIM_TRIGGER_TOKENS", "")
    if value.isdigit():
        config.trigger_tokens = int(value)
    return config


def handle(event: dict, config: Config | None = None) -> dict | None:
    """Return the hook's JSON output, or None to let the tool run unchanged."""
    if os.environ.get("SEARCHSLIM", "").lower() == "off":
        return None
    # Subagents have their own context: tool calls made inside one carry
    # `agent_id`, and must not count as lines the main agent has seen.
    session_id = event.get("session_id") or ""
    if session_id and event.get("agent_id"):
        session_id = f"{session_id}.{event['agent_id']}"
    if event.get("hook_event_name") == "PreCompact":
        if session_id:
            SessionStore(session_id).clear()
        return None
    config = config or config_from_env()
    tool = event.get("tool_name")
    tool_input = event.get("tool_input") or {}
    cwd = event.get("cwd") or os.getcwd()

    rerank = os.environ.get("SEARCHSLIM_RERANK", DEFAULT_RERANK).lower()
    rerank = rerank if rerank in ("lexical", "claude") else ""
    transcript = event.get("transcript_path") or ""
    use_session = bool(session_id) and session_enabled()

    event_name = event.get("hook_event_name") or "PreToolUse"
    grep_mode = os.environ.get("SEARCHSLIM_GREP_MODE", "post").lower()

    if tool in ("Bash", "PowerShell") and event_name == "PreToolUse":
        run_args = [f"--max-tokens={config.max_tokens}", f"--trigger-tokens={config.trigger_tokens}"]
        if rerank:
            run_args.append(f"--rerank={rerank}")
            if transcript:
                run_args.append(f"--transcript={transcript}")
        if use_session:
            run_args.append(f"--session={session_id}")
        if tool == "PowerShell":
            new_command = rewrite_powershell(tool_input.get("command", ""), run_args=run_args, check_path=True)
        else:
            new_command = rewrite_command(tool_input.get("command", ""), run_args=run_args, check_path=True)
        if not new_command:
            return None
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "updatedInput": {**tool_input, "command": new_command},
            }
        }

    if event_name == "PreToolUse" and grep_mode != "deny":
        return None  # reduced after the tool runs (PostToolUse)
    if event_name not in ("PreToolUse", "PostToolUse"):
        return None
    if tool == "Grep":
        built = grep_to_rg(tool_input, cwd)
    elif tool == "Glob":
        built = glob_to_rg(tool_input, cwd)
    else:
        return None
    if built is None:
        return None
    argv, run_cwd, kind, postprocess, default_path = built

    response = event.get("tool_response") if event_name == "PostToolUse" else None
    raw = _response_text(tool, response) if isinstance(response, dict) else None
    if raw is None:
        # No usable tool result (deny mode, or an older Claude Code): search ourselves.
        raw = _run(argv, run_cwd)
        if raw is None:
            return None
        raw = postprocess(raw)
    else:
        run_cwd = cwd
    if estimate_tokens(raw) <= max(config.max_tokens, config.trigger_tokens):
        return None  # small enough: let the real tool answer
    scorer = query = None
    if rerank:
        from .rerank import Query, make_scorer, query_from_transcript

        scorer = make_scorer(rerank)
        pattern = tool_input.get("pattern", "")
        query = query_from_transcript(transcript, pattern) if transcript else Query(pattern=pattern)
    session = SessionStore(session_id) if use_session else None
    reduced = slim(
        raw, kind=kind, config=config, scorer=scorer, query=query, default_path=default_path,
        session=session, cwd=run_cwd,
    )
    header = GLOB_REASON_HEADER if tool == "Glob" else GREP_REASON_HEADER
    if default_path:
        header += f" All lines are from {default_path}."
    if event_name == "PostToolUse":
        # Claude Code validates updatedToolOutput against the tool's own output
        # schema and silently keeps the original on a mismatch, so return the
        # tool's result object with its text fields replaced.
        mode = "files" if tool == "Glob" else tool_input.get("output_mode") or "files_with_matches"
        base = response if isinstance(response, dict) else {}
        return {
            "hookSpecificOutput": {
                "hookEventName": "PostToolUse",
                "updatedToolOutput": shape_output(tool, mode, base, reduced.text),
            }
        }
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": f"{header}{DENY_NOTE}\n\n{reduced.text}",
        }
    }


def _response_text(tool: str, response: dict) -> str | None:
    """The tool's own result as raw text, or None if its shape is unknown."""
    if tool == "Grep" and isinstance(response.get("content"), str) and response.get("mode") != "files_with_matches":
        return response["content"]
    names = response.get("filenames")
    if isinstance(names, list) and all(isinstance(n, str) for n in names):
        return "\n".join(names)
    return None


def shape_output(tool: str, mode: str, response: dict, text: str) -> dict:
    """The reduced result in the tool's output shape.

    Grep: {mode, numFiles, filenames, content, numLines, ...}; Glob:
    {filenames, numFiles, truncated, durationMs}. Fields not set here keep the
    tool's own values. In list modes the [searchslim] note becomes the last
    entry, so the model still sees what was left out; numFiles stays the
    tool's total, which the note explains.
    """
    out = dict(response)
    lines = text.splitlines()
    if tool == "Grep" and mode in ("content", "count"):
        out["mode"] = mode
        out["content"] = text
        out["numLines"] = len(lines)
        out.setdefault("numFiles", 0)
        out.setdefault("filenames", [])
        return out
    body = [ln for ln in lines if not ln.startswith(NOTE_PREFIX)]
    notes = [ln for ln in lines if ln.startswith(NOTE_PREFIX)]
    out["filenames"] = body + notes
    out.setdefault("numFiles", len(body))
    if tool == "Glob":
        out["truncated"] = True
        out.setdefault("durationMs", 0)
    else:
        out["mode"] = "files_with_matches"
    return out


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

    path = tool_input.get("path")
    # A single-file search prints no filename (as rg does for one file), which
    # keeps the output, and so the budget check, the same as the real tool's.
    single = ""
    if path:
        full = path if os.path.isabs(path) else os.path.join(cwd, path)
        single = path if os.path.isfile(full) else ""

    if mode == "content":
        kind = Kind.CONTENT
        argv += ["--no-heading"]
        argv += ["--no-filename"] if single else ["--with-filename"]
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

    return argv, cwd, kind, postprocess, single if mode == "content" else ""


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

    return argv, str(base), Kind.PATHS, postprocess, ""


def _run(argv: list[str], cwd: str) -> str | None:
    if shutil.which(argv[0]) is None:
        return None
    try:
        proc = subprocess.run(
            argv, cwd=cwd, stdin=subprocess.DEVNULL, capture_output=True, encoding="utf-8", errors="replace", timeout=RG_TIMEOUT_S
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    # rg: 0 = matches, 1 = no matches, 2 = error.
    if proc.returncode not in (0, 1):
        return None
    return proc.stdout


def main() -> int:
    try:
        event = json.loads(sys.stdin.buffer.read().decode("utf-8", errors="replace"))
        out = handle(event)
    except Exception:  # never block the agent because of this hook
        return 0
    if out:
        json.dump(out, sys.stdout)
    return 0
