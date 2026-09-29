"""Command line entry points.

  searchslim filter [opts] < raw_output     reduce output read from stdin
  searchslim run [opts] -- rg -n foo src    run a search command, reduce its stdout
  searchslim run --shell -- 'rg -n foo | grep -v test'   same, for a filter pipeline
  searchslim hook < event.json              Claude Code PreToolUse hook (see hooks.py)
  searchslim install [--user | DIR]         enable the hook in Claude Code settings

`run` keeps the command's exit code and stderr untouched, so it can stand in
for rg/fd/grep/find in scripts and agent shells.

filter/run rank by relevance before cutting (lexical, no deps) unless
`--rerank off` or `SEARCHSLIM_RERANK=off`. Input and output are UTF-8 on every
platform (rg prints UTF-8; the Windows locale codec would garble it).
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys

from . import slim
from .models import Kind
from .rules import Config


def _default_rerank() -> str:
    value = os.environ.get("SEARCHSLIM_RERANK", "lexical").lower()
    return value if value in ("off", "none", "lexical", "claude") else "lexical"


def _decode(data: bytes) -> str:
    return data.decode("utf-8", errors="replace")


def _utf8_stdio() -> None:
    # On Windows, text streams default to the ANSI code page (cp1252 etc.), so
    # Turkish letters and emoji from rg would be garbled on the way out.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def _add_common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--kind", choices=[k.value for k in Kind], help="force the output shape instead of auto-detecting")
    p.add_argument("--max-tokens", type=int, default=Config.max_tokens)
    p.add_argument("--merge-gap", type=int, default=Config.merge_gap)
    p.add_argument("--max-matches-per-file", type=int, default=Config.max_matches_per_file)
    p.add_argument("--max-line-chars", type=int, default=Config.max_line_chars)
    p.add_argument("--default-path", default="", help="path for lines printed without a filename")
    p.add_argument("--stats", action="store_true", help="print reduction stats as JSON on stderr")
    p.add_argument(
        "--rerank",
        choices=["off", "none", "lexical", "claude"],
        default=_default_rerank(),
        help="rank by relevance before cutting (default: $SEARCHSLIM_RERANK or lexical; off = plain rules)",
    )
    p.add_argument("--intent", default="", help="user goal, for --rerank")
    p.add_argument("--subtask", default="", help="agent's current step, for --rerank")
    p.add_argument("--query", default="", help="search pattern, for --rerank")
    p.add_argument("--transcript", default="", help="Claude Code transcript (JSONL) to take intent/subtask from, for --rerank")
    p.add_argument("--session", default="", help="agent session id: don't repeat lines earlier searches in it already showed")


def _config(args: argparse.Namespace) -> Config:
    return Config(
        max_tokens=args.max_tokens,
        merge_gap=args.merge_gap,
        max_matches_per_file=args.max_matches_per_file,
        max_line_chars=args.max_line_chars,
    )


def _emit(raw: str, args: argparse.Namespace) -> None:
    if not raw.strip():
        return
    scorer = query = None
    if args.rerank not in ("off", "none"):
        from .rerank import Query, make_scorer

        scorer = make_scorer(args.rerank)
        if args.transcript:
            from .rerank import query_from_transcript

            query = query_from_transcript(args.transcript, args.query)
        else:
            query = Query(args.intent, args.subtask, args.query)
    session = None
    if args.session:
        from .session import SessionStore, enabled

        if enabled():
            session = SessionStore(args.session)
    reduced = slim(
        raw,
        kind=Kind(args.kind) if args.kind else None,
        config=_config(args),
        default_path=args.default_path,
        scorer=scorer,
        query=query,
        session=session,
        cwd=os.getcwd(),
    )
    sys.stdout.write(reduced.text)
    if reduced.text and not reduced.text.endswith("\n"):
        sys.stdout.write("\n")
    if args.stats:
        sys.stderr.write(json.dumps(reduced.stats) + "\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="searchslim", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_filter = sub.add_parser("filter", help="reduce search output read from stdin")
    _add_common(p_filter)

    p_run = sub.add_parser("run", help="run a search command and reduce its stdout")
    _add_common(p_run)
    p_run.add_argument("--shell", action="store_true", help="run the command (one string) through the shell, e.g. a filter pipeline")
    p_run.add_argument("--no-anchor", action="store_true", help="do not add the flags that keep path:line on every line (rg/grep/git grep)")
    p_run.add_argument("command", nargs=argparse.REMAINDER, help="command to run, after --")

    sub.add_parser("hook", help="Claude Code PreToolUse hook: JSON event on stdin")

    for name, help_text in (("install", "enable the hook in Claude Code settings"), ("uninstall", "remove the hook")):
        p_inst = sub.add_parser(name, help=help_text)
        p_inst.add_argument("project", nargs="?", default=".", help="project directory (default: current)")
        p_inst.add_argument("--user", action="store_true", help="use ~/.claude/settings.json (all projects)")

    p_bench = sub.add_parser("bench-model", help="rules+model entry for benchmark/bench.py --model-cmd (JSON on stdin)")
    p_bench.add_argument("--scorer", choices=["lexical", "claude"], default="lexical")

    args = parser.parse_args(argv)
    _utf8_stdio()

    if args.cmd == "hook":
        from .hooks import main as hook_main

        return hook_main()

    if args.cmd in ("install", "uninstall"):
        from .install import install, settings_path, uninstall

        path = settings_path(args.project, args.user)
        changed = (install if args.cmd == "install" else uninstall)(path)
        state = {"install": ("added to", "already in"), "uninstall": ("removed from", "not in")}[args.cmd]
        print(f"searchslim hook {state[0] if changed else state[1]} {path}")
        return 0

    if args.cmd == "bench-model":
        from .rerank import make_scorer, run_for_benchmark

        text, usage = run_for_benchmark(json.loads(_decode(sys.stdin.buffer.read())), make_scorer(args.scorer))
        sys.stdout.write(text + ("\n" if text and not text.endswith("\n") else ""))
        sys.stderr.write(json.dumps({k: usage[k] for k in ("input_tokens", "output_tokens") if k in usage}) + "\n")
        return 0

    if args.cmd == "filter":
        _emit(_decode(sys.stdin.buffer.read()), args)
        return 0

    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        parser.error("run needs a command, e.g. searchslim run -- rg -n foo")
    if args.shell:
        proc = subprocess.run(" ".join(command), shell=True, capture_output=True)
    else:
        if not args.no_anchor:
            from .rewrite import prepare

            prepared = prepare(command)
            command = prepared.argv
            args.default_path = args.default_path or prepared.default_path
            args.kind = args.kind or prepared.kind
        if not args.query and args.rerank not in ("off", "none"):
            from .rerank import pattern_and_paths

            args.query = pattern_and_paths(command)[0]
        try:
            proc = subprocess.run(command, capture_output=True)
        except FileNotFoundError:
            sys.stderr.write(f"searchslim: command not found: {command[0]}\n")
            return 127
    sys.stderr.write(_decode(proc.stderr))
    _emit(_decode(proc.stdout), args)
    return proc.returncode
