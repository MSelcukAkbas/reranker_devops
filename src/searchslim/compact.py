"""Compact test and build output: drop passing/progress noise, keep failures.

Output-aware, not command-aware: a rule only runs when the output itself shows
the tool that printed it (a pytest/jest/vitest/mocha/go/cargo/dotnet summary
line, or many cargo/pip/maven/dotnet progress lines), so an agent's own summary
script or any other stdout is left alone.

What is dropped:
  - status lines of passing and skipped tests (`t.py::test_x PASSED`, `✓ name`,
    `--- PASS: TestX`, `test x ... ok`, `Passed Name [2 ms]`), and pytest
    progress lines made only of `.`/`s`/`x`;
  - lines under a jest `PASS <file>` header and pytest's `PASSES` section
    (captured output of passing tests), except lines that mention an error,
    failure, warning or exception;
  - dependency/build progress lines (cargo `Compiling ...`, pip `Collecting ...`,
    maven `Downloading ...`, dotnet `Restored ...`, `X -> bin/X.dll`);
  - runs of 5+ consecutive lines identical apart from timestamps (log
    repetition): the first and last line stay, with a `[searchslim] N similar
    lines not shown` line between them;
  - coverage table rows at 100% (jest/istanbul, pytest-cov), maven surefire
    lines of passing test classes, gradle `> Task` and vite/webpack asset lines;
  - repeated compiler/linter diagnostics (tsc, ESLint, MSBuild/dotnet, gcc/clang,
    rustc, go vet, mypy, ruff/flake8): a diagnostic whose message (and code
    frame, line numbers aside) repeats at 3+ places is shown once, followed by
    `[searchslim] N more places with this same diagnostic (<message>): path
    pos pos; path pos` naming every other location; one printed twice at the
    same place (MSBuild repeats warnings in its summary) is shown once.

Everything else stays verbatim and in order: failures, tracebacks, assertion
diffs, errors, warnings, compiler diagnostics and the summary counts. Every
diagnostic location of the raw output is either a kept line or listed in a
`more places` note. A single
trailing `[searchslim] not shown: ...` line counts what was dropped. Any error
or a small saving returns None, so the caller keeps the raw output.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

from .rules import NOTE_PREFIX, estimate_tokens

# Every rule only drops passing/progress noise or lists the places of a repeat,
# so mid-sized outputs (a 1k-token failed build) are worth compacting too.
DEFAULT_COMPACT_TRIGGER_TOKENS = 500
# Below this saving the raw output is kept: a compacted result that is barely
# smaller only adds a note to read.
MIN_SAVED_RATIO = 0.1
MIN_PROGRESS_LINES = 10  # a build-progress family must be this common to count
MIN_REPEAT_RUN = 5

# Lines never dropped by section rules (jest PASS suites, pytest PASSES).
PROTECT_RE = re.compile(r"(?i)(error|fail|exception|traceback|panic|warn|assert)")

# --- test runners -----------------------------------------------------------

PYTEST_DETECT = re.compile(
    r"^=+ (test session starts|.*\b(passed|failed|errors?|skipped|no tests ran)\b.* in [\d.]+s.*) =+$"
)
PYTEST_PASS = re.compile(
    r"^(\S+::\S.*\s(PASSED|SKIPPED)(\s+\(.*\))?(\s+\[\s*\d+%\])?"
    r"|(\[gw\d+\] )?\[\s*\d+%\] (PASSED|SKIPPED) \S+::\S.*"
    r"|PASSED \S+::\S.*)$"
)
PYTEST_PROGRESS = re.compile(r"^(\S+\.py )?[.sx]+(\s+\[\s*\d+%\])?$")
PYTEST_PROGRESS_ANY = re.compile(r"^(\S+\.py )?[.sxXFE]+(\s+\[\s*\d+%\])?$")
PYTEST_SUMMARY_FAIL = re.compile(r"^(FAILED|ERROR) \S+::")
# warnings summary: `  /src/a.py:5: DeprecationWarning: msg` under its test ids
PYTEST_WARNING = re.compile(r"^  (?P<path>\S.*?):(?P<line>\d+): (?P<msg>\w*Warning\b.*)$")
PYTEST_SECTION = re.compile(r"^=+ (.+?) =+$")
PYTEST_TEST_HEADER = re.compile(r"^_+ .+ _+$")

JS_DETECT = re.compile(r"^(Tests:\s+\d|Test Suites:\s+\d|\s*Test Files\s+\d|\s+\d+ passing \()")
JS_PASS = re.compile(r"^\s*[✓✔√↓○] ")
JEST_SUITE = re.compile(r"^\s*(PASS|FAIL)\s+\S")

GO_DETECT = re.compile(r"^(\s*--- (PASS|FAIL|SKIP): |(ok|FAIL)\s+\S+\s+(\(cached\)|[\d.]+s))")
GO_PASS = re.compile(r"^(\s*--- (PASS|SKIP): |ok\s+\S+\s+(\(cached\)|[\d.]+s)|\?\s+\S+\s+\[no test files\])")
# `=== RUN X` names the test whose log lines follow; dropped only when nothing follows.
GO_HEADER = re.compile(r"^\s*=== (RUN|PAUSE|CONT|NAME)\s")

CARGO_TEST_DETECT = re.compile(r"^test result: ")
CARGO_TEST_PASS = re.compile(r"^test .+ \.\.\. (ok|ignored)$")

DOTNET_DETECT = re.compile(r"^((Passed|Failed)!\s+- |Total tests: |\s*Test Run (Successful|Failed))")
DOTNET_PASS = re.compile(r"^\s+(Passed|Skipped) \S.*$")

# --- build / dependency progress (label, pattern) ----------------------------

PROGRESS = (
    (
        "cargo progress",
        re.compile(
            r"^\s+(Compiling|Checking|Downloaded|Downloading|Fresh|Locking|Adding|Updating|Documenting|Installing|Installed|Unpacking) \S+ v?\d"
        ),
    ),
    (
        "pip progress",
        re.compile(
            r"^\s*(Requirement already satisfied: |Collecting \S|Downloading \S|Using cached \S|Obtaining \S|"
            r"Building wheel|Created wheel|Stored in directory:|Preparing metadata|Getting requirements|"
            r"Installing build dependencies|━+)"
        ),
    ),
    ("maven progress", re.compile(r"^(\[INFO\] )?Download(ing|ed) from \S+: ")),
    ("gradle progress", re.compile(r"^> Task :\S+( (UP-TO-DATE|NO-SOURCE|FROM-CACHE|SKIPPED))?$")),
    # vite `dist/assets/index-x.js   12.3 kB │ gzip: 4.1 kB`, webpack `asset main.js 1.2 KiB [emitted]`
    ("bundle asset", re.compile(r"^(\S*[./]\S*\s+[\d.,]+ k?B( │ .*)?|asset \S+ [\d.]+ (bytes|KiB|MiB) .*)$")),
    (
        "dotnet build progress",
        re.compile(r"^\s+(Determining projects to restore|All projects are up-to-date|Restored \S|\S+ -> \S+\.(dll|exe)$)"),
    ),
)

# Other families, matched only after their tool's own marker line.
SUREFIRE_DETECT = re.compile(r"^(\[INFO\] )?Tests run: \d+, Failures: \d+, Errors: \d+, Skipped: \d+(, Time elapsed|$)")
SUREFIRE_PASS = re.compile(r"^(\[INFO\] )?Tests run: \d+, Failures: 0, Errors: 0, Skipped: \d+, Time elapsed: .* - in (\S+)$")
SUREFIRE_RUNNING = re.compile(r"^(\[INFO\] )?Running (\S+)$")

COVERAGE_ISTANBUL_HEAD = re.compile(r"^\s*File\s*\|\s*% Stmts\s*\|")
COVERAGE_ISTANBUL_FULL = re.compile(r"^(?!\s*All files\b)[^|]*\S[^|]*(\|\s*100\s*){4}\|\s*\|?\s*$")
COVERAGE_PYTEST_HEAD = re.compile(r"^Name\s+Stmts\s+Miss\b")
COVERAGE_PYTEST_FULL = re.compile(r"^(?!TOTAL\b)\S+(\s+\d+)+\s+100%\s*$")

# Timestamps are masked when comparing lines for log repetition; nothing else
# is, so lines that differ in real data (ids, counts, values) never collapse.
TIMESTAMP_RE = re.compile(
    r"\d{4}-\d\d-\d\d([T ]\d\d:\d\d(:\d\d)?([.,]\d+)?(Z|[+-]\d\d:?\d\d)?)?|\d\d:\d\d:\d\d([.,]\d+)?"
)


@dataclass
class Compacted:
    text: str
    stats: dict = field(default_factory=dict)


def compact(raw: str, trigger_tokens: int = DEFAULT_COMPACT_TRIGGER_TOKENS) -> Compacted | None:
    """The compacted output, or None when it should pass unchanged."""
    raw_tokens = estimate_tokens(raw)
    if raw_tokens <= trigger_tokens:
        return None
    lines = raw.splitlines()
    drops: dict[int, str] = {}
    tools: list[str] = []
    for name, rule in RUNNERS:
        found = rule(lines)
        if found:
            tools.append(name)
            for i, label in found.items():
                drops.setdefault(i, label)
    for label, pattern in PROGRESS:
        hits = [i for i, ln in enumerate(lines) if pattern.match(ln)]
        if len(hits) >= MIN_PROGRESS_LINES:
            tools.append(label.split()[0])
            for i in hits:
                drops.setdefault(i, f"{label} lines")

    diag_drops, notes = _group_diagnostics(lines)
    if diag_drops or notes:
        tools.append("diagnostics")
        for i, label in diag_drops.items():
            drops.setdefault(i, label)
    warn_drops, warn_notes = _pytest_warnings(lines)
    if warn_drops:
        tools.append("pytest-warnings")
        for i, label in warn_drops.items():
            drops.setdefault(i, label)
        for i, extra in warn_notes.items():
            notes.setdefault(i, []).extend(extra)

    kept: list[tuple[float, str]] = []
    for i, ln in enumerate(lines):
        if i not in drops:
            kept.append((i, ln))
        # A note never takes part in a repeat run: its index is not an integer.
        kept.extend((i + 0.5, note) for note in notes.get(i, ()))
    body, repeated = _collapse_repeats(kept)

    counts: dict[str, int] = {}
    for label in drops.values():
        counts[label] = counts.get(label, 0) + 1
    if repeated:
        counts["similar repeated lines"] = repeated
    if not counts:
        return None
    parts = ", ".join(f"{n} {label}" for label, n in counts.items())
    body.append(f"{NOTE_PREFIX} not shown: {parts}.")
    text = "\n".join(body) + ("\n" if raw.endswith("\n") else "")
    tokens = estimate_tokens(text)
    if tokens > raw_tokens * (1 - MIN_SAVED_RATIO):
        return None
    return Compacted(
        text=text,
        stats={"raw_tokens": raw_tokens, "tokens": tokens, "tools": tools, "dropped": counts},
    )


def _pytest(lines: list[str]) -> dict[int, str]:
    if not any(PYTEST_DETECT.match(ln) for ln in lines):
        return {}
    drops = {}
    section = ""
    passes: list[list[int]] = []  # PASSES section header, then one group per test header
    for i, ln in enumerate(lines):
        header = PYTEST_SECTION.match(ln)
        if header:
            section = header.group(1).strip()
            if section == "PASSES":
                passes.append([i])
            continue
        if PYTEST_PASS.match(ln):
            drops[i] = "passing/skipped test lines"
        elif section == "test session starts" and ln.strip() and PYTEST_PROGRESS.match(ln):
            # Progress lines only appear before the first report section; a
            # line of dots inside captured output is data.
            drops[i] = "all-pass progress lines"
        elif section == "PASSES":
            if PYTEST_TEST_HEADER.match(ln):
                passes.append([i])
            else:
                passes[-1].append(i)
    _drop_groups(lines, passes, drops, "passing test output lines")
    _drop_failing_progress(lines, drops)
    return drops


def _drop_failing_progress(lines: list[str], drops: dict[int, str]) -> None:
    """Progress lines with F/E go too when `short test summary info` names every
    one of those failures and errors (same counts), so they tell nothing more."""
    section = ""
    progress: list[int] = []
    letters = {"F": 0, "E": 0}
    listed = {"FAILED": 0, "ERROR": 0}
    for i, ln in enumerate(lines):
        header = PYTEST_SECTION.match(ln)
        if header:
            section = header.group(1).strip()
            continue
        if section == "test session starts" and ln.strip() and PYTEST_PROGRESS_ANY.match(ln):
            progress.append(i)
            dots = ln.split(" [")[0].rstrip().split(" ")[-1]
            letters["F"] += dots.count("F")
            letters["E"] += dots.count("E")
        elif section == "short test summary info":
            m = PYTEST_SUMMARY_FAIL.match(ln)
            if m:
                listed[m.group(1)] += 1
    if letters["F"] + letters["E"] and letters["F"] == listed["FAILED"] and letters["E"] == listed["ERROR"]:
        for i in progress:
            drops.setdefault(i, "progress lines (failures listed in the summary)")


def _pytest_warnings(lines: list[str]) -> tuple[dict[int, str], dict[int, list[str]]]:
    """pytest's warnings summary: a warning whose message repeats at 3+ places is
    shown once (test ids, location, source line); the others follow it as rows
    `  path:line test-id | source line` (`::name` = a test in that same file,
    source without its indentation), so nothing but layout changes. Any block
    that does not read as `ids, location line, one indented source line` leaves
    the section untouched."""
    drops: dict[int, str] = {}
    notes: dict[int, list[str]] = {}
    try:
        start = next(i for i, ln in enumerate(lines) if PYTEST_SECTION.match(ln) and "warnings summary" in ln)
    except StopIteration:
        return drops, notes
    end = next((i for i in range(start + 1, len(lines)) if PYTEST_SECTION.match(lines[i]) or lines[i].startswith("-- Docs: ")), len(lines))
    blocks = []  # (first, last, ids, path, line, message, source)
    i = start + 1
    while i < end:
        if not lines[i].strip():
            i += 1
            continue
        first, ids = i, []
        while i < end and lines[i].strip() and not lines[i][0].isspace():
            ids.append(lines[i])
            i += 1
        m = PYTEST_WARNING.match(lines[i]) if i < end else None
        if not ids or not m:
            return {}, {}
        i += 1
        source = []
        while i < end and lines[i].strip() and lines[i][0].isspace():
            source.append(lines[i])
            i += 1
        blocks.append((first, i - 1, ids, m["path"], m["line"], m["msg"], source))
    groups: dict[str, list] = {}
    for b in blocks:
        groups.setdefault(b[5], []).append(b)
    for message, group in groups.items():
        rest = group[1:]
        if len(group) < MIN_GROUP or any(len(b[6]) != 1 or not b[6][0].startswith("    ") for b in rest):
            continue
        paths = sorted({b[3] for b in rest})
        prefix = ""
        if len(paths) > 1:
            common = os.path.commonprefix(paths)
            prefix = common[: max(common.rfind("/"), common.rfind("\\")) + 1]
        under = f", under {prefix}" if prefix else ""
        rows = [
            f"  {b[3][len(prefix):]}:{b[4]} {' '.join(_short_test_id(t, b[3]) for t in b[2])} | {b[6][0].strip()}"
            for b in rest
        ]
        head = f"{NOTE_PREFIX} {len(rest)} more places with this same warning ({message}){under}, as path:line test-id | source line:"
        if len(head) + sum(len(r) + 1 for r in rows) >= sum(len(lines[k]) + 1 for b in rest for k in range(b[0], b[1] + 2)):
            continue
        for b in rest:
            for k in range(b[0], b[1] + 1):
                drops[k] = "repeated warning lines"
            if b[1] + 1 < end and not lines[b[1] + 1].strip():
                drops[b[1] + 1] = "repeated warning lines"
        notes.setdefault(group[0][1], []).extend([head, *rows])
    return drops, notes


def _short_test_id(test_id: str, path: str) -> str:
    """`a/test_x.py::test_y` -> `::test_y` when the warning's own file is a/test_x.py."""
    file, sep, name = test_id.partition("::")
    norm = path.replace("\\", "/")
    if sep and file and (norm == file or norm.endswith("/" + file)):
        return "::" + name
    return test_id


def _js(lines: list[str]) -> dict[int, str]:
    if not any(JS_DETECT.match(ln) for ln in lines):
        return {}
    drops = {}
    suites: list[list[int]] = []  # jest `PASS <file>` header and the lines under it
    suite_indent = -1  # indent of the current PASS header, -1 outside one
    for i, ln in enumerate(lines):
        indent = len(ln) - len(ln.lstrip())
        if JEST_SUITE.match(ln):
            suite_indent = indent if ln.lstrip().startswith("PASS") else -1
            if suite_indent >= 0:
                suites.append([i])
            continue
        if suite_indent >= 0 and ln.strip() and indent <= suite_indent:
            suite_indent = -1
        if JS_PASS.match(ln):
            drops[i] = "passing/skipped test lines"
        elif suite_indent >= 0:
            suites[-1].append(i)
    _drop_groups(lines, suites, drops, "passing suite lines")
    _drop_jest_summary_repeats(lines, drops)
    return drops


def _drop_jest_summary_repeats(lines: list[str], drops: dict[int, str]) -> None:
    """Jest's `Summary of all failing tests` repeats failure details printed above
    it; a repeated block keeps its `FAIL <file>` line, its identical body goes."""
    try:
        start = lines.index("Summary of all failing tests")
    except ValueError:
        return
    heads = [i for i in range(start + 1, len(lines)) if JEST_SUITE.match(lines[i])]
    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("Test Suites:")), len(lines))
    for n, head in enumerate(heads):
        stop = min(heads[n + 1] if n + 1 < len(heads) else end, end)
        body = lines[head + 1 : stop]
        while body and not body[-1].strip():
            body.pop()
        if not body:
            continue
        for e in range(start):
            if lines[e] == lines[head] and lines[e + 1 : e + 1 + len(body)] == body:
                for k in range(head + 1, head + 1 + len(body)):
                    drops[k] = "failure lines repeated in jest's summary"
                break


def _drop_groups(lines: list[str], groups: list[list[int]], drops: dict[int, str], label: str) -> None:
    """Drop each group (header index, then body indices) of passing-test output.

    Body lines that mention an error, failure, warning or exception stay, and
    so does their group's header (and a leading section header), so a kept
    line is never shown under the wrong test.
    """
    section_kept = False
    for n, (head, *body) in enumerate(groups):
        keep = [i for i in body if PROTECT_RE.search(lines[i])]
        for i in body:
            if i not in keep and i not in drops:
                drops[i] = label
        if keep:
            section_kept = True
        else:
            drops[head] = label
    # A PASSES section header opens the list; keep it when any group stayed.
    if groups and section_kept and PYTEST_SECTION.match(lines[groups[0][0]]):
        drops.pop(groups[0][0], None)


def _simple(detect: re.Pattern, passing: re.Pattern):
    def rule(lines: list[str]) -> dict[int, str]:
        if not any(detect.match(ln) for ln in lines):
            return {}
        return {i: "passing/skipped test lines" for i, ln in enumerate(lines) if passing.match(ln)}

    return rule


def _go(lines: list[str]) -> dict[int, str]:
    if not any(GO_DETECT.match(ln) for ln in lines):
        return {}
    drops = {}
    for i, ln in enumerate(lines):
        if GO_PASS.match(ln):
            drops[i] = "passing/skipped test lines"
        elif GO_HEADER.match(ln):
            nxt = lines[i + 1] if i + 1 < len(lines) else ""
            if GO_HEADER.match(nxt) or GO_PASS.match(nxt) or GO_DETECT.match(nxt):
                drops[i] = "passing/skipped test lines"
    return drops


def _surefire(lines: list[str]) -> dict[int, str]:
    if not any(SUREFIRE_DETECT.match(ln) for ln in lines):
        return {}
    drops = {}
    for i, ln in enumerate(lines):
        passed = SUREFIRE_PASS.match(ln)
        if passed:
            drops[i] = "passing/skipped test lines"
            running = SUREFIRE_RUNNING.match(lines[i - 1]) if i else None
            if running and running.group(2) == passed.group(2):
                drops[i - 1] = "passing/skipped test lines"
    return drops


def _coverage(lines: list[str]) -> dict[int, str]:
    """Rows of fully covered files in a coverage table (the totals row stays)."""
    drops = {}
    for head, full in ((COVERAGE_ISTANBUL_HEAD, COVERAGE_ISTANBUL_FULL), (COVERAGE_PYTEST_HEAD, COVERAGE_PYTEST_FULL)):
        if any(head.match(ln) for ln in lines):
            drops.update((i, "fully covered coverage rows") for i, ln in enumerate(lines) if full.match(ln))
    return drops


RUNNERS = (
    ("pytest", _pytest),
    ("js", _js),
    ("go", _go),
    ("cargo", _simple(CARGO_TEST_DETECT, CARGO_TEST_PASS)),
    ("dotnet", _simple(DOTNET_DETECT, DOTNET_PASS)),
    ("maven", _surefire),
    ("coverage", _coverage),
)


def _collapse_repeats(kept: list[tuple[float, str]]) -> tuple[list[str], int]:
    """Keep the first and last line of each run of timestamp-only-different lines."""
    out: list[str] = []
    hidden = 0
    j = 0
    while j < len(kept):
        key = _repeat_key(kept[j][1])
        k = j + 1
        # A run must also be contiguous in the input, so dropped lines between
        # two kept ones never make them look adjacent.
        while k < len(kept) and key and _repeat_key(kept[k][1]) == key and kept[k][0] == kept[k - 1][0] + 1:
            k += 1
        run = k - j
        if run >= MIN_REPEAT_RUN:
            out.append(kept[j][1])
            out.append(f"{NOTE_PREFIX} {run - 2} similar lines not shown")
            out.append(kept[k - 1][1])
            hidden += run - 2
        else:
            out.extend(ln for _, ln in kept[j:k])
        j = k
    return out, hidden


def _repeat_key(line: str) -> str:
    return TIMESTAMP_RE.sub("T", line.rstrip()) if line.strip() else ""


# --- compiler / linter diagnostics -------------------------------------------

MIN_GROUP = 3  # a message must repeat at this many places to be grouped
ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_PATH = r"(?:[A-Za-z]:[\\/])?[^\s:(][^:(]*?"
_SEVERITY = r"(?:fatal error|error|warning|info|information|message|note|hint|remark)\b"
# tsc `a.ts(3,5): error TS2322: ...`, MSBuild `A.cs(3,5): warning CS8618: ... [p.csproj]`
DIAG_PAREN = re.compile(rf"^\s*(?P<path>{_PATH})(?P<pos>\(\d+(?:,\d+){{0,3}}\))\s?:\s*(?P<msg>{_SEVERITY}.*)$")
# tsc --pretty / pyright `a.ts:3:5 - error TS2322: ...`
DIAG_DASH = re.compile(rf"^\s*(?P<path>{_PATH}):(?P<pos>\d+:\d+) - (?P<msg>{_SEVERITY}.*)$")
# gcc/clang/go/mypy/ruff `a.c:3:5: warning: ...`, `a.py:3: error: ...`, `a.py:1:1: F401 ...`
DIAG_COLON = re.compile(rf"^\s*(?P<path>{_PATH}):(?P<pos>\d+(?::\d+)?):\s?(?P<msg>\S.*)$")
DIAG_COLON_MSG = re.compile(rf"^(?:{_SEVERITY}|[A-Z]{{1,5}}\d{{2,5}}\b)")
TSC_CODE = re.compile(r"^(error|warning) TS\d+:")
PATH_LIKE = re.compile(r"[\\/]|\.[A-Za-z]\w*$")
RUST_HEAD = re.compile(r"^(?:error|warning)(?:\[\w+\])?: \S")
RUST_LOC = re.compile(rf"^\s*--> (?P<path>{_PATH}):(?P<pos>\d+:\d+)$")
ESLINT_ROW = re.compile(r"^\s+(?P<pos>\d+:\d+)\s+(?P<msg>(?:error|warning)\s+\S.*)$")
# Lines that head the diagnostics under them: gcc `a.c: In function 'f':`, go `# pkg`.
CONTEXT_LINE = re.compile(r"^(# \S+|\S.*: (In (member )?function|In constructor|In destructor|At top level|In instantiation of)\b.*:)$")
PIPE_GUTTER = re.compile(r"^\s*\d*\s*\|")  # gcc/rustc code frame `  12 |`
NUMBERED_GUTTER = re.compile(r"^\s*\d+\s*\|")
DIGIT_GUTTER = re.compile(r"^\d+ ")  # tsc --pretty code frame `12 const x`
MARKER_RUN = re.compile(r"[~^]+|-{2,}\^?|(?<=\s)-(?=\s|$)")  # column markers `~~~`, `^^^`, `----^`


@dataclass
class _Diag:
    start: int
    end: int  # exclusive
    path: str
    pos: str
    key: str  # message plus the rest of the record, line numbers masked
    ctx: int | None  # the context line it sits under (eslint file, gcc function, go package)


def _head(line: str):
    """(path, pos, message, style) of a one-line diagnostic head, or None.
    style says which lines may continue it (see `_continues`)."""
    m = DIAG_DASH.match(line)
    style = "frame"
    if m is None:
        m = DIAG_PAREN.match(line)
        # tsc chains messages on indented lines; MSBuild/cl heads stand alone
        # (the indented lines after them are progress and summary counts).
        style = "indent" if m and TSC_CODE.match(m["msg"]) else "alone"
    if m is None:
        m = DIAG_COLON.match(line)
        style = "indent"
        if m and not (DIAG_COLON_MSG.match(m["msg"]) or m["path"].endswith(".go")):
            m = None
    if m is None or not PATH_LIKE.search(m["path"]):
        return None
    return m["path"], m["pos"], m["msg"], style


def _continues(lines: list[str], j: int, style: str) -> bool:
    """Whether line j belongs to the record above it: an indented continuation or
    code frame line, a `note:` about it, or (style "frame": tsc --pretty, rustc)
    a blank line before more of its code frame."""
    if style == "alone":
        return False
    ln = lines[j]
    head = _head(ln)
    if head:
        return head[2].startswith("note")
    if RUST_HEAD.match(ln):
        return False
    nxt = lines[j + 1] if j + 1 < len(lines) else ""
    if not ln.strip():
        return style == "frame" and bool(nxt.strip()) and not _head(nxt) and _continues(lines, j + 1, style)
    if ln[0].isspace() or PIPE_GUTTER.match(ln):
        return True
    return style == "frame" and bool(DIGIT_GUTTER.match(ln) and nxt[:1].isspace())


def _masked(body: list[str]) -> str:
    """The rest of a record as compared across places: its own rustc `-->` line and
    quoted source lines (they differ with the place) left out, gutters removed.
    Caret/label lines stay, so rustc's `expected X, found Y` still tells apart."""
    out = []
    for k, ln in enumerate(body):
        if (k == 0 and RUST_LOC.match(ln)) or NUMBERED_GUTTER.match(ln) or DIGIT_GUTTER.match(ln):
            continue
        ln = MARKER_RUN.sub("^", PIPE_GUTTER.sub("", ln, count=1)).strip()
        if ln.strip("^|"):  # marker-only lines just point at the column
            out.append(ln)
    return "\n".join(out)


def _diagnostics(lines: list[str]) -> list[_Diag]:
    recs: list[_Diag] = []
    ctx: int | None = None
    eslint_file = False
    i, n = 0, len(lines)
    while i < n:
        ln = lines[i]
        head = _head(ln)
        rust = None if head else (RUST_HEAD.match(ln) and i + 1 < n and RUST_LOC.match(lines[i + 1]))
        if head or rust:
            path, pos, msg, style = head if head else (rust["path"], rust["pos"], ln, "frame")
            j = i + 1
            while j < n and _continues(lines, j, style):
                j += 1
            recs.append(_Diag(i, j, path, pos, msg + "\n" + _masked(lines[i + 1 : j]), ctx))
            i = j
            continue
        row = ESLINT_ROW.match(ln) if eslint_file else None
        if row:
            msg = re.sub(r"\s{2,}", "  ", row["msg"].strip())
            recs.append(_Diag(i, i + 1, lines[ctx].strip(), row["pos"], msg, ctx))
        elif ln.strip() and not ln[0].isspace() and i + 1 < n and ESLINT_ROW.match(lines[i + 1]):
            ctx, eslint_file = i, True  # an ESLint (stylish) file heading
        elif CONTEXT_LINE.match(ln):
            ctx, eslint_file = i, False
        else:
            ctx, eslint_file = None, False
        i += 1
    return recs


def _group_diagnostics(lines: list[str]) -> tuple[dict[int, str], dict[int, list[str]]]:
    """Lines to drop and notes to add after given lines, for repeated diagnostics.

    The first diagnostic of each repeated message stays whole; the others are
    dropped and their locations listed in one note after it (for ESLint, after
    its file's list). A context line whose diagnostics are all dropped goes too.
    """
    plain = [ANSI_RE.sub("", ln) for ln in lines]
    recs = _diagnostics(plain)
    drops: dict[int, str] = {}
    notes: dict[int, list[str]] = {}
    if len(recs) < 2:
        return drops, notes

    def drop(rec: _Diag, label: str) -> None:
        for k in range(rec.start, rec.end):
            drops[k] = label
        # A multi-line record's separating blank line goes with it.
        if rec.end - rec.start > 1 and rec.end < len(plain) and not plain[rec.end].strip():
            drops[rec.end] = label

    seen: set[tuple[str, str, str]] = set()
    groups: dict[str, list[_Diag]] = {}
    for rec in recs:
        ident = (rec.path, rec.pos, rec.key)
        if ident in seen:
            drop(rec, "duplicate diagnostic lines")
            continue
        seen.add(ident)
        groups.setdefault(rec.key, []).append(rec)

    section_end: dict[int, int] = {}
    for rec in recs:
        if rec.ctx is not None:
            section_end[rec.ctx] = rec.end - 1
    for key, group in groups.items():
        if len(group) < MIN_GROUP:
            continue
        first, rest = group[0], group[1:]
        note = _places_note(key.split("\n", 1)[0], rest)
        if len(note) >= sum(len(plain[k]) + 1 for rec in rest for k in range(rec.start, rec.end)):
            continue  # listing the places would not be shorter
        for rec in rest:
            drop(rec, "repeated diagnostic lines")
        eslint = first.end - first.start == 1 and ESLINT_ROW.match(plain[first.start])
        after = section_end[first.ctx] if eslint and first.ctx is not None else first.end - 1
        notes.setdefault(after, []).append(note)

    # Context lines left with no diagnostic under them.
    under: dict[int, list[_Diag]] = {}
    for rec in recs:
        if rec.ctx is not None:
            under.setdefault(rec.ctx, []).append(rec)
    for ctx, members in under.items():
        if all(m.start in drops for m in members):
            label = drops[members[0].start]
            drops[ctx] = label
            end = section_end[ctx] + 1
            if ESLINT_ROW.match(plain[members[0].start]) and end < len(plain) and not plain[end].strip():
                drops[end] = label
    return drops, notes


def _places_note(message: str, recs: list[_Diag]) -> str:
    """`[searchslim] N more places with this same diagnostic (msg): a.c 3:1 9:1; b.c 4:2`.
    Paths sharing a long directory are written relative to it (`under dir/: ...`)."""
    places: dict[str, list[str]] = {}
    for rec in recs:
        places.setdefault(rec.path, []).append(rec.pos)
    prefix = ""
    if len(places) > 1:
        common = os.path.commonprefix(list(places))
        cut = max(common.rfind("/"), common.rfind("\\"))
        if cut >= 12:
            prefix = common[: cut + 1]
    where = "; ".join(f"{path[len(prefix):]} {' '.join(pos)}" for path, pos in places.items())
    under = f", under {prefix}" if prefix else ""
    return f"{NOTE_PREFIX} {len(recs)} more places with this same diagnostic ({message}){under}: {where}"
