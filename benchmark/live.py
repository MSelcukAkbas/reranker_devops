"""Live agent layer (tier B): run Claude Code headless on each task, hook off vs on, paired.

  python3 benchmark/live.py --only cobra-execute --repeat 1
  python3 benchmark/live.py --modes off on --repeat 3 --jsonl live.jsonl
  python3 benchmark/live.py --workload --per-category 1 --modes off on --repeat 3 --jsonl w.jsonl
  python3 benchmark/live.py --tasks my_tasks.json --repo-dir ../arvis_code --modes off on --repeat 3
  python3 benchmark/live.py --summarize w.jsonl            re-print the tables from saved rows

Each run is `claude -p` in the task's checkout with read-only tools plus Bash,
and the searchslim hooks (the events and matchers `searchslim install`
writes) either absent (`off`) or passed through `--settings`, with ranking
off (`rules`) or lexical (`on`, the hook's default). Modes are interleaved
per repetition, so an (off, on) pair ran under the same conditions.

Task sources:
  default      benchmark/tasks.json (search tasks on the pinned repos)
  --workload   benchmark/workload.json: its search tasks, plus its "live" test/build
               tasks (a pinned repo, an optional patch from patches/ applied in a
               throwaway git worktree, and a prompt that runs the tests or build)
  --tasks F    your own file: {"tasks": [{"id", "category", "prompt" | "intent",
               "evidence": [...], "repo"?}]}; tasks without "repo" run in --repo-dir

Evidence: {"path", "line"?, "accept"?} must be cited in the answer (within 2
lines), {"mention": "text"} must appear in it (case-insensitive),
{"regex": "...", "min": N} must match at least N distinct strings (list-all
tasks: e.g. every env var name). Only "critical" entries decide success.

Recorded per run, from the stream-json transcript: success, turns, output
tokens, input tokens split into uncached / cache writes / cache reads,
billed cost (Claude Code's total_cost_usd), search and build/test calls,
tokens of their results as the model saw them, results searchslim touched,
and compensatory calls: a search whose pattern repeats or narrows an
earlier one, and a test/build command run again unchanged.

Every run spends real API money; `--dry-run` prints the commands instead.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import shlex
import shutil
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent / "src"))

import bench  # noqa: E402

SEARCH_BINARIES = {"rg", "grep", "egrep", "fgrep", "fd", "fdfind", "find", "git"}
POWERSHELL_SEARCH = re.compile(r"(?i)\b(select-string|get-childitem|gci|sls|rg)\b")
BUILD_RE = re.compile(
    r"^(cargo (build|check|clippy|test|nextest)|go (build|vet|test)|n?px tsc|tsc|npm (test|t|run \S+)|pnpm \S+|yarn \S+"
    r"|make|mvn|gradle|\./gradlew|dotnet (build|test)|pytest|python3? -m (pytest|mypy)|mypy|ruff|eslint|jest|vitest)\b"
)
PROMPT = (
    "{intent}\n\n"
    "Work only by searching and reading this repository; do not edit anything. "
    "Finish with a short answer that cites the exact places as path:line."
)
# Our own session's identity must not leak into the child run.
DROP_ENV = ("CLAUDECODE", "CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_REMOTE_SESSION_ID", "CLAUDE_CODE_CHILD_SESSION", "CLAUDE_PID")
METRICS = ("cost_usd", "turns", "output_tokens", "cache_read_tokens", "tool_output_tokens", "search_calls", "build_calls", "re_searches", "reruns")


# --- settings and command line ----------------------------------------------------


def hook_settings(src: Path | None) -> dict:
    """The hooks `searchslim install` registers, running searchslim from `src` when given."""
    from searchslim.install import EVENTS, hook_command

    command = hook_command(sys.executable)
    if src and os.name != "nt":
        command = f"PYTHONPATH={shlex.quote(str(src))} {command}"  # Windows: PYTHONPATH goes in the env
    entry = {"type": "command", "command": command, "timeout": 30}
    if command.startswith("& "):
        entry["shell"] = "powershell"
    hooks = {}
    for event, matcher in EVENTS:
        hooks[event] = [{**({"matcher": matcher} if matcher else {}), "hooks": [dict(entry)]}]
    return {"hooks": hooks}


def claude_argv(prompt: str, settings_path: str | None, model: str | None, max_turns: int) -> list[str]:
    argv = [
        shutil.which("claude") or "claude", "-p", prompt,
        "--output-format", "stream-json", "--verbose",
        "--no-session-persistence", "--session-id", str(uuid.uuid4()),
        "--strict-mcp-config", "--setting-sources", "project",
        "--max-turns", str(max_turns),
        "--allowedTools", "Grep", "Glob", "Read", "Bash", "PowerShell",
        "--disallowedTools", "Edit", "Write", "NotebookEdit", "WebFetch", "WebSearch", "Agent", "Task",
    ]
    if settings_path:
        argv += ["--settings", settings_path]
    if model:
        argv += ["--model", model]
    return argv


# --- classifying tool calls -----------------------------------------------------------


def _words(cmd: str) -> list[str]:
    try:
        words = shlex.split(cmd)
    except ValueError:
        words = cmd.split()
    while len(words) > 2 and words[0] in ("cd", "Set-Location", "sl", "Push-Location") and words[2] in ("&&", ";"):
        words = words[3:]  # a leading `cd x &&`
    while words and re.match(r"^\w+=", words[0]):
        words = words[1:]  # VAR=value prefixes
    return words


def is_search_call(name: str, tool_input: dict) -> bool:
    if name in ("Grep", "Glob"):
        return True
    cmd = tool_input.get("command", "")
    if name == "PowerShell":
        return bool(POWERSHELL_SEARCH.search(cmd))
    if name != "Bash":
        return False
    words = _words(cmd)
    if not words:
        return False
    first = os.path.basename(words[0])
    if first == "git":
        return words[1:2] in (["grep"], ["ls-files"])
    return first in SEARCH_BINARIES


def is_build_call(name: str, tool_input: dict) -> bool:
    if name not in ("Bash", "PowerShell"):
        return False
    words = _words(tool_input.get("command", ""))
    return bool(words) and bool(BUILD_RE.match(" ".join([os.path.basename(words[0])] + words[1:])))


def search_key(name: str, tool_input: dict) -> str:
    """The pattern a search looks for, normalized; '' when it has none (a file listing)."""
    if name in ("Grep", "Glob"):
        pat = tool_input.get("pattern", "")
    else:
        words = _words(tool_input.get("command", ""))[1:]
        if words[:1] in (["grep"], ["ls-files"]):
            words = words[1:]
        pat = ""
        for i, w in enumerate(words):
            if w in ("-e", "--regexp", "-name", "-iname", "-g", "--glob", "-Pattern") and i + 1 < len(words):
                pat = words[i + 1]
                break
            if not pat and not w.startswith("-") and not w.isdigit():
                pat = w
    return re.sub(r"[\\'\"]", "", pat).lower()


def _refines(key: str, earlier: str) -> bool:
    if not key or not earlier:
        return False
    return key == earlier or (min(len(key), len(earlier)) >= 4 and (key in earlier or earlier in key))


def _text_of(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(c.get("text", "") for c in content if isinstance(c, dict))
    return ""


def parse_transcript(lines: list[str]) -> dict:
    """Summarise a stream-json transcript."""
    calls: dict[str, tuple[str, dict]] = {}
    search_calls = read_calls = build_calls = 0
    search_chars = build_chars = 0
    touched = 0  # results searchslim reduced or compacted
    re_searches = re_after_touched = reruns = 0
    searches: list[tuple[str, bool]] = []  # (key, result touched)
    builds: list[str] = []
    pending: dict[str, str] = {}  # tool_use_id -> search key
    final: dict = {}
    for raw in lines:
        try:
            ev = json.loads(raw)
        except ValueError:
            continue
        if ev.get("type") == "assistant":
            for block in ev.get("message", {}).get("content", []):
                if block.get("type") != "tool_use":
                    continue
                name, tool_input = block["name"], block.get("input") or {}
                calls[block["id"]] = (name, tool_input)
                if is_search_call(name, tool_input):
                    search_calls += 1
                    key = search_key(name, tool_input)
                    earlier = [t for k, t in searches if _refines(key, k)]
                    if earlier:
                        re_searches += 1
                        re_after_touched += any(earlier)
                    pending[block["id"]] = key
                elif is_build_call(name, tool_input):
                    build_calls += 1
                    cmd = " ".join(_words(tool_input.get("command", "")))
                    reruns += cmd in builds
                    builds.append(cmd)
                elif name == "Read":
                    read_calls += 1
        elif ev.get("type") == "user":
            content = ev.get("message", {}).get("content", [])
            for block in content if isinstance(content, list) else []:
                if block.get("type") != "tool_result":
                    continue
                name, tool_input = calls.get(block.get("tool_use_id"), ("", {}))
                text = _text_of(block.get("content"))
                hit = "[searchslim]" in text or "searchslim ran this search" in text
                if is_search_call(name, tool_input):
                    search_chars += len(text)
                    touched += hit
                    if block.get("tool_use_id") in pending:
                        searches.append((pending.pop(block["tool_use_id"]), hit))
                elif is_build_call(name, tool_input):
                    build_chars += len(text)
                    touched += hit
        elif ev.get("type") == "result":
            final = ev
    usage = final.get("usage") or {}
    uncached = usage.get("input_tokens", 0)
    cache_read = usage.get("cache_read_input_tokens", 0)
    cache_write = usage.get("cache_creation_input_tokens", 0)
    return {
        "answer": final.get("result", ""),
        "is_error": bool(final.get("is_error")) or final.get("subtype", "success") != "success",
        "turns": final.get("num_turns", 0),
        "cost_usd": final.get("total_cost_usd", 0.0),
        "duration_ms": final.get("duration_ms", 0),
        "input_tokens": uncached + cache_read + cache_write,
        "uncached_input_tokens": uncached,
        "cache_read_tokens": cache_read,
        "cache_write_tokens": cache_write,
        "output_tokens": usage.get("output_tokens", 0),
        "search_calls": search_calls,
        "build_calls": build_calls,
        "read_calls": read_calls,
        "search_result_tokens": (search_chars + 3) // 4,
        "build_result_tokens": (build_chars + 3) // 4,
        "tool_output_tokens": (search_chars + build_chars + 3) // 4,
        "hook_answered": touched,
        "re_searches": re_searches,
        "re_searches_after_reduced": re_after_touched,
        "reruns": reruns,
    }


# --- scoring the answer -------------------------------------------------------------


_FILE_REF = re.compile(r"(?<![\w.-])((?:[\w.-]+[/\\])*[\w.-]+\.\w+)(?::|#L| line |, line )(\d+)(?:[-–](\d+))?")
_BARE_REF = re.compile(r"(?<![\w./-]):(\d+)(?:[-–](\d+))?\b")


def line_refs(answer: str) -> list[tuple[str, int, int]]:
    """(file name, first line, last line) for every citation in the answer.

    Agents often write `path:10` once and then `:15`, `:20-24` for the same
    file; a bare `:N` is attributed to the file named most recently before it.
    """
    refs = []
    marks = []  # (offset, file name)
    base = lambda p: os.path.basename(p.replace("\\", "/"))  # noqa: E731
    for m in _FILE_REF.finditer(answer):
        name = base(m.group(1))
        lo = int(m.group(2))
        refs.append((name, lo, int(m.group(3) or lo)))
        marks.append((m.start(), name))
    for m in re.finditer(r"(?<![\w.-])((?:[\w.-]+[/\\])*[\w.-]+\.\w+)", answer):
        marks.append((m.start(), base(m.group(1))))
    marks.sort()
    for m in _BARE_REF.finditer(answer):
        before = [name for off, name in marks if off < m.start()]
        if before:
            lo = int(m.group(1))
            refs.append((before[-1], lo, int(m.group(2) or lo)))
    return refs


def cited(answer: str, ev: dict, tolerance: int = 2) -> bool:
    if "mention" in ev:
        return ev["mention"].lower() in answer.lower()
    if "regex" in ev:
        return len(set(re.findall(ev["regex"], answer))) >= ev.get("min", 1)
    path = ev["path"]
    name = os.path.basename(path)
    if "line" not in ev:
        return path in answer.replace("\\", "/") or re.search(rf"(?<![\w/.-]){re.escape(name)}\b", answer) is not None
    # `accept` lists other ranges that answer the same question equally well
    # (a call site instead of the definition, the function body instead of its line).
    targets = [(name, ev["line"], ev["line"])] + [(os.path.basename(p), a, b) for p, a, b in ev.get("accept", [])]
    return any(
        n == t_name and lo - tolerance <= t_hi and t_lo <= hi + tolerance
        for n, lo, hi in line_refs(answer)
        for t_name, t_lo, t_hi in targets
    )


# --- tasks and runs ---------------------------------------------------------------------


def load_tasks(args) -> list[dict]:
    if args.tasks:
        spec = json.loads(Path(args.tasks).read_text(encoding="utf-8"))
        tasks = spec["tasks"] if isinstance(spec, dict) else spec
    elif args.workload:
        import workload

        wspec, wtasks = workload.load_workload()
        tasks = [t for t in wtasks if t.get("kind") != "shell"] + list(wspec.get("live", []))
        if args.per_category:
            seen: dict[str, int] = {}
            picked = []
            for t in tasks:
                if seen.get(t["category"], 0) < args.per_category:
                    seen[t["category"]] = seen.get(t["category"], 0) + 1
                    picked.append(t)
            tasks = picked
    else:
        tasks = bench.load_spec()["tasks"]
    return [t for t in tasks if not args.only or t["id"] in args.only]


def repo_dir_for(task: dict, args) -> Path:
    if task.get("cwd"):
        return Path(task["cwd"])
    if task.get("repo"):
        import workload

        return bench.ensure_repo(task["repo"], workload.repos()[task["repo"]])
    if not args.repo_dir:
        raise SystemExit(f"{task['id']}: no repo; pass --repo-dir")
    return Path(args.repo_dir)


def checkout(task: dict, repo_dir: Path, dry_run: bool):
    """(dir to run in, cleanup); a task with a patch gets its own worktree."""
    if not task.get("patch"):
        return repo_dir, lambda: None
    wt = Path(tempfile.mkdtemp(prefix=f"live-{task['id']}-"))
    if dry_run:
        return wt, lambda: shutil.rmtree(wt, ignore_errors=True)
    shutil.rmtree(wt)
    subprocess.run(["git", "-C", str(repo_dir), "worktree", "add", "-q", "--detach", str(wt), "HEAD"], check=True, stdin=subprocess.DEVNULL)
    subprocess.run(["git", "-C", str(wt), "apply", str(ROOT / task["patch"])], check=True, stdin=subprocess.DEVNULL)

    def cleanup():
        subprocess.run(["git", "-C", str(repo_dir), "worktree", "remove", "--force", str(wt)], stdin=subprocess.DEVNULL, capture_output=True)

    return wt, cleanup


def run_one(task: dict, repo_dir: Path, mode: str, src: Path | None, args) -> dict:
    settings_path = None
    tmp = None
    if mode != "off":
        tmp = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        json.dump(hook_settings(src), tmp)
        tmp.close()
        settings_path = tmp.name
    prompt = task.get("prompt") or PROMPT.format(intent=task["intent"])
    argv = claude_argv(prompt, settings_path, args.model, args.max_turns)
    cwd, cleanup = checkout(task, repo_dir, args.dry_run)
    if args.dry_run:
        print(f"[{task['id']} {mode}] cd {cwd} && {shlex.join(argv)}")
        cleanup()
        return {}
    env = {k: v for k, v in os.environ.items() if k not in DROP_ENV}
    env["SEARCHSLIM"] = "off" if mode == "off" else "on"
    # "rules": deterministic rules only; "on": rules + lexical ranking (the hook's default).
    env["SEARCHSLIM_RERANK"] = "off" if mode == "rules" else "lexical"
    if src:
        env["PYTHONPATH"] = str(src) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    t0 = time.perf_counter()
    try:
        proc = subprocess.run(
            argv, cwd=cwd, env=env, capture_output=True, encoding="utf-8", errors="replace",
            timeout=args.timeout, stdin=subprocess.DEVNULL,
        )
        out = proc.stdout.splitlines()
    except subprocess.TimeoutExpired as exc:
        data = exc.stdout or b""
        out = (data.decode("utf-8", errors="replace") if isinstance(data, bytes) else data).splitlines()
    finally:
        if tmp:
            os.unlink(tmp.name)
        cleanup()
    row = parse_transcript(out)
    row["wall_ms"] = round((time.perf_counter() - t0) * 1000)
    critical = [ev for ev in task["evidence"] if ev.get("critical")]
    row["cited"] = sum(cited(row["answer"], ev) for ev in critical)
    row["critical"] = len(critical)
    row["success"] = not row["is_error"] and row["cited"] == row["critical"]
    row.update(task=task["id"], mode=mode, category=task.get("category", ""))
    return row


# --- summaries ----------------------------------------------------------------------------


def _ratio_ci(pairs: list[tuple[float, float]], seed: int = 0, rounds: int = 2000) -> tuple[float, float, float]:
    """sum(on)/sum(off) - 1, with a 95% bootstrap interval over pairs."""
    off = sum(a for a, _ in pairs)
    on = sum(b for _, b in pairs)
    point = on / off - 1 if off else 0.0
    rng = random.Random(seed)
    stats = []
    for _ in range(rounds):
        sample = [pairs[rng.randrange(len(pairs))] for _ in pairs]
        s_off = sum(a for a, _ in sample)
        if s_off:
            stats.append(sum(b for _, b in sample) / s_off - 1)
    stats.sort()
    if not stats:
        return point, point, point
    return point, stats[int(0.025 * len(stats))], stats[int(0.975 * len(stats)) - 1]


def pairs_of(rows: list[dict], base: str, test: str) -> list[tuple[dict, dict]]:
    by = {(r["task"], r.get("rep", 0), r["mode"]): r for r in rows}
    return [(by[(t, rep, base)], r) for (t, rep, m), r in sorted(by.items()) if m == test and (t, rep, base) in by]


def summarize(rows: list[dict], modes: list[str]) -> str:
    out = [
        "| task | mode | ok | cited | turns | search | build | re-search | tool out tok | out tok | cache read | $ | s |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        out.append(
            f"| {r['task']} | {r['mode']} | {'yes' if r['success'] else 'no'} | {r['cited']}/{r['critical']} | {r['turns']} | "
            f"{r['search_calls']} | {r.get('build_calls', 0)} | {r.get('re_searches', 0)} | {r.get('tool_output_tokens', r.get('search_result_tokens', 0))} | "
            f"{r.get('output_tokens', 0)} | {r.get('cache_read_tokens', 0)} | {r['cost_usd']:.3f} | {r['wall_ms'] / 1000:.0f} |"
        )
    out += [
        "",
        "| mode | runs | success | median turns | median tool out tok | median out tok | cache read tok | re-searches | reruns | total $ | median s |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for m in modes:
        rs = [r for r in rows if r["mode"] == m]
        if not rs:
            continue
        med = lambda k: statistics.median(r.get(k, 0) for r in rs)  # noqa: E731
        out.append(
            f"| {m} | {len(rs)} | {sum(r['success'] for r in rs)}/{len(rs)} | {med('turns'):g} | {med('tool_output_tokens'):g} | "
            f"{med('output_tokens'):g} | {sum(r.get('cache_read_tokens', 0) for r in rs)} | {sum(r.get('re_searches', 0) for r in rs)} | "
            f"{sum(r.get('reruns', 0) for r in rs)} | {sum(r['cost_usd'] for r in rs):.2f} | {med('wall_ms') / 1000:.0f} |"
        )
    base = "off" if "off" in modes else None
    for test in [m for m in modes if base and m != base]:
        pairs = pairs_of(rows, base, test)
        if not pairs:
            continue
        out += [
            "",
            f"Paired {test} vs {base}: {len(pairs)} pairs; change of the summed metric, 95% bootstrap interval over pairs.",
            "",
            "| metric | off | on | change | 95% CI |",
            "|---|---|---|---|---|",
        ]
        out.append(f"| success | {sum(a['success'] for a, _ in pairs)} | {sum(b['success'] for _, b in pairs)} | | |")
        for k in METRICS:
            vals = [(float(a.get(k, 0)), float(b.get(k, 0))) for a, b in pairs]
            p, lo, hi = _ratio_ci(vals)
            fmt = (lambda v: f"{v:.3f}") if k == "cost_usd" else (lambda v: f"{v:.0f}")
            out.append(
                f"| {k} | {fmt(sum(a for a, _ in vals))} | {fmt(sum(b for _, b in vals))} | {100 * p:+.0f}% | {100 * lo:+.0f}% .. {100 * hi:+.0f}% |"
            )
        cats = sorted({a.get("category", "") for a, _ in pairs} - {""})
        if cats:
            out += ["", "| category | pairs | success off/on | $ change | tool out tok change | turns change | re-searches off/on |", "|---|---|---|---|---|---|---|"]
            for c in cats:
                ps = [(a, b) for a, b in pairs if a.get("category") == c]
                ch = lambda k: 100 * _ratio_ci([(float(a.get(k, 0)), float(b.get(k, 0))) for a, b in ps], rounds=1)[0]  # noqa: E731
                out.append(
                    f"| {c} | {len(ps)} | {sum(a['success'] for a, _ in ps)}/{sum(b['success'] for _, b in ps)} | {ch('cost_usd'):+.0f}% | "
                    f"{ch('tool_output_tokens'):+.0f}% | {ch('turns'):+.0f}% | "
                    f"{sum(a.get('re_searches', 0) for a, _ in ps)}/{sum(b.get('re_searches', 0) for _, b in ps)} |"
                )
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--only", nargs="*")
    p.add_argument("--modes", nargs="+", default=["off", "rules", "on"], choices=["off", "rules", "on"],
                   help="off: no hook; rules: hook without ranking; on: hook with lexical ranking")
    p.add_argument("--repeat", type=int, default=3)
    p.add_argument("--workload", action="store_true", help="tasks from workload.json (search + live test/build)")
    p.add_argument("--per-category", type=int, default=0, help="with --workload: at most N tasks per category")
    p.add_argument("--tasks", help="your own task file (see the module docstring)")
    p.add_argument("--repo-dir", help="checkout for tasks without a repo (with --tasks)")
    p.add_argument("--model", help="model for the agent (default: Claude Code's default)")
    p.add_argument("--max-turns", type=int, default=20)
    p.add_argument("--timeout", type=int, default=600, help="seconds per run")
    p.add_argument("--searchslim-src", default=str(ROOT.parent / "src"), help="searchslim to run the hook from; '' = the installed one")
    p.add_argument("--jsonl")
    p.add_argument("--summarize", help="print the tables for rows saved with --jsonl, run nothing")
    p.add_argument("--jobs", type=int, default=1, help="runs in parallel")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args(argv)

    if args.summarize:
        rows = [json.loads(ln) for ln in Path(args.summarize).read_text(encoding="utf-8").splitlines() if ln.strip()]
        modes = [m for m in ("off", "rules", "on") if any(r["mode"] == m for r in rows)]
        print(summarize(rows, modes))
        return 0

    src = Path(args.searchslim_src).resolve() if args.searchslim_src else None
    if src and set(args.modes) - {"off"} and not (src / "searchslim" / "hooks.py").exists():
        raise SystemExit(f"{src} has no searchslim/hooks.py; pass --searchslim-src pointing at a checkout with the hook")

    jobs = []
    for task in load_tasks(args):
        repo_dir = repo_dir_for(task, args)
        for i in range(args.repeat):
            for mode in args.modes:  # interleave modes so drift over time hits both
                jobs.append((task, repo_dir, mode, i))

    rows = []
    sink = open(args.jsonl, "a", encoding="utf-8") if args.jsonl else None
    lock = threading.Lock()

    def work(job):
        task, repo_dir, mode, i = job
        row = run_one(task, repo_dir, mode, src, args)
        if not row:
            return
        row["rep"] = i
        with lock:
            rows.append(row)
            print(f"{task['id']} {mode} #{i}: ok={row['success']} turns={row['turns']} ${row['cost_usd']:.3f}", file=sys.stderr)
            if sink:
                sink.write(json.dumps(row) + "\n")
                sink.flush()

    # Runs are independent claude -p processes (own session id, own hook cache).
    with ThreadPoolExecutor(max_workers=max(1, args.jobs)) as pool:
        list(pool.map(work, jobs))
    order = {(j[0]["id"], j[3], j[2]): n for n, j in enumerate(jobs)}
    rows.sort(key=lambda r: order[(r["task"], r["rep"], r["mode"])])
    if sink:
        sink.close()
    if rows:
        print(summarize(rows, args.modes))
    return 0


if __name__ == "__main__":
    sys.exit(main())
