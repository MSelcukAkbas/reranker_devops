"""Representative workload: what the hook saves per category, and what it loses.

  python3 benchmark/workload.py fetch     capture fixtures of workload-only search tasks (clones pinned repos)
  python3 benchmark/workload.py run       hook defaults over every fixture; per task, per category, total

`run` is offline: it reads the committed fixtures only. Tasks live in
benchmark/workload.json. A task is one of:

  {"ref": "<tasks.json id>", "category": C}      a bench.py task and its fixture
  {"id", "category", "repo", "cmd", ...}          a search task captured by `fetch` (fixtures/workload/)
  {"id", "category", "kind": "shell", "fixture"}  real test/build output (fixtures/workload/shell/);
                                                  `source` says how it was produced (patches/ for injected faults)

Search output goes through what the hook does by default: `searchslim run`
for Bash searches, the PostToolUse path for Grep (`"tool": "Grep"`: small
results and results that would cross Claude Code's inline limit stay the
tool's own). Shell output goes through `compact` at its hook trigger.

Per task it reports raw and output tokens, saving, and three recalls:
  evidence   critical anchors still in the output: search anchors (bench.py) must be
             "kept", shell anchors are substrings that must appear in an output line
  locations  search: every match location of the raw output ((path, line) for content,
             the path for file lists and counts), read back from the output;
             shell: every raw line naming a failure or a source location, verbatim
  latency    median ms of the reduction itself

The goal check (workload.json "goal") uses the category average: each
category's total saving, weighted by its "weight", so one huge fixture
cannot carry the result. Task average and plain total are printed too.
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent / "src"))

import bench  # noqa: E402
from searchslim import Config, Kind, detect_kind, parse, slim  # noqa: E402
from searchslim.compact import (  # noqa: E402
    CARGO_TEST_PASS, DEFAULT_COMPACT_TRIGGER_TOKENS, DOTNET_PASS, GO_PASS, JS_PASS, PYTEST_PASS, compact,
)
from searchslim.hooks import GREP_INLINE_CHARS  # noqa: E402
from searchslim.lossless import match_locations  # noqa: E402
from searchslim.rerank import Query, make_scorer, pattern_and_paths  # noqa: E402
from searchslim.rules import DEFAULT_VIEW, estimate_tokens, view_defaults  # noqa: E402

WORKLOAD = ROOT / "workload.json"
FIXTURES = ROOT / "fixtures" / "workload"

# A shell line that names a failure or a place in the code.
FAILURE_RE = re.compile(
    r"(?i)(\berror\b|\bfail(ed|ure|s)?\b|\bpanic|exception|traceback|assert|^E\s|✗|×|●|warning)"
)
LOCATION_RE = re.compile(r"[\w./\\-]+\.[A-Za-z]\w*(:\d+|\(\d+,\d+\))|File \".+\", line \d+")
# Status lines of passing tests are not failure evidence, even when a test id
# happens to contain "error" or "warning".
PASS_RES = (PYTEST_PASS, JS_PASS, GO_PASS, CARGO_TEST_PASS, DOTNET_PASS)


# --- spec -----------------------------------------------------------------------


def load_workload(path: Path = WORKLOAD) -> tuple[dict, list[dict]]:
    """The spec and its tasks with `ref` entries resolved against tasks.json."""
    spec = json.loads(path.read_text(encoding="utf-8"))
    bench_tasks = {t["id"]: t for t in bench.load_spec()["tasks"]}
    tasks = []
    for t in spec["tasks"]:
        if "ref" in t:
            if t["ref"] not in bench_tasks:
                raise SystemExit(f"workload.json: unknown ref {t['ref']}")
            tasks.append({**bench_tasks[t["ref"]], "ref": t["ref"], "category": t["category"], "fixture_dir": str(bench.FIXTURES)})
        else:
            tasks.append({**t, "fixture_dir": str(FIXTURES)})
        if tasks[-1]["category"] not in spec["categories"]:
            raise SystemExit(f"workload.json: {tasks[-1]['id']} has unknown category {tasks[-1]['category']}")
    return spec, tasks


def repos() -> dict:
    return {**bench.load_spec()["repos"], **json.loads(WORKLOAD.read_text(encoding="utf-8"))["repos"]}


def steps(task: dict) -> list[tuple[dict, Path]]:
    if task.get("kind") == "shell":
        return [(task, Path(task["fixture_dir"]) / task["fixture"])]
    return [(st, Path(task["fixture_dir"]) / f"{st.get('fixture', st['id'])}.txt") for st in bench.task_steps(task)]


# --- what the hook does -----------------------------------------------------------


def hook_config() -> Config:
    max_tokens, trigger = view_defaults(DEFAULT_VIEW)
    return Config(max_tokens=max_tokens, trigger_tokens=trigger, view=DEFAULT_VIEW)


_SCORER = None


def reduce_search(raw: str, step: dict, config: Config) -> tuple[str, str]:
    """(output text, level) as the hook would return it with default settings."""
    global _SCORER
    _SCORER = _SCORER or make_scorer("lexical")
    pattern = pattern_and_paths(step["cmd"])[0] if step.get("cmd") else ""
    grep = step.get("tool") == "Grep"
    if grep and estimate_tokens(raw) <= config.trigger_tokens:
        return raw, "small"
    reduced = slim(
        raw, config=config, scorer=_SCORER, query=Query(step.get("intent", ""), step.get("subtask", ""), pattern),
        default_path=bench.default_path(step), pattern=pattern,
    )
    level = str(reduced.stats.get("level", "rules"))
    if reduced.text.rstrip("\n") == raw.rstrip("\n"):
        return raw, "pass"
    if grep and len(raw) <= GREP_INLINE_CHARS < len(reduced.text) + 500:
        return raw, "inline-kept"
    return reduced.text, level


def reduce_shell(raw: str, trigger: int) -> tuple[str, str]:
    out = compact(raw, trigger_tokens=trigger)
    return (raw, "pass") if out is None else (out.text, "+".join(out.stats.get("tools", [])) or "compact")


# --- recall -----------------------------------------------------------------------


def search_locations(raw: str, output: str, dpath: str) -> tuple[int, int]:
    """(found, total) raw match locations that the output still has."""
    kind = detect_kind(raw)
    if kind is Kind.CONTENT:
        want = match_locations(raw, dpath)
        have = match_locations(output, dpath)
    elif kind is Kind.PATHS:
        want = {bench._norm(p) for p in parse(raw, kind=kind).paths}
        have = {bench._norm(p) for p in parse(output, kind=kind).paths}
    elif kind is Kind.COUNT:
        want = {bench._norm(c.path) for c in parse(raw, kind=kind).counts}
        have = {bench._norm(c.path) for c in parse(output, kind=kind).counts}
    else:
        want = set(raw.splitlines())
        have = set(output.splitlines())
    return len(want & have), len(want)


# compact.py lists the other places of a repeated diagnostic in one note:
# `[searchslim] N more places with this same diagnostic (msg)[, under dir/]: a.ts (3,5) (9,1); b.ts (4,2)`.
PLACES_NOTE = re.compile(
    r"^\[searchslim\] \d+ more places with this same diagnostic \((?P<msg>.*)\)(?:, under (?P<under>\S+))?: (?P<where>.*)$"
)
_POS = re.compile(r"^(\(\d+(,\d+)*\)|\d+(:\d+)*)$")
# ... and of a repeated pytest warning, one row per place:
# `[searchslim] N more places with this same warning (msg)[, under dir/], as path:line test-id | source line:`
# then `  a.py:8 ::test_x | source` (`::name` = a test in that file).
WARNING_NOTE = re.compile(
    r"^\[searchslim\] \d+ more places with this same warning \((?P<msg>.*)\)(?:, under (?P<under>\S+))?, as path:line test-id \| source line:$"
)
WARNING_ROW = re.compile(r"^  (?P<path>\S.*?):(?P<line>\d+) (?P<tests>.+?) \| (?P<source>.*)$")


def listed_lines(output: str) -> set[str]:
    """Output lines, stripped, plus each place a diagnostic note lists, written back as the
    tool prints it (`a.ts(3,5): msg`, `a.c:3:5: msg`) and as a rustc `--> a.rs:3:5` line."""
    have = {ln.strip() for ln in output.splitlines()}
    warning = None
    for ln in output.splitlines():
        row = WARNING_ROW.match(ln) if warning else None
        if row:
            path = (warning["under"] or "") + row["path"]
            have.add(f"{path}:{row['line']}: {warning['msg']}")
            have.add(row["source"].strip())
            for test in row["tests"].split(" "):
                have.add(path.rsplit("/", 1)[-1] + test if test.startswith("::") else test)
            continue
        warning = WARNING_NOTE.match(ln)
        m = PLACES_NOTE.match(ln)
        if not m:
            continue
        for entry in m["where"].split("; "):
            words = entry.split(" ")
            k = len(words)
            while k > 1 and _POS.match(words[k - 1]):
                k -= 1
            path = (m["under"] or "") + " ".join(words[:k])
            for pos in words[k:]:
                have.add(f"{path}{pos}: {m['msg']}" if pos.startswith("(") else f"{path}:{pos}: {m['msg']}")
                have.add(f"--> {path}:{pos}")
    return have


def shell_locations(raw: str, output: str) -> tuple[int, int, list[str]]:
    """(found, total, missing) raw lines naming a failure or a location: kept verbatim,
    or (a repeated diagnostic) listed with its path, position and message in a note."""
    want = {
        ln for ln in raw.splitlines()
        if ln.strip() and (FAILURE_RE.search(ln) or LOCATION_RE.search(ln)) and not any(p.match(ln) for p in PASS_RES)
    }
    have = listed_lines(output)
    missing = sorted(ln for ln in want if ln.strip() not in have)
    return len(want) - len(missing), len(want), missing


def shell_evidence(task: dict, output: str) -> tuple[int, int]:
    lines = listed_lines(output)
    ev = task.get("evidence", [])
    return sum(any(s in ln for ln in lines) for s in ev), len(ev)


# --- run ----------------------------------------------------------------------------


@dataclass
class Row:
    task: str
    category: str
    group: str
    raw_tokens: int = 0
    tokens: int = 0
    ms: float = 0.0
    level: str = ""
    ev_found: int = 0
    ev_total: int = 0
    loc_found: int = 0
    loc_total: int = 0
    missing: list = field(default_factory=list)

    @property
    def saving(self) -> float:
        return 1 - self.tokens / self.raw_tokens if self.raw_tokens else 0.0


def timed(fn, repeat: int):
    times, out = [], None
    for _ in range(max(1, repeat)):
        t0 = time.perf_counter()
        out = fn()
        times.append((time.perf_counter() - t0) * 1000)
    return out, statistics.median(times)


def run_task(task: dict, spec: dict, config: Config, count, args) -> Row:
    group = spec["categories"][task["category"]]["group"]
    row = Row(task["id"], task["category"], group)
    levels = []
    ev_status: dict[str, str] = {}
    for step, path in steps(task):
        if not path.exists():
            raise SystemExit(f"missing fixture {path}; run `workload.py fetch` (search) or see the task's source (shell)")
        raw = path.read_text(encoding="utf-8")
        if task.get("kind") == "shell":
            (out, level), ms = timed(lambda: reduce_shell(raw, args.compact_trigger), args.repeat)
            found, total, missing = shell_locations(raw, out)
            ef, et = shell_evidence(task, out)
            row.ev_found += ef
            row.ev_total += et
            row.missing += missing
        else:
            bench.resolve_lines(step, raw)
            (out, level), ms = timed(lambda: reduce_search(raw, step, config), args.repeat)
            found, total = search_locations(raw, out, bench.default_path(step))
            kind = detect_kind(raw)
            for ev in task["evidence"]:
                if ev.get("critical") and "path" in ev:  # {"mention"} anchors score live answers only
                    key = f"{ev['path']}:{ev.get('line', '')}"
                    st = bench.evidence_status(ev, out, kind, bench.default_path(step))
                    if ev_status.get(key) != "kept":
                        ev_status[key] = st
        row.raw_tokens += count(raw)
        row.tokens += count(out)
        row.ms += ms
        row.loc_found += found
        row.loc_total += total
        levels.append(level)
    if task.get("kind") != "shell":
        row.ev_found = sum(st == "kept" for st in ev_status.values())
        row.ev_total = len(ev_status)
    row.level = "/".join(levels)
    return row


def pct(x: float) -> str:
    return f"{100 * x:.0f}%"


def aggregate(rows: list[Row]) -> dict:
    raw = sum(r.raw_tokens for r in rows)
    out = sum(r.tokens for r in rows)
    lat = sorted(r.ms for r in rows)
    return {
        "tasks": len(rows),
        "raw": raw,
        "out": out,
        "total_saving": 1 - out / raw if raw else 0.0,
        "task_mean_saving": statistics.mean(r.saving for r in rows) if rows else 0.0,
        "touched": sum(r.tokens != r.raw_tokens for r in rows),
        "ev": (sum(r.ev_found for r in rows), sum(r.ev_total for r in rows)),
        "loc": (sum(r.loc_found for r in rows), sum(r.loc_total for r in rows)),
        "p50_ms": statistics.median(lat) if lat else 0.0,
        "p95_ms": lat[min(len(lat) - 1, int(0.95 * len(lat)))] if lat else 0.0,
    }


def category_mean(spec: dict, rows: list[Row], cats: list[str]) -> float:
    weighted = [(spec["categories"][c]["weight"], aggregate([r for r in rows if r.category == c])["total_saving"]) for c in cats]
    total = sum(w for w, _ in weighted)
    return sum(w * s for w, s in weighted) / total if total else 0.0


def recall_str(pair: tuple[int, int]) -> str:
    found, total = pair
    return f"{found}/{total}" if total else "-"


def render(spec: dict, rows: list[Row], method: str, config: Config, args) -> tuple[str, bool]:
    cats = [c for c in spec["categories"] if any(r.category == c for r in rows)]
    out = [
        f"Tokens: {method}. Search: view={config.view} max_tokens={config.max_tokens} trigger={config.trigger_tokens} "
        f"(lexical ranking, Grep inline rule). Shell: compact trigger {args.compact_trigger}.",
        "",
        "| task | category | raw tok | out tok | saving | level | evidence | locations | ms |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        out.append(
            f"| {r.task} | {r.category} | {r.raw_tokens} | {r.tokens} | {pct(r.saving)} | {r.level} | "
            f"{recall_str((r.ev_found, r.ev_total))} | {recall_str((r.loc_found, r.loc_total))} | {r.ms:.1f} |"
        )
    out += [
        "",
        "| category | group | tasks | touched | raw tok | out tok | total saving | task-mean saving | evidence | locations | p50 ms | p95 ms |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for c in cats:
        a = aggregate([r for r in rows if r.category == c])
        meta = spec["categories"][c]
        out.append(
            f"| {meta['label']} | {meta['group']} | {a['tasks']} | {a['touched']} | {a['raw']} | {a['out']} | {pct(a['total_saving'])} | "
            f"{pct(a['task_mean_saving'])} | {recall_str(a['ev'])} | {recall_str(a['loc'])} | {a['p50_ms']:.1f} | {a['p95_ms']:.1f} |"
        )
    goal = spec["goal"]
    out += ["", "| scope | tasks | raw tok | out tok | total saving | task-mean saving | category-mean saving | evidence | locations |", "|---|---|---|---|---|---|---|---|---|"]
    verdict = {}
    for name, sel in (("search", "search"), ("build/test", "build"), ("all", None)):
        rs = [r for r in rows if sel is None or r.group == sel]
        if not rs:
            continue
        cs = [c for c in cats if sel is None or spec["categories"][c]["group"] == sel]
        a = aggregate(rs)
        cm = category_mean(spec, rs, cs)
        verdict[name] = (cm, a)
        out.append(
            f"| {name} | {a['tasks']} | {a['raw']} | {a['out']} | {pct(a['total_saving'])} | {pct(a['task_mean_saving'])} | "
            f"{pct(cm)} | {recall_str(a['ev'])} | {recall_str(a['loc'])} |"
        )
    cm, a = verdict["all"]
    checks = [
        (f"category-mean saving {pct(cm)} >= {pct(goal['min_saving'])}", cm >= goal["min_saving"]),
        (f"evidence recall {recall_str(a['ev'])}", a["ev"][0] >= goal["evidence_recall"] * a["ev"][1]),
        (f"location recall {recall_str(a['loc'])}", a["loc"][0] >= goal["location_recall"] * a["loc"][1]),
    ]
    ok = all(passed for _, passed in checks)
    out += ["", f"Goal ({'MET' if ok else 'NOT MET'}): " + "; ".join(f"{text} {'ok' if passed else 'FAIL'}" for text, passed in checks)]
    out.append("Offline only: extra turns, re-searches and billed cost need `live.py --workload`.")
    missing = [(r.task, m) for r in rows for m in r.missing]
    if missing:
        out += ["", "Shell lines naming a failure or location that the output dropped:"]
        out += [f"- {t}: `{m[:160]}`" for t, m in missing[:40]]
    return "\n".join(out), ok


def cmd_run(args) -> int:
    spec, tasks = load_workload()
    config = hook_config()
    if args.view:
        max_tokens, trigger = view_defaults(args.view)
        config = Config(max_tokens=max_tokens, trigger_tokens=trigger, view=args.view)
    if args.max_tokens is not None:
        config.max_tokens = args.max_tokens
    if args.trigger_tokens is not None:
        config.trigger_tokens = args.trigger_tokens
    count = bench.TokenCounter(args.tokenizer)
    rows = [
        run_task(t, spec, config, count, args)
        for t in tasks
        if (not args.only or t["id"] in args.only) and (not args.category or t["category"] in args.category)
    ]
    text, ok = render(spec, rows, count.method, config, args)
    print(text)
    if args.jsonl:
        with open(args.jsonl, "w", encoding="utf-8") as fh:
            for r in rows:
                fh.write(json.dumps({**r.__dict__, "saving": r.saving, "tokenizer": count.method}) + "\n")
    return 0 if ok or not args.check else 1


def cmd_fetch(args) -> int:
    _, tasks = load_workload()
    all_repos = repos()
    FIXTURES.mkdir(parents=True, exist_ok=True)
    problems = []
    for task in tasks:
        if "ref" in task or task.get("kind") == "shell" or "repo" not in task or (args.only and task["id"] not in args.only):
            continue
        repo_dir = bench.ensure_repo(task["repo"], all_repos[task["repo"]])
        problems += bench.verify_evidence({**task, "evidence": [e for e in task["evidence"] if "path" in e]}, repo_dir)
        for step, path in steps(task):
            raw = bench.run_search(bench.capture_cmd(step), repo_dir)
            path.write_text(raw, encoding="utf-8")
            print(f"{path.name}: {len(raw)} bytes, {estimate_tokens(raw)} tokens")
    for p in problems:
        print("ANCHOR", p, file=sys.stderr)
    return 1 if problems else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("fetch", help="capture fixtures of workload-only search tasks")
    p.add_argument("--only", nargs="*")
    p = sub.add_parser("run", help="hook defaults over every fixture")
    p.add_argument("--only", nargs="*")
    p.add_argument("--category", nargs="*")
    p.add_argument("--tokenizer", choices=["auto", "api", "chars"], default="chars")
    p.add_argument("--view", choices=["lossless", "coverage", "notes"], help="search view (default: the hook's)")
    p.add_argument("--max-tokens", type=int)
    p.add_argument("--trigger-tokens", type=int)
    p.add_argument("--compact-trigger", type=int, default=DEFAULT_COMPACT_TRIGGER_TOKENS)
    p.add_argument("--repeat", type=int, default=5, help="runs per step for the latency median")
    p.add_argument("--jsonl")
    p.add_argument("--check", action="store_true", help="exit 1 when the goal is not met")
    args = parser.parse_args(argv)
    return {"fetch": cmd_fetch, "run": cmd_run}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
