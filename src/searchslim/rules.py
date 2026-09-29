"""Deterministic reduction rules.

The rules never reorder results by guessed relevance and never invent text:
they dedupe, merge, and, only when over budget, drop in a fixed order
(context lines first, then extra matches per file, then whole files from the
end). Everything dropped is summarised in a trailing note so the reader knows
what to search again for.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field, replace
import posixpath
import re

from .models import Block, Kind, Line, PathCount, SearchResult

NOTE_PREFIX = "[searchslim]"
DEFAULT_TRIGGER_TOKENS = 6000
# The note is a neutral count of what is not shown, with no advice: in live
# runs any wording about truncation or narrowing ("see them", "only if you
# need") led agents to search again for the rest, costing more than it saved.


@dataclass
class Config:
    max_tokens: int = 2000
    # Only outputs above this many tokens are reduced (to max_tokens); smaller
    # ones pass unchanged. 0 means max_tokens. The hook and CLI default to
    # DEFAULT_TRIGGER_TOKENS: trimming a mid-sized result made agents search
    # again for what was cut, costing more than the tokens saved.
    trigger_tokens: int = 0
    # Blocks in the same file closer than this many lines are merged into one.
    merge_gap: int = 0
    # When over budget, keep at most N match lines per file. N is the largest
    # value that fits the budget, but never below this floor (1 for a
    # single-file search, where the cap is the only way to shrink).
    max_matches_per_file: int = 8
    # Longer lines (minified code, data blobs) are cut; the line itself is kept.
    max_line_chars: int = 300
    # How many omitted files to name individually in the note.
    note_max_files: int = 10


@dataclass
class Reduced:
    text: str
    stats: dict = field(default_factory=dict)


def for_output(config: Config, raw: str) -> Config:
    """`config` for this raw output: below the trigger the whole output is the budget."""
    if config.trigger_tokens > config.max_tokens and estimate_tokens(raw) <= config.trigger_tokens:
        return replace(config, max_tokens=config.trigger_tokens)
    return config


def estimate_tokens(text: str) -> int:
    # Rough cl100k-style estimate; the benchmark uses a real tokenizer.
    return (len(text) + 3) // 4


def reduce(result: SearchResult, config: Config | None = None) -> Reduced:
    config = config or Config()
    if result.kind is Kind.CONTENT:
        return _reduce_content(result, config)
    if result.kind is Kind.COUNT:
        return _reduce_counts(result, config)
    if result.kind is Kind.LINES:
        return _reduce_lines(result, config)
    return _reduce_paths(result, config)


def _assemble(result: SearchResult, body: str, note: str) -> str:
    """Tool framing lines around the body; the searchslim note always last."""
    return "\n".join(p for p in [*result.header, body, *result.footer, note] if p)


# --- content -----------------------------------------------------------------


def dedupe_lines(lines: list[Line]) -> list[Line]:
    """One entry per (path, line); a match beats a context line for the same spot."""
    seen: dict[tuple[str, int], Line] = {}
    for ln in lines:
        key = (ln.path, ln.number)
        prev = seen.get(key)
        if prev is None or (ln.is_match and not prev.is_match):
            seen[key] = ln
    return list(seen.values())


def build_blocks(lines: list[Line], merge_gap: int = 0) -> "OrderedDict[str, list[Block]]":
    """Group lines per file (first-seen file order) into merged line-range blocks."""
    per_file: OrderedDict[str, list[Line]] = OrderedDict()
    for ln in lines:
        per_file.setdefault(ln.path, []).append(ln)

    blocks: OrderedDict[str, list[Block]] = OrderedDict()
    for path, file_lines in per_file.items():
        file_lines.sort(key=lambda ln: ln.number)
        file_blocks: list[Block] = []
        for ln in file_lines:
            if file_blocks and ln.number <= file_blocks[-1].end + 1 + merge_gap:
                file_blocks[-1].add(ln)
            else:
                block = Block(path)
                block.add(ln)
                file_blocks.append(block)
        blocks[path] = file_blocks
    return blocks


def render_blocks(blocks: "OrderedDict[str, list[Block]]", max_line_chars: int) -> str:
    has_context = any(
        len(b.lines) > len(b.matches) for bs in blocks.values() for b in bs
    )
    out: list[str] = []
    first = True
    for path, file_blocks in blocks.items():
        for block in file_blocks:
            if has_context and not first:
                out.append("--")
            first = False
            for num in sorted(block.lines):
                sep = ":" if num in block.matches else "-"
                prefix = f"{path}{sep}" if path else ""  # single-file output has no path
                out.append(f"{prefix}{num}{sep}{_clip(block.lines[num], max_line_chars)}")
    return "\n".join(out)


def _reduce_content(result: SearchResult, config: Config) -> Reduced:
    raw_lines = len(result.lines)
    lines = dedupe_lines(result.lines)
    blocks = build_blocks(lines, config.merge_gap)
    total_matches = sum(len(b.matches) for bs in blocks.values() for b in bs)
    steps: list[str] = []

    def fits(bs) -> bool:
        return estimate_tokens(render_blocks(bs, config.max_line_chars)) <= config.max_tokens

    if not fits(blocks):
        had_context = any(len(b.lines) > len(b.matches) for bs in blocks.values() for b in bs)
        blocks = _drop_context(blocks)
        if had_context:
            steps.append("context lines dropped")

    omitted: OrderedDict[str, int] = OrderedDict()
    if not fits(blocks):
        # Leave room for the note, which grows once matches are omitted.
        note_room = 60 + 15 * config.note_max_files
        cap = _largest_fitting_cap(
            blocks,
            config,
            lambda bs: estimate_tokens(render_blocks(bs, config.max_line_chars)) + note_room <= config.max_tokens,
        )
        blocks, capped = _cap_matches_per_file(blocks, cap)
        for path, n in capped.items():
            omitted[path] = omitted.get(path, 0) + n
        if capped:
            steps.append(f"max {cap} matches per file")

    if not fits(blocks):
        blocks, dropped = _drop_files_to_budget(blocks, config)
        for path, n in dropped.items():
            omitted[path] = omitted.get(path, 0) + n
        if dropped:
            steps.append("files dropped from the end")

    body = render_blocks(blocks, config.max_line_chars)
    body = "\n".join(filter(None, [body, *result.unparsed]))
    kept_matches = sum(len(b.matches) for bs in blocks.values() for b in bs)

    note = ""
    if steps or len(lines) < raw_lines:
        note = _content_note(
            raw_lines, body, total_matches, kept_matches, steps, omitted, config,
            result.default_path, len({ln.path for ln in lines}),
        )

    return Reduced(
        text=_assemble(result, body, note),
        stats={
            "kind": "content",
            "input_lines": raw_lines,
            "unique_lines": len(lines),
            "matches_total": total_matches,
            "matches_kept": kept_matches,
            "files_total": len({ln.path for ln in lines}),
            "files_kept": len(blocks),
            "steps": steps,
            "omitted": dict(omitted),
        },
    )


def _drop_context(blocks):
    out: OrderedDict[str, list[Block]] = OrderedDict()
    for path, file_blocks in blocks.items():
        kept_lines = [
            Line(path, num, b.lines[num], True) for b in file_blocks for num in sorted(b.matches)
        ]
        out[path] = build_blocks(kept_lines)[path] if kept_lines else []
    return out


def _cap_matches_per_file(blocks, cap: int):
    out: OrderedDict[str, list[Block]] = OrderedDict()
    capped: OrderedDict[str, int] = OrderedDict()
    for path, file_blocks in blocks.items():
        match_nums = sorted(n for b in file_blocks for n in b.matches)
        if len(match_nums) <= cap:
            out[path] = file_blocks
            continue
        keep = set(match_nums[:cap])
        kept_lines = [
            Line(path, n, b.lines[n], n in b.matches)
            for b in file_blocks
            for n in sorted(b.lines)
            if n in keep or n not in b.matches
        ]
        # Context lines only survive while they touch a kept match.
        rebuilt = [b for b in build_blocks(kept_lines)[path] if b.matches]
        out[path] = rebuilt
        capped[path] = len(match_nums) - cap
    return out, capped


def _largest_fitting_cap(blocks, config: Config, fits) -> int:
    """Largest per-file match cap whose output fits, not below the floor.

    Budget-driven, not relevance-driven: every file gets the same cap, so a
    large file keeps as many of its matches (in line order) as the budget allows.
    """
    floor = 1 if len(blocks) == 1 else config.max_matches_per_file
    most = max((sum(len(b.matches) for b in bs) for bs in blocks.values()), default=0)
    lo, hi = floor, max(floor, most - 1)
    if not fits(_cap_matches_per_file(blocks, lo)[0]):
        return lo
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if fits(_cap_matches_per_file(blocks, mid)[0]):
            lo = mid
        else:
            hi = mid - 1
    return lo


def _drop_files_to_budget(blocks, config: Config):
    # Reserve room for the note so body + note stays near the budget.
    budget = max(config.max_tokens - 60 - 15 * config.note_max_files, config.max_tokens // 2)
    kept: OrderedDict[str, list[Block]] = OrderedDict()
    dropped: OrderedDict[str, int] = OrderedDict()
    used = 0
    for path, file_blocks in blocks.items():
        cost = estimate_tokens(render_blocks(OrderedDict([(path, file_blocks)]), config.max_line_chars)) + 1
        if not dropped and (used + cost <= budget or not kept):
            kept[path] = file_blocks
            used += cost
        else:
            # Once one file is dropped, all later ones go too, so the kept set
            # stays a prefix of the original order.
            dropped[path] = sum(len(b.matches) for b in file_blocks)
    return kept, dropped


def _content_note(raw_lines, body, total, kept, steps, omitted, config, default_path="", files_total=0) -> str:
    parts = [f"{NOTE_PREFIX} {raw_lines} -> {body.count(chr(10)) + 1 if body else 0} lines"]
    parts.append(f"{kept}/{total} matches shown")
    if steps:
        parts.append("; ".join(steps))
    note = ", ".join(parts) + "."
    if omitted:
        items = list(omitted.items())
        listed = items[: config.note_max_files]
        names = ", ".join(f"{p or default_path or 'this file'} ({n})" for p, n in listed)
        note += f" Not shown: {names}"
        rest = items[len(listed):]
        if rest:
            # Every omitted file stays covered by a named directory, so an
            # agent can always tell where to narrow the search.
            dirs = format_dirs(rollup_dirs(rest, config.note_max_files))
            note += f"; {len(rest)} more files by directory: {dirs}"
        note += "."
    return note


def rollup_dirs(path_counts, limit: int) -> list[tuple[str, int]]:
    """Group (path, count) pairs by directory, moving the deepest directories up
    to their parents until at most `limit` groups remain. Every input is counted
    in some group, so no omitted file goes unmentioned."""
    groups: OrderedDict[str, int] = OrderedDict()
    for p, n in path_counts:
        d = posixpath.dirname(group_path(p)) or "."
        groups[d] = groups.get(d, 0) + n
    limit = max(limit, 2)
    while len(groups) > limit:
        deepest = max(d.count("/") for d in groups)
        if deepest == 0:
            return _head_and_rest(list(groups.items()), limit)
        merged: OrderedDict[str, int] = OrderedDict()
        for d, n in groups.items():
            if d.count("/") == deepest:
                d = posixpath.dirname(d)
            merged[d] = merged.get(d, 0) + n
        if len(merged) < 3 < len(groups):
            # Merging would leave one or two big dirs (everything under /usr/lib/...),
            # which says nothing; name the biggest subdirs instead.
            return _head_and_rest(sorted(groups.items(), key=lambda kv: -kv[1]), limit)
        groups = merged
    return list(groups.items())


def _head_and_rest(items, limit: int):
    """The first `limit - 1` groups by name, the rest lumped into one counted group."""
    if len(items) <= limit:
        return items
    head, tail = items[: limit - 1], items[limit - 1:]
    return head + [(f"+{len(tail)} other dirs", sum(n for _, n in tail))]


def group_path(p: str) -> str:
    """Path as used for directory grouping: `/` separators, no leading `./`.

    Windows tools print `.\\dir\\file.py`; without this every such path would
    land in one `.` group. Shown lines keep the tool's own spelling.
    """
    p = p.replace("\\", "/")
    while p.startswith("./"):
        p = p[2:].lstrip("/")
    return p


_ABSOLUTE = re.compile(r"^(?:/|[A-Za-z]:/)")


def format_dirs(groups) -> str:
    names = [d for d, _ in groups if not d.startswith("+")]
    base = ""
    if len(names) > 1 and all(_ABSOLUTE.match(n) for n in names):
        try:
            base = posixpath.commonpath(names)
        except ValueError:  # different drives
            base = ""
    if len(base) < 12 or base in names:
        base = ""
    trim = len(base) + 1 if base else 0
    listed = ", ".join(f"{d} ({n})" if d.startswith("+") else f"{d[trim:]}/ ({n})" for d, n in groups)
    # Absolute paths share a long prefix (Glob output); say it once.
    return f"under {base}/: {listed}" if base else listed


def _clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return f"{text[:limit]}…[+{len(text) - limit} chars]"


# --- paths -------------------------------------------------------------------


def _normalize_path(p: str) -> str:
    return p[2:] if p.startswith("./") else p


def _reduce_paths(result: SearchResult, config: Config) -> Reduced:
    unique = list(OrderedDict.fromkeys(_normalize_path(p) for p in result.paths))
    budget = config.max_tokens - 60
    kept: list[str] = []
    used = 0
    for p in unique:
        cost = estimate_tokens(p) + 1
        if used + cost > budget and kept:
            break
        kept.append(p)
        used += cost
    rest = unique[len(kept):]

    body = "\n".join(kept)
    note = ""
    if rest:
        dirs = format_dirs(rollup_dirs([(p, 1) for p in rest], config.note_max_files))
        note = (
            f"{NOTE_PREFIX} {len(kept)}/{len(unique)} paths shown. Not shown, by directory: {dirs}"
            + "."
        )
    elif len(unique) < len(result.paths):
        note = f"{NOTE_PREFIX} {len(result.paths) - len(unique)} duplicate paths removed."
    return Reduced(
        text=_assemble(result, body, note),
        stats={"kind": "paths", "input": len(result.paths), "unique": len(unique), "kept": len(kept)},
    )


# --- counts ------------------------------------------------------------------


def _reduce_counts(result: SearchResult, config: Config) -> Reduced:
    merged: OrderedDict[str, int] = OrderedDict()
    for pc in result.counts:
        path = _normalize_path(pc.path)
        merged[path] = max(merged.get(path, 0), pc.count)
    lines = [f"{p}:{n}" for p, n in merged.items()]
    budget = config.max_tokens - 40
    kept: list[str] = []
    used = 0
    for ln in lines:
        cost = estimate_tokens(ln) + 1
        if used + cost > budget and kept:
            break
        kept.append(ln)
        used += cost
    body = "\n".join(kept + result.unparsed)
    note = ""
    if len(kept) < len(lines):
        rest = list(merged.values())[len(kept):]
        note = f"{NOTE_PREFIX} {len(kept)}/{len(lines)} files shown; {len(rest)} files with {sum(rest)} matches not shown."
    return Reduced(
        text=_assemble(result, body, note),
        stats={"kind": "count", "input": len(result.counts), "unique": len(merged), "kept": len(kept)},
    )


# --- listings (tree, ls -R, filtered pipelines) --------------------------------

# tree entry: indent of "│   " / "|   " / "    " units, then a branch marker.
_TREE_ENTRY = re.compile(r"^((?:[│|]\s{3}|\s{4})*)(?:├──|└──|\|--|`--)\s")
_TREE_SUMMARY = re.compile(r"^\d+ director(?:y|ies)(?:, \d+ files?)?$")


def _reduce_lines(result: SearchResult, config: Config) -> Reduced:
    rows = result.rows
    text = "\n".join(rows)
    stats = {"kind": "lines", "input": len(rows)}
    if estimate_tokens(text) <= config.max_tokens:
        return Reduced(text=text, stats={**stats, "kept": len(rows), "unique": len(rows)})
    # The note is short; reserve a little room for it.
    budget = config.max_tokens - 40 - 12 * config.note_max_files
    reduced = _reduce_tree(rows, budget, config) or _reduce_ls_sections(rows, budget, config)
    if reduced is None:
        kept = _prefix_fitting(rows, budget)
        shown = sum(1 for r in kept if r.strip())
        total = sum(1 for r in rows if r.strip())
        note = f"{NOTE_PREFIX} {shown}/{total} lines shown; the last {total - shown} lines not shown."
        reduced = (kept, note, "prefix")
    kept, note, how = reduced
    kept_count = sum(1 for r in kept if r.strip())
    return Reduced(
        text="\n".join([*kept, note]),
        stats={**stats, "kept": kept_count, "unique": sum(1 for r in rows if r.strip()), "steps": [how]},
    )


def _prefix_fitting(rows: list[str], budget: int) -> list[str]:
    kept, used = [], 0
    for r in rows:
        cost = estimate_tokens(_clip(r, 300)) + 1
        if used + cost > budget and kept:
            break
        kept.append(r)
        used += cost
    return kept


def _fits_rows(rows, budget: int) -> bool:
    return sum(estimate_tokens(r) + 1 for r in rows) <= budget


def _reduce_tree(rows: list[str], budget: int, config: Config):
    """tree output: keep the shallow levels whole and drop the deepest ones, so the
    agent still sees the full layout and knows which directories to expand."""
    depths = []
    for r in rows:
        m = _TREE_ENTRY.match(r)
        depths.append(len(m.group(1)) // 4 + 1 if m else 0)
    if sum(1 for d in depths if d) < len(rows) // 2:
        return None
    deepest = max(depths)
    for level in range(deepest - 1, 0, -1):
        kept_idx = [i for i, d in enumerate(depths) if d <= level]
        if _fits_rows([rows[i] for i in kept_idx], budget):
            break
    else:
        return None
    # Attribute each hidden entry to the kept entry it sits under.
    hidden: OrderedDict[str, int] = OrderedDict()
    stack: list[str] = []
    for r, d in zip(rows, depths):
        if 0 < d <= level:
            stack = stack[: d - 1] + [_TREE_ENTRY.sub("", r).strip()]
        elif d > level:
            owner = "/".join(stack)
            hidden[owner] = hidden.get(owner, 0) + 1
    total_hidden = sum(hidden.values())
    ranked = sorted(hidden.items(), key=lambda kv: -kv[1])
    listed, rest = ranked[: config.note_max_files], ranked[config.note_max_files:]
    names = ", ".join(f"{name}/ ({n})" for name, n in listed)
    if rest:
        names += f", +{len(rest)} more dirs ({sum(n for _, n in rest)})"
    note = (
        f"{NOTE_PREFIX} {len(kept_idx)}/{len(rows)} lines shown: {total_hidden} entries deeper than level {level} not shown"
        f" (under {names})."
        " Run tree on one of these directories to see them."
    )
    return [rows[i] for i in kept_idx], note, f"tree depth {level}"


def _reduce_ls_sections(rows: list[str], budget: int, config: Config):
    """ls -R output ("dir:" header, entries, blank line): keep whole sections for
    the shallowest directories and name the rest by directory."""
    starts = [i for i, r in enumerate(rows) if r.endswith(":") and (i == 0 or not rows[i - 1].strip())]
    if len(starts) < 2 or starts[0] != 0:
        return None
    sections = []
    for n, s in enumerate(starts):
        end = starts[n + 1] if n + 1 < len(starts) else len(rows)
        name = rows[s][:-1]
        sections.append((name, rows[s:end]))
    root_depth = sections[0][0].rstrip("/").count("/")
    depth = [name.rstrip("/").count("/") - root_depth for name, _ in sections]
    for level in range(max(depth) - 1, -1, -1):
        kept = [sec for sec, d in zip(sections, depth) if d <= level]
        if _fits_rows([r for _, body in kept for r in body], budget):
            break
    else:
        return None
    kept_names = {name for name, _ in kept}
    # Name omitted sections by their first hidden level (a child of a listed
    # directory), biggest first; the tail is counted so every one is covered.
    groups: OrderedDict[str, int] = OrderedDict()
    n_omitted = 0
    for (name, body), d in zip(sections, depth):
        if name in kept_names:
            continue
        n_omitted += 1
        parts = name.rstrip("/").split("/")
        top = "/".join(parts[: len(parts) - (d - level - 1)])
        groups[top] = groups.get(top, 0) + sum(1 for r in body[1:] if r.strip())
    ranked = sorted(groups.items(), key=lambda kv: -kv[1])
    listed, rest = ranked[: config.note_max_files], ranked[config.note_max_files:]
    dirs = ", ".join(f"{d}/ ({n})" for d, n in listed)
    if rest:
        dirs += f", +{len(rest)} other dirs ({sum(n for _, n in rest)})"
    out = [r for _, body in kept for r in body]
    while out and not out[-1].strip():
        out.pop()
    note = (
        f"{NOTE_PREFIX} {len(kept)}/{len(sections)} directories listed; {n_omitted} deeper directories"
        f" not listed ({sum(groups.values())} entries), by directory: {dirs}. List one of them to see it."
    )
    return out, note, f"ls depth {level}"


__all__ = ["Config", "Reduced", "reduce", "estimate_tokens", "dedupe_lines", "build_blocks", "NOTE_PREFIX", "PathCount"]
