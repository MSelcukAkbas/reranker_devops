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
    lines not shown` line between them.

Everything else stays verbatim and in order: failures, tracebacks, assertion
diffs, errors, warnings, compiler diagnostics and the summary counts. A single
trailing `[searchslim] not shown: ...` line counts what was dropped. Any error
or a small saving returns None, so the caller keeps the raw output.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .rules import NOTE_PREFIX, estimate_tokens

DEFAULT_COMPACT_TRIGGER_TOKENS = 2000
# Below this saving the raw output is kept: a compacted result that is barely
# smaller only adds a note to read.
MIN_SAVED_RATIO = 0.2
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
    (
        "dotnet build progress",
        re.compile(r"^\s+(Determining projects to restore|All projects are up-to-date|Restored \S|\S+ -> \S+\.(dll|exe)$)"),
    ),
)

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

    kept = [(i, ln) for i, ln in enumerate(lines) if i not in drops]
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
    return drops


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
    return drops


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


RUNNERS = (
    ("pytest", _pytest),
    ("js", _js),
    ("go", _go),
    ("cargo", _simple(CARGO_TEST_DETECT, CARGO_TEST_PASS)),
    ("dotnet", _simple(DOTNET_DETECT, DOTNET_PASS)),
)


def _collapse_repeats(kept: list[tuple[int, str]]) -> tuple[list[str], int]:
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
