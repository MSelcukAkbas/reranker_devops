"""Session memory: don't re-send code lines the agent has already been shown.

Claude often searches the same area several times in one session, sometimes
as parallel tool calls in one turn. When a later search is over budget, lines
an earlier search already showed (same file, line number and text) are not
printed again; the trailing note lists them as `path:line` ranges instead, so
the evidence stays referenced and the budget goes to lines the agent has not
seen. Small outputs still pass through unchanged, and a line whose text
changed since counts as new. The lossless view never leaves a line out for
this: only its last level (the coverage view) does.

Two caches per session, because they go stale for different reasons:

- content (physical): what files on disk contained, as a content hash per
  file with the size and mtime it was taken at. It stays true whatever the
  agent's context holds, so compaction keeps it; it lets a later check see
  whether a file changed without re-reading it.
- visible (model-visible evidence): the lines the agent was actually shown,
  each tied to the content hash of the file it came from. A line counts as
  already shown only while it is still in the agent's context and still
  true: compaction (PreCompact) clears this cache, an Edit/Write of a file
  invalidates that file's lines, and any other change to the file (a Bash
  `sed -i`, a checkout, a formatter) makes its hash differ so they lapse too.
  Only what the agent sees counts: a result Claude Code persists to a file
  (Grep over 20,000 chars, Bash over 30,000) records just its ~2 KB preview.

Storage is safe for concurrent hook processes, on POSIX and Windows alike,
without locks: each call writes its own file (temp file + os.replace), named
by time, into the session's directory, and readers take the union of all
files. Every race (a sibling call still running, a file mid-compaction) can
only make a line look unseen, so it gets shown again; it never hides a line.
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
# Claude Code shows a Grep result inline up to 20,000 chars (hooks.GREP_INLINE_CHARS)
# and Bash output up to 30,000; longer results are persisted to a file and the
# agent sees only a ~2 KB preview, so only that much counts as shown.
VISIBLE_CHARS = 20000
BASH_VISIBLE_CHARS = 30000
PREVIEW_CHARS = 1800
# A file modified this recently is re-hashed even if size and mtime match.
RACY_NS = 2_000_000_000
MISSING = "missing"


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


def file_id(path: str) -> str:
    raw = os.path.normcase(os.path.normpath(path)).encode("utf-8", "replace")
    return hashlib.blake2b(raw, digest_size=8).hexdigest()


def line_key(path: str, number: int, text: str) -> str:
    raw = f"{os.path.normcase(os.path.normpath(path))}\0{number}\0{text}".encode("utf-8", "replace")
    return hashlib.blake2b(raw, digest_size=10).hexdigest()


class SessionStore:
    """One agent session's content cache and model-visible evidence cache."""

    def __init__(self, session_id: str, root: Path | None = None, visible_chars: int = VISIBLE_CHARS):
        safe = _SAFE_ID.sub("_", session_id)[:128]
        if not safe.strip("._"):
            raise ValueError("empty session id")
        self.dir = (root or cache_root()) / safe
        self.visible = self.dir / "visible"
        self.content = self.dir / "content"
        # Output longer than this is persisted by Claude Code; the agent sees a preview.
        self.visible_chars = visible_chars

    # -- model-visible evidence --

    def seen(self) -> set[str]:
        """Keys of lines the agent was shown that are still in context and still true."""
        invalid_after = self._invalidations()
        known = self._content()
        current: dict[str, str] = {}
        keys: set[str] = set()
        for ns, f in _parts(self.visible, ".keys"):
            try:
                rows = f.read_text(encoding="utf-8").splitlines()
            except OSError:  # removed by a concurrent compaction: its keys live on elsewhere
                continue
            for row in rows:
                parts = row.split(" ")
                if len(parts) == 1 and parts[0]:
                    keys.add(parts[0])  # a key tied to no file
                    continue
                if len(parts) != 3:
                    continue
                key, fid, digest = parts
                if invalid_after.get(fid, -1) >= ns:
                    continue
                if fid not in current:
                    entry = known.get(fid)
                    current[fid] = self._digest(entry[3], entry) if entry else ""
                if current[fid] == digest:
                    keys.add(key)
        return keys

    def record_output(self, text: str, default_path: str, cwd: str) -> None:
        """Record the content lines of output the agent is shown (visible part only)."""
        from .parsers import parse

        text = visible_part(text, self.visible_chars)
        if text:
            self.record_lines(shown_entries(parse(text, kind=Kind.CONTENT, default_path=default_path), cwd))

    def record_lines(self, entries) -> None:
        """Record (absolute path, line number, text) triples as shown now."""
        by_path: dict[str, list[str]] = {}
        for path, number, text in entries:
            by_path.setdefault(path, []).append(line_key(path, number, text))
        if not by_path:
            return
        known = self._content()
        fresh: list[str] = []
        rows: list[str] = []
        for path, keys in by_path.items():
            fid = file_id(path)
            size, mtime, digest = self._fingerprint(path, known.get(fid))
            if known.get(fid, (None,))[:3] != (size, mtime, digest):
                fresh.append(f"{fid}\t{size}\t{mtime}\t{digest}\t{path}")
            rows.extend(f"{k} {fid} {digest}" for k in sorted(set(keys)))
        if fresh:
            self.content.mkdir(parents=True, exist_ok=True)
            _atomic_write(self.content / _part_name("fp"), "\n".join(fresh))
        self.visible.mkdir(parents=True, exist_ok=True)
        _atomic_write(self.visible / _part_name("keys"), "\n".join(rows))
        self._maybe_compact()
        _prune_old(self.dir.parent)

    def record(self, keys) -> None:
        """Record bare line keys (tied to no file: only compaction drops them)."""
        keys = sorted(set(keys))
        if not keys:
            return
        self.visible.mkdir(parents=True, exist_ok=True)
        _atomic_write(self.visible / _part_name("keys"), "\n".join(keys))
        self._maybe_compact()
        _prune_old(self.dir.parent)

    def invalidate(self, path: str) -> None:
        """The agent changed this file (Edit/Write): its earlier lines no longer count as shown."""
        if not self.dir.exists():
            return  # nothing recorded in this session
        self.visible.mkdir(parents=True, exist_ok=True)
        _atomic_write(self.visible / _part_name("inv"), file_id(path))

    def clear_visible(self) -> None:
        """Compaction: the agent's context no longer holds earlier results; disk facts stay."""
        shutil.rmtree(self.visible, ignore_errors=True)

    def clear(self) -> None:
        shutil.rmtree(self.dir, ignore_errors=True)

    # -- content (physical) cache --

    def _content(self) -> dict[str, tuple[int, int, str, str]]:
        """file id -> (size, mtime_ns, digest, path), the latest record per file."""
        out: dict[str, tuple[int, int, str, str]] = {}
        for _, f in _parts(self.content, ".fp"):  # oldest first: later records win
            try:
                rows = f.read_text(encoding="utf-8").splitlines()
            except OSError:
                continue
            for row in rows:
                parts = row.split("\t", 4)
                if len(parts) == 5 and parts[1].lstrip("-").isdigit() and parts[2].lstrip("-").isdigit():
                    out[parts[0]] = (int(parts[1]), int(parts[2]), parts[3], parts[4])
        return out

    def _fingerprint(self, path: str, known=None) -> tuple[int, int, str]:
        try:
            st = os.stat(path)
        except OSError:
            return (-1, -1, MISSING)
        # Reuse a known hash when size and mtime match, unless the mtime is so
        # recent that a same-size edit could still share it (git's "racy" case).
        if known and known[:2] == (st.st_size, st.st_mtime_ns) and time.time_ns() - st.st_mtime_ns > RACY_NS:
            return (st.st_size, st.st_mtime_ns, known[2])
        try:
            with open(path, "rb") as fh:
                digest = hashlib.blake2b(fh.read(), digest_size=12).hexdigest()
        except OSError:
            return (-1, -1, MISSING)
        return (st.st_size, st.st_mtime_ns, digest)

    def _digest(self, path: str, known=None) -> str:
        return self._fingerprint(path, known)[2]

    def _invalidations(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for ns, f in _parts(self.visible, ".inv"):
            try:
                for fid in f.read_text(encoding="utf-8").split():
                    out[fid] = max(out.get(fid, -1), ns)
            except OSError:
                continue
        return out

    def _maybe_compact(self) -> None:
        parts = _parts(self.visible, ".keys")
        if len(parts) >= COMPACT_AT:
            invalid_after = self._invalidations()
            rows: set[str] = set()
            merged = []
            for ns, f in parts:
                try:
                    lines = f.read_text(encoding="utf-8").splitlines()
                except OSError:
                    continue
                merged.append(f)
                for row in lines:
                    fields = row.split(" ")
                    if len(fields) == 3 and invalid_after.get(fields[1], -1) >= ns:
                        continue
                    if row:
                        rows.add(row)
            # Write the union first, then drop the parts: a reader in between sees
            # duplicates, never fewer keys than were written (except the brief
            # window after an unlink, which only makes lines look unseen). The
            # merged file takes the oldest part's time, so an invalidation that
            # raced with this merge still applies to every key in it.
            oldest = parts[0][0]
            _atomic_write(self.visible / f"{oldest}-merged-{uuid.uuid4().hex[:8]}.keys", "\n".join(sorted(rows)))
            _unlink(merged)
        fps = _parts(self.content, ".fp")
        if len(fps) >= COMPACT_AT:
            latest = self._content()
            # Named now, so it wins over the parts; a record racing with this is
            # at worst superseded by an older fingerprint, which a later stat catches.
            _atomic_write(
                self.content / _part_name("fp"),
                "\n".join(f"{fid}\t{s}\t{m}\t{d}\t{p}" for fid, (s, m, d, p) in latest.items()),
            )
            _unlink([f for _, f in fps])


def _part_name(ext: str) -> str:
    return f"{time.time_ns()}-{uuid.uuid4().hex[:8]}.{ext}"


def _parts(directory: Path, suffix: str) -> list[tuple[int, Path]]:
    """(time, file) for a cache directory's part files, oldest first."""
    try:
        files = [f for f in directory.iterdir() if f.suffix == suffix]
    except OSError:
        return []
    out = []
    for f in files:
        stamp = f.name.split("-", 1)[0]
        if stamp.isdigit():
            out.append((int(stamp), f))
    return sorted(out)


def _unlink(files) -> None:
    for f in files:
        try:
            f.unlink()
        except OSError:
            pass


def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_name(f"{path.name}.tmp{uuid.uuid4().hex[:8]}")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _prune_old(root: Path) -> None:
    cutoff = time.time() - MAX_AGE_S
    try:
        for d in root.iterdir():
            if d.is_dir() and d.stat().st_mtime < cutoff:
                shutil.rmtree(d, ignore_errors=True)
    except OSError:
        pass


def visible_part(text: str, limit: int) -> str:
    """The part of a tool result the agent sees: all of it, or a persisted result's preview."""
    if len(text) <= limit:
        return text
    head = text[:PREVIEW_CHARS]
    return head[: head.rfind("\n")] if "\n" in head else ""


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


def shown_entries(result: SearchResult, cwd: str) -> list[tuple[str, int, str]]:
    """(absolute path, line, text) of the content lines in (parsed) output."""
    if result.kind is not Kind.CONTENT:
        return []
    return [
        (_abs(ln.path or result.default_path, cwd), ln.number, ln.text)
        for ln in result.lines
        if ln.path or result.default_path
    ]


def shown_keys(result: SearchResult, cwd: str) -> set[str]:
    """Keys of the content lines in (parsed) output that the agent will see."""
    return {line_key(*entry) for entry in shown_entries(result, cwd)}


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
