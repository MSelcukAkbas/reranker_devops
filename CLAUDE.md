# CLAUDE.md

This repo builds `searchslim`, a drop-in layer that shrinks the output of
agent search tools (Claude Code Grep/Glob, `rg`, `grep`, `fd`, `find`) so the
agent reads less context but keeps every piece of code evidence it needs.
The main consumer is an agent like Claude Code, not a human: design output for
an agent that must decide from it whether to search again.

## Phase 1 plan (keep in this order)

1. Common data model + parsers for every tool's output shape. **Done** (`models.py`, `parsers.py`).
2. Deterministic rules (dedupe, merge overlapping ranges, keep `path:line`, budget). **Done** (`rules.py`).
3. Drop-in integration: wrappers for `rg`/`fd`, Claude Code hooks for Grep/Glob. Must not add a new tool.
4. Benchmark harness: raw vs rules vs rules+model on tokens, latency, cost,
   extra searches, and lost critical evidence.
5. Optional light-model reranker on top of rules. It may only reorder or
   select existing blocks; it must never rewrite code or produce new results.

## Invariants (tests enforce these; do not break them)

- Output keeps the tool's own format (`path:line:text`, `path-line-text`, `--`,
  one path per line, `path:N`, pathless `N:text` for single-file searches),
  so it is a drop-in replacement. Claude Code framing lines (`Found N files`,
  truncation notices) are kept verbatim around the body.
- Every emitted body line is a line from the raw input (only very long lines
  are clipped, with a `…[+N chars]` marker). Nothing is rewritten or invented.
- Rules never reorder by guessed relevance. When over budget they drop in a
  fixed order: context lines, then matches beyond N per file (N is the largest
  cap that fits the budget, at least `max_matches_per_file`, or 1 for a
  single-file search), then whole files from the end. Kept files are a prefix
  of the input order, and kept matches a prefix of each file's line order.
- Anything dropped is accounted for in one trailing `[searchslim] ...` note
  (counts per file or directory) so the agent knows what to narrow and re-run.
  Every omitted file is covered: files past the named ones are rolled up into
  directories, so input order (rg is unordered without `--sort path`) never
  hides where evidence went.
- Small outputs pass through unchanged.

## Layout

- `src/searchslim/models.py`   Line, Block, SearchResult, Kind
- `src/searchslim/parsers.py`  raw text -> SearchResult (auto-detects shape; rg --json supported)
- `src/searchslim/rules.py`    SearchResult -> reduced text + stats
- `src/searchslim/cli.py`      `searchslim filter` (stdin) and `searchslim run -- <cmd>`
- `tests/`                     pytest; one test runs real `rg` if installed

## Commands

```sh
python3 -m pip install -e '.[dev]'
python3 -m pytest -q
rg -n -C2 foo src | searchslim filter --stats
searchslim run --max-tokens 1500 -- rg -n -C2 foo src
```

No runtime dependencies; keep it that way for the rules layer. Token counts
use a chars/4 estimate; the benchmark should use a real tokenizer.
