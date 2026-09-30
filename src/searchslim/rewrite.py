"""Rewrite a shell search command so its output goes through searchslim.

Wrapped (optionally after `cd dir &&`, optionally with `2>/dev/null` / `2>&1`):

  rg, grep/egrep/fgrep, git grep     content, file lists and counts
  fd/fdfind, find, git ls-files      path lists
  tree, ls -R                        directory listings
  <one of the above> | head/tail/sort/uniq/grep ...
                                     simple filter pipelines (POSIX shells only)

Anything else with pipes, redirects, command chaining beyond a leading `cd`,
subshells, or side-effect flags is left alone: the rewrite must never change
what a command does, only how much of its stdout is shown.

A single command becomes `searchslim run -- <cmd>`: `run` adds the flags that
keep every line anchored (see `prepare`) after the shell has expanded globs.
A pipeline becomes `searchslim run --shell -- '<pipeline>'` and is run
unchanged; only its final stdout is reduced.

Prefix a command with `SEARCHSLIM=off ` to get raw output.
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

OFF_PREFIX = "SEARCHSLIM=off "

_SEARCH_TOOLS = {"rg", "grep", "egrep", "fgrep", "fd", "fdfind", "find", "git", "tree", "ls"}
_GREP_TOOLS = {"grep", "egrep", "fgrep"}
_UNSAFE_CHARS = set("|;&<>`$(){}\n")
# stderr redirects that do not change stdout; allowed anywhere in a command.
_STDERR_REDIRECT = re.compile(r"(?<!\S)2>\s*(?:/dev/null|&1)(?!\S)")
# find flags that run things or print in custom formats.
_FIND_UNSAFE = {"-exec", "-execdir", "-ok", "-okdir", "-delete", "-fprint", "-fprintf", "-fls", "-printf", "-print0", "-fprint0", "-ls"}
_FD_UNSAFE = {"-x", "--exec", "-X", "--exec-batch", "-0", "--print0", "-l", "--list-details", "--format"}
# rg/grep modes whose output is not path/line oriented, or that we should not second-guess.
_RG_PASSTHROUGH = {"--help", "-h", "--version", "-V", "--type-list", "--pcre2-version", "-0", "--null", "--vimgrep", "--replace", "-r", "-o", "--only-matching", "--passthru", "--stats", "-q", "--quiet"}
_GREP_PASSTHROUGH = {"--help", "--version", "-V", "-Z", "--null", "-z", "--null-data", "-o", "--only-matching", "-q", "--quiet", "--silent"}
_GIT_GREP_PASSTHROUGH = {"-h", "--help", "-O", "--open-files-in-pager", "-z", "--null", "-o", "--only-matching", "-q", "--quiet", "-p", "--show-function", "-W", "--function-context"}
_LS_FILES_PASSTHROUGH = {"-h", "--help", "-z"}
_LS_FILES_NOT_PATHS = {"-s", "--stage", "-t", "-v", "-f", "--debug", "--eol", "--format", "-u", "--unmerged", "--resolve-undo"}
_TREE_PASSTHROUGH = {"-o", "-J", "-X", "-H", "--help", "--version", "-R"}
_RG_NO_LINE_MODES = {"-l", "--files-with-matches", "--files-without-match", "-c", "--count", "--count-matches", "--files", "--json", "-N", "--no-line-number"}
_GREP_NO_LINE_MODES = {"-l", "--files-with-matches", "-L", "--files-without-match", "-c", "--count"}
_GIT_GREP_NO_LINE_MODES = {"-l", "--files-with-matches", "--name-only", "-L", "--files-without-match", "-c", "--count"}
_PATH_MODES = {"-l", "--files-with-matches", "--files-without-match", "-L", "--files", "--name-only"}
_COUNT_MODES = {"-c", "--count", "--count-matches"}
_LINE_NUMBER_FLAGS = {"-n", "--line-number"}
_WITH_FILENAME = {"-H", "--with-filename"}

# rg/grep flags that take a value as the next argument.
_RG_VALUE_FLAGS = {
    "-e", "--regexp", "-f", "--file", "-g", "--glob", "--iglob", "-t", "--type", "-T", "--type-not",
    "--type-add", "--type-clear", "-A", "--after-context", "-B", "--before-context", "-C", "--context",
    "-m", "--max-count", "-M", "--max-columns", "-d", "--max-depth", "--max-filesize", "-E", "--encoding",
    "-j", "--threads", "--sort", "--sortr", "--pre", "--pre-glob", "--ignore-file", "--path-separator",
    "--context-separator", "--field-match-separator", "--field-context-separator", "--colors", "--color",
    "--engine", "--dfa-size-limit", "--regex-size-limit", "--max-columns-preview", "--hostname-bin",
}
_GREP_VALUE_FLAGS = {"-e", "--regexp", "-f", "--file", "-A", "-B", "-C", "-m", "--max-count", "--include", "--exclude", "--exclude-dir", "-d", "-D", "--label", "--color", "--colour"}

# Pipeline stages after the search: filters that keep each line as it is.
_FILTER_TOOLS = {"head", "tail", "sort", "uniq", "grep", "egrep", "fgrep", "rg"}
_HEAD_TAIL_OK = re.compile(r"^(?:-\d+|-n\d*|--lines=\d+|\d+|-q|-v)$")
_SORT_OK = {"-u", "-r", "-n", "-f", "-V", "-s", "-h", "-b", "-d", "-g", "-k", "-t", "--unique", "--reverse", "--numeric-sort", "--ignore-case", "--version-sort", "--stable"}
_UNIQ_OK = {"-u", "-d", "-i", "--unique", "--repeated", "--ignore-case"}
_FILTER_GREP_OK = {"-v", "-i", "-E", "-F", "-w", "-x", "-P", "-e", "-G", "--invert-match", "--ignore-case", "--word-regexp", "--line-regexp", "--fixed-strings", "--extended-regexp", "--regexp"}


def rewrite_command(command: str, runner: str | None = None, run_args: list[str] | None = None, check_path: bool = False) -> str | None:
    """Return the wrapped command, or None when it should run unchanged.

    `check_path`: only wrap when the search tool is installed (the hook sets it,
    so a tool that the agent's shell only knows as an alias still runs as typed).
    """
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

    stages = _split_pipeline(body)
    if not stages or any(_has_unsafe(_STDERR_REDIRECT.sub(" ", s)) for s in stages):
        return None
    try:
        argvs = [shlex.split(_STDERR_REDIRECT.sub(" ", s)) for s in stages]
    except ValueError:
        return None
    if any(not a for a in argvs):
        return None

    argv = argvs[0]
    kind = search_kind(argv)
    if kind is None:
        return None
    if check_path and shutil.which(argv[0]) is None:
        return None

    runner = runner or default_runner()
    opts = "".join(f" {shlex.quote(a)}" for a in run_args or [])
    if len(argvs) == 1:
        return f"{prefix}{runner} run{opts} -- {body}"

    # A pipeline: every later stage must be a line filter, and the shell must be POSIX.
    if os.name != "posix" or not all(_is_line_filter(a) for a in argvs[1:]):
        return None
    if kind == "content" and not _flags(argv) & _LINE_NUMBER_FLAGS:
        kind = "lines"  # piped search output has no line numbers without -n: keep lines opaque
    return f"{prefix}{runner} run{opts} --kind={kind} --shell -- {shlex.quote(body)}"


# Test runners whose output compact.py knows. Wrapping them in `searchslim run
# --compact` compacts the output before Claude Code sees it: a failing run (exit
# != 0) gets no PostToolUse, and output over ~30 KB reaches hooks truncated.
_TEST_RUNNERS = {"pytest", "py.test", "jest", "vitest", "mocha"}
_PYTHONS = {"python", "python3", "py"}
_JS_EXEC = {"npx", "pnpx", "bunx", "pnpm", "yarn", "bun"}
_ENV_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=\S*$")
# Interactive or never-ending modes: captured output would never be shown.
_TEST_NO_WRAP = {"--watch", "--watchAll", "-w", "--pdb", "--trace", "--ui", "watch", "dev", "--help", "-h"}


# `cd x && ` or `cd x; ` before a test command (a quoted path may hold spaces).
_CD_PREFIX = re.compile(r"""^cd\s+(?:'[^']*'|"[^"$`\\]*"|[^\s;&|'"$`(){}<>\\]+)\s*(?:&&|;)\s*""")


def is_test_command(argv: list[str]) -> bool:
    """True for a plain test-runner invocation (pytest, python -m pytest, npx jest,
    npm test, go test, cargo test, dotnet test, uv/poetry run pytest ...)."""
    if not argv or any(a in _TEST_NO_WRAP for a in argv[1:]):
        return False
    name = re.sub(r"\.(exe|cmd|bat)$", "", Path(argv[0].replace("\\", "/")).name.lower())
    rest = argv[1:]
    if name in _TEST_RUNNERS:
        return True
    if name in _PYTHONS or re.fullmatch(r"python3(\.\d+)?", name):
        return rest[:2] == ["-m", "pytest"]
    if name in ("uv", "poetry", "pipenv") and rest[:1] == ["run"]:
        return is_test_command(rest[1:])
    if name in _JS_EXEC or name == "npm":
        words = [a for a in rest if not a.startswith("-")]
        if name != "npm" and words[:1] and words[0] in ("jest", "vitest", "mocha"):
            return True
        return words[:1] in (["test"], ["t"]) or words[:2] == ["run", "test"]
    if name in ("go", "dotnet"):
        return rest[:1] == ["test"]
    if name == "cargo":
        return rest[:1] == ["test"] or rest[:2] == ["nextest", "run"]
    return False


# Build, type-check and lint commands: compact.py groups their repeated
# diagnostics and drops their progress lines.
_BUILD_TOOLS = {"tsc", "vue-tsc", "eslint", "webpack", "mypy", "flake8", "pyright"}
_BUILD_SUBCOMMANDS = {
    "next": {"build", "lint"},
    "vite": {"build"},
    "go": {"build", "vet"},
    "cargo": {"build", "check", "clippy"},
    "dotnet": {"build"},
    "ruff": {"check"},
}
_BUILD_SCRIPTS = {"build", "lint", "typecheck", "type-check", "tsc", "check", "compile"}
_JVM_BUILDS = {"mvn", "mvnw", "gradle", "gradlew"}
_JVM_GOALS = {"compile", "test-compile", "test", "package", "verify", "install", "build", "check", "assemble"}
# Servers and watchers: their output never ends, so it must stream.
_BUILD_NO_WRAP = _TEST_NO_WRAP | {"serve", "start", "preview", "--serve"}


def is_build_command(argv: list[str]) -> bool:
    """True for a plain build, type-check or lint run (tsc, eslint, npm run build,
    pnpm lint, next/vite build, go build/vet, cargo build/check/clippy, dotnet
    build, mvn/gradle goals, mypy, ruff check ...)."""
    if not argv or any(a in _BUILD_NO_WRAP for a in argv[1:]):
        return False
    name = re.sub(r"\.(exe|cmd|bat)$", "", Path(argv[0].replace("\\", "/")).name.lower())
    rest = argv[1:]
    words = [a for a in rest if not a.startswith("-")]
    if name in _BUILD_TOOLS:
        return True
    if name in _BUILD_SUBCOMMANDS:
        return words[:1] != [] and words[0] in _BUILD_SUBCOMMANDS[name] and rest[:1] == words[:1]
    if name in _PYTHONS or re.fullmatch(r"python3(\.\d+)?", name):
        return rest[:1] == ["-m"] and rest[1:2] in (["mypy"], ["flake8"], ["ruff"]) and is_build_command(rest[1:])
    if name in ("uv", "poetry", "pipenv") and rest[:1] == ["run"]:
        return is_build_command(rest[1:])
    if name in _JVM_BUILDS:
        return any(w.split(":")[-1] in _JVM_GOALS for w in words)
    if name in _JS_EXEC or name == "npm":
        if words[:2] and words[0] == "run":
            return words[1:2] != [] and words[1] in _BUILD_SCRIPTS
        if name != "npm" and words and (words[0] in _BUILD_TOOLS or words[0] in ("next", "vite")):
            return is_build_command(rest[rest.index(words[0]):])
        return name in ("pnpm", "yarn", "bun") and words[:1] != [] and words[0] in _BUILD_SCRIPTS
    return False


def rewrite_test_command(command: str, runner: str | None = None, check_path: bool = False) -> str | None:
    """Wrap a plain test-runner or build command in `searchslim run --compact`, or return None.

    Allowed around it: a leading `cd x &&`, leading VAR=value assignments and
    `2>&1`/`2>/dev/null`; no pipes, other redirects, chaining or substitutions.
    `run` keeps the command's exit code.
    """
    stripped = command.strip()
    if not stripped or stripped.startswith(OFF_PREFIX.strip()):
        return None
    prefix, body = "", stripped
    cd = _CD_PREFIX.match(body)
    if cd:
        prefix, body = cd.group(0), body[cd.end():]
    if _has_unsafe(_STDERR_REDIRECT.sub(" ", body)):
        return None
    try:
        argv = shlex.split(_STDERR_REDIRECT.sub(" ", body))
    except ValueError:
        return None
    envs = []
    while argv and _ENV_ASSIGN.match(argv[0]):
        envs.append(argv.pop(0))
    if not (is_test_command(argv) or is_build_command(argv)):
        return None
    if check_path and shutil.which(argv[0]) is None:
        return None
    # Simple VAR=value assignments stay in front, so they reach the runner through `run`.
    env_text = re.match(r"^(\s*[A-Za-z_][A-Za-z0-9_]*=\S*\s+){%d}" % len(envs), body).group(0) if envs else ""
    return f"{prefix}{env_text}{runner or default_runner()} run --compact -- {body[len(env_text):]}"


def search_kind(argv: list[str]) -> str | None:
    """Output shape of a wrappable search command ("content", "paths", "count",
    "lines"), or None when the command must not be wrapped."""
    tool = argv[0]
    if tool not in _SEARCH_TOOLS:
        return None
    flags = _flags(argv)
    if tool == "rg":
        if flags & _RG_PASSTHROUGH:
            return None
        return _mode_kind(flags)
    if tool in _GREP_TOOLS:
        if flags & _GREP_PASSTHROUGH:
            return None
        return _mode_kind(flags)
    if tool in {"fd", "fdfind"}:
        return None if flags & _FD_UNSAFE else "paths"
    if tool == "find":
        return None if set(argv[1:]) & _FIND_UNSAFE else "paths"
    if tool == "git":
        sub = argv[1] if len(argv) > 1 else ""
        sub_flags = _flags(argv[1:])
        if sub == "grep":
            return None if sub_flags & _GIT_GREP_PASSTHROUGH else _mode_kind(sub_flags)
        if sub == "ls-files":
            if sub_flags & _LS_FILES_PASSTHROUGH:
                return None
            return "lines" if sub_flags & _LS_FILES_NOT_PATHS else "paths"
        return None
    if tool == "tree":
        return None if flags & _TREE_PASSTHROUGH or any(a.startswith("--output") for a in argv) else "lines"
    if tool == "ls":
        return "lines" if "-R" in flags or "--recursive" in flags else None
    return None


def _mode_kind(flags: set[str]) -> str:
    if flags & _PATH_MODES:
        return "paths"
    if flags & _COUNT_MODES:
        return "count"
    return "content"


@dataclass
class Prepared:
    argv: list[str]
    default_path: str = ""  # the one file searched, for single-file (pathless) output
    kind: str | None = None  # forced output shape, or None to auto-detect


def prepare(argv: list[str]) -> Prepared:
    """Add the flags that keep every output line anchored, at run time.

    Run after the shell has expanded globs, so it can tell a single-file search
    (printed without a filename, like the tool does on a terminal, which keeps
    small outputs small) from a multi-file one (every line gets its path).
    Piped rg and grep drop line numbers, and rg also drops the filename.
    """
    if not argv:
        return Prepared(argv)
    tool = argv[0]
    kind = search_kind(argv)
    if kind is None:
        return Prepared(argv)
    flags = _flags(argv)
    if tool == "rg":
        # rg searches in parallel, so file order changes run to run; sorting
        # keeps what survives the budget (and the note) reproducible.
        extra = [] if flags & {"--sort", "--sortr"} else ["--sort=path"]
        if flags & _RG_NO_LINE_MODES:
            return Prepared([tool, *extra, *argv[1:]])
        single = _single_file(argv, _RG_VALUE_FLAGS)
        if single and not flags & _WITH_FILENAME:
            return Prepared([tool, *extra, "--line-number", *argv[1:]], default_path=single)
        return Prepared([tool, *extra, "--with-filename", "--line-number", *argv[1:]])
    if tool in _GREP_TOOLS:
        if flags & _GREP_NO_LINE_MODES:
            return Prepared(argv)
        single = _single_file(argv, _GREP_VALUE_FLAGS)
        if single and not flags & _WITH_FILENAME:
            return Prepared([tool, "-n", *argv[1:]], default_path=single)
        return Prepared([tool, "-H", "-n", *argv[1:]])
    if tool == "git" and argv[1] == "grep":
        if _flags(argv[1:]) & _GIT_GREP_NO_LINE_MODES:
            return Prepared(argv)
        return Prepared(["git", "grep", "-n", *argv[2:]])
    return Prepared(argv, kind="lines" if kind == "lines" else None)


def _single_file(argv: list[str], value_flags: set[str]) -> str:
    """The searched path when it is exactly one regular file, else ""."""
    positional, skip, explicit_pattern, after_dashdash = [], False, False, False
    for a in argv[1:]:
        if skip:
            skip = False
            continue
        if after_dashdash:
            positional.append(a)
        elif a == "--":
            after_dashdash = True
        elif a in value_flags:
            skip = True
            explicit_pattern |= a in {"-e", "--regexp", "-f", "--file"}
        elif a.startswith("-") and a != "-":
            if a.startswith(("--regexp=", "--file=")) or (a[:2] in ("-e", "-f") and len(a) > 2 and not a.startswith("--")):
                explicit_pattern = True
        else:
            positional.append(a)
    paths = positional if explicit_pattern else positional[1:]
    if len(paths) == 1 and os.path.isfile(paths[0]):
        return paths[0]
    return ""


def default_runner() -> str:
    # The Bash tool's shell may not have this package on its path, so point at it explicitly.
    package_root = Path(__file__).resolve().parent.parent
    return f"PYTHONPATH={shlex.quote(str(package_root))} {shlex.quote(sys.executable)} -m searchslim"


def _split_pipeline(s: str) -> list[str] | None:
    """Split on unquoted single `|`; None when quotes are unbalanced or `||` is used."""
    parts, cur, quote, escaped = [], [], None, False
    i = 0
    while i < len(s):
        ch = s[i]
        if escaped:
            escaped = False
        elif ch == "\\" and quote != "'":
            escaped = True
        elif quote:
            if ch == quote:
                quote = None
        elif ch in "'\"":
            quote = ch
        elif ch == "|":
            if s[i + 1 : i + 2] in ("|", "&") or (i and s[i - 1] == ">"):
                return None
            parts.append("".join(cur).strip())
            cur = []
            i += 1
            continue
        cur.append(ch)
        i += 1
    if quote or escaped:
        return None
    parts.append("".join(cur).strip())
    return parts if all(parts) else None


def _is_line_filter(argv: list[str]) -> bool:
    """A pipeline stage that only drops, keeps or reorders whole lines, reads only
    stdin and writes nothing but stdout."""
    tool, args = argv[0], argv[1:]
    if tool not in _FILTER_TOOLS:
        return False
    if tool in ("head", "tail"):
        # `-n 20` or `-n20` or `-20`; never `-f` (tail would follow forever) or files.
        ok, skip = True, False
        for a in args:
            if skip:
                skip = False
                ok &= a.lstrip("+-").isdigit()
            elif a == "-n":
                skip = True
            else:
                ok &= bool(_HEAD_TAIL_OK.match(a)) and not a.isdigit()
        return ok and not skip
    if tool == "sort":
        skip = False
        for a in args:
            if skip:
                skip = False
            elif a in ("-k", "-t"):
                skip = True
            elif not (a.startswith("-") and (a in _SORT_OK or a[:2] in ("-k", "-t") or set(a[1:]) <= set("urnfVshbdg"))):
                return False
        return not skip
    if tool == "uniq":
        return all(a in _UNIQ_OK or (a.startswith("-") and set(a[1:]) <= set("udi")) for a in args)
    # grep/rg as a filter: flags from a safe set, one pattern, no files.
    positional, skip, has_e = [], False, False
    for a in args:
        if skip:
            skip = False
        elif a in ("-e", "--regexp"):
            skip = has_e = True
        elif a.startswith("-") and a != "-":
            if a.startswith("--"):
                if a.split("=", 1)[0] not in _FILTER_GREP_OK:
                    return False
            elif not set(a[1:]) <= set("viEFwxPG"):
                return False
        else:
            positional.append(a)
    return not skip and len(positional) == (0 if has_e else 1)


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


if __name__ == "__main__":  # pragma: no cover
    print(rewrite_command(" ".join(sys.argv[1:])))


# --- PowerShell (Claude Code's PowerShell tool on Windows) --------------------

_PS_GCI = {"get-childitem", "gci", "ls", "dir"}
_PS_SLS = {"select-string", "sls"}
# `$(...)`, script blocks, redirects, statement separators, call operators and
# backtick escapes: anything that could run more than a plain search.
_PS_UNSAFE_CHARS = set(";&<>`$(){}@\n")
# Make PowerShell hand native commands UTF-8 and decode their UTF-8 output, so
# rg/searchslim bytes survive a cp1254/cp1252 console.
PS_UTF8 = "$OutputEncoding = [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false); "


def ps_quote(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def rewrite_powershell(command: str, python: str | None = None, run_args: list[str] | None = None, check_path: bool = False) -> str | None:
    """Wrap a PowerShell search command, or return None to leave it unchanged.

    `rg ...`                                 -> searchslim run (same as Bash)
    `Get-ChildItem -Recurse ... [| Select-String ...]`, `Select-String ...`
                                             -> formatted output piped into searchslim filter
    Only plain commands are touched: no variables, subexpressions, script
    blocks, redirects, `;`/`&&` or pipeline stages other than these cmdlets.
    """
    stripped = command.strip()
    if not stripped or "SEARCHSLIM" in stripped:
        return None
    wrapped = _ps_test_command(stripped, python, check_path)
    if wrapped:
        return wrapped
    if not _ps_unquoted(stripped)[1]:
        return None
    stages = _ps_split(stripped)
    if not stages:
        return None
    argvs = [_ps_argv(s) for s in stages]
    if any(not a for a in argvs):
        return None
    runner = f"& {ps_quote((python or sys.executable).replace(chr(92), '/'))} -m searchslim"
    opts = "".join(f" {ps_quote(a)}" for a in run_args or [])

    first = argvs[0][0].lower()
    if first in ("rg", "rg.exe") and len(argvs) == 1:
        argv = ["rg"] + argvs[0][1:]
        if search_kind(argv) is None or (check_path and shutil.which("rg") is None):
            return None
        # No `--` before the command: some PowerShell versions drop it on the way to
        # a native program; `run` takes everything from the first positional on.
        return f"{PS_UTF8}{runner} run{opts} {stripped}"

    names = [a[0].lower() for a in argvs]
    if not all(n in _PS_GCI | _PS_SLS for n in names):
        return None
    has_sls = any(n in _PS_SLS for n in names)
    recurse = any(n in _PS_GCI and any(x.lower().startswith("-rec") for x in a[1:]) for n, a in zip(names, argvs))
    if not (has_sls or recurse):
        return None  # a plain directory listing is small
    if has_sls:
        kind = ""  # MatchInfo prints path:line:text; let the parser detect it
    elif any(x.lower() == "-name" for a in argvs for x in a[1:]):
        kind = " --kind=paths"
    else:
        kind = " --kind=lines"  # Get-ChildItem's table format
    return f"{PS_UTF8}{stripped} | Out-String -Stream -Width 4096 | {runner} filter{opts}{kind}"


_PS_MERGE_STDERR = re.compile(r"\s+2>&1$")
# `Set-Location x; ...`, `cd 'x y' && ...`: a directory change before the test command.
_PS_CD_PREFIX = re.compile(
    r"^(?:set-location|cd|sl|push-location|pushd)\s+(?:-(?:literal)?path\s+)?"
    r"(?:'[^']*'|\"[^\"$`]*\"|[^\s;&|'\"$`(){}<>@]+)\s*(?:;|&&)\s*",
    re.IGNORECASE,
)


def _ps_test_command(stripped: str, python: str | None, check_path: bool) -> str | None:
    """`[cd x; ]pytest ...[ 2>&1]` -> `...; [cd x; ]& '<python>' -m searchslim run --compact pytest ...`."""
    cd = _PS_CD_PREFIX.match(stripped)
    prefix = cd.group(0) if cd else ""
    body = stripped[len(prefix):]
    merge = _PS_MERGE_STDERR.search(body)
    if merge:
        body = body[: merge.start()]
    if not body or not _ps_unquoted(body)[1] or "|" in _ps_unquoted(body)[0]:
        return None
    argv = _ps_argv(body)
    if not (is_test_command(argv) or is_build_command(argv)) or (check_path and shutil.which(argv[0]) is None):
        return None
    runner = f"& {ps_quote((python or sys.executable).replace(chr(92), '/'))} -m searchslim"
    # The encoding assignment goes first: an assignment can't follow `&&`.
    return f"{PS_UTF8}{prefix}{runner} run --compact {body}{' 2>&1' if merge else ''}"


def _ps_unquoted(s: str) -> tuple[str, bool]:
    """Text outside quotes, and whether quoting is balanced and free of unsafe characters.
    PowerShell has no backslash escapes, and `$` inside double quotes interpolates."""
    out, quote = [], None
    for ch in s:
        if quote:
            if ch == quote:
                quote = None
            elif quote == '"' and ch in "$`":
                return "", False
        elif ch in "'\"":
            quote = ch
        else:
            if ch in _PS_UNSAFE_CHARS:
                return "", False
            out.append(ch)
    return "".join(out), quote is None


def _ps_split(s: str) -> list[str] | None:
    parts, cur, quote = [], [], None
    for ch in s:
        if quote:
            if ch == quote:
                quote = None
        elif ch in "'\"":
            quote = ch
        elif ch == "|":
            parts.append("".join(cur).strip())
            cur = []
            continue
        cur.append(ch)
    parts.append("".join(cur).strip())
    return parts if all(parts) else None


def _ps_argv(stage: str) -> list[str]:
    lex = shlex.shlex(stage, posix=True)
    lex.whitespace_split = True
    lex.escape = ""  # backslashes are path separators in PowerShell
    try:
        return list(lex)
    except ValueError:
        return []
