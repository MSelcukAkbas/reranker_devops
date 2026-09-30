"""Command line entry points.

  searchslim filter [opts] < raw_output     reduce output read from stdin
  searchslim run [opts] -- rg -n foo src    run a search command, reduce its stdout
  searchslim run --shell -- 'rg -n foo | grep -v test'   same, for a filter pipeline
  searchslim compact [opts] < test_output    drop passing-test/progress noise from test/build output
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
from .rules import VIEWS, Config, view_defaults, view_from_env


def _version() -> str:
    try:
        from importlib.metadata import version

        return version("searchslim")
    except Exception:
        return "unknown"


def _default_trigger() -> int | None:
    value = os.environ.get("SEARCHSLIM_TRIGGER_TOKENS", "")
    return int(value) if value.isdigit() else None


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
    p.add_argument(
        "--max-tokens",
        type=int,
        default=None,
        help="budget: above it matches are dropped (default: $SEARCHSLIM_MAX_TOKENS, else 4800 for lossless, 2000 otherwise)",
    )
    p.add_argument(
        "--trigger-tokens",
        type=int,
        default=_default_trigger(),
        help="change only outputs above this many tokens (default: $SEARCHSLIM_TRIGGER_TOKENS, else 1000 for lossless, 6000 otherwise)",
    )
    p.add_argument(
        "--view",
        choices=list(VIEWS),
        default=None,
        help="lossless = every match kept, repetition removed, dropping only when still over --max-tokens; coverage = index of every matching file + selected evidence; notes = trailing not-shown note (default: $SEARCHSLIM_VIEW or lossless)",
    )
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
    view = args.view or view_from_env()
    max_tokens, trigger = view_defaults(view)
    env_max = os.environ.get("SEARCHSLIM_MAX_TOKENS", "")
    if args.max_tokens is not None:
        max_tokens = args.max_tokens
    elif env_max.isdigit():
        max_tokens = int(env_max)
    return Config(
        max_tokens=max_tokens,
        trigger_tokens=trigger if args.trigger_tokens is None else args.trigger_tokens,
        merge_gap=args.merge_gap,
        max_matches_per_file=args.max_matches_per_file,
        max_line_chars=args.max_line_chars,
        view=view,
    )


def _write(text: str, stream=None) -> None:
    """Write UTF-8 bytes directly, so no console/locale codec can fail on them."""
    stream = stream or sys.stdout
    stream.flush()
    buf = getattr(stream, "buffer", None)
    if buf is None:  # e.g. a StringIO in tests
        stream.write(text)
        return
    buf.write(text.encode("utf-8", errors="replace"))
    buf.flush()


def _emit(raw: str, args: argparse.Namespace) -> None:
    """Reduce and print `raw`. Any failure prints `raw` unchanged (fail open)."""
    if not raw.strip():
        return
    try:
        text, stats = _reduce(raw, args)
    except Exception as exc:  # never swallow the search result
        sys.stderr.write(f"searchslim: {type(exc).__name__}: {exc}; showing raw output\n")
        text, stats = raw, None
    _write(text + ("\n" if text and not text.endswith("\n") else ""))
    if args.stats and stats is not None:
        _write(json.dumps(stats) + "\n", sys.stderr)


def _reduce(raw: str, args: argparse.Namespace):
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
        from .session import BASH_VISIBLE_CHARS, SessionStore, enabled

        if enabled():
            # `run` output reaches the agent as a Bash/PowerShell result.
            session = SessionStore(args.session, visible_chars=BASH_VISIBLE_CHARS)
    reduced = slim(
        raw,
        kind=Kind(args.kind) if args.kind else None,
        config=_config(args),
        default_path=args.default_path,
        scorer=scorer,
        query=query,
        session=session,
        cwd=os.getcwd(),
        pattern=args.query,
    )
    return reduced.text, reduced.stats


def _decode_output(data: bytes) -> str:
    """UTF-8, else the locale's codec (a Windows console program may print cp1254)."""
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        import locale

        return data.decode(locale.getpreferredencoding(False) or "utf-8", errors="replace")


def _run_compact(command: list[str]) -> int:
    """Run a test/build command, compact stdout and stderr, keep its exit code.

    Compacting here, before Claude Code sees the output, covers what a
    PostToolUse hook cannot: failing runs (no PostToolUse on exit != 0) and
    output Claude Code truncates at ~30 KB before hooks see it.
    """
    import shutil

    from .compact import DEFAULT_COMPACT_TRIGGER_TOKENS, compact
    from .rules import estimate_tokens

    # A bare `npx`/`npm` is `npx.cmd` on Windows, which CreateProcess only runs by full path.
    exe = shutil.which(command[0])
    argv = [exe or command[0]] + command[1:]
    env = dict(os.environ)
    env.setdefault("PYTHONIOENCODING", "utf-8")  # pytest & co. would print in the console code page
    try:
        proc = subprocess.run(argv, capture_output=True, env=env)
    except FileNotFoundError:
        sys.stderr.write(f"searchslim: command not found: {command[0]}\n")
        return 127
    out, err = _decode_output(proc.stdout), _decode_output(proc.stderr)
    value = os.environ.get("SEARCHSLIM_COMPACT_TRIGGER_TOKENS", "")
    trigger = int(value) if value.isdigit() else DEFAULT_COMPACT_TRIGGER_TOKENS
    if os.environ.get("SEARCHSLIM_COMPACT", "").lower() != "off" and estimate_tokens(out) + estimate_tokens(err) > trigger:
        out, err = (_compact_or_raw(compact, text) for text in (out, err))
    _write(err, sys.stderr)
    _write(out)
    return proc.returncode


def _compact_or_raw(compact, text: str) -> str:
    try:
        result = compact(text, trigger_tokens=0)
    except Exception:  # fail open: the raw output
        result = None
    return result.text if result is not None else text


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="searchslim", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--version", action="version", version=f"searchslim {_version()}")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_filter = sub.add_parser("filter", help="reduce search output read from stdin")
    _add_common(p_filter)

    p_run = sub.add_parser("run", help="run a search command and reduce its stdout")
    _add_common(p_run)
    p_run.add_argument("--shell", action="store_true", help="run the command (one string) through the shell, e.g. a filter pipeline")
    p_run.add_argument("--no-anchor", action="store_true", help="do not add the flags that keep path:line on every line (rg/grep/git grep)")
    p_run.add_argument("--compact", action="store_true", help="the command is a test/build run: compact its stdout and stderr (compact.py) instead of reducing search output")
    p_run.add_argument("command", nargs=argparse.REMAINDER, help="command to run, after --")

    p_compact = sub.add_parser("compact", help="compact test/build output read from stdin (see compact.py)")
    p_compact.add_argument("--trigger-tokens", type=int, default=None, help="compact only above this many tokens (default 2000)")
    p_compact.add_argument("--stats", action="store_true", help="print stats as JSON on stderr")

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
        _write(text + ("\n" if text and not text.endswith("\n") else ""))
        sys.stderr.write(json.dumps({k: usage[k] for k in ("input_tokens", "output_tokens") if k in usage}) + "\n")
        return 0

    if args.cmd == "compact":
        from .compact import DEFAULT_COMPACT_TRIGGER_TOKENS, compact

        raw = _decode(sys.stdin.buffer.read())
        trigger = DEFAULT_COMPACT_TRIGGER_TOKENS if args.trigger_tokens is None else args.trigger_tokens
        try:
            result = compact(raw, trigger_tokens=trigger)
        except Exception as exc:  # fail open: the raw output
            sys.stderr.write(f"searchslim: {type(exc).__name__}: {exc}; showing raw output\n")
            result = None
        _write(result.text if result else raw)
        if args.stats:
            _write(json.dumps(result.stats if result else {"unchanged": True}) + "\n", sys.stderr)
        return 0

    if args.cmd == "filter":
        _emit(_decode(sys.stdin.buffer.read()), args)
        return 0

    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        parser.error("run needs a command, e.g. searchslim run -- rg -n foo")
    if args.compact:
        return _run_compact(command)
    if args.shell:
        proc = subprocess.run(" ".join(command), shell=True, capture_output=True)
    else:
        if not args.no_anchor:
            from .rewrite import prepare

            prepared = prepare(command)
            command = prepared.argv
            args.default_path = args.default_path or prepared.default_path
            args.kind = args.kind or prepared.kind
        if not args.query:
            from .rerank import pattern_and_paths

            args.query = pattern_and_paths(command)[0]
        try:
            proc = subprocess.run(command, capture_output=True)
        except FileNotFoundError:
            sys.stderr.write(f"searchslim: command not found: {command[0]}\n")
            return 127
    _write(_decode(proc.stderr), sys.stderr)
    _emit(_decode(proc.stdout), args)
    return proc.returncode
