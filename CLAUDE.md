# CLAUDE.md

This repo builds `searchslim`, a drop-in layer that shrinks the output of
agent search tools (Claude Code Grep/Glob, `rg`, `grep`, `fd`, `find`) so the
agent reads less context but keeps every piece of code evidence it needs.
The main consumer is an agent like Claude Code, not a human: design output for
an agent that must decide from it whether to search again.

## Phase 1 plan (keep in this order)

1. Common data model + parsers for every tool's output shape. **Done** (`models.py`, `parsers.py`).
2. Deterministic rules (dedupe, merge overlapping ranges, keep `path:line`, budget). **Done** (`rules.py`).
3. Drop-in integration via a Claude Code PreToolUse hook; no new tool. **Done** (`rewrite.py`, `hooks.py`, `.claude/settings.json`).
4. **Done** (`benchmark/`, PR #3). Benchmark harness: raw vs rules vs rules+model on tokens, latency, cost,
   extra searches, and lost critical evidence.
5. Optional light-model reranker on top of rules (rules+model mode). It may only reorder or
   select existing blocks; it must never rewrite code or produce new results. **Done** (`rerank.py`).

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
- `src/searchslim/rewrite.py`  wraps plain rg/grep/fd/find shell commands in `searchslim run --`
- `src/searchslim/hooks.py`    PreToolUse hook for Bash, Grep, Glob
- `src/searchslim/rerank.py`   rules+model: units, scorers (lexical default, Claude optional), budgeted selection
- `src/searchslim/session.py`  session memory: lines already shown in this agent session (lock-free store)
- `src/searchslim/install.py`  `searchslim install [--user|DIR]` merges the hook into Claude Code settings
- `src/searchslim/cli.py`      `searchslim filter` (stdin), `searchslim run -- <cmd>`, `searchslim hook`,
                               `searchslim bench-model` (the benchmark's `--model-cmd` contract)
- `tests/`                     pytest; one test runs real `rg` if installed

## Hook behaviour (this repo dogfoods it via `.claude/settings.json`)

- Bash: plain `rg`/`grep`/`fd`/`find` commands are rewritten through `updatedInput`
  to `searchslim run -- <cmd>`. Pipes, redirects, `$(...)`, chaining (other than a
  leading `cd x &&`) and side-effect flags (`find -exec/-delete`, `fd -x`, `rg -r`)
  are never rewritten. `rg`/`grep` get `--with-filename --line-number` / `-H -n`
  added so every line keeps its anchor.
- Grep/Glob: a hook cannot rewrite a built-in tool's result, so the hook runs the
  equivalent `rg` itself. If that fits the budget it returns nothing and the real
  tool runs. Otherwise it returns `permissionDecision: deny` with the reduced result
  as the reason, which Claude Code passes to the model. The reason starts with a
  header saying this is the result, not an error.
- Any failure returns nothing, so the original call runs. `SEARCHSLIM=off` (env, or
  as a command prefix) disables it; `SEARCHSLIM_MAX_TOKENS` sets the budget.
- Hook stdin is the event JSON: any subprocess the hook starts must get
  `stdin=DEVNULL` and an explicit path, or `rg` will search the JSON.

## Session memory (session.py)

- Only for content results over budget: lines an earlier search in the same session showed
  (same absolute path, line number and text) are left out and referenced as `path:line`
  ranges in the note; new context lines keep the run up to their nearest match. Repeating a
  search therefore pages forward. Small outputs still pass through unchanged.
- Store: one directory per session (`session_id`, plus `.agent_id` inside subagents), one
  atomically written file per call, readers take the union; merged past 64 files. No locks.
  Every race can only make a line look unseen (shown again), never hide one.
- `PreCompact` clears the session. Bash rewrites pass `--session=<id>` to `searchslim run`.
  Grep/Glob calls the hook lets through are not recorded (their output is the real tool's).
- `SEARCHSLIM_SESSION=off` disables; `SEARCHSLIM_CACHE_DIR` moves the store.
  Benchmark: `benchmark/sessions.py` (`run`, `latency`).

## rules+model (rerank.py)

- Only runs when the rules layer would drop something; otherwise output is identical to rules.
- Blocks are split into units (a cluster of up to 5 nearby matches plus the context lines
  closest to it). A scorer returns one score per unit; the model never returns text, so
  output is always rendered from parsed input lines.
- Selection: best units keep context while they fit in half the budget, then only match
  lines are added. Files are printed best-first, lines in file order; the note lists
  omitted files most-relevant-first.
- `LexicalScorer` (default, no deps): BM25 over identifier-split, lightly stemmed terms of
  intent + subtask, plus a bonus for definitions (bigger when the defined name is a query
  term), and down-weights for comment-only matches, test code, docs and changelogs.
- `ClaudeScorer`: lexical top-60 shortlist, then `claude-haiku-4-5` returns an ordering of
  unit ids (`pip install 'searchslim[claude]'`, API credentials needed). Unknown ids are ignored.
- Hook: ranking is on by default (`SEARCHSLIM_RERANK=lexical|claude|off`); intent = last user message and subtask = last
  assistant text from the hook's `transcript_path`.

Benchmark results live in `benchmark/results/` (latest: 2026-09-29, rules+model keeps 34/34 critical lines at the default budget vs 30/34 for rules).

## Commands

```sh
python3 -m pip install -e '.[dev]'
python3 -m pytest -q
rg -n -C2 foo src | searchslim filter --stats
searchslim run --max-tokens 1500 -- rg -n -C2 foo src
searchslim run --rerank lexical --intent "why does X fail" -- rg -n -C2 foo src
```

No runtime dependencies; keep it that way for the rules layer. Token counts
use a chars/4 estimate; the benchmark should use a real tokenizer.
