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

    With a `session` (see `session.SessionStore`), an over-budget result leaves
    out lines earlier searches already showed, referencing them in the note,
    and the lines shown now are recorded. `cwd` resolves relative paths.
    """
    config = config or Config()
    config = for_output(config, raw)  # below the trigger, rules pass it through unchanged
    result = parse(raw, kind=kind, default_path=default_path)
    shown = []
    if session is not None and estimate_tokens(raw) > config.max_tokens:
        from .session import split_seen

        result, shown = split_seen(result, session.seen(), cwd)
    if scorer is None:
        reduced = reduce(result, config)
    else:
        from .rerank import Query, reduce_ranked

        reduced = reduce_ranked(result, query or Query(), scorer, config)
    if session is not None:
        from .session import attach_note, seen_note, shown_keys

        if shown:
            reduced = attach_note(reduced, seen_note(shown, result.default_path, config))
            reduced.stats["seen_lines_skipped"] = len(shown)
        if result.kind is Kind.CONTENT:
            session.record(shown_keys(parse(reduced.text, kind=Kind.CONTENT, default_path=default_path), cwd))
    return reduced
