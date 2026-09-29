"""Session memory: don't re-send code lines the agent has already been shown.

Claude often searches the same area several times in one session, sometimes
as parallel tool calls in one turn. When a later search is over budget, lines
an earlier search already showed (same file, line number and text) are not
printed again; the trailing note lists them as `path:line` ranges instead, so
the evidence stays referenced and the budget goes to lines the agent has not
seen. Small outputs still pass through unchanged, and a line whose text
changed since counts as new.

Storage is safe for concurrent hook processes, on POSIX and Windows alike,
without locks: each call writes its own file (temp file + os.replace) into
the session's directory, and readers take the union of all files. Every race
(a sibling call still running, a file mid-compaction) can only make a line
look unseen, so it gets shown again; it never hides a line.

Claude Code compacts long sessions, and after that the earlier results are no
longer in context: the PreCompact hook clears the session (`clear`).
`SEARCHSLIM_SESSION=off` disables it; `SEARCHSLIM_CACHE_DIR` moves the store.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import tempfile
import time
import uuid
from collections import OrderedDict
from pathlib import Path

from .models import Kind, Line, SearchResult
from .rules import NOTE_PREFIX, Config, Reduced, rollup_dirs

# Merge the per-call files once there are this many, to keep reads cheap.
COMPACT_AT = 64
# Session directories untouched for this long are removed.
MAX_AGE_S = 3 * 24 * 3600
_SAFE_ID = re.compile(r"[^A-Za-z0-9_.-]")


def enabled() -> bool:
    return os.environ.get("SEARCHSLIM_SESSION", "").lower() != "off"


def cache_root() -> Path:
    root = os.environ.get("SEARCHSLIM_CACHE_DIR")
    if root:
        return Path(root)
    try:
        user = _SAFE_ID.sub("_", os.environ.get("USER") or os.environ.get("USERNAME") or str(os.getuid()))
    except AttributeError:  # no getuid on Windows
        user = "user"
    return Path(tempfile.gettempdir()) / f"searchslim-{user}"


def line_key(path: str, number: int, text: str) -> str:
    raw = f"{os.path.normcase(os.path.normpath(path))}\0{number}\0{text}".encode("utf-8", "replace")
    return hashlib.blake2b(raw, digest_size=10).hexdigest()


class SessionStore:
    """The set of line keys already shown in one agent session."""

    def __init__(self, session_id: str, root: Path | None = None):
        safe = _SAFE_ID.sub("_", session_id)[:128]
        if not safe.strip("._"):
            raise ValueError("empty session id")
        self.dir = (root or cache_root()) / safe

    def seen(self) -> set[str]:
        keys: set[str] = set()
        try:
            files = list(self.dir.glob("*.keys"))
        except OSError:
            return keys
        for f in files:
            try:
                keys.update(f.read_text(encoding="ascii").split())
            except OSError:  # removed by a concurrent compaction: its keys live on elsewhere
                continue
        return keys

    def record(self, keys) -> None:
        keys = sorted(set(keys))
        if not keys:
            return
        self.dir.mkdir(parents=True, exist_ok=True)
        _atomic_write(self.dir / f"{time.time_ns()}-{uuid.uuid4().hex[:8]}.keys", "\n".join(keys))
        self._maybe_compact()
        _prune_old(self.dir.parent)

    def clear(self) -> None:
        shutil.rmtree(self.dir, ignore_errors=True)

    def _maybe_compact(self) -> None:
        files = list(self.dir.glob("*.keys"))
        if len(files) < COMPACT_AT:
            return
        keys: set[str] = set()
        merged = []
        for f in files:
            try:
                keys.update(f.read_text(encoding="ascii").split())
                merged.append(f)
            except OSError:
                continue
        # Write the union first, then drop the parts: a reader in between sees
        # duplicates, never fewer keys than were written (except the brief
        # window after an unlink, which only makes lines look unseen).
        _atomic_write(self.dir / f"{time.time_ns()}-merged-{uuid.uuid4().hex[:8]}.keys", "\n".join(sorted(keys)))
        for f in merged:
            try:
                f.unlink()
            except OSError:
                pass


def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_suffix(f".tmp{uuid.uuid4().hex[:8]}")
    tmp.write_text(text, encoding="ascii")
    os.replace(tmp, path)


def _prune_old(root: Path) -> None:
    cutoff = time.time() - MAX_AGE_S
    try:
        for d in root.iterdir():
            if d.is_dir() and d.stat().st_mtime < cutoff:
                shutil.rmtree(d, ignore_errors=True)
    except OSError:
        pass


# --- applying it to a search result -------------------------------------------


def _abs(path: str, cwd: str) -> str:
    return os.path.join(cwd, path) if path else path


def split_seen(result: SearchResult, seen: set[str], cwd: str) -> tuple[SearchResult, list[Line]]:
    """Remove lines already shown; return the rest and the match lines removed.

    New context lines (say, wider -C around a match seen earlier) are kept
    together with the lines up to their nearest match, so every kept run still
    holds its anchor match and nothing new loses its place.
    """
    if result.kind is not Kind.CONTENT or not seen:
        return result, []

    def is_seen(ln: Line) -> bool:
        path = ln.path or result.default_path
        return bool(path) and line_key(_abs(path, cwd), ln.number, ln.text) in seen

    per_file: OrderedDict[str, list[Line]] = OrderedDict()
    for ln in result.lines:
        per_file.setdefault(ln.path, []).append(ln)
    keep: set[tuple[str, int]] = set()
    for path, file_lines in per_file.items():
        by_num: dict[int, Line] = {}
        for ln in file_lines:  # a match beats a context line for the same spot
            if ln.number not in by_num or (ln.is_match and not by_num[ln.number].is_match):
                by_num[ln.number] = ln
        run: list[Line] = []
        for ln in [by_num[n] for n in sorted(by_num)] + [None]:
            if ln is not None and run and ln.number == run[-1].number + 1:
                run.append(ln)
                continue
            keep.update((path, n) for n in _keep_in_run(run, is_seen))
            run = [ln] if ln is not None else []
    if all((ln.path, ln.number) in keep for ln in result.lines):
        return result, []
    kept = [ln for ln in result.lines if (ln.path, ln.number) in keep]
    shown = [ln for ln in result.lines if (ln.path, ln.number) not in keep and ln.is_match]
    rest = SearchResult(
        kind=result.kind, lines=kept, unparsed=result.unparsed,
        header=result.header, footer=result.footer, default_path=result.default_path,
    )
    return rest, shown


def _keep_in_run(run: list[Line], is_seen) -> set[int]:
    matches = [ln.number for ln in run if ln.is_match]
    keep: set[int] = set()
    for ln in run:
        if is_seen(ln):
            continue
        if ln.is_match or not matches:
            keep.add(ln.number)
            continue
        anchor = min(matches, key=lambda m: abs(m - ln.number))
        lo, hi = sorted((anchor, ln.number))
        keep.update(range(lo, hi + 1))
    return keep


def shown_keys(result: SearchResult, cwd: str) -> set[str]:
    """Keys of the content lines in (parsed) output that the agent will see."""
    if result.kind is not Kind.CONTENT:
        return set()
    return {
        line_key(_abs(ln.path or result.default_path, cwd), ln.number, ln.text)
        for ln in result.lines
        if ln.path or result.default_path
    }


def _ranges(numbers: list[int]) -> str:
    nums = sorted(set(numbers))
    out: list[str] = []
    start = prev = nums[0]
    for n in nums[1:] + [None]:
        if n is not None and n == prev + 1:
            prev = n
            continue
        out.append(f"{start}-{prev}" if prev > start else str(start))
        if n is not None:
            start = prev = n
    return ",".join(out)


def seen_note(shown: list[Line], default_path: str, config: Config) -> str:
    """One sentence referencing the lines left out because they were shown before."""
    per_file: OrderedDict[str, list[int]] = OrderedDict()
    for ln in shown:
        per_file.setdefault(ln.path or default_path, []).append(ln.number)
    items = list(per_file.items())
    listed = items[: config.note_max_files]
    refs = "; ".join(f"{p}:{_ranges(nums)}" for p, nums in listed)
    rest = items[len(listed):]
    if rest:
        dirs = rollup_dirs([(p, len(set(nums))) for p, nums in rest], config.note_max_files)
        refs += "; " + ", ".join(f"{d} ({n})" if d.startswith("+") else f"{d}/ ({n} lines)" for d, n in dirs)
    n = len({(ln.path, ln.number) for ln in shown})
    return f"{n} lines already shown by an earlier search this session are not repeated: {refs}."


def attach_note(reduced: Reduced, sentence: str) -> Reduced:
    """Add a sentence to the trailing [searchslim] note (or start one)."""
    lines = reduced.text.split("\n") if reduced.text else []
    if lines and lines[-1].startswith(NOTE_PREFIX):
        lines[-1] = f"{lines[-1]} {sentence}"
    else:
        lines.append(f"{NOTE_PREFIX} {sentence}")
    return Reduced(text="\n".join(lines), stats=reduced.stats)
