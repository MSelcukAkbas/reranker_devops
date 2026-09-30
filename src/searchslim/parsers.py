"""Turn raw Grep/Glob/rg/fd/find output into a SearchResult.

Supported content shapes:
  path:12:text        match line (rg, grep -n, Claude Code Grep content mode)
  path-12-text        context line (-A/-B/-C)
  --                  separator between context groups
  path                heading line, followed by `12:text` / `12-text` (rg --heading)
  12:text             no filename (single-file search); kept without a path,
                      `default_path` only names the file in the note
  [searchslim] 3 matches are this same line: text
    path:12,40        one line text at several places (lossless.py)
  rg --json           one JSON event per line

Paths shape: one path per line (Glob, fd, find, rg -l, grep -l), or a `dir/`
line with indented file names under it (lossless.py).
Count shape: path:N (rg -c, grep -c).
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
    result = SearchResult(kind=Kind.COUNT)
    for ln in raw.splitlines():
        if not ln.strip():
            continue
        m = _COUNT.match(ln)
        if m:
            result.counts.append(PathCount(m["path"], int(m["count"])))
        else:
            result.unparsed.append(ln)
    return result


def parse_content(raw: str) -> SearchResult:
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

    for ln in raw_lines:
        head = _GROUP_HEAD.match(ln)
        if head:
            group_text, heading = head["text"], None
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
            heading = None  # rg --heading puts a blank line between files
            continue

        bare = _BARE.match(ln)
        if heading is not None and bare:
            result.lines.append(
                Line(heading, int(bare["num"]), bare["text"], bare["sep"] == ":")
            )
            continue

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

        if not bare and looks_like_path(ln):
            heading = ln
            known_paths.add(ln)
            continue

        pending_context.append(ln)

    result.unparsed.extend(pending_context)
    return result


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
