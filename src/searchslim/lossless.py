"""Lossless view: every match, with the repetition taken out.

In live runs, cutting search output made agents search again for what was cut
("list every process.env use" went from 2-3 turns to 7-12). Most of the tokens
in a big search result are not irrelevant lines but the same text written
again and again: the path on every line, context lines printed twice by
overlapping -C windows, the same import line in forty files. Removing that
loses no evidence, so the agent has no reason to search again.

Three levels, tried in order (`slim` with `Config.view == "lossless"`):

  L1  lossless (default). Duplicate lines dropped, overlapping context merged,
      the path printed once per file (rg's own --heading layout), and a line
      whose text repeats across many places written once with its locations:

          src/auth/token.ts
          41:export function refreshToken() {
          44:const token = ...

          src/auth/logout.ts:20:import { revokeToken } from "./token"

          [searchslim] 5 matches are this same line: const timeout = process.env.TIMEOUT;
            src/a.ts:20,44
            src/b.ts:18

      Applied only when it saves at least `MIN_SAVING` of the raw size;
      otherwise the raw output passes through. Every file, line number and
      matched text is still there.

      When L1 is still over `max_tokens` and the search printed context lines
      (-A/-B/-C), the same layout with every match but no context comes next.

  L2  projection, only when that is still over `max_tokens` and the search is a
      known kind of listing (env variables, require, imports): each matching
      line is written as the name it refers to plus its locations, so every
      match still has a place. Lines the recognizer does not read are kept as
      L1 lines.

  L3  the ranked coverage view (coverage.py): only when L2 does not apply or
      does not fit.

Paths (Glob, fd, find, rg -l) get the L1 treatment too: files of the same
directory under one `dir/` line, names indented below it.
"""

from __future__ import annotations

import re
from collections import OrderedDict
from dataclasses import dataclass, replace

from .models import Block, Kind, Line, SearchResult
from .parsers import SAME_LINE, reads_as_heading
from .rules import NOTE_PREFIX, Config, Reduced, _clip, build_blocks, dedupe_lines, estimate_tokens

# L1 is used only if it is at least this much smaller than the raw output.
MIN_SAVING = 0.2
# A match line is factored out once its text appears on this many lines ...
FACTOR_MIN = 3
# ... and is at least this long (short lines cost less than their locations).
FACTOR_MIN_CHARS = 16
# L1 keeps lines whole up to this length; longer ones are minified blobs.
LINE_CHARS = 1000
INDENT = "  "


# --- L1: content ----------------------------------------------------------------


@dataclass
class Group:
    """One line text found at several places."""

    text: str
    places: "OrderedDict[str, list[int]]"

    @property
    def count(self) -> int:
        return sum(len(v) for v in self.places.values())


def lossless_content(result: SearchResult, config: Config) -> Reduced:
    lines = dedupe_lines(result.lines)
    blocks = build_blocks(lines, config.merge_gap)
    has_context = any(len(b.lines) > len(b.matches) for bs in blocks.values() for b in bs)
    groups: list[Group] = [] if has_context else _factor(lines)
    if groups:
        factored = {(p, n) for g in groups for p, ns in g.places.items() for n in ns}
        blocks = build_blocks([ln for ln in lines if (ln.path, ln.number) not in factored], config.merge_gap)
    line_chars = max(config.max_line_chars, LINE_CHARS)
    body = render_grouped(blocks, line_chars, has_context)
    group_text = render_groups(groups, line_chars)
    body = "\n".join(p for p in [body, group_text, *result.unparsed] if p)
    matches = sum(len(b.matches) for bs in blocks.values() for b in bs) + sum(g.count for g in groups)
    return Reduced(
        text="\n".join(p for p in [*result.header, body, *result.footer] if p),
        stats={
            "kind": "content",
            "view": "lossless",
            "level": "L1",
            "input_lines": len(result.lines),
            "unique_lines": len(lines),
            "matches_total": matches,
            "matches_kept": matches,
            "files_total": len({ln.path for ln in lines}),
            "factored_lines": sum(g.count for g in groups),
        },
    )


def _factor(lines: list[Line]) -> list[Group]:
    """Match lines whose text repeats often enough that one copy plus locations is shorter."""
    by_text: OrderedDict[str, list[Line]] = OrderedDict()
    for ln in lines:
        key = ln.text.strip()
        if ln.is_match and len(key) >= FACTOR_MIN_CHARS:
            by_text.setdefault(key, []).append(ln)
    groups = []
    for text, occ in by_text.items():
        if len(occ) < FACTOR_MIN:
            continue
        places: OrderedDict[str, list[int]] = OrderedDict()
        for ln in occ:
            places.setdefault(ln.path, []).append(ln.number)
        group = Group(text, places)
        # Cost as printed, with the path counted once per file either way.
        plain = sum(len(str(ln.number)) + 1 + len(ln.text) + 1 for ln in occ)
        factored = len(render_groups([group], LINE_CHARS)) - sum(len(p) + 3 for p in places)
        if factored < plain:
            groups.append(group)
    return groups


def render_grouped(blocks: "OrderedDict[str, list[Block]]", line_chars: int, has_context: bool) -> str:
    """rg --heading layout: the path once, then `N:text` (match) / `N-text` (context).

    Files are separated by a blank line and blocks inside a file by `--` (when
    there is context). A file with a single line is printed as one flat
    `path:N:text` line, since a heading would cost more than it saves; so is a
    file whose path would not read back as a heading.
    """
    out: list[str] = []
    prev_flat = False
    for path, file_blocks in blocks.items():
        file_blocks = [b for b in file_blocks if b.lines]
        if not file_blocks:
            continue
        n_lines = sum(len(b.lines) for b in file_blocks)
        flat = bool(path) and (n_lines == 1 or not reads_as_heading(path))
        if out and not (flat and prev_flat):
            out.append("")
        if path and not flat:
            out.append(path)
        for i, block in enumerate(file_blocks):
            if i and has_context:
                out.append("--")
            for num in sorted(block.lines):
                sep = ":" if num in block.matches else "-"
                prefix = f"{path}{sep}" if flat else ""
                out.append(f"{prefix}{num}{sep}{_clip(block.lines[num], line_chars)}")
        prev_flat = flat
    return "\n".join(out)


def render_groups(groups: list[Group], line_chars: int) -> str:
    out = []
    for g in groups:
        out.append(f"{NOTE_PREFIX} {g.count} {SAME_LINE}{_clip(g.text, line_chars)}")
        for path, nums in g.places.items():
            loc = ",".join(str(n) for n in nums)
            out.append(f"{INDENT}{path}:{loc}" if path else f"{INDENT}{loc}")
    return "\n".join(out)


# --- L1: paths ------------------------------------------------------------------


def lossless_paths(result: SearchResult) -> Reduced:
    unique = list(OrderedDict.fromkeys(p[2:] if p.startswith("./") else p for p in result.paths))
    body = "\n".join(group_paths(unique))
    return Reduced(
        text="\n".join(p for p in [*result.header, body, *result.footer] if p),
        stats={"kind": "paths", "view": "lossless", "level": "L1", "input": len(result.paths), "unique": len(unique), "kept": len(unique)},
    )


def group_paths(paths: list[str]) -> list[str]:
    """Files of one directory under a `dir/` line, names indented; directories in first-seen order."""
    by_dir: OrderedDict[str, list[str]] = OrderedDict()
    for p in paths:
        i = max(p.rfind("/"), p.rfind("\\"))
        d, name = p[: i + 1], p[i + 1:]
        if not name:  # a directory entry ("src/"): its own line
            d, name = p, ""
        by_dir.setdefault(d, []).append(name)
    out: list[str] = []
    for d, names in by_dir.items():
        files = [n for n in names if n]
        if d and len(files) >= 2:
            out.append(d)
            out += [f"{INDENT}{n}" for n in files]
            out += [d] * (len(names) - len(files))
        else:
            out += [d + n for n in names]
    return out


# --- L2: projection -------------------------------------------------------------


@dataclass(frozen=True)
class Recognizer:
    name: str
    # Applies only when the search pattern is about this kind of name.
    trigger: re.Pattern
    # Each match's first non-empty group is the name.
    key: re.Pattern

    def names(self, text: str) -> list[str]:
        found = []
        for m in self.key.finditer(text):
            name = next((g for g in m.groups() if g), None)
            if name and name not in found:
                found.append(name)
        return found


RECOGNIZERS = [
    Recognizer(
        "env",
        re.compile(r"env|environ|getenv|EnvironmentVariable", re.I),
        re.compile(
            r"process\.env\.([A-Za-z_]\w*)"
            r"|process\.env\[\s*['\"`]([^'\"`]+)['\"`]\s*\]"
            r"|import\.meta\.env\.([A-Za-z_]\w*)"
            r"|os\.environ(?:\.get)?\s*[\[(]\s*['\"]([^'\"]+)['\"]"
            r"|os\.getenv\(\s*['\"]([^'\"]+)['\"]"
            r"|Environment\.GetEnvironmentVariable\(\s*\"([^\"]+)\""
            r"|\$env:([A-Za-z_]\w*)"
        ),
    ),
    Recognizer(
        "require",
        re.compile(r"require"),
        re.compile(r"\brequire\(\s*['\"`]([^'\"`]+)['\"`]\s*\)"),
    ),
    Recognizer(
        "import",
        re.compile(r"import|from|using"),
        re.compile(
            r"\bfrom\s+['\"]([^'\"]+)['\"]"
            r"|^\s*import\s+['\"]([^'\"]+)['\"]"
            r"|\bimport\(\s*['\"]([^'\"]+)['\"]\s*\)"
            r"|^\s*from\s+(\.*[\w.]*)\s+import\b"
            r"|^\s*import\s+([\w.]+(?:\s*,\s*[\w.]+)*)\s*$"
            r"|^\s*using\s+(?:static\s+)?([\w.]+)\s*;"
        ),
    ),
]
# Projection is used only if the recognizer reads at least this share of the match lines.
MIN_RECOGNIZED = 0.5


def project(result: SearchResult, config: Config, pattern: str) -> Reduced | None:
    """L2: matching lines as recognized names with every location, or None if no recognizer fits."""
    if not pattern or result.kind is not Kind.CONTENT:
        return None
    matches = [ln for ln in dedupe_lines(result.lines) if ln.is_match]
    if not matches:
        return None
    best = None
    for rec in RECOGNIZERS:
        if not rec.trigger.search(pattern):
            continue
        named = {(ln.path, ln.number): rec.names(ln.text) for ln in matches}
        hits = sum(1 for v in named.values() if v)
        if hits >= MIN_RECOGNIZED * len(matches) and (best is None or hits > best[2]):
            best = (rec, named, hits)
    if best is None:
        return None
    rec, named, hits = best
    places: dict[str, OrderedDict[str, list[int]]] = {}
    for ln in matches:
        for name in named[(ln.path, ln.number)]:
            places.setdefault(name, OrderedDict()).setdefault(ln.path, []).append(ln.number)
    rest = [ln for ln in matches if not named[(ln.path, ln.number)]]
    n_files = len({ln.path for ln in matches})
    names = sorted(places, key=str.lower)
    lead = (
        f"{NOTE_PREFIX} {len(matches)} matches in {n_files} file{'s' if n_files != 1 else ''}."
        f" {hits} matching lines read as {len(places)} {rec.name} names"
    )
    tail = f"; the other {len(rest)} matching lines below." if rest else "."
    # By name (each name, then every path:lines) or by file (each path, then
    # name:line for each match, with all names listed once): whichever is shorter.
    by_name = [f"{lead}, each with every place it is used (path:lines){tail}"]
    for name in names:
        locs = " ".join(f"{p}:{','.join(map(str, ns))}" if p else ",".join(map(str, ns)) for p, ns in places[name].items())
        by_name.append(f"{INDENT}{name}  {locs}")
    by_file = [f"{lead}: {', '.join(names)}. Each file with name:line for its matches{tail}"]
    per_file: OrderedDict[str, list[str]] = OrderedDict()
    for ln in matches:
        for name in named[(ln.path, ln.number)]:
            per_file.setdefault(ln.path, []).append(f"{name}:{ln.number}")
    for path, items in per_file.items():
        by_file.append(f"{INDENT}{path or '(file)'}  {' '.join(items)}")
    rows = min(by_name, by_file, key=lambda r: len("\n".join(r)))
    head, rows = rows[0], rows[1:]
    rest_body = render_grouped(build_blocks(rest), max(config.max_line_chars, LINE_CHARS), False) if rest else ""
    text = "\n".join(p for p in [*result.header, head, *rows, rest_body, *result.footer] if p)
    return Reduced(
        text=text,
        stats={
            "kind": "content",
            "view": "lossless",
            "level": "L2",
            "recognizer": rec.name,
            "matches_total": len(matches),
            "matches_kept": len(matches),
            "projected_lines": hits,
            "names": len(places),
            "files_total": n_files,
        },
    )


# --- driver ---------------------------------------------------------------------


def lossless_view(raw: str, result: SearchResult, config: Config, pattern: str = "") -> Reduced | None:
    """L1, else L2, for an output above the trigger; None means fall back to L3 (ranked coverage).

    Returns the raw output itself when L1 would not save `MIN_SAVING` and the
    raw output is within `max_tokens`.
    """
    raw_tokens = estimate_tokens(raw)
    if result.kind is Kind.CONTENT:
        l1 = lossless_content(result, config)
    elif result.kind is Kind.PATHS:
        l1 = lossless_paths(result)
    else:
        return None
    l1_tokens = estimate_tokens(l1.text)
    saves = l1_tokens <= (1 - MIN_SAVING) * raw_tokens
    if raw_tokens <= config.max_tokens and not saves:
        return passthrough(raw, raw_tokens, l1_tokens)
    if l1_tokens <= config.max_tokens:
        l1.stats.update(raw_tokens=raw_tokens, tokens=l1_tokens)
        return l1
    if any(not ln.is_match for ln in result.lines):
        # Every match, without the -A/-B/-C context lines.
        matches = replace(result, lines=[ln for ln in result.lines if ln.is_match])
        l1m = lossless_content(matches, config)
        n_matches, n_files = l1m.stats["matches_total"], l1m.stats["files_total"]
        note = (
            f"{NOTE_PREFIX} all {n_matches} match{'es' if n_matches != 1 else ''} in {n_files} file{'s' if n_files != 1 else ''};"
            " context lines left out."
        )
        text = _with_lead(l1m.text, result.header, note)
        if estimate_tokens(text) <= config.max_tokens:
            l1m.stats.update(level="L1-matches", raw_tokens=raw_tokens, tokens=estimate_tokens(text), l1_tokens=l1_tokens)
            return Reduced(text=text, stats=l1m.stats)
    l2 = project(result, config, pattern)
    if l2 is not None and estimate_tokens(l2.text) <= config.max_tokens:
        l2.stats.update(raw_tokens=raw_tokens, tokens=estimate_tokens(l2.text), l1_tokens=l1_tokens)
        return l2
    return None


def _with_lead(text: str, header: list[str], lead: str) -> str:
    """`lead` right after the tool's framing header lines."""
    lines = text.splitlines()
    n = len(header) if header and lines[: len(header)] == header else 0
    return "\n".join([*lines[:n], lead, *lines[n:]])


def passthrough(raw: str, raw_tokens: int, l1_tokens: int | None = None) -> Reduced:
    stats = {"view": "lossless", "level": "L0", "raw_tokens": raw_tokens, "tokens": raw_tokens}
    if l1_tokens is not None:
        stats["l1_tokens"] = l1_tokens
    return Reduced(text=raw, stats=stats)
