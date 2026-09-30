"""Coverage view: lossless topology + lossy detail.

When a content search is over budget, the plain rules/rerank output shows some
matches and ends with a note counting what is "not shown". In live runs agents
read that as data withheld from them and searched again for it, which cost
more turns than the trimmed tokens saved.

The coverage view instead leads with an index of every matching file (match
count, line span, first definition line), so the agent knows the whole shape
of the result, and then prints the selected evidence in the tool's own format:

    [searchslim] 84 matches in 17 files. Coverage: 17/17 matching files indexed (matches, line span). Evidence: 12 blocks from 7 files expanded below.
      src/auth/token.ts  18 matches  L41-210  def L41  (5 expanded)
      src/auth/logout.ts  7 matches  L73-89
      ...
    src/auth/token.ts:41:export function refreshToken(...) {
    ...

Index lines are the `[searchslim]` line plus the indented lines right after it
(`split_note` separates them); evidence lines are still lines of the raw
input. With many files the index lists the files that have evidence and rolls
the others up by directory, so every file stays counted somewhere.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import replace

from .models import Kind, SearchResult
from .parsers import parse
from .rules import (
    NOTE_PREFIX,
    Config,
    Reduced,
    build_blocks,
    dedupe_lines,
    estimate_tokens,
    group_path,
    rollup_dirs,
)

INDEX_INDENT = "  "
# The index may use up to this share of the budget; past it, files are rolled up by directory.
INDEX_SHARE = 0.35

_DEFINITION = None  # set lazily from rerank, which owns the definition regex


def split_note(text: str) -> tuple[str, str]:
    """(body, notes): `[searchslim]` lines and the indented index lines that follow one."""
    body, notes = [], []
    in_index = False
    for ln in text.splitlines():
        if ln.startswith(NOTE_PREFIX):
            notes.append(ln)
            in_index = True
        elif in_index and ln.startswith(INDEX_INDENT):
            notes.append(ln)
        else:
            in_index = False
            body.append(ln)
    return "\n".join(body), "\n".join(notes)


def _definition_line(lines: dict[int, str], matches: set[int]) -> int | None:
    global _DEFINITION
    if _DEFINITION is None:
        from .rerank import _DEFINITION as regex

        _DEFINITION = regex
    for n in sorted(matches):
        if _DEFINITION.match(lines[n]):
            return n
    return None


def file_index(result: SearchResult) -> "OrderedDict[str, dict]":
    """Per file, in input order: match count, first/last match line, first definition line."""
    index: OrderedDict[str, dict] = OrderedDict()
    for path, blocks in build_blocks(dedupe_lines(result.lines)).items():
        lines: dict[int, str] = {}
        matches: set[int] = set()
        for b in blocks:
            lines.update(b.lines)
            matches |= b.matches
        if not matches:
            continue
        index[path] = {
            "matches": len(matches),
            "first": min(matches),
            "last": max(matches),
            "def": _definition_line(lines, matches),
        }
    return index


def _span(info: dict) -> str:
    a, b = info["first"], info["last"]
    return f"L{a}" if a == b else f"L{a}-{b}"


def _file_row(path: str, info: dict, shown: int, default_path: str) -> str:
    name = path or default_path or "(this file)"
    row = f"{INDEX_INDENT}{name}  {_n(info['matches'], 'match')}  {_span(info)}"
    if info["def"] is not None:
        row += f"  def L{info['def']}"
    if shown:
        row += "  (all expanded)" if shown >= info["matches"] else f"  ({shown} expanded)"
    return row


def _dir_row(d: str, files: int, matches: int) -> str:
    if d.startswith("+"):  # "+N other dirs" from rollup_dirs
        return f"{INDEX_INDENT}{d}  {_n(matches, 'match')}"
    return f"{INDEX_INDENT}{d}/  {_n(files, 'file')}  {_n(matches, 'match')}"


def _n(count: int, word: str) -> str:
    return f"{count} {word}{'es' if word.endswith('ch') else 's'}" if count != 1 else f"1 {word}"


def index_rows(index, shown: dict[str, int], default_path: str, budget: int) -> tuple[list[str], bool]:
    """Index lines within `budget` tokens, and whether files were rolled up by directory.

    Every file is listed if that fits. Otherwise files with expanded evidence
    stay listed and the rest are grouped by directory, with fewer groups (and,
    if needed, fewer named files) until it fits. Every file stays counted.
    """
    rows = [_file_row(p, info, shown.get(p, 0), default_path) for p, info in index.items()]
    if estimate_tokens("\n".join(rows)) <= budget or len(rows) <= 3:
        return rows, False
    named = [p for p in index if shown.get(p)]
    while True:
        named_set = set(named)
        rest = [p for p in index if p not in named_set]
        for limit in (12, 6, 3, 2):
            rows = [_file_row(p, index[p], shown[p], default_path) for p in named] + _group_rows(index, rest, limit)
            if estimate_tokens("\n".join(rows)) <= budget:
                return rows, True
        if not named:
            return rows, True
        named = named[: len(named) // 2]


def _group_rows(index, rest: list[str], limit: int) -> list[str]:
    if not rest:
        return []
    groups = rollup_dirs([(p, 1) for p in rest], limit)
    named_dirs = [g for g, _ in groups if not g.startswith("+")]
    rows = []
    for d, n_files in groups:
        if d.startswith("+"):  # "+N other dirs" from rollup_dirs
            m = sum(index[p]["matches"] for p in rest if not any(_under(p, g) for g in named_dirs))
        else:
            m = sum(index[p]["matches"] for p in rest if _under(p, d))
        rows.append(_dir_row(d, n_files, m))
    return rows


def _under(path: str, d: str) -> bool:
    p = group_path(path)
    return d == "." and "/" not in p or p.startswith(d.rstrip("/") + "/")


def _shown_matches(text: str, default_path: str) -> dict[str, int]:
    body, _ = split_note(text)
    parsed = parse(body, kind=Kind.CONTENT, default_path=default_path)
    shown: dict[str, int] = {}
    for ln in parsed.lines:
        if ln.is_match:
            shown[ln.path] = shown.get(ln.path, 0) + 1
    return shown


def _evidence_blocks(text: str, default_path: str) -> int:
    body, _ = split_note(text)
    parsed = parse(body, kind=Kind.CONTENT, default_path=default_path)
    return sum(len(bs) for bs in build_blocks(parsed.lines).values())


def coverage_view(full: SearchResult, evidence_fn, config: Config, pattern: str = "") -> Reduced | None:
    """The coverage view of an over-budget content result, or None if nothing was dropped.

    `full` is the whole parsed result (before any session filtering), used for
    the index; `evidence_fn(config)` returns the rules or rules+model reduction
    of the (possibly session-filtered) result under that config.
    """
    first = evidence_fn(config)
    if full.kind is not Kind.CONTENT:
        return None
    index = file_index(full)
    total = sum(i["matches"] for i in index.values())
    if not index or first.stats.get("matches_kept", total) >= first.stats.get("matches_total", total):
        return None  # every match fits: keep the plain output

    # Size the index first, as if every file had evidence (the widest rows),
    # then give the evidence the rest of the budget.
    index_budget = int(config.max_tokens * INDEX_SHARE)
    widest = {p: max(info["matches"] - 1, 1) for p, info in index.items()}
    rows, _ = index_rows(index, widest, full.default_path, index_budget)
    planned = estimate_tokens("\n".join(rows)) + len(rows)  # slack for annotation widths
    index_cost = planned + 60  # + the summary line
    ev_config = replace(config, max_tokens=max(config.max_tokens - index_cost, config.max_tokens // 2), note_max_files=0)
    evidence = evidence_fn(ev_config)
    ev_body, _ = split_note(evidence.text)
    shown = _shown_matches(evidence.text, full.default_path)
    rows, rolled = index_rows(index, shown, full.default_path, planned)

    n_files = len(index)
    expanded_files = sum(1 for p in index if shown.get(p))
    head = f"{NOTE_PREFIX} "
    if pattern:
        head += f"{pattern}: "
    head += f"{_n(total, 'match')} in {_n(n_files, 'file')}."
    how = "by directory where many" if rolled else "matches, line span"
    head += f" Coverage: {n_files}/{n_files} matching files indexed ({how})."
    blocks = _evidence_blocks(evidence.text, full.default_path)
    head += f" Evidence: {_n(blocks, 'block')} from {_n(expanded_files, 'file')} expanded below."

    # Framing lines (Claude Code "Found N files", ...) stay first/last around the body.
    header = [ln for ln in full.header if ln]
    body_lines = ev_body.splitlines()
    if header and body_lines[: len(header)] == header:
        body_lines = body_lines[len(header):]
    text = "\n".join([*header, head, *rows, *body_lines])
    stats = dict(evidence.stats)
    stats.update(
        {
            "view": "coverage",
            "matches_total": total,
            "files_total": n_files,
            "files_indexed": n_files,
            "files_expanded": expanded_files,
            "index_rolled_up": rolled,
        }
    )
    return Reduced(text=text, stats=stats)
