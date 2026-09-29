"""Command line entry points.

  searchslim filter [opts] < raw_output     reduce output read from stdin
  searchslim run [opts] -- rg -n foo src    run a search command, reduce its stdout
  searchslim hook < event.json              Claude Code PreToolUse hook (see hooks.py)

`run` keeps the command's exit code and stderr untouched, so it can stand in
for rg/fd/grep/find in scripts and agent shells.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys

from . import slim
from .models import Kind
from .rules import Config


def _add_common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--kind", choices=[k.value for k in Kind], help="force the output shape instead of auto-detecting")
    p.add_argument("--max-tokens", type=int, default=Config.max_tokens)
    p.add_argument("--merge-gap", type=int, default=Config.merge_gap)
    p.add_argument("--max-matches-per-file", type=int, default=Config.max_matches_per_file)
    p.add_argument("--max-line-chars", type=int, default=Config.max_line_chars)
    p.add_argument("--default-path", default="", help="path for lines printed without a filename")
    p.add_argument("--stats", action="store_true", help="print reduction stats as JSON on stderr")


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
    reduced = slim(raw, kind=Kind(args.kind) if args.kind else None, config=_config(args), default_path=args.default_path)
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
    p_run.add_argument("command", nargs=argparse.REMAINDER, help="command to run, after --")

    sub.add_parser("hook", help="Claude Code PreToolUse hook: JSON event on stdin")

    args = parser.parse_args(argv)

    if args.cmd == "hook":
        from .hooks import main as hook_main

        return hook_main()

    if args.cmd == "filter":
        _emit(sys.stdin.read(), args)
        return 0

    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        parser.error("run needs a command, e.g. searchslim run -- rg -n foo")
    proc = subprocess.run(command, capture_output=True, text=True, errors="replace")
    sys.stderr.write(proc.stderr)
    _emit(proc.stdout, args)
    return proc.returncode
