"""Benchmark harness: raw vs rules vs rules+model on benchmark/tasks.json.

  python3 benchmark/bench.py fetch        clone pinned repos, capture raw fixtures, verify evidence anchors
  python3 benchmark/bench.py run          run the modes over the fixtures, print a markdown summary
  python3 benchmark/bench.py stability    rerun unsorted rg N times, count evidence status flips

`run` needs only the committed fixtures, so it works offline. Design and
metric definitions: docs/benchmark-design.md.

rules+model: pass --model-cmd. The command gets one JSON object on stdin
({"intent", "subtask", "cmd", "raw", "rules", "max_tokens"}) and prints the
reduced output on stdout. It may print {"input_tokens": N, "output_tokens": M}
as the last stderr line so its cost is counted. Its output is rejected if any
body line is not a line of the raw input.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import statistics
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parent / "src"))

from searchslim import Config, Kind, detect_kind, estimate_tokens, parse, slim  # noqa: E402
from searchslim.coverage import split_note as _split_note  # noqa: E402
from searchslim.lossless import match_locations  # noqa: E402
from searchslim.rerank import pattern_and_paths  # noqa: E402

TASKS = ROOT / "tasks.json"
FIXTURES = ROOT / "fixtures"
CACHE = Path(os.environ.get("SEARCHSLIM_BENCH_CACHE", Path.home() / ".cache" / "searchslim-bench"))

# USD per million tokens (Anthropic first-party list prices, 2026-09).
AGENT_MODEL, AGENT_IN = "claude-opus-5-5", 4.00
RERANKER_IN, RERANKER_OUT = 1.00, 5.00  # claude-haiku-4-5


# --- tasks and fixtures -------------------------------------------------------


def load_spec(path: Path = TASKS) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def capture_cmd(task: dict) -> list[str]:
    """The task command with a fixed file order, so fixtures are reproducible."""
    cmd = list(task["cmd"])
    if cmd[0] == "rg" and "--sort" not in cmd:
        cmd[1:1] = ["--sort", "path"]
    return cmd


def run_search(cmd: list[str], cwd: Path) -> str:
    # stdin must not be inherited: rg with no path argument would search it.
    proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, errors="replace", stdin=subprocess.DEVNULL)
    return proc.stdout


def default_path(task: dict) -> str:
    return task.get("default_path", "")


def task_steps(task: dict) -> list[dict]:
    """A task is one search (`cmd`) or a sequence (`steps`), like an agent that
    lists files first and then searches one of them. Each step shares the
    task's evidence and has its own fixture."""
    if "steps" not in task:
        return [task]
    return [
        {**task, "cmd": st["cmd"], "default_path": st.get("default_path", ""), "fixture": f"{task['id']}.{i}"}
        for i, st in enumerate(task["steps"])
    ]


def fixture_path(step: dict) -> Path:
    return FIXTURES / f"{step.get('fixture', step['id'])}.txt"


_RANK = {"kept": 0, "recoverable": 1, "lost": 2}


def combine(task: dict, rows: list["Row"]) -> "Row":
    """One row for a multi-step task: costs add up, each evidence takes its best status."""
    if len(rows) == 1:
        return rows[0]
    evidence = {}
    for r in rows:
        for key, status in r.evidence.items():
            if key not in evidence or _RANK[status] < _RANK[evidence[key]]:
                evidence[key] = status
    recover = {key.split(":")[0] for key, st in evidence.items() if st == "recoverable"}
    return Row(
        task["id"], rows[0].mode, sum(r.tokens for r in rows), sum(r.latency_ms for r in rows),
        sum(r.cost_usd for r in rows), evidence, len(recover), sum(r.invalid_lines for r in rows),
    )


def ensure_repo(name: str, repo: dict) -> Path:
    dest = CACHE / name
    if not dest.exists():
        CACHE.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "clone", "-q", "--depth", "1", "--branch", repo["tag"], repo["url"], str(dest)], check=True)
    head = subprocess.run(["git", "-C", str(dest), "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
    if head != repo["commit"]:
        raise SystemExit(f"{name}: expected commit {repo['commit']}, got {head}")
    return dest


def verify_evidence(task: dict, repo_dir: Path) -> list[str]:
    """Check every anchor against the pinned checkout; return problems."""
    problems = []
    for ev in task["evidence"]:
        f = repo_dir / ev["path"]
        if not f.is_file():
            problems.append(f"{task['id']}: missing file {ev['path']}")
            continue
        if "text" not in ev:
            continue
        lines = f.read_text(encoding="utf-8", errors="replace").split("\n")
        if "line" in ev:
            if not (0 < ev["line"] <= len(lines)) or ev["text"] not in lines[ev["line"] - 1]:
                problems.append(f"{task['id']}: {ev['path']}:{ev['line']} does not contain {ev['text']!r}")
        elif not any(ev["text"] in ln for ln in lines):
            problems.append(f"{task['id']}: {ev['path']} does not contain {ev['text']!r}")
    return problems


def resolve_lines(task: dict, raw: str) -> None:
    """Fill in `line` for text-only anchors from the raw output (fixtures carry no repo)."""
    for ev in task["evidence"]:
        if "text" in ev and "line" not in ev:
            for ln in parse(raw, default_path=default_path(task)).lines:
                if _norm(ln.path) == ev["path"] and ev["text"] in ln.text:
                    ev["line"] = ln.number
                    break


# --- evidence scoring -----------------------------------------------------------


def _norm(path: str) -> str:
    return path[2:] if path.startswith("./") else path


def split_note(text: str) -> tuple[str, str]:
    # [searchslim] lines, plus the indented coverage index rows that follow one.
    return _split_note(text)


def evidence_status(ev: dict, output: str, kind: Kind, dpath: str = "") -> str:
    """kept, recoverable or lost, as defined in docs/benchmark-design.md."""
    body, note = split_note(output)
    # Content: parse the whole output, so lines the lossless view writes once
    # for several places ("[searchslim] N matches are this same line") count.
    parsed = parse(output if kind is Kind.CONTENT else body, kind=kind, default_path=dpath)
    path = ev["path"]
    # Single-file output has no filename; the parser leaves path "" for those lines.
    line_path = lambda ln: _norm(ln.path or dpath)  # noqa: E731
    if kind is Kind.CONTENT:
        seen_paths = {line_path(ln) for ln in parsed.lines}
        if "line" in ev and any(line_path(ln) == path and ln.number == ev["line"] for ln in parsed.lines):
            return "kept"
        # An L2 projection writes a name instead of the line text; its location still counts.
        if "line" in ev and (path, ev["line"]) in match_locations(output, dpath):
            return "kept"
        if "line" not in ev and path in seen_paths:
            return "kept"
    elif kind is Kind.PATHS:
        if path in {_norm(p) for p in parsed.paths}:
            return "kept"
    elif path in {_norm(c.path) for c in parsed.counts}:
        return "kept"
    body_paths = {line_path(ln) for ln in parsed.lines} | {_norm(p) for p in parsed.paths} | {_norm(c.path) for c in parsed.counts}
    note_paths = {_norm(re.sub(r":[\d,]+$", "", tok.rstrip(",.:;()"))) for tok in note.split()}
    if path in note_paths or path in body_paths:
        return "recoverable"
    parent = os.path.dirname(path)
    while parent:
        if parent in note_paths or parent + "/" in note_paths:
            return "recoverable"
        parent = os.path.dirname(parent)
    return "lost"


def validate_subset(output: str, raw: str) -> list[str]:
    """Body lines of a model's output that are not lines of the raw input.

    Lossless output (path once per file, repeated lines written once) is
    compared line by line after parsing: path, number and text must match a
    raw line (text up to indentation for a line written once for many places).
    """
    kind = detect_kind(raw)
    if kind is Kind.CONTENT and detect_kind(split_note(output)[0]) is Kind.CONTENT:
        raw_keys = {(_norm(ln.path), ln.number): ln.text for ln in parse(raw, kind=Kind.CONTENT).lines}
        bad = []
        for ln in parse(output, kind=Kind.CONTENT).lines:
            text = raw_keys.get((_norm(ln.path), ln.number))
            clipped = ln.text.split("…[+")[0]
            if text is None or not (text == ln.text or text.strip() == ln.text or ("…[+" in ln.text and text.lstrip().startswith(clipped.lstrip()))):
                bad.append(f"{ln.path}:{ln.number}:{ln.text}")
        return bad
    if kind is Kind.PATHS:
        raw_paths = {_norm(p) for p in parse(raw, kind=Kind.PATHS).paths}
        return [p for p in parse(split_note(output)[0], kind=Kind.PATHS).paths if _norm(p) not in raw_paths]
    raw_lines = set(raw.splitlines())
    raw_lines |= {_norm(ln) for ln in raw_lines}  # `./x` printed as `x` is the same path
    bad = []
    for ln in split_note(output)[0].splitlines():
        if ln in raw_lines:
            continue
        if "…[+" in ln and any(r.startswith(ln.split("…[+")[0]) for r in raw_lines):
            continue  # line clipped by the rules layer
        bad.append(ln)
    return bad


# --- metrics --------------------------------------------------------------------


class TokenCounter:
    """Anthropic count_tokens when the SDK and credentials are there, else chars/4."""

    def __init__(self, mode: str):
        self.method = "chars/4"
        self.client = None
        self.base = 0
        if mode in ("auto", "api"):
            try:
                import anthropic

                self.client = anthropic.Anthropic()
                self.base = self._api(".")
                self.method = f"count_tokens({AGENT_MODEL})"
            except Exception as exc:  # no SDK, no credentials, no network
                if mode == "api":
                    raise SystemExit(f"count_tokens unavailable: {exc}")
                self.client = None

    def _api(self, text: str) -> int:
        resp = self.client.messages.count_tokens(model=AGENT_MODEL, messages=[{"role": "user", "content": text}])
        return resp.input_tokens

    def __call__(self, text: str) -> int:
        if not text:
            return 0
        if self.client is None:
            return estimate_tokens(text)
        return max(self._api(text) - self.base, 0)


@dataclass
class Row:
    task: str
    mode: str
    tokens: int
    latency_ms: float
    cost_usd: float
    evidence: dict = field(default_factory=dict)  # "path:line" -> status, critical only
    extra_searches: int = 0
    invalid_lines: int = 0

    @property
    def lost(self) -> int:
        return sum(1 for s in self.evidence.values() if s == "lost")


def score(task: dict, mode: str, raw: str, output: str, ms: float, tokens: int, model_cost: float = 0.0) -> Row:
    kind = detect_kind(raw)
    dpath = default_path(task)
    evidence = {}
    recover_files = set()
    for ev in task["evidence"]:
        if not ev.get("critical"):
            continue
        key = f"{ev['path']}:{ev['line']}" if "line" in ev else ev["path"]
        status = evidence_status(ev, output, kind, dpath)
        evidence[key] = status
        if status == "recoverable":
            recover_files.add(ev["path"])
    cost = tokens * AGENT_IN / 1e6 + model_cost
    return Row(task["id"], mode, tokens, ms, cost, evidence, len(recover_files))


def timed(fn, repeat: int) -> tuple[object, float]:
    times, out = [], None
    for _ in range(repeat):
        t0 = time.perf_counter()
        out = fn()
        times.append((time.perf_counter() - t0) * 1000)
    return out, statistics.median(times)


def run_model(cmd: str, task: dict, raw: str, rules_text: str, config: Config) -> tuple[str, float, float]:
    payload = json.dumps(
        {"intent": task["intent"], "subtask": task["subtask"], "cmd": task["cmd"], "raw": raw, "rules": rules_text, "max_tokens": config.max_tokens, "trigger_tokens": config.trigger_tokens, "view": config.view}
    )
    t0 = time.perf_counter()
    proc = subprocess.run(cmd, shell=True, input=payload, capture_output=True, text=True)
    ms = (time.perf_counter() - t0) * 1000
    if proc.returncode != 0:
        raise SystemExit(f"model command failed on {task['id']}: {proc.stderr.strip()[-500:]}")
    cost = 0.0
    tail = proc.stderr.strip().splitlines()[-1:] if proc.stderr.strip() else []
    if tail:
        try:
            usage = json.loads(tail[0])
            cost = usage.get("input_tokens", 0) * RERANKER_IN / 1e6 + usage.get("output_tokens", 0) * RERANKER_OUT / 1e6
        except (ValueError, AttributeError):
            pass
    return proc.stdout, ms, cost


# --- commands -------------------------------------------------------------------


def cmd_fetch(args: argparse.Namespace) -> int:
    spec = load_spec()
    FIXTURES.mkdir(exist_ok=True)
    problems = []
    for task in spec["tasks"]:
        if args.only and task["id"] not in args.only:
            continue
        repo_dir = ensure_repo(task["repo"], spec["repos"][task["repo"]])
        problems += verify_evidence(task, repo_dir)
        kept: dict[int, bool] = {}
        for step in task_steps(task):
            raw = run_search(capture_cmd(step), repo_dir)
            fixture_path(step).write_text(raw, encoding="utf-8")
            resolve_lines(step, raw)
            kind = detect_kind(raw)
            for i, ev in enumerate(task["evidence"]):
                kept[i] = kept.get(i, False) or evidence_status(ev, raw, kind, default_path(step)) == "kept"
            print(f"{fixture_path(step).stem}: {len(raw)} bytes")
        for i, ev in enumerate(task["evidence"]):
            if ev.get("critical") and not kept[i]:
                problems.append(f"{task['id']}: critical evidence {ev['path']}:{ev.get('line', '')} is not in the raw output")
    for p in problems:
        print("ANCHOR", p, file=sys.stderr)
    return 1 if problems else 0


def cmd_run(args: argparse.Namespace) -> int:
    spec = load_spec()
    config = Config(max_tokens=args.max_tokens, trigger_tokens=args.trigger_tokens, view=args.view)
    count = TokenCounter(args.tokenizer)
    modes = ["raw", "rules"] + (["rules+model"] if args.model_cmd else [])
    rows: list[Row] = []
    for task in spec["tasks"]:
        if args.only and task["id"] not in args.only:
            continue
        per_mode: dict[str, list[Row]] = {m: [] for m in modes}
        for step in task_steps(task):
            fixture = fixture_path(step)
            if not fixture.exists():
                raise SystemExit(f"missing fixture {fixture}; run `bench.py fetch` first")
            raw = fixture.read_text(encoding="utf-8")
            resolve_lines(step, raw)
            per_mode["raw"].append(score(step, "raw", raw, raw, 0.0, count(raw)))
            pattern = pattern_and_paths(step["cmd"])[0] if step.get("cmd") else ""
            reduced, ms = timed(lambda: slim(raw, config=config, default_path=default_path(step), pattern=pattern), args.repeat)
            row = score(step, "rules", raw, reduced.text, ms, count(reduced.text))
            row.invalid_lines = len(validate_subset(reduced.text, raw))
            per_mode["rules"].append(row)
            if args.model_cmd:
                if reduced.text.rstrip("\n") == raw.rstrip("\n"):
                    out, mms, mcost = reduced.text, ms, 0.0  # passed through unchanged: model not called
                else:
                    out, mms, mcost = run_model(args.model_cmd, step, raw, reduced.text, config)
                    mms += ms
                row = score(step, "rules+model", raw, out, mms, count(out), mcost)
                row.invalid_lines = len(validate_subset(out, raw))
                per_mode["rules+model"].append(row)
        rows += [combine(task, per_mode[m]) for m in modes]

    if args.jsonl:
        with open(args.jsonl, "w", encoding="utf-8") as fh:
            for r in rows:
                fh.write(json.dumps({**r.__dict__, "tokenizer": count.method}) + "\n")
    print(render(rows, modes, count.method))
    bad = [r for r in rows if r.invalid_lines]
    for r in bad:
        print(f"INVALID {r.task}: {r.invalid_lines} lines not in raw input", file=sys.stderr)
    return 1 if bad else 0


def render(rows: list[Row], modes: list[str], method: str) -> str:
    out = [f"Tokens: {method}. Cost: agent input at ${AGENT_IN}/MTok ({AGENT_MODEL}), k=1.", ""]
    out.append("| task | mode | tokens | ms | kept | recoverable | lost | extra searches |")
    out.append("|---|---|---|---|---|---|---|---|")
    for r in rows:
        st = list(r.evidence.values())
        flag = f" ({r.invalid_lines} invalid)" if r.invalid_lines else ""
        out.append(
            f"| {r.task} | {r.mode}{flag} | {r.tokens} | {r.latency_ms:.1f} | {st.count('kept')}/{len(st)} | "
            f"{st.count('recoverable')} | {st.count('lost')} | {r.extra_searches} |"
        )
    out += ["", "| mode | tokens | vs raw | cost $ (k=1) | cost $ (k=10) | p95 ms | kept | recoverable | lost | tasks with loss | extra searches |"]
    out.append("|---|---|---|---|---|---|---|---|---|---|---|")
    raw_total = sum(r.tokens for r in rows if r.mode == "raw") or 1
    for m in modes:
        rs = [r for r in rows if r.mode == m]
        st = [s for r in rs for s in r.evidence.values()]
        tok = sum(r.tokens for r in rs)
        cost1 = sum(r.cost_usd for r in rs)
        agent = tok * AGENT_IN / 1e6
        cost10 = cost1 + 9 * agent  # the search output is re-read on later turns; model cost is paid once
        lat = sorted(r.latency_ms for r in rs)
        p95 = lat[min(len(lat) - 1, int(0.95 * len(lat)))] if lat else 0.0
        out.append(
            f"| {m} | {tok} | {100 * tok / raw_total:.0f}% | {cost1:.4f} | {cost10:.4f} | {p95:.1f} | "
            f"{st.count('kept')}/{len(st)} | {st.count('recoverable')} | {st.count('lost')} | "
            f"{sum(1 for r in rs if r.lost)} | {sum(r.extra_searches for r in rs)} |"
        )
    return "\n".join(out)


def cmd_stability(args: argparse.Namespace) -> int:
    spec = load_spec()
    config = Config(max_tokens=args.max_tokens)
    print("| task | runs | distinct outputs | runs with lost evidence |")
    print("|---|---|---|---|")
    for task in spec["tasks"]:
        if "cmd" not in task or task["cmd"][0] != "rg" or (args.only and task["id"] not in args.only):
            continue
        repo_dir = ensure_repo(task["repo"], spec["repos"][task["repo"]])
        outputs, lost_runs = set(), 0
        for _ in range(args.runs):
            raw = run_search(task["cmd"], repo_dir)  # rg's own, unsorted order
            resolve_lines(task, raw)
            text = slim(raw, config=config, default_path=default_path(task)).text
            outputs.add(text)
            if score(task, "rules", raw, text, 0.0, 0).lost:
                lost_runs += 1
        print(f"| {task['id']} | {args.runs} | {len(outputs)} | {lost_runs} |")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("fetch", help="clone repos, capture fixtures, verify anchors")
    p.add_argument("--only", nargs="*")

    p = sub.add_parser("run", help="run modes over the fixtures")
    p.add_argument("--only", nargs="*")
    p.add_argument("--max-tokens", type=int, default=Config.max_tokens)
    p.add_argument("--trigger-tokens", type=int, default=0, help="reduce only outputs above this (0 = --max-tokens; the hook uses 6000)")
    p.add_argument("--tokenizer", choices=["auto", "api", "chars"], default="auto")
    p.add_argument("--repeat", type=int, default=20, help="runs per task for the rules latency median")
    p.add_argument("--model-cmd", help="reranker command for the rules+model mode")
    p.add_argument("--view", choices=["notes", "coverage", "lossless"], default="notes", help="over-budget layout (the hook defaults to lossless, with --max-tokens 7000 --trigger-tokens 1500)")
    p.add_argument("--jsonl", help="also write per-task rows to this file")

    p = sub.add_parser("stability", help="count output changes across unsorted rg runs")
    p.add_argument("--only", nargs="*")
    p.add_argument("--runs", type=int, default=10)
    p.add_argument("--max-tokens", type=int, default=Config.max_tokens)

    args = parser.parse_args(argv)
    return {"fetch": cmd_fetch, "run": cmd_run, "stability": cmd_stability}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
