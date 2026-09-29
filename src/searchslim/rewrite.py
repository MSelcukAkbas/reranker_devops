"""Rewrite a shell search command so its output goes through searchslim.

Only plain, single search commands are touched (`rg ...`, `grep -rn ...`,
`fd ...`, `find ...`, optionally after `cd dir &&`). Anything with pipes,
redirects, command chaining beyond a leading `cd`, subshells, or side-effect
flags is left alone: the rewrite must never change what a command does, only
how much of its stdout is shown.

Prefix a command with `SEARCHSLIM=off ` to get raw output.
"""

from __future__ import annotations

import shlex
import sys
from pathlib import Path

OFF_PREFIX = "SEARCHSLIM=off "

_SEARCH_TOOLS = {"rg", "grep", "egrep", "fgrep", "fd", "fdfind", "find"}
_UNSAFE_CHARS = set("|;&<>`$(){}\n")
# find flags that run things or print in custom formats.
_FIND_UNSAFE = {"-exec", "-execdir", "-ok", "-okdir", "-delete", "-fprint", "-fprintf", "-fls", "-printf", "-print0", "-fprint0", "-ls"}
_FD_UNSAFE = {"-x", "--exec", "-X", "--exec-batch", "-0", "--print0", "-l", "--list-details", "--format"}
# rg/grep modes whose output is not path/line oriented, or that we should not second-guess.
_RG_PASSTHROUGH = {"--help", "-h", "--version", "-V", "--type-list", "--pcre2-version", "-0", "--null", "--vimgrep", "--replace", "-r", "-o", "--only-matching", "--passthru", "--stats", "-q", "--quiet"}
_GREP_PASSTHROUGH = {"--help", "--version", "-V", "-Z", "--null", "-z", "--null-data", "-o", "--only-matching", "-q", "--quiet", "--silent"}
_RG_NO_LINE_MODES = {"-l", "--files-with-matches", "--files-without-match", "-c", "--count", "--count-matches", "--files", "--json", "-N", "--no-line-number"}
_GREP_NO_LINE_MODES = {"-l", "--files-with-matches", "-L", "--files-without-match", "-c", "--count"}


def rewrite_command(command: str, runner: str | None = None, run_args: list[str] | None = None) -> str | None:
    """Return the wrapped command, or None when it should run unchanged."""
    stripped = command.strip()
    if not stripped or stripped.startswith(OFF_PREFIX.strip()):
        return None

    prefix = ""
    body = stripped
    if body.startswith("cd ") and "&&" in body:
        cd_part, _, rest = body.partition("&&")
        if _has_unsafe(cd_part) or "&&" in rest:
            return None
        prefix = cd_part.strip() + " && "
        body = rest.strip()

    if _has_unsafe(body):
        return None
    try:
        argv = shlex.split(body)
    except ValueError:
        return None
    if not argv or argv[0] not in _SEARCH_TOOLS:
        return None

    extra = _extra_flags(argv)
    if extra is None:
        return None

    # Keep the user's own text after the tool name: re-quoting it would stop the
    # shell from expanding unquoted globs and ~ exactly as before.
    rest = body[len(argv[0]):] if body.startswith(argv[0]) else None
    if rest is None:
        return None
    head = " ".join([argv[0], *extra])
    runner = runner or default_runner()
    opts = "".join(f" {shlex.quote(a)}" for a in run_args or [])
    return f"{prefix}{runner} run{opts} -- {head}{rest}"


def default_runner() -> str:
    # The Bash tool's shell may not have this package on its path, so point at it explicitly.
    package_root = Path(__file__).resolve().parent.parent
    return f"PYTHONPATH={shlex.quote(str(package_root))} {shlex.quote(sys.executable)} -m searchslim"


def _has_unsafe(s: str) -> bool:
    # Quoted text is fine (rg 'a|b'); only shell syntax outside quotes counts.
    text = _unquoted(s)
    return text is None or any(ch in _UNSAFE_CHARS for ch in text)


def _unquoted(s: str) -> str | None:
    """`s` with quoted parts removed; None if quotes are unbalanced."""
    out, quote, escaped = [], None, False
    for ch in s:
        if escaped:
            escaped = False
            out.append("_")  # an escaped char is literal, never shell syntax
        elif ch == "\\" and quote != "'":
            escaped = True
        elif quote:
            if ch == quote:
                quote = None
            elif quote == '"' and ch in "`$":
                out.append(ch)  # still expanded inside double quotes
        elif ch in "'\"":
            quote = ch
        else:
            out.append(ch)
    return None if quote or escaped else "".join(out)


def _flags(argv: list[str]) -> set[str]:
    flags = set()
    for a in argv[1:]:
        if a == "--":
            break
        if a.startswith("--"):
            flags.add(a.split("=", 1)[0])
        elif a.startswith("-") and len(a) > 1:
            flags.add(a)
            if a[1:].isalpha():  # combined short flags: -rn -> -r, -n
                flags.update(f"-{c}" for c in a[1:])
    return flags


def _extra_flags(argv: list[str]) -> list[str] | None:
    """Flags to add so every output line keeps its path:line anchor; None = do not wrap."""
    tool = argv[0]
    flags = _flags(argv)
    if tool == "rg":
        if flags & _RG_PASSTHROUGH:
            return None
        # rg searches in parallel, so file order changes run to run; sorting
        # keeps what survives the budget (and the note) reproducible.
        extra = [] if flags & {"--sort", "--sortr"} else ["--sort=path"]
        # Piped rg drops line numbers and, for one file, the filename.
        return extra if flags & _RG_NO_LINE_MODES else [*extra, "--with-filename", "--line-number"]
    if tool in {"grep", "egrep", "fgrep"}:
        if flags & _GREP_PASSTHROUGH:
            return None
        return [] if flags & _GREP_NO_LINE_MODES else ["-H", "-n"]
    if tool in {"fd", "fdfind"}:
        return None if flags & _FD_UNSAFE else []
    if tool == "find":
        return None if set(argv[1:]) & _FIND_UNSAFE else []
    return None


if __name__ == "__main__":  # pragma: no cover
    print(rewrite_command(" ".join(sys.argv[1:])))
