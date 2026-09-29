"""Multi-search benchmark: session memory on vs off, and hook latency under parallel calls.

  python3 benchmark/sessions.py run [--max-tokens N]   replay benchmark/sessions.json
  python3 benchmark/sessions.py latency [--parallel N] time the real hook process, serial vs parallel

`run` needs the repos cloned by `bench.py fetch`. Each session is the list of
searches an agent runs on one task (narrow, widen context, repeat). Every
search is reduced the way the hook does it (rg --sort=path --with-filename,
lexical ranking by default), once with a fresh output each time ("off") and
once sharing a session store ("on").

Per session it reports: tokens sent to the agent, lines re-sent that an
earlier search already showed, unique raw lines the agent got to see across
the session, and whether the task's critical evidence was shown at least once.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parent / "src"))
sys.path.insert(0, str(ROOT))

from bench import CACHE, TokenCounter, evidence_status, load_spec, split_note, validate_subset  # noqa: E402
from searchslim import Config, Kind, parse, slim  # noqa: E402
from searchslim.rerank import Query, make_scorer  # noqa: E402
from searchslim.session import SessionStore  # noqa: E402

SESSIONS = ROOT / "sessions.json"


def hook_cmd(cmd: list[str]) -> list[str]:
    """What the Bash hook runs: sorted, every line anchored with path:line."""
    return [cmd[0], "--sort=path", "--with-filename", "--line-number", *cmd[1:]]


def run_raw(cmd: list[str], cwd: Path) -> str:
    return subprocess.run(cmd, cwd=cwd, stdin=subprocess.DEVNULL, capture_output=True, text=True, errors="replace").stdout


def _norm(p: str) -> str:
    return p[2:] if p.startswith("./") else p


def body_keys(text: str) -> set[tuple[str, int]]:
    body, _ = split_note(text)
    return {(_norm(ln.path), ln.number) for ln in parse(body, kind=Kind.CONTENT).lines}


def replay(session: dict, task: dict, raws: list[str], config: Config, rerank: str, memory: bool, count) -> dict:
    scorer = make_scorer(rerank) if rerank != "none" else None
    query = Query(task["intent"], task["subtask"], "")
    repo = str(CACHE / session["repo"])
    with tempfile.TemporaryDirectory() as tmp:
        store = SessionStore("bench", root=Path(tmp)) if memory else None
        outs, ms = [], []
        for cmd, raw in zip(session["cmds"], raws):
            q = Query(query.intent, query.subtask, cmd[-2] if len(cmd) > 2 else "")
            start = time.perf_counter()
            out = slim(raw, config=config, scorer=scorer, query=q if scorer else None, session=store, cwd=repo)
            ms.append((time.perf_counter() - start) * 1000)
            bad = validate_subset(out.text, raw)
            assert not bad, f"{session['id']}: invented lines {bad[:2]}"
            outs.append(out.text)
    shown: set = set()
    resent = 0
    for text in outs:
        keys = body_keys(text)
        resent += len(keys & shown)
        shown |= keys
    raw_union = set().union(*(body_keys(r) for r in raws))
    critical = [ev for ev in task["evidence"] if ev.get("critical")]
    found = sum(1 for ev in critical if any(evidence_status(ev, t, Kind.CONTENT) == "kept" for t in outs))
    return {
        "tokens": sum(count(t) for t in outs),
        "resent": resent,
        "unique_shown": len(shown & raw_union),
        "unique_raw": len(raw_union),
        "critical": f"{found}/{len(critical)}",
        "ms": statistics.median(ms),
    }


def cmd_run(args) -> int:
    spec = json.loads(SESSIONS.read_text())
    tasks = {t["id"]: t for t in load_spec()["tasks"]}
    count = TokenCounter(args.tokenizer)
    config = Config(max_tokens=args.max_tokens)
    rows = []
    for session in spec["sessions"]:
        repo = CACHE / session["repo"]
        if not repo.exists():
            print(f"missing {repo}: run `python3 benchmark/bench.py fetch` first", file=sys.stderr)
            return 1
        raws = [run_raw(hook_cmd(c), repo) for c in session["cmds"]]
        task = tasks[session["evidence_from"]]
        raw_tokens = sum(count(r) for r in raws)
        for memory in (False, True):
            r = replay(session, task, raws, config, args.rerank, memory, count)
            rows.append({"session": session["id"], "searches": len(raws), "raw_tokens": raw_tokens, "memory": memory, **r})

    print(f"Budget {args.max_tokens} tokens per search, ranking {args.rerank}, tokens by {count.method}.\n")
    print("| session | searches | raw tokens | memory | tokens sent | lines re-sent | unique lines seen | critical shown | median ms |")
    print("|---|---|---|---|---|---|---|---|---|")
    for r in rows:
        print(
            f"| {r['session']} | {r['searches']} | {r['raw_tokens']} | {'on' if r['memory'] else 'off'} | {r['tokens']} "
            f"| {r['resent']} | {r['unique_shown']}/{r['unique_raw']} | {r['critical']} | {r['ms']:.1f} |"
        )
    for memory in (False, True):
        sel = [r for r in rows if r["memory"] == memory]
        print(
            f"\nTotal, memory {'on' if memory else 'off'}: {sum(r['tokens'] for r in sel)} tokens sent "
            f"(raw {sum(r['raw_tokens'] for r in sel)}), {sum(r['resent'] for r in sel)} lines re-sent, "
            f"{sum(r['unique_shown'] for r in sel)}/{sum(r['unique_raw'] for r in sel)} unique lines seen."
        )
    if args.json:
        Path(args.json).write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    return 0


def cmd_latency(args) -> int:
    """Wall time of `python -m searchslim hook` on a large Grep, one at a time vs N at once."""
    repo = CACHE / "pytest"
    if not repo.exists():
        print(f"missing {repo}: run `python3 benchmark/bench.py fetch` first", file=sys.stderr)
        return 1
    with tempfile.TemporaryDirectory() as tmp:
        env = {**os.environ, "SEARCHSLIM_CACHE_DIR": tmp, "PYTHONPATH": str(ROOT.parent / "src")}

        def call(i: int) -> float:
            event = {
                "hook_event_name": "PreToolUse", "tool_name": "Grep", "cwd": str(repo), "session_id": f"s{i % 2}",
                "tool_input": {"pattern": ["scope", "fixture", "raises", "def "][i % 4], "output_mode": "content", "-C": 1},
            }
            start = time.perf_counter()
            proc = subprocess.run([sys.executable, "-m", "searchslim", "hook"], input=json.dumps(event), capture_output=True, text=True, env=env)
            elapsed = (time.perf_counter() - start) * 1000
            assert proc.returncode == 0 and '"deny"' in proc.stdout, proc.stderr
            return elapsed

        serial = [call(i) for i in range(args.parallel)]
        with ThreadPoolExecutor(args.parallel) as pool:
            start = time.perf_counter()
            par = list(pool.map(call, range(args.parallel)))
            wall = (time.perf_counter() - start) * 1000
    print(f"{args.parallel} Grep calls on pytest, {os.cpu_count()} CPUs")
    print(f"serial:   median {statistics.median(serial):.0f} ms, max {max(serial):.0f} ms, total {sum(serial):.0f} ms")
    print(f"parallel: median {statistics.median(par):.0f} ms, max {max(par):.0f} ms, wall {wall:.0f} ms")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--max-tokens", type=int, default=Config.max_tokens)
    r.add_argument("--rerank", choices=["none", "lexical"], default="lexical")
    r.add_argument("--tokenizer", choices=["auto", "api", "chars"], default="auto")
    r.add_argument("--json", default="", help="also write rows as JSONL here")
    lat = sub.add_parser("latency")
    lat.add_argument("--parallel", type=int, default=8)
    args = p.parse_args()
    return cmd_run(args) if args.cmd == "run" else cmd_latency(args)


if __name__ == "__main__":
    sys.exit(main())
