"""searchslim: shrink Grep/Glob/rg/fd output without losing code evidence."""

from .models import Block, Kind, Line, PathCount, SearchResult
from .parsers import detect_kind, parse
from .rules import Config, Reduced, estimate_tokens, for_output, reduce

__all__ = [
    "Block",
    "Config",
    "Kind",
    "Line",
    "PathCount",
    "Reduced",
    "SearchResult",
    "detect_kind",
    "estimate_tokens",
    "parse",
    "reduce",
]


def slim(
    raw: str,
    kind: Kind | None = None,
    config: Config | None = None,
    default_path: str = "",
    scorer=None,
    query=None,
    session=None,
    cwd: str = "",
) -> Reduced:
    """Parse raw tool output and reduce it.

    With no `scorer` this is the rules mode. With a scorer (see `rerank`), it is
    rules+model: when the rules would drop something, units are ranked by
    relevance to `query` before the budget is applied.

    With `config.view == "coverage"` an over-budget content result leads with
    an index of every matching file, then the selected evidence (see `coverage`).

    With a `session` (see `session.SessionStore`), an over-budget result leaves
    out lines earlier searches already showed, referencing them in the note,
    and the lines shown now are recorded. `cwd` resolves relative paths.
    """
    config = config or Config()
    config = for_output(config, raw)  # below the trigger, rules pass it through unchanged
    result = parse(raw, kind=kind, default_path=default_path)
    shown = []
    full = result
    if session is not None and estimate_tokens(raw) > config.max_tokens:
        from .session import split_seen

        result, shown = split_seen(result, session.seen(), cwd)

    def evidence(cfg: Config) -> Reduced:
        if scorer is None:
            return reduce(result, cfg)
        from .rerank import Query, reduce_ranked

        return reduce_ranked(result, query or Query(), scorer, cfg)

    reduced = None
    if config.view == "coverage" and result.kind is Kind.CONTENT and estimate_tokens(raw) > config.max_tokens:
        from .coverage import coverage_view

        reduced = coverage_view(full, evidence, config, pattern=getattr(query, "pattern", "") or "")
    if reduced is None:
        reduced = evidence(config)
    if session is not None:
        from .session import attach_note, seen_note, shown_keys

        if shown:
            reduced = attach_note(reduced, seen_note(shown, result.default_path, config))
            reduced.stats["seen_lines_skipped"] = len(shown)
        if result.kind is Kind.CONTENT:
            session.record(shown_keys(parse(reduced.text, kind=Kind.CONTENT, default_path=default_path), cwd))
    return reduced
