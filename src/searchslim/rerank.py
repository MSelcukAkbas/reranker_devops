"""rules+model mode: rank candidate blocks by relevance, then fit the budget.

The model never sees or returns code it could rewrite into the output: it
receives numbered candidate units and returns scores (or an ordering of unit
ids). Output text is always rendered from the parsed input lines, so the
"no rewriting, no new results" rule holds by construction.

Flow (content results):
  1. dedupe + merge into blocks (same as rules)
  2. split blocks into units: one unit per cluster of nearby matches, with the
     context lines closest to it
  3. score units with a Scorer (lexical by default, optionally Claude)
  4. greedily keep the best units that fit the token budget
  5. render kept units in the tool's own format, files ordered by their best
     unit, lines in file order; the note lists omitted matches, most relevant first

Only used when the rules layer had to drop something; otherwise the rules
output (which is then the full result) is returned unchanged.
"""

from __future__ import annotations

import json
import math
import os
import re
from collections import Counter, OrderedDict
from dataclasses import dataclass, field
from typing import Protocol

from .models import Block, Kind, Line, SearchResult
from .rules import (
    NOTE_PREFIX,
    Config,
    Reduced,
    build_blocks,
    dedupe_lines,
    estimate_tokens,
    reduce,
    render_blocks,
)


@dataclass
class Query:
    """What the agent is trying to do. All fields optional."""

    intent: str = ""  # the user's goal
    subtask: str = ""  # the agent's current step
    pattern: str = ""  # the search pattern itself

    def text(self) -> str:
        return "\n".join(p for p in (self.intent, self.subtask) if p)


@dataclass
class Unit:
    id: int
    path: str
    lines: dict[int, str]
    matches: set[int]

    def text(self) -> str:
        return "\n".join(self.lines[n] for n in sorted(self.lines))


@dataclass
class ScoreResult:
    scores: list[float]
    # Filled by model-backed scorers so the benchmark can compute cost.
    usage: dict = field(default_factory=dict)


class Scorer(Protocol):
    name: str

    def score(self, query: Query, units: list[Unit]) -> ScoreResult: ...


# --- units -------------------------------------------------------------------

DENSE_GAP = 2  # matches this close stay in one unit
MAX_CLUSTER_MATCHES = 5  # a dense run of matches is cut into units of at most this many
CONTEXT_SHARE = 0.5  # share of the budget where ranked units keep their context lines


def split_units(blocks: "OrderedDict[str, list[Block]]") -> list[Unit]:
    units: list[Unit] = []
    for path, file_blocks in blocks.items():
        for block in file_blocks:
            nums = sorted(block.lines)
            matches = sorted(block.matches)
            if not matches:
                units.append(Unit(len(units), path, dict(block.lines), set()))
                continue
            clusters: list[list[int]] = [[matches[0]]]
            for m in matches[1:]:
                if m - clusters[-1][-1] <= DENSE_GAP and len(clusters[-1]) < MAX_CLUSTER_MATCHES:
                    clusters[-1].append(m)
                else:
                    clusters.append([m])
            # Each context line goes to the nearest cluster (ties to the earlier one).
            owner: dict[int, int] = {}
            for n in nums:
                best = min(
                    range(len(clusters)),
                    key=lambda i: (0 if clusters[i][0] <= n <= clusters[i][-1] else min(abs(n - clusters[i][0]), abs(n - clusters[i][-1])), i),
                )
                owner[n] = best
            for i, cluster in enumerate(clusters):
                lines = {n: block.lines[n] for n in nums if owner[n] == i}
                units.append(Unit(len(units), path, lines, set(cluster)))
    return units


# --- lexical scorer ----------------------------------------------------------

_WORD = re.compile(r"[A-Za-z][A-Za-z0-9]*")
_CAMEL = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|[0-9]+")
_STOP = set(
    """a an and are as at be by can do does find for from how if in into is it its
    of on or should so that the then this to up use used uses using when where which
    who why will with without not some all any fix bug add make get set new file files
    code line lines function method class""".split()
)
_DEFINITION = re.compile(
    r"^\s*(?:export\s+)?(?:pub(?:\([a-z]+\))?\s+)?(?:async\s+)?"
    r"(?:def|class|func|fn|function|struct|enum|trait|impl|interface|type|module|const|let|var)\b"
    r"(?:\s*\([^)]*\))?\s*\*?\s*([A-Za-z_][A-Za-z0-9_]*)?"
)
_COMMENT = re.compile(r"^\s*(#|//|/\*|\*|--|;|\"\"\"|\'\'\'|<!--)")
_TEST_CODE = re.compile(r"#\[test\]|@Test\b|@pytest\.|\bdef test_|\bfn test_|\bfunc Test[A-Z]|\b(it|describe|test)\(\s*['\"`]|\bassert(_eq|Equal|_ne)?!?\s*[(!]")
_NOISE_PATH = re.compile(r"(^|/)(changelog|history|news|releases?)(\.[a-z]+)?$|(^|/)(docs?|doc/en|vendor|node_modules)/", re.I)
_TEST_PATH = re.compile(r"(^|/)(tests?|testing|__tests__|spec)/|(_test|\.test|\.spec|test_)[^/]*$", re.I)


def terms(text: str) -> list[str]:
    out = []
    for word in _WORD.findall(text):
        parts = [word] + [p for p in _CAMEL.findall(word) if p != word]
        for p in parts:
            for q in p.split("_"):
                q = q.lower()
                if len(q) > 1 and q not in _STOP:
                    out.append(_stem(q))
    return out


def _stem(word: str) -> str:
    # Just enough stemming that "checks"/"checked" meet "check" and "lines" meets "line".
    for suffix in ("ing", "ed", "es", "s", "e"):
        if word.endswith(suffix) and len(word) - len(suffix) >= 3:
            return word[: -len(suffix)]
    return word


class LexicalScorer:
    """BM25 over unit text + path, plus small structural signals.

    Structural signals: a match line that defines something (def/class/fn/...)
    gets a bonus, a bigger one when the defined name is a query term; docs,
    changelogs and tests are down-weighted unless the query mentions them.
    Deterministic and dependency-free, so it is the default model.
    """

    name = "lexical"

    def __init__(self, k1: float = 1.2, b: float = 0.75):
        self.k1, self.b = k1, b

    def score(self, query: Query, units: list[Unit]) -> ScoreResult:
        q_terms = Counter(terms(query.text()))
        # Every candidate matched the pattern, so pattern terms barely discriminate;
        # they still help rank among multi-term patterns.
        for t in terms(query.pattern):
            q_terms[t] += 0 if t in q_terms else 0.3
        docs = [Counter(terms(u.path) + terms(u.text())) for u in units]
        n = len(docs) or 1
        avg_len = sum(sum(d.values()) for d in docs) / n or 1.0
        df = Counter(t for d in docs for t in d)
        qtext = query.text().lower()
        wants_tests = "test" in qtext
        wants_docs = any(w in qtext for w in ("doc", "changelog", "history", "release"))

        scores = []
        for unit, doc in zip(units, docs):
            length = sum(doc.values()) or 1
            s = 0.0
            for t, qw in q_terms.items():
                tf = doc.get(t, 0)
                if not tf:
                    continue
                idf = math.log(1 + (n - df[t] + 0.5) / (df[t] + 0.5))
                s += qw * idf * tf * (self.k1 + 1) / (tf + self.k1 * (1 - self.b + self.b * length / avg_len))
            if unit.matches and all(_COMMENT.match(unit.lines[m]) for m in unit.matches):
                s *= 0.6  # every match is in a comment or docstring
            for m in unit.matches:
                d = _DEFINITION.match(unit.lines[m])
                if d:
                    s += 0.5
                    name = d.group(1) or ""
                    if name and set(terms(name)) & set(q_terms):
                        s += 2.0
            if _NOISE_PATH.search(unit.path) and not wants_docs:
                s *= 0.5
            if not wants_tests and (_TEST_PATH.search(unit.path) or _TEST_CODE.search(unit.text())):
                s *= 0.5
            scores.append(s)
        return ScoreResult(scores)


# --- Claude scorer -----------------------------------------------------------

CLAUDE_DEFAULT_MODEL = "claude-haiku-4-5"  # the lightest current Claude model
CLAUDE_SYSTEM = (
    "You rank code search results for a coding agent. You get the agent's goal "
    "and numbered candidate snippets (path:line: text). Return only JSON: "
    '{"ranking": [ids, most useful first]}. Include only snippets the agent '
    "would plausibly need; omit clearly irrelevant ones. Never output code."
)


class ClaudeScorer:
    """Ask a small Claude model to order candidates; lexical score breaks ties.

    Candidates are pre-filtered to the lexical top `max_candidates` to bound
    cost and latency. Needs the optional `anthropic` package and credentials.
    """

    name = "claude"

    def __init__(self, model: str | None = None, max_candidates: int = 60, max_unit_chars: int = 400, client=None):
        self.model = model or os.environ.get("SEARCHSLIM_CLAUDE_MODEL", CLAUDE_DEFAULT_MODEL)
        self.max_candidates = max_candidates
        self.max_unit_chars = max_unit_chars
        self._client = client
        self.fallback = LexicalScorer()

    def _get_client(self):
        if self._client is None:
            import anthropic  # optional dependency: pip install 'searchslim[claude]'

            self._client = anthropic.Anthropic()
        return self._client

    def score(self, query: Query, units: list[Unit]) -> ScoreResult:
        base = self.fallback.score(query, units).scores
        shortlist = sorted(range(len(units)), key=lambda i: (-base[i], i))[: self.max_candidates]
        prompt = self._prompt(query, [units[i] for i in shortlist])
        response = self._get_client().messages.create(
            model=self.model,
            max_tokens=1024,
            system=CLAUDE_SYSTEM,
            messages=[{"role": "user", "content": prompt}],
        )
        text = "".join(getattr(b, "text", "") for b in response.content if getattr(b, "type", "") == "text")
        ranking = parse_ranking(text, valid_ids={units[i].id for i in shortlist})
        # Ranked units first (in model order), then everything else by lexical score.
        top = max(base, default=0.0) + 1.0
        scores = [b / (top + 1.0) for b in base]  # keep lexical order below any ranked unit
        for rank, uid in enumerate(ranking):
            scores[uid] = top + (len(ranking) - rank)
        usage = {
            "model": self.model,
            "input_tokens": getattr(response.usage, "input_tokens", 0),
            "output_tokens": getattr(response.usage, "output_tokens", 0),
            "candidates": len(shortlist),
            "ranked": len(ranking),
        }
        return ScoreResult(scores, usage)

    def _prompt(self, query: Query, units: list[Unit]) -> str:
        parts = [f"Goal: {query.intent or '(not given)'}"]
        if query.subtask:
            parts.append(f"Current step: {query.subtask}")
        if query.pattern:
            parts.append(f"Search pattern: {query.pattern}")
        parts.append("\nCandidates:")
        for u in units:
            body = "\n".join(f"{u.path}:{n}: {u.lines[n]}" for n in sorted(u.lines))
            if len(body) > self.max_unit_chars:
                body = body[: self.max_unit_chars] + " …"
            parts.append(f"[{u.id}]\n{body}")
        return "\n".join(parts)


def parse_ranking(text: str, valid_ids: set[int]) -> list[int]:
    """Extract {"ranking": [...]} from model text; keep only known ids, once each."""
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return []
    try:
        data = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return []
    seen: list[int] = []
    for item in data.get("ranking", []) if isinstance(data, dict) else []:
        if isinstance(item, int) and item in valid_ids and item not in seen:
            seen.append(item)
    return seen


SCORERS = {"lexical": LexicalScorer, "claude": ClaudeScorer}


def make_scorer(name: str) -> Scorer:
    try:
        return SCORERS[name]()
    except KeyError:
        raise ValueError(f"unknown scorer {name!r}; choose from {sorted(SCORERS)}") from None


# --- selection ---------------------------------------------------------------


def reduce_ranked(result: SearchResult, query: Query, scorer: Scorer, config: Config | None = None) -> Reduced:
    config = config or Config()
    rules_out = reduce(result, config)
    stats = rules_out.stats
    dropped = bool(stats.get("steps")) if result.kind is Kind.CONTENT else stats.get("kept", 0) < stats.get("unique", 0)
    if not dropped:
        # Nothing was dropped, so there is nothing to choose between.
        rules_out.stats["reranked"] = False
        return rules_out
    if result.kind is Kind.CONTENT:
        return _rank_content(result, query, scorer, config)
    if result.kind is Kind.PATHS:
        return _rank_paths(result, query, scorer, config)
    rules_out.stats["reranked"] = False
    return rules_out


def _rank_content(result: SearchResult, query: Query, scorer: Scorer, config: Config) -> Reduced:
    lines = dedupe_lines(result.lines)
    blocks = build_blocks(lines, config.merge_gap)
    units = split_units(blocks)
    scored = scorer.score(query, units)
    order = sorted(range(len(units)), key=lambda i: (-scored.scores[i], i))

    budget = max(config.max_tokens - 60 - 15 * config.note_max_files, config.max_tokens // 2)
    # The best units keep their context lines while they fit in the first part of
    # the budget; after that only match lines are added, so more places get shown.
    context_budget = int(budget * CONTEXT_SHARE)
    kept: list[int] = []
    used = 0
    for i in order:
        full = _unit_cost(units[i], config)
        if used + full <= context_budget:
            kept.append(i)
            used += full
            continue
        bare = _matches_only(units[i])
        cost = _unit_cost(bare, config)
        if bare.lines and used + cost <= budget:
            units[i] = bare
            kept.append(i)
            used += cost
    kept_set = set(kept)

    rank_of = {i: r for r, i in enumerate(order)}
    body = _render_units([units[i] for i in kept], rank_of, config)
    body = "\n".join(filter(None, [body, *result.unparsed]))

    omitted: OrderedDict[str, int] = OrderedDict()
    for i in order:  # most relevant omitted files first
        if i not in kept_set and units[i].matches:
            omitted[units[i].path] = omitted.get(units[i].path, 0) + len(units[i].matches)
    total = sum(len(u.matches) for u in units)
    kept_matches = sum(len(units[i].matches) for i in kept)

    note = f"{NOTE_PREFIX} {len(result.lines)} -> {body.count(chr(10)) + 1 if body else 0} lines, {kept_matches}/{total} matches shown, ranked by relevance ({scorer.name})."
    if omitted:
        listed = list(omitted.items())[: config.note_max_files]
        note += " Omitted matches, most relevant first: " + ", ".join(f"{p} ({n})" for p, n in listed)
        rest = list(omitted.items())[len(listed):]
        if rest:
            note += f", +{len(rest)} more files ({sum(n for _, n in rest)} matches)"
        note += ". Narrow the search (path/glob) to see them."
    return Reduced(
        text=body + ("\n" + note if body else note),
        stats={
            "kind": "content",
            "reranked": True,
            "scorer": scorer.name,
            "units": len(units),
            "units_kept": len(kept),
            "matches_total": total,
            "matches_kept": kept_matches,
            "files_total": len(blocks),
            "files_kept": len({units[i].path for i in kept}),
            "omitted": dict(omitted),
            "model_usage": scored.usage,
        },
    )


def _matches_only(unit: Unit) -> Unit:
    return Unit(unit.id, unit.path, {n: unit.lines[n] for n in unit.matches}, set(unit.matches))


def _unit_cost(unit: Unit, config: Config) -> int:
    single = OrderedDict([(unit.path, build_blocks([Line(unit.path, n, t, n in unit.matches) for n, t in unit.lines.items()])[unit.path])])
    return estimate_tokens(render_blocks(single, config.max_line_chars)) + 1


def _render_units(units: list[Unit], rank_of: dict[int, int], config: Config) -> str:
    best: dict[str, int] = {}
    for u in units:
        best[u.path] = min(best.get(u.path, 1 << 30), rank_of[u.id])
    lines = [
        Line(u.path, n, t, n in u.matches)
        for u in sorted(units, key=lambda u: (best[u.path], u.path))
        for n, t in u.lines.items()
    ]
    grouped = build_blocks(lines)
    ordered = OrderedDict(sorted(grouped.items(), key=lambda kv: best[kv[0]]))
    return render_blocks(ordered, config.max_line_chars)


def _rank_paths(result: SearchResult, query: Query, scorer: Scorer, config: Config) -> Reduced:
    paths = list(OrderedDict.fromkeys(p[2:] if p.startswith("./") else p for p in result.paths))
    units = [Unit(i, p, {1: ""}, set()) for i, p in enumerate(paths)]
    scored = scorer.score(query, units)
    order = sorted(range(len(paths)), key=lambda i: (-scored.scores[i], i))
    budget = config.max_tokens - 60
    kept, used = [], 0
    for i in order:
        cost = estimate_tokens(paths[i]) + 1
        if used + cost <= budget:
            kept.append(i)
            used += cost
    body = "\n".join(paths[i] for i in kept)
    missing = len(paths) - len(kept)
    note = f"{NOTE_PREFIX} {len(kept)}/{len(paths)} paths shown, ranked by relevance ({scorer.name})."
    if missing:
        dirs: OrderedDict[str, int] = OrderedDict()
        kept_set = set(kept)
        for i in order:
            if i not in kept_set:
                d = os.path.dirname(paths[i]) or "."
                dirs[d] = dirs.get(d, 0) + 1
        listed = list(dirs.items())[: config.note_max_files]
        note += " Not shown, by directory: " + ", ".join(f"{d}/ ({n})" for d, n in listed)
        if len(dirs) > len(listed):
            note += f", +{len(dirs) - len(listed)} more dirs"
        note += ". Narrow the pattern to see them."
    return Reduced(
        text=body + "\n" + note,
        stats={"kind": "paths", "reranked": True, "scorer": scorer.name, "unique": len(paths), "kept": len(kept), "model_usage": scored.usage},
    )


# --- query from a Claude Code transcript --------------------------------------


def query_from_transcript(path: str, pattern: str = "", max_chars: int = 1000) -> Query:
    """Best-effort: last real user message = intent, last assistant text = subtask.

    Claude Code hooks get `transcript_path` (JSONL). Tool results also arrive as
    user entries, so only entries with plain text count as the user's words.
    """
    intent = subtask = ""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for raw in f:
                try:
                    entry = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                message = entry.get("message") if isinstance(entry, dict) else None
                if not isinstance(message, dict):
                    continue
                text = _message_text(message.get("content"))
                if not text:
                    continue
                if entry.get("type") == "user" and message.get("role") == "user":
                    intent = text
                elif entry.get("type") == "assistant":
                    subtask = text
    except OSError:
        pass
    return Query(intent[-max_chars:], subtask[-max_chars // 2 :], pattern)


def _message_text(content) -> str:
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        if any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
            return ""
        return "\n".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text").strip()
    return ""


# --- benchmark harness entry point ----------------------------------------------

_VALUE_FLAGS = {"-e", "--regexp", "-g", "--glob", "-t", "--type", "-T", "--type-not", "-A", "-B", "-C", "-m", "--max-count", "-f", "--file", "--iglob", "-M", "--max-columns"}


def pattern_and_paths(cmd: list[str]) -> tuple[str, list[str]]:
    """Search pattern and path arguments of an rg/grep argv (best effort)."""
    pattern, positional, skip = "", [], False
    for i, arg in enumerate(cmd[1:], 1):
        if skip:
            skip = False
            if cmd[i - 1] in ("-e", "--regexp") and not pattern:
                pattern = arg
            continue
        if arg in _VALUE_FLAGS:
            skip = True
        elif arg.startswith("-") and arg != "-":
            if arg.startswith("--regexp="):
                pattern = pattern or arg.split("=", 1)[1]
        else:
            positional.append(arg)
    if not pattern and positional and cmd[0] in ("rg", "grep", "egrep", "fgrep"):
        pattern, positional = positional[0], positional[1:]
    return pattern, positional


def run_for_benchmark(payload: dict, scorer: Scorer) -> tuple[str, dict]:
    """The benchmark's --model-cmd contract: JSON in, reduced text + usage out.

    Output keeps the raw input's own line format, so every body line is a line
    of `raw` (the harness rejects anything else).
    """
    cmd = payload.get("cmd") or []
    pattern, paths = pattern_and_paths(cmd)
    single = paths[0] if len(paths) == 1 and "." in os.path.basename(paths[0]) else ""
    config = Config(max_tokens=int(payload.get("max_tokens") or Config.max_tokens))
    from .parsers import parse

    result = parse(payload.get("raw", ""), default_path=single)
    reduced = reduce_ranked(result, Query(payload.get("intent", ""), payload.get("subtask", ""), pattern), scorer, config)
    text = reduced.text
    raw_lines = set(payload.get("raw", "").splitlines())
    if single and not any(ln.startswith(single + ":") for ln in raw_lines):
        # Lines were read without a filename; print them the same way.
        out = []
        for ln in text.splitlines():
            for sep in (":", "-"):
                if ln.startswith(single + sep) and ln[len(single) + 1 :] and ln[len(single) + 1 :][0].isdigit():
                    ln = ln[len(single) + 1 :]
                    break
            out.append(ln)
        text = "\n".join(out)
    return text, reduced.stats.get("model_usage") or {}
