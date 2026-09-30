# CLAUDE.md

This repo builds `searchslim`, a drop-in layer that shrinks the output of
agent search tools (Claude Code Grep/Glob, `rg`, `grep`, `fd`, `find`) so the
agent reads less context but keeps every piece of code evidence it needs.
The main consumer is an agent like Claude Code, not a human: design output for
an agent that must decide from it whether to search again.

## Phase 1 plan (keep in this order)

1. Common data model + parsers for every tool's output shape. **Done** (`models.py`, `parsers.py`).
2. Deterministic rules (dedupe, merge overlapping ranges, keep `path:line`, budget). **Done** (`rules.py`).
3. Drop-in integration via Claude Code hooks (PreToolUse Bash, PostToolUse Grep/Glob); no new tool. **Done** (`rewrite.py`, `hooks.py`, `.claude/settings.json`).
4. **Done** (`benchmark/`, PR #3). Benchmark harness: raw vs rules vs rules+model on tokens, latency, cost,
   extra searches, and lost critical evidence.
5. Optional light-model reranker on top of rules (rules+model mode). It may only reorder or
   select existing blocks; it must never rewrite code or produce new results. **Done** (`rerank.py`).

## Invariants (tests enforce these; do not break them)

- Output keeps the tool's own format (`path:line:text`, `path-line-text`, `--`,
  one path per line, `path:N`, pathless `N:text` for single-file searches, tree/ls -R lines),
  so it is a drop-in replacement. Claude Code framing lines (`Found N files`,
  truncation notices) are kept verbatim around the body.
- Every emitted body line is a line from the raw input (only very long lines
  are clipped, with a `…[+N chars]` marker). Nothing is rewritten or invented.
  The lossless view changes only the layout: parsed back, each (path, line, text)
  is a raw line (text up to indentation for a line written once for many places);
  only its L2 projection writes names instead of text, and says so.
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
- Lossless view (`lossless.py`, `Config.view="lossless"`; hook and CLI default since 0.6,
  max_tokens 4800 (~19k chars: Claude Code shows Grep results over 20,000 chars (measured) only as a ~2 KB preview of a persisted file; the hook keeps an inline tool result rather than return one within 500 chars of that limit, `hooks.GREP_INLINE_CHARS`), trigger 1500; `SEARCHSLIM_VIEW=coverage|notes` restores 0.4/0.3 with 2000/6000):
  every match kept, only repetition removed. L1: dedupe, overlapping context merged, path once per file
  (rg `--heading`: `path` line, `N:text`/`N-text`, blank line between files; a one-line file or a path
  that would not parse back as a heading stays flat `path:N:text`); files sharing a directory go under a
  `dir/` line (first-seen dir order) as `  name:N:text` or `  name` + `    N:text`, whichever is shorter
  (a long file stays on its own heading when that is cheaper), a match text (stripped, >= 16 chars)
  on >= 3 lines written once as `[searchslim] N matches are this same line: <text>` + indented
  `  path:n,m` rows (no-context outputs only, and only when the result is shorter); path lists grouped as `dir/` + indented names. Used only
  when it saves >= 20% (else raw passes). Still over max_tokens: the same without context lines (lead
  `[searchslim] all N matches in F files; context lines left out.`), then L2 projection (recognizers
  env/require/import, only when the search pattern names that kind and >= 50% of match lines are read;
  names with every location, by name or by file (dir-grouped, all names in one trailing line so a persisted-output preview shows locations), whichever is shorter; other match lines
  kept as L1). If none fits max_tokens, the smallest of these (every match location kept) is used up to
  3x max_tokens (`EMERGENCY_FACTOR`, stats `over_budget`); only past that L3 = the coverage view below.
  (0.6.1 had a per-name summary here; live it dropped whole services behind "+N other dirs", removed.) Output the parser cannot account for (`parsers.reliable`: >5%
  unparsed lines, context lines without any match, or `path:text` lines taken as paths, e.g. Grep
  `-n false`) passes through raw in every view: in 0.6.0 such output lost 884 of 887 lines. The parser reads headings, same-line groups and grouped path lists
  back, so session memory and the benchmark see every line. Glob keeps its own `truncated` flag on L1.
- Coverage view (`coverage.py`, `Config.view="coverage"`; 0.4 default, now the lossless view's L3): when matches would be dropped, output leads with one `[searchslim]` summary line
  and indented index rows (`  path  N matches  La-b  def Lx  (k expanded)`, directory rows when many
  files) covering every matching file, then the evidence body in the tool's format. `coverage.split_note`
  separates index from body. Library `Config()` keeps `view="notes"`, so rules tests are unchanged.

## Layout

- `src/searchslim/models.py`   Line, Block, SearchResult, Kind
- `src/searchslim/parsers.py`  raw text -> SearchResult (auto-detects shape; rg --json supported)
- `src/searchslim/rules.py`    SearchResult -> reduced text + stats
- `src/searchslim/rewrite.py`  wraps shell search commands (and filter pipelines) in `searchslim run --`;
                               `prepare` adds anchor flags at run time
- `src/searchslim/hooks.py`    hook: PreToolUse Bash/PowerShell, PostToolUse Grep/Glob/Bash/PowerShell, PreCompact
- `src/searchslim/lossless.py` lossless view: grouped/factored output (L1), projection (L2), driver
- `src/searchslim/coverage.py` coverage view: file index (lossless) + selected evidence (lossy)
- `src/searchslim/compact.py`  test/build output compaction (Bash/PowerShell PostToolUse, `searchslim compact`)
- `src/searchslim/rerank.py`   rules+model: units, scorers (lexical default, Claude optional), budgeted selection
- `src/searchslim/session.py`  session memory: lines already shown in this agent session (lock-free store)
- `src/searchslim/install.py`  `searchslim install [--user|DIR]` merges the hook into Claude Code settings
- `src/searchslim/cli.py`      `searchslim filter` (stdin), `searchslim run -- <cmd>`, `searchslim hook`,
                               `searchslim bench-model` (the benchmark's `--model-cmd` contract)
- `tests/`                     pytest; one test runs real `rg` if installed

## Hook behaviour (this repo dogfoods it via `.claude/settings.json`)

- Bash: `rg`, `grep`, `git grep`, `fd`, `find`, `git ls-files`, `tree`, `ls -R` commands are
  rewritten through `updatedInput` to `searchslim run -- <cmd>`, only when the tool is on PATH
  (an alias-only `rg` is left alone). `run` adds anchor flags at run time via `rewrite.prepare`,
  after glob expansion: `--with-filename --line-number` / `-H -n` / `git grep -n`, except that a
  single-file search stays pathless (`N:text`, `default_path` names it) so it is not pushed over
  budget by path prefixes. `2>/dev/null` and `2>&1` are allowed. Pipelines are wrapped only when
  every later stage is a line filter (`head`/`tail -n`, `sort`, `uniq` without -c, `grep`/`rg`
  filter with no file args or output-mode flags); they run unchanged via
  `searchslim run --kind=K --shell -- '<pipeline>'` (POSIX only). Other redirects, `$(...)`,
  chaining (other than a leading `cd x &&`) and side-effect flags (`find -exec/-delete`,
  `fd -x`, `rg -r`, `git grep -O`, `tree -o`) are never rewritten.
- PowerShell (Claude Code's PowerShell tool; on Windows agents used it instead of Grep for
  big searches): `rewrite.rewrite_powershell` wraps a lone `rg ...` as `& '<python>' -m searchslim
  run <opts> rg ...` (no `--`: some PowerShell versions drop it) and `Get-ChildItem -Recurse` /
  `Select-String` pipelines as `<cmd> | Out-String -Stream -Width 4096 | & '<python>' -m searchslim
  filter`, prefixed with a UTF-8 `$OutputEncoding`/`[Console]::OutputEncoding` assignment. Anything
  with `$`, `()`, `{}`, `;`, `&`, redirects, backticks or other cmdlets is left alone.
- `tree`/`ls -R` output is `Kind.LINES`: kept verbatim, no dedupe; when over budget the deepest
  levels are dropped (tree entries / ls sections) and the note counts hidden entries per directory.
- Grep/Glob: the tool runs normally; on PostToolUse the hook runs the equivalent `rg`
  reduces the tool's own `tool_response` (Grep `content` or `filenames`, Glob `filenames`) when
  it is over budget and returns it via `updatedToolOutput` as the same object, other fields
  kept: Claude Code validates it against the tool's output schema and silently keeps the
  original on a mismatch (a plain string is rejected). In list modes the note is the last
  `filenames` entry. Without a `tool_response` dict it falls back to running `rg` itself.
  PreToolUse does nothing for them: a deny reaches the model as a "hook error" and made
  agents search again (live Windows test). `SEARCHSLIM_GREP_MODE=deny` restores the
  PreToolUse deny-with-reason answer for Claude Code versions without `updatedToolOutput`.
- `install` writes the command PowerShell and Git Bash both parse: unquoted, `/`
  separators on Windows (a quoted path then args is a PowerShell ParserError); a path
  with spaces gets its 8.3 short form, else `& '<path>' ...` with `"shell": "powershell"`.
  Re-running install rewrites older install-written commands.
- Any failure returns nothing, so the original call runs. `SEARCHSLIM=off` (env, or
  as a command prefix) disables it; `SEARCHSLIM_MAX_TOKENS` sets the budget.
- Hook and CLI only touch outputs above `Config.trigger_tokens` (lossless default 1500, coverage/notes
  6000; `SEARCHSLIM_TRIGGER_TOKENS`, `--trigger-tokens`); below it output passes unchanged. Trimming
  mid-sized results made agents search again for what was cut (benchmark/results/2026-09-29-trigger.md);
  the lossless view cuts nothing below max_tokens, so its trigger is lower. A hook result equal to the
  raw text returns nothing.
  Library `Config()` keeps trigger 0 (= max_tokens), so tests and the benchmark are unchanged.
- The note is a neutral count (what is not shown, how many, where) with no advice: any
  wording about truncation or narrowing led agents to search again in live runs. Net gain
  only shows on very large outputs (README "Canlı Windows testi"); don't retune the note.
- Hook stdin is the event JSON: any subprocess the hook starts must get
  `stdin=DEVNULL` and an explicit path, or `rg` will search the JSON.

## Test/build output (compact.py)

- PostToolUse Bash/PowerShell: `tool_response` `stdout`/`stderr` strings are compacted and
  returned via `updatedToolOutput` as the same object (other fields kept). Output-aware, not
  command-aware: a runner's rules apply only when its own summary/marker line is in the output
  (pytest `=== ... in 0.2s ===`, jest `Tests:`, vitest `Test Files`, mocha `N passing (`, go
  `--- FAIL:`/`ok pkg 0.1s`, cargo `test result:`, dotnet `Passed!/Failed! -`), build progress
  families only with >= 10 lines. Agent-written summaries and other stdout are never touched.
- Dropped: passing/skipped status lines, all-pass pytest progress lines (before the first report
  section only), lines under jest `PASS` suites and pytest `PASSES`, build progress, and the
  middle of 5+ consecutive lines equal after masking timestamps only (an inline
  `[searchslim] N similar lines not shown` marks the spot). Lines mentioning
  error/fail/warn/exception/assert in a dropped section stay, with their test/suite header.
  go `=== RUN X` stays when log lines follow it. Kept lines are verbatim, in order; one trailing
  `[searchslim] not shown: ...` count.
- PreToolUse also wraps plain test-runner commands (`rewrite.is_test_command`: pytest, python -m pytest,
  uv/poetry run, npx/pnpm/yarn jest|vitest|mocha, npm/yarn/pnpm test, go test, cargo test/nextest,
  dotnet test; not watch/--pdb) as `searchslim run --compact -- <cmd>` (Bash: optional `cd x &&`/`cd x;`,
  VAR=v prefixes, `2>&1`; PowerShell: optional `Set-Location|cd|sl|Push-Location x;`/`&&` prefix and
  trailing `2>&1`). `run --compact` captures both
  streams, compacts, keeps the exit code. Needed because on exit != 0 Claude Code fires
  PostToolUseFailure (only a middle-truncated `error` string, no tool_response), and over ~30 KB
  hooks get a cut `stdout` (live Windows test, 0.5.0). PostToolUse still covers unwrapped commands:
  when `tool_response.persistedOutputPath` is set it compacts that full file and drops the
  `persistedOutput*` fields.
- Runs only above 2000 tokens (`SEARCHSLIM_COMPACT_TRIGGER_TOKENS`) and when it saves >= 20%;
  `SEARCHSLIM_COMPACT=off` disables. Default pytest and non-TTY jest/vitest output is already
  short; the gain is on verbose runs. Real runner outputs are in `tests/fixtures/compact/`.

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
- CLI `filter`/`run`: ranking is on by default too (`--rerank off` or `SEARCHSLIM_RERANK=off`);
  `run` takes the query pattern from the command. CLI and hook read/write UTF-8 regardless of
  locale (Windows cp1252 garbled rg output). Directory grouping normalizes `\` and leading `./`/`.\`
  (`rules.group_path`); shown lines keep the tool's spelling.
- Hook: ranking is on by default (`SEARCHSLIM_RERANK=lexical|claude|off`); intent = last user message and subtask = last
  assistant text from the hook's `transcript_path`.

Benchmark results live in `benchmark/results/` (latest: 2026-09-30-lossless.md: hook defaults 76.6k -> 42.9k tokens, 36/36 critical lines kept; 0.4 coverage 48.5k).

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
