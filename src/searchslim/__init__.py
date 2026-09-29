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


def slim(raw: str, kind: Kind | None = None, config: Config | None = None, default_path: str = "") -> Reduced:
    """Parse raw tool output and apply the rules in one call."""
    return reduce(parse(raw, kind=kind, default_path=default_path), config)
