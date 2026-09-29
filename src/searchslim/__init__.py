"""searchslim: shrink Grep/Glob/rg/fd output without losing code evidence."""

from .models import Block, Kind, Line, PathCount, SearchResult
from .parsers import detect_kind, parse
from .rules import Config, Reduced, estimate_tokens, reduce

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
) -> Reduced:
    """Parse raw tool output and reduce it.

    With no `scorer` this is the rules mode. With a scorer (see `rerank`), it is
    rules+model: when the rules would drop something, units are ranked by
    relevance to `query` before the budget is applied.
    """
    result = parse(raw, kind=kind, default_path=default_path)
    if scorer is None:
        return reduce(result, config)
    from .rerank import Query, reduce_ranked

    return reduce_ranked(result, query or Query(), scorer, config)
