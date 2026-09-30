"""Offline benchmark for test/build output compaction (compact.py).

  python3 benchmark/compact_bench.py [--tokenizer auto|api|chars] [--out results.md]

Runs `compact` over the real runner/compiler outputs in tests/fixtures/compact
and prints tokens raw -> compacted per fixture and per category, plus lost
evidence: failure/error lines of the raw output that are neither shown nor
listed. A line counts as listed when it is a diagnostic dropped as a repeat
and its path and position appear in a `more places` note.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "benchmark"))

from bench import TokenCounter  # noqa: E402
from searchslim.compact import ANSI_RE, _diagnostics, compact  # noqa: E402
from searchslim.rules import NOTE_PREFIX  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures" / "compact"
CATEGORIES = {
    "test runners": ["pytest-v.txt", "pytest-ra.txt", "vitest-verbose.txt", "go-v.txt", "cargo-test.txt", "jest.txt"],
    "coverage": ["jest-coverage.txt"],
    "compiler/type-check": ["tsc.txt", "tsc-pretty.txt", "gcc.txt", "cargo-build.txt", "go-vet.txt", "dotnet-build.txt"],
    "lint": ["eslint.txt"],
}
CRITICAL = re.compile(r"(?i)\b(fail(ed|ure|s)?|errors?|panic(ked)?|exception|assert\w*)\b|^E\s")


def lost_evidence(raw: str, out: str) -> int:
    shown = set(out.splitlines())
    notes = [ln for ln in shown if ln.startswith(NOTE_PREFIX) and "more places" in ln]
    lines = raw.splitlines()
    record_of = {}
    for rec in _diagnostics([ANSI_RE.sub("", ln) for ln in lines]):
        for k in range(rec.start, rec.end):
            record_of[k] = rec
    lost = 0
    for i, ln in enumerate(lines):
        if not CRITICAL.search(ln) or ln in shown:
            continue
        rec = record_of.get(i)
        # Dropped repeat: its location must be listed (path, possibly under a
        # common directory, and position).
        if rec and any(rec.pos in n and Path(rec.path).name in n for n in notes):
            continue
        lost += 1
    return lost


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--tokenizer", choices=["auto", "api", "chars"], default="auto")
    p.add_argument("--out", help="also write the markdown table to this file")
    args = p.parse_args()
    count = TokenCounter(args.tokenizer)

    rows, totals = [], []
    for category, names in CATEGORIES.items():
        raw_sum = out_sum = lost_sum = 0
        for name in names:
            raw = (FIXTURES / name).read_text(encoding="utf-8")
            result = compact(raw, trigger_tokens=0)
            out = result.text if result else raw
            raw_t, out_t = count(raw), count(out)
            lost = lost_evidence(raw, out)
            tools = ", ".join(result.stats["tools"]) if result else "(unchanged)"
            rows.append(f"| {category} | {name} | {tools} | {raw_t} | {out_t} | {_pct(raw_t, out_t)} | {lost} |")
            raw_sum, out_sum, lost_sum = raw_sum + raw_t, out_sum + out_t, lost_sum + lost
        totals.append(f"| {category} | {raw_sum} | {out_sum} | {_pct(raw_sum, out_sum)} | {lost_sum} |")

    text = "\n".join(
        [f"Tokens: {count.method}.", "", "| category | fixture | rules | raw | compacted | saved | lost evidence |", "|---|---|---|---:|---:|---:|---:|"]
        + rows
        + ["", "| category | raw | compacted | saved | lost evidence |", "|---|---:|---:|---:|---:|"]
        + totals
    )
    print(text)
    if args.out:
        Path(args.out).write_text(text + "\n", encoding="utf-8")
    return 0


def _pct(raw: int, out: int) -> str:
    return f"{100 * (raw - out) / raw:.0f}%" if raw else "-"


if __name__ == "__main__":
    raise SystemExit(main())
