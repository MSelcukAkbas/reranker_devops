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
4. Benchmark harness: raw vs rules vs rules+model on tokens, latency, cost,
   extra searches, and lost critical evidence.
5. Optional light-model reranker on top of rules. It may only reorder or
   select existing blocks; it must never rewrite code or produce new results.

## Invariants (tests enforce these; do not break them)

- Output keeps the tool's own format (`path:line:text`, `path-line-text`, `--`,
  one path per line, `path:N`), so it is a drop-in replacement.
- Every emitted body line is a line from the raw input (only very long lines
  are clipped, with a `…[+N chars]` marker). Nothing is rewritten or invented.
- Rules never reorder by guessed relevance. When over budget they drop in a
  fixed order: context lines, then matches beyond N per file, then whole files
  from the end. Kept files are a prefix of the input order.
- Anything dropped is accounted for in one trailing `[searchslim] ...` note
  (counts per file or directory) so the agent knows what to narrow and re-run.
- Small outputs pass through unchanged.

## Layout

- `src/searchslim/models.py`   Line, Block, SearchResult, Kind
- `src/searchslim/parsers.py`  raw text -> SearchResult (auto-detects shape; rg --json supported)
- `src/searchslim/rules.py`    SearchResult -> reduced text + stats
- `src/searchslim/rewrite.py`  wraps plain rg/grep/fd/find shell commands in `searchslim run --`
- `src/searchslim/hooks.py`    PreToolUse hook for Bash, Grep, Glob
- `src/searchslim/cli.py`      `searchslim filter` (stdin), `searchslim run -- <cmd>`, `searchslim hook`
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

## Commands

```sh
python3 -m pip install -e '.[dev]'
python3 -m pytest -q
rg -n -C2 foo src | searchslim filter --stats
searchslim run --max-tokens 1500 -- rg -n -C2 foo src
```

No runtime dependencies; keep it that way for the rules layer. Token counts
use a chars/4 estimate; the benchmark should use a real tokenizer.
