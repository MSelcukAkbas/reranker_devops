"""Common data model for search tool output.

Every parser turns raw tool output into one of these shapes so the rules
layer (and later the reranker) never has to know which tool produced it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Kind(str, Enum):
    CONTENT = "content"  # path:line:text lines (rg, grep -n, Claude Code Grep content mode)
    PATHS = "paths"  # one path per line (Glob, fd, find, rg -l, Grep files_with_matches)
    COUNT = "count"  # path:count lines (rg -c, Grep count mode)


@dataclass(frozen=True)
class Line:
    """One output line from a content search.

    `is_match` is False for context lines (rg -A/-B/-C, printed as path-line-text).
    """

    path: str
    number: int
    text: str
    is_match: bool = True


@dataclass
class Block:
    """A contiguous line range in one file, kept as a unit of evidence."""

    path: str
    lines: dict[int, str] = field(default_factory=dict)
    matches: set[int] = field(default_factory=set)

    @property
    def start(self) -> int:
        return min(self.lines)

    @property
    def end(self) -> int:
        return max(self.lines)

    def add(self, line: Line) -> None:
        self.lines[line.number] = line.text
        if line.is_match:
            self.matches.add(line.number)


@dataclass
class PathCount:
    path: str
    count: int


@dataclass
class SearchResult:
    """Parsed tool output. Exactly one of the collections is used, per `kind`."""

    kind: Kind
    lines: list[Line] = field(default_factory=list)
    paths: list[str] = field(default_factory=list)
    counts: list[PathCount] = field(default_factory=list)
    # Lines the parser could not interpret. Kept verbatim so nothing is lost silently.
    unparsed: list[str] = field(default_factory=list)
