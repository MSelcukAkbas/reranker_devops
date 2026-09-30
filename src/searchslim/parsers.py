"""Turn raw Grep/Glob/rg/fd/find output into a SearchResult.

Supported content shapes:
  path:12:text        match line (rg, grep -n, Claude Code Grep content mode)
  path-12-text        context line (-A/-B/-C)
  --                  separator between context groups
  path                heading line, followed by `12:text` / `12-text` (rg --heading)
  dir/                directory line, then indented `  name:12:text`, or `  name`
                      with `    12:text` lines under it (lossless.py)
  12:text             no filename (single-file search); kept without a path,
                      `default_path` only names the file in the note
  [searchslim] 3 matches are this same line: text
    path:12,40        one line text at several places (lossless.py)
  rg --json           one JSON event per line
  > path:12:text      PowerShell Select-String -Context: `> ` marks a match,
    path:11:text      two spaces a context line

Paths shape: one path per line (Glob, fd, find, rg -l, grep -l), or a `dir/`
line with indented file names under it (lossless.py).
Count shape: path:N (rg -c, grep -c), or a `dir/` line with indented
`name:N` rows under it (lossless.py).
Lines shape: anything else (tree, ls -R, a filtered pipeline); only chosen when
the caller knows the command, never auto-detected.

Claude Code's Grep/Glob wrap results in framing lines ("Found 3 files",
"No files found", "Found 5 total occurrences across 2 files.",
"(Results are truncated...)", "[Showing results with pagination ...]").
These are split off into `header`/`footer` so they are neither parsed as
paths nor dropped.
"""

from __future__ import annotations

import json
import re

from .models import Kind, Line, PathCount, SearchResult

_MATCH = re.compile(r"^(?P<path>.+?):(?P<num>\d+):(?P<text>.*)$")
_CONTEXT = re.compile(r"^(?P<path>.+?)-(?P<num>\d+)-(?P<text>.*)$")
_BARE = re.compile(r"^(?P<num>\d+)(?P<sep>[:-])(?P<text>.*)$")
_COUNT = re.compile(r"^(?P<path>.+):(?P<count>\d+)$")
_SEPARATOR = "--"
_PLAIN_PATH = re.compile(r"^(?:[A-Za-z]:)?[^:]*$")
# Lossless view (lossless.py): one line text found at several places, written once.
SAME_LINE = "matches are this same line: "
_GROUP_HEAD = re.compile(r"^\[searchslim\] \d+ " + re.escape(SAME_LINE) + r"(?P<text>.*)$")
_GROUP_PLACE = re.compile(r"^  (?:(?P<path>.+):)?(?P<nums>\d+(?:,\d+)*)$")
_FRAMING = re.compile(
    r"^(?:Found \d+ .+|No (?:files|matches) found\.?"
    r"|\(Results are truncated[^)]*\)\.?|\[Showing results with pagination[^\]]*\])$"
)


def detect_kind(raw: str) -> Kind:
    lines = [ln for ln in raw.splitlines() if ln.strip() and ln != _SEPARATOR]
    if not lines:
        return Kind.PATHS
    if lines[0].startswith("{") and _is_rg_json(lines[0]):
        return Kind.CONTENT
    if all(_COUNT.match(ln) for ln in lines):
        return Kind.COUNT
    if _grouped_counts(lines):
        return Kind.COUNT
    if any(_MATCH.match(ln) or _BARE.match(ln) for ln in lines):
        return Kind.CONTENT
    return Kind.PATHS


def parse(raw: str, kind: Kind | None = None, default_path: str = "") -> SearchResult:
    if kind is Kind.LINES:  # no Claude Code framing around shell listings
        return _parse_body(raw, kind, default_path)
    header, raw, footer = split_framing(raw)
    result = _parse_body(raw, kind, default_path)
    result.header, result.footer = header, footer
    return result


def split_framing(raw: str) -> tuple[list[str], str, list[str]]:
    """Split Claude Code framing lines off the start and end of `raw`."""
    lines = raw.splitlines()
    start, end = 0, len(lines)
    while start < end and (not lines[start].strip() or _FRAMING.match(lines[start].strip())):
        start += 1
    while end > start and (not lines[end - 1].strip() or _FRAMING.match(lines[end - 1].strip())):
        end -= 1
    header = [ln for ln in lines[:start] if ln.strip()]
    footer = [ln for ln in lines[end:] if ln.strip()]
    if not header and not footer:
        return [], raw, []
    return header, "\n".join(lines[start:end]), footer


def _parse_body(raw: str, kind: Kind | None, default_path: str) -> SearchResult:
    kind = kind or detect_kind(raw)
    if kind is Kind.LINES:
        return SearchResult(kind=Kind.LINES, rows=raw.splitlines())
    if kind is Kind.PATHS:
        return parse_paths(raw)
    if kind is Kind.COUNT:
        return parse_counts(raw)
    first = next((ln for ln in raw.splitlines() if ln.strip()), "")
    if first.startswith("{") and _is_rg_json(first):
        return parse_rg_json(raw)
    result = parse_content(raw)
    result.default_path = default_path
    return result


def parse_paths(raw: str) -> SearchResult:
    """One path per line; a `dir/` line followed by indented names (lossless.py) is expanded."""
    result = SearchResult(kind=Kind.PATHS)
    lines = raw.splitlines()
    directory = None
    for i, ln in enumerate(lines):
        if directory is not None and ln.startswith("  ") and ln.strip():
            result.paths.append(directory + ln.strip())
            continue
        directory = None
        ln = ln.strip()
        if not ln:
            continue
        nxt = lines[i + 1] if i + 1 < len(lines) else ""
        if ln.endswith(("/", "\\")) and nxt.startswith("  ") and nxt.strip():
            directory = ln
            continue
        result.paths.append(ln)
    return result


def parse_counts(raw: str) -> SearchResult:
    """path:N lines; a `dir/` line followed by indented `name:N` rows (lossless.py) is expanded."""
    result = SearchResult(kind=Kind.COUNT)
    directory = None
    lines = raw.splitlines()
    for i, ln in enumerate(lines):
        if not ln.strip():
            directory = None
            continue
        if directory is not None and ln.startswith("  "):
            m = _COUNT.match(ln[2:])
            if m and not m["path"].startswith(" "):
                result.counts.append(PathCount(directory + m["path"], int(m["count"])))
                continue
        directory = None
        nxt = lines[i + 1] if i + 1 < len(lines) else ""
        if ln.endswith(("/", "\\")) and not ln.startswith(" ") and nxt.startswith("  ") and _COUNT.match(nxt[2:]):
            directory = ln
            continue
        m = _COUNT.match(ln)
        if m:
            result.counts.append(PathCount(m["path"], int(m["count"])))
        else:
            result.unparsed.append(ln)
    return result


def _grouped_counts(lines: list[str]) -> bool:
    """Non-blank lines that are `path:N`, `dir/` or indented `name:N` (lossless count layout)."""
    grouped = False
    for i, ln in enumerate(lines):
        if ln.startswith("  "):
            if not (_COUNT.match(ln[2:]) and i and (lines[i - 1].endswith(("/", "\\")) or lines[i - 1].startswith("  "))):
                return False
            grouped = True
        elif not (_COUNT.match(ln) or (ln.endswith(("/", "\\")) and i + 1 < len(lines) and lines[i + 1].startswith("  "))):
            return False
    return grouped


_SELECT_STRING = re.compile(r"^(?P<mark>> |  )(?P<path>\S.*?):(?P<num>\d+):(?P<text>.*)$")


def parse_select_string(raw: str) -> SearchResult | None:
    """Select-String -Context output, or None when `raw` is not that shape."""
    rows = [ln for ln in raw.splitlines() if ln.strip()]
    parsed = [_SELECT_STRING.match(ln) for ln in rows]
    if not rows or not all(parsed) or not any(m["mark"] == "> " for m in parsed):
        return None
    result = SearchResult(kind=Kind.CONTENT)
    result.lines = [Line(m["path"], int(m["num"]), m["text"], m["mark"] == "> ") for m in parsed]
    return result


def parse_content(raw: str) -> SearchResult:
    select_string = parse_select_string(raw)
    if select_string is not None:
        return select_string
    result = SearchResult(kind=Kind.CONTENT)
    known_paths: set[str] = set()
    heading: str | None = None
    pending_context: list[str] = []  # context lines seen before their path is known
    group_text: str | None = None  # inside a "same line" group

    raw_lines = raw.splitlines()
    # First pass: collect paths from match lines so context lines whose path
    # contains "-12-" style fragments can be split at a known path.
    for ln in raw_lines:
        m = _MATCH.match(ln)
        if m:
            known_paths.add(m["path"])

    heading_used = False
    sub_heading: str | None = None  # a file under a directory group

    def drop_heading() -> None:
        # A "heading" no `N:text` line followed was not one (e.g. `path:text`
        # output without line numbers): keep it verbatim instead of losing it.
        nonlocal heading, sub_heading
        if heading is not None and not heading_used:
            pending_context.append(heading)
        heading = sub_heading = None

    for ln in raw_lines:
        head = _GROUP_HEAD.match(ln)
        if head:
            drop_heading()
            group_text = head["text"]
            continue
        place = _GROUP_PLACE.match(ln) if group_text is not None else None
        if place:
            for n in place["nums"].split(","):
                result.lines.append(Line(place["path"] or "", int(n), group_text, True))
            continue
        group_text = None
        if ln == _SEPARATOR:
            continue
        if not ln.strip():
            drop_heading()  # rg --heading puts a blank line between files
            continue

        if heading is not None and heading.endswith(("/", "\\")) and ln.startswith("  "):
            # Directory group (lossless.py): `dir/`, then `  name:N:text`, or
            # `  name` with `    N:text` lines under it.
            if ln.startswith("    ") and sub_heading is not None:
                sub = ln[4:]
                if sub == _SEPARATOR:
                    continue
                sb = _BARE.match(sub)
                if sb:
                    result.lines.append(Line(sub_heading, int(sb["num"]), sb["text"], sb["sep"] == ":"))
                    heading_used = True
                    continue
            elif not ln.startswith("   "):
                full = heading + ln[2:]
                line = _MATCH.match(full)
                ctx = None if line else _split_context(full, known_paths)
                if line or ctx:
                    result.lines.append(Line(line["path"], int(line["num"]), line["text"], True) if line else ctx)
                    heading_used = True
                    continue
                if looks_like_path(full):
                    sub_heading = full
                    continue

        bare = _BARE.match(ln)
        if heading is not None and bare:
            result.lines.append(
                Line(heading, int(bare["num"]), bare["text"], bare["sep"] == ":")
            )
            heading_used = True
            continue
        drop_heading()

        m = _MATCH.match(ln)
        if m:
            result.lines.append(Line(m["path"], int(m["num"]), m["text"], True))
            continue

        ctx = _split_context(ln, known_paths)
        if ctx:
            result.lines.append(ctx)
            continue

        if bare:
            # Single-file search: no path in the output, so none is added.
            result.lines.append(Line("", int(bare["num"]), bare["text"], bare["sep"] == ":"))
            continue

        if looks_like_path(ln):
            heading, heading_used = ln, False
            known_paths.add(ln)
            continue

        pending_context.append(ln)

    drop_heading()
    result.unparsed.extend(pending_context)
    return result


def reliable(result: SearchResult) -> bool:
    """Whether a content parse can be trusted to account for the output.

    Output without line numbers (`path:text`, Grep with -n false) does not
    parse as content: its lines end up unparsed, or look like context lines
    of a dated file name (`x_2026-07-18.js:...`). Reducing such a parse would
    reshape or lose lines, so callers pass the raw output through instead.
    """
    if result.kind is Kind.PATHS:
        # `path:text` lines auto-detected as paths: a real path has no ":" past a drive letter.
        return all(_PLAIN_PATH.match(p) for p in result.paths)
    if result.kind is not Kind.CONTENT:
        return True
    total = len(result.lines) + len(result.unparsed)
    if not total:
        return True
    if result.lines and not any(ln.is_match for ln in result.lines):
        return False  # context lines with no match: not real search output
    return len(result.unparsed) <= max(2, 0.05 * total)


def parse_rg_json(raw: str) -> SearchResult:
    result = SearchResult(kind=Kind.CONTENT)
    for ln in raw.splitlines():
        if not ln.strip():
            continue
        try:
            event = json.loads(ln)
        except json.JSONDecodeError:
            result.unparsed.append(ln)
            continue
        etype = event.get("type")
        if etype not in ("match", "context"):
            continue
        data = event["data"]
        path = _rg_text(data["path"])
        text = _rg_text(data["lines"]).rstrip("\n")
        start = data["line_number"]
        for offset, part in enumerate(text.split("\n")):
            result.lines.append(Line(path, start + offset, part, etype == "match"))
    return result


def _split_context(ln: str, known_paths: set[str]) -> Line | None:
    # Longest known path first, so "a-1-b.py" beats "a" when both are known:
    # try each "-" as the path/number separator, rightmost first. (A set lookup
    # per "-" keeps this linear; scanning all known paths per line was the
    # parser's hot spot on big outputs.)
    i = len(ln)
    while known_paths:
        i = ln.rfind("-", 0, i)
        if i <= 0:
            break
        if ln[:i] in known_paths:
            num, sep, text = ln[i + 1:].partition("-")
            if sep and num.isdigit():
                return Line(ln[:i], int(num), text, False)
    m = _CONTEXT.match(ln)
    if m and looks_like_path(m["path"]):
        return Line(m["path"], int(m["num"]), m["text"], False)
    return None


def reads_as_heading(path: str) -> bool:
    """Whether `path` alone on a line parses back as an rg --heading path."""
    if not looks_like_path(path) or _MATCH.match(path) or _BARE.match(path) or _GROUP_HEAD.match(path):
        return False
    m = _CONTEXT.match(path)
    return not (m and looks_like_path(m["path"]))


def looks_like_path(s: str) -> bool:
    return (
        not s.startswith(" ")
        and not s.startswith("\t")
        and ("/" in s or "\\" in s or "." in s)
        and " " not in s.strip()
    )


def _is_rg_json(line: str) -> bool:
    try:
        return json.loads(line).get("type") in {"begin", "match", "context", "end", "summary"}
    except (json.JSONDecodeError, AttributeError):
        return False


def _rg_text(obj: dict) -> str:
    if "text" in obj:
        return obj["text"]
    # Non-UTF8 data comes base64-encoded; keep a marker instead of guessing.
    return "<non-utf8>"
