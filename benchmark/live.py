"""Live agent layer (tier B): run Claude Code headless on each task, hook off vs on.

  python3 benchmark/live.py --only cobra-execute --repeat 1
  python3 benchmark/live.py --modes off on --repeat 3 --jsonl live.jsonl

Each run is `claude -p` in the pinned checkout with the task's `intent` as the
prompt, read-only tools, and the searchslim PreToolUse hook either absent
(`off`) or installed through `--settings` (`on`). From the stream-json
transcript it records: whether the answer cites every critical evidence line
(within 2 lines), number of search calls (Grep, Glob, Bash rg/grep/find/fd),
Read calls, tokens of search results the agent read, turns, total input
tokens, cost and wall time.

Every run spends real API money; `--dry-run` prints the commands instead.
The hook needs a searchslim with `hooks.py` (step 3); point `--searchslim-src`
at a checkout that has it if this one does not.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import statistics
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import bench  # noqa: E402

SEARCH_BINARIES = {"rg", "grep", "egrep", "fgrep", "fd", "fdfind", "find"}
PROMPT = (
    "{intent}\n\n"
    "Work only by searching and reading this repository; do not edit anything. "
    "Finish with a short answer that cites the exact places as path:line."
)
# Our own session's identity must not leak into the child run.
DROP_ENV = ("CLAUDECODE", "CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_REMOTE_SESSION_ID", "CLAUDE_CODE_CHILD_SESSION", "CLAUDE_PID")


def hook_settings(src: Path) -> dict:
    command = f"PYTHONPATH={shlex.quote(str(src))} {shlex.quote(sys.executable)} -m searchslim hook"
    return {"hooks": {"PreToolUse": [{"matcher": "Bash|Grep|Glob", "hooks": [{"type": "command", "command": command, "timeout": 30}]}]}}


def claude_argv(prompt: str, settings_path: str | None, model: str | None, max_turns: int) -> list[str]:
    argv = [
        "claude", "-p", prompt,
        "--output-format", "stream-json", "--verbose",
        "--no-session-persistence", "--session-id", str(uuid.uuid4()),
        "--strict-mcp-config", "--setting-sources", "project",
        "--max-turns", str(max_turns),
        "--allowedTools", "Grep", "Glob", "Read", "Bash",
        "--disallowedTools", "Edit", "Write", "NotebookEdit", "WebFetch", "WebSearch", "Agent", "Task",
    ]
    if settings_path:
        argv += ["--settings", settings_path]
    if model:
        argv += ["--model", model]
    return argv


def is_search_call(name: str, tool_input: dict) -> bool:
    if name in ("Grep", "Glob"):
        return True
    if name != "Bash":
        return False
    cmd = tool_input.get("command", "")
    try:
        first = shlex.split(cmd)[:1]
    except ValueError:
        first = cmd.split()[:1]
    return bool(first) and os.path.basename(first[0]) in SEARCH_BINARIES


def _text_of(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(c.get("text", "") for c in content if isinstance(c, dict))
    return ""


def parse_transcript(lines: list[str]) -> dict:
    """Summarise a stream-json transcript."""
    calls: dict[str, tuple[str, dict]] = {}
    search_calls = read_calls = 0
    search_result_chars = 0
    denied_with_result = 0  # Grep/Glob answered by the hook through a deny reason
    final: dict = {}
    for raw in lines:
        try:
            ev = json.loads(raw)
        except ValueError:
            continue
        if ev.get("type") == "assistant":
            for block in ev.get("message", {}).get("content", []):
                if block.get("type") == "tool_use":
                    calls[block["id"]] = (block["name"], block.get("input") or {})
                    if is_search_call(block["name"], block.get("input") or {}):
                        search_calls += 1
                    elif block["name"] == "Read":
                        read_calls += 1
        elif ev.get("type") == "user":
            content = ev.get("message", {}).get("content", [])
            for block in content if isinstance(content, list) else []:
                if block.get("type") != "tool_result":
                    continue
                name, tool_input = calls.get(block.get("tool_use_id"), ("", {}))
                if is_search_call(name, tool_input):
                    text = _text_of(block.get("content"))
                    search_result_chars += len(text)
                    if "searchslim ran this search" in text:
                        denied_with_result += 1
        elif ev.get("type") == "result":
            final = ev
    usage = final.get("usage") or {}
    return {
        "answer": final.get("result", ""),
        "is_error": bool(final.get("is_error")) or final.get("subtype", "success") != "success",
        "turns": final.get("num_turns", 0),
        "cost_usd": final.get("total_cost_usd", 0.0),
        "duration_ms": final.get("duration_ms", 0),
        "input_tokens": usage.get("input_tokens", 0) + usage.get("cache_read_input_tokens", 0) + usage.get("cache_creation_input_tokens", 0),
        "output_tokens": usage.get("output_tokens", 0),
        "search_calls": search_calls,
        "read_calls": read_calls,
        "search_result_tokens": (search_result_chars + 3) // 4,
        "hook_answered": denied_with_result,
    }


def cited(answer: str, ev: dict, tolerance: int = 2) -> bool:
    path = ev["path"]
    name = os.path.basename(path)
    if "line" not in ev:
        return path in answer or re.search(rf"(?<![\w/.-]){re.escape(name)}\b", answer) is not None
    for m in re.finditer(rf"(?<![\w.-])(?:[\w./-]*/)?{re.escape(name)}(?::|#L| line |, line )(\d+)(?:[-–](\d+))?", answer):
        lo = int(m.group(1))
        hi = int(m.group(2) or lo)
        if lo - tolerance <= ev["line"] <= hi + tolerance:
            return True
    return False


def run_one(task: dict, repo_dir: Path, mode: str, src: Path, args) -> dict:
    settings_path = None
    tmp = None
    if mode == "on":
        tmp = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        json.dump(hook_settings(src), tmp)
        tmp.close()
        settings_path = tmp.name
    argv = claude_argv(PROMPT.format(intent=task["intent"]), settings_path, args.model, args.max_turns)
    if args.dry_run:
        print(f"[{task['id']} {mode}] cd {repo_dir} && {shlex.join(argv)}")
        return {}
    env = {k: v for k, v in os.environ.items() if k not in DROP_ENV}
    env["SEARCHSLIM"] = "on" if mode == "on" else "off"
    t0 = time.perf_counter()
    try:
        proc = subprocess.run(argv, cwd=repo_dir, env=env, capture_output=True, text=True, timeout=args.timeout, stdin=subprocess.DEVNULL)
        out = proc.stdout.splitlines()
    except subprocess.TimeoutExpired as exc:
        out = (exc.stdout or b"").decode(errors="replace").splitlines() if isinstance(exc.stdout, bytes) else (exc.stdout or "").splitlines()
    finally:
        if tmp:
            os.unlink(tmp.name)
    row = parse_transcript(out)
    row["wall_ms"] = round((time.perf_counter() - t0) * 1000)
    critical = [ev for ev in task["evidence"] if ev.get("critical")]
    row["cited"] = sum(cited(row["answer"], ev) for ev in critical)
    row["critical"] = len(critical)
    row["success"] = not row["is_error"] and row["cited"] == row["critical"]
    row.update(task=task["id"], mode=mode)
    return row


def summarize(rows: list[dict], modes: list[str]) -> str:
    out = ["| task | mode | ok | cited | search calls | reads | search result tok | input tok | turns | $ | s |", "|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in rows:
        out.append(
            f"| {r['task']} | {r['mode']} | {'yes' if r['success'] else 'no'} | {r['cited']}/{r['critical']} | {r['search_calls']} | "
            f"{r['read_calls']} | {r['search_result_tokens']} | {r['input_tokens']} | {r['turns']} | {r['cost_usd']:.3f} | {r['wall_ms'] / 1000:.0f} |"
        )
    out += ["", "| mode | runs | success | median search calls | median search result tok | median input tok | total $ | median s |", "|---|---|---|---|---|---|---|---|"]
    for m in modes:
        rs = [r for r in rows if r["mode"] == m]
        if not rs:
            continue
        med = lambda k: statistics.median(r[k] for r in rs)  # noqa: E731
        out.append(
            f"| {m} | {len(rs)} | {sum(r['success'] for r in rs)}/{len(rs)} | {med('search_calls'):g} | {med('search_result_tokens'):g} | "
            f"{med('input_tokens'):g} | {sum(r['cost_usd'] for r in rs):.2f} | {med('wall_ms') / 1000:.0f} |"
        )
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--only", nargs="*")
    p.add_argument("--modes", nargs="+", default=["off", "on"], choices=["off", "on"])
    p.add_argument("--repeat", type=int, default=3)
    p.add_argument("--model", help="model for the agent (default: Claude Code's default)")
    p.add_argument("--max-turns", type=int, default=20)
    p.add_argument("--timeout", type=int, default=600, help="seconds per run")
    p.add_argument("--searchslim-src", default=str(ROOT.parent / "src"))
    p.add_argument("--jsonl")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args(argv)

    src = Path(args.searchslim_src).resolve()
    if "on" in args.modes and not (src / "searchslim" / "hooks.py").exists():
        raise SystemExit(f"{src} has no searchslim/hooks.py; pass --searchslim-src pointing at a checkout with the hook")

    spec = bench.load_spec()
    rows = []
    sink = open(args.jsonl, "a", encoding="utf-8") if args.jsonl else None
    for task in spec["tasks"]:
        if args.only and task["id"] not in args.only:
            continue
        repo_dir = bench.ensure_repo(task["repo"], spec["repos"][task["repo"]])
        for i in range(args.repeat):
            for mode in args.modes:  # interleave modes so drift over time hits both
                row = run_one(task, repo_dir, mode, src, args)
                if not row:
                    continue
                row["rep"] = i
                rows.append(row)
                print(f"{task['id']} {mode} #{i}: ok={row['success']} searches={row['search_calls']} ${row['cost_usd']:.3f}", file=sys.stderr)
                if sink:
                    sink.write(json.dumps(row) + "\n")
                    sink.flush()
    if sink:
        sink.close()
    if rows:
        print(summarize(rows, args.modes))
    return 0


if __name__ == "__main__":
    sys.exit(main())
