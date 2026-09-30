from searchslim import Config, Kind, estimate_tokens, parse, slim
from searchslim.hooks import handle
from searchslim.lossless import group_paths, lossless_content, project
from searchslim.rules import NOTE_PREFIX
from searchslim.session import SessionStore

LOSSLESS = Config(view="lossless", max_tokens=7000, trigger_tokens=0)


def keys(text: str, kind=Kind.CONTENT):
    return {(ln.path, ln.number, ln.text, ln.is_match) for ln in parse(text, kind=kind).lines}


def flat(files: int, per_file: int, path="src/services/customer/verification/handler_{i}.ts") -> str:
    return "\n".join(
        f"{path.format(i=i)}:{n * 7 + 1}:    const value{n} = compute(input{i}, {n});"
        for i in range(files)
        for n in range(per_file)
    )


def test_path_printed_once_per_file_and_every_line_kept():
    raw = flat(5, 10)
    out = slim(raw, config=LOSSLESS)
    assert out.stats["level"] == "L1"
    assert keys(out.text) == keys(raw)
    assert out.text.count("src/services/customer/verification/handler_0.ts") == 1
    assert "\n1:    const value0 = compute(input0, 0);" in out.text
    assert estimate_tokens(out.text) < 0.7 * estimate_tokens(raw)


def test_single_line_files_stay_flat():
    raw = flat(1, 6) + "\n" + "\n".join(f"lib/other_{i}.py:3:x = compute_something_long({i})" for i in range(4))
    out = slim(raw, config=LOSSLESS).text
    assert "lib/other_2.py:3:x = compute_something_long(2)" in out.splitlines()
    assert keys(out) == keys(raw)


def test_overlapping_context_is_printed_once():
    raw = "\n".join(
        [f"a/token.ts-{n}-line {n}" for n in (38, 39, 40)]
        + ["a/token.ts:41:MATCH one", "a/token.ts-42-line 42", "a/token.ts-43-line 43", "--"]
        + ["a/token.ts-40-line 40", "a/token.ts-41-MATCH one", "a/token.ts-42-line 42"]
        + ["a/token.ts:43:MATCH two", "a/token.ts-44-line 44", "a/token.ts-45-line 45"]
    )
    out = slim(raw, config=LOSSLESS).text
    assert out.splitlines() == [
        "a/token.ts", "38-line 38", "39-line 39", "40-line 40", "41:MATCH one",
        "42-line 42", "43:MATCH two", "44-line 44", "45-line 45",
    ]


def test_repeated_line_is_written_once_with_its_places():
    line = "const timeout = process.env.TIMEOUT;"
    places = [("src/a.ts", 20), ("src/a.ts", 44), ("src/b.ts", 18), ("src/c.ts", 91), ("src/d.ts", 7)]
    raw = "\n".join(f"{p}:{n}:{line}" for p, n in places) + "\n" + flat(2, 8)
    out = slim(raw, config=LOSSLESS)
    assert f"{NOTE_PREFIX} 5 matches are this same line: {line}" in out.text
    assert "  src/a.ts:20,44" in out.text.splitlines()
    assert out.stats["factored_lines"] == 5
    assert keys(out.text) == keys(raw)


def test_repeated_line_with_other_indentation_counts_as_same():
    raw = "\n".join(f"m{i}.py:{i + 1}:{' ' * (i % 3)}from .models import Block, Kind" for i in range(6)) + "\n" + flat(2, 20)
    out = slim(raw, config=LOSSLESS).text
    assert f"{NOTE_PREFIX} 6 matches are this same line: from .models import Block, Kind" in out
    assert {(p, n) for p, n, _, _ in keys(out)} == {(p, n) for p, n, _, _ in keys(raw)}


def test_short_repeated_lines_are_not_factored():
    raw = "\n".join(f"f{i}.py:{i}:}}" for i in range(10)) + "\n" + flat(3, 10)
    assert "same line" not in slim(raw, config=LOSSLESS).text


def test_pathless_single_file_search():
    raw = "\n".join(f"{n}:    handler_{n}(request, response, next_function_argument)" for n in range(1, 200))
    out = slim(raw, config=LOSSLESS, default_path="app.js")
    assert out.text == raw  # nothing to save: passes through unchanged
    raw2 = raw + "\n" + raw  # duplicated lines
    out2 = slim(raw2, config=LOSSLESS, default_path="app.js")
    assert out2.text == raw


def test_little_saving_passes_through():
    raw = "\n".join(f"pkg{i}/mod.py:1:x = {i}" for i in range(200))  # one line per file
    out = slim(raw, config=LOSSLESS)
    assert out.text == raw and out.stats["level"] == "L0"


def test_below_trigger_passes_through():
    raw = flat(5, 10)
    out = slim(raw, config=Config(view="lossless", max_tokens=7000, trigger_tokens=estimate_tokens(raw)))
    assert out.text == raw


def test_heading_that_would_not_read_back_stays_flat():
    raw = "\n".join(f"{p}:{n}:value = compute_value({n})" for p in ("Makefile", "src/a-1-b.py") for n in range(1, 30))
    out = slim(raw, config=LOSSLESS).text
    assert keys(out) == keys(raw)
    assert "Makefile:1:value = compute_value(1)" in out.splitlines()


def test_framing_lines_stay_around_the_body():
    raw = "Found 50 total occurrences across 5 files.\n" + flat(5, 10)
    out = slim(raw, config=LOSSLESS).text
    assert out.splitlines()[0] == "Found 50 total occurrences across 5 files."


def test_context_dropped_before_any_match():
    raw = "\n".join(
        f"src/mod{i}.py-{n}-    context line {n} with some more words" if n % 5 else f"src/mod{i}.py:{n}:def f{n}():"
        for i in range(20)
        for n in range(1, 100)
    )
    out = slim(raw, config=Config(view="lossless", max_tokens=1500, trigger_tokens=0))
    assert out.stats["level"] == "L1-matches"
    lines = out.text.splitlines()
    assert lines[0] == f"{NOTE_PREFIX} all 380 matches in 20 files; context lines left out."
    assert {k for k in keys(out.text) if k[3]} == {k for k in keys(raw) if k[3]}


def test_env_listing_is_projected_when_still_too_big():
    raw = "\n".join(
        f"apps/backend/src/modules/m{i}/config/settings.ts:{n}:  const v{n} = process.env.KEY_{n % 25} ?? defaults.value{n};"
        for i in range(40)
        for n in range(1, 12)
    )
    config = Config(view="lossless", max_tokens=2500, trigger_tokens=0)
    out = slim(raw, config=config, pattern="process.env")
    assert out.stats["level"] == "L2" and out.stats["recognizer"] == "env"
    assert out.stats["matches_kept"] == out.stats["matches_total"] == 440
    assert "KEY_1, KEY_10, KEY_11, KEY_2" in out.text.splitlines()[0]  # every name, once
    assert "  apps/backend/src/modules/m39/config/settings.ts  KEY_1:1 KEY_2:2" in out.text
    # Without an env-like pattern there is no projection: the ranked coverage view.
    out = slim(raw, config=config, pattern="settings")
    assert out.stats.get("view") == "coverage"


def test_projection_keeps_unrecognized_lines():
    lines = [f"src/f{i}.js:{i + 1}:const m{i} = require('mod{i % 7}');" for i in range(30)]
    lines += ["src/g.js:5:// require is used above", "src/g.js:9:// require again"]
    out = project(parse("\n".join(lines)), Config(), "require")
    assert "the other 2 matching lines below" in out.text
    assert "5:// require is used above" in out.text
    assert "  mod0  src/f0.js:1 src/f7.js:8" in out.text or "  src/f7.js  mod0:8" in out.text


def test_paths_grouped_by_directory():
    paths = [f"C:/Users/dev/project/src/modules/customer/f{i}.ts" for i in range(5)]
    paths += ["C:/Users/dev/project/README.md", "C:\\x\\y\\a.cs", "C:\\x\\y\\b.cs", "src/", "src/"]
    grouped = group_paths(list(dict.fromkeys(paths)))
    assert grouped[:2] == ["C:/Users/dev/project/src/modules/customer/", "  f0.ts"]
    assert "C:/Users/dev/project/README.md" in grouped and "C:\\x\\y\\" in grouped
    assert parse("\n".join(grouped), kind=Kind.PATHS).paths == list(dict.fromkeys(paths))


def test_path_list_through_slim():
    raw = "\n".join(f"./packages/app/src/components/widgets/w{i}/index.tsx" for i in range(3) for _ in range(2))
    raw += "\n" + "\n".join(f"./packages/app/src/components/buttons/b{i}.tsx" for i in range(300))
    out = slim(raw, config=LOSSLESS)
    assert out.stats["level"] == "L1"
    assert parse(out.text, kind=Kind.PATHS).paths == list(dict.fromkeys(p[2:] for p in raw.splitlines()))


def test_session_records_factored_lines(tmp_path):
    session = SessionStore("s1", root=tmp_path)
    line = "import { something } from '../../shared/helpers';"
    raw = "\n".join(f"src/p{i}.ts:1:{line}" for i in range(8)) + "\n" + flat(3, 12)
    slim(raw, config=LOSSLESS, session=session, cwd="/repo")
    from searchslim.session import line_key

    assert line_key("/repo/src/p5.ts", 1, line) in session.seen()


def test_glob_hook_keeps_tool_truncated_flag(tmp_path):
    names = [str(tmp_path / "pkg" / "components" / f"widget_{i:03d}.tsx") for i in range(100)]
    event = {
        "hook_event_name": "PostToolUse",
        "tool_name": "Glob",
        "cwd": str(tmp_path),
        "tool_input": {"pattern": "**/*.tsx"},
        "tool_response": {"filenames": names, "numFiles": 100, "truncated": False, "durationMs": 5},
    }
    out = handle(event, Config(view="lossless", max_tokens=7000, trigger_tokens=500))
    result = out["hookSpecificOutput"]["updatedToolOutput"]
    assert result["truncated"] is False and result["numFiles"] == 100
    assert result["filenames"][0] == str(tmp_path / "pkg" / "components") + "/"
    assert parse("\n".join(result["filenames"]), kind=Kind.PATHS).paths == names


def test_lossless_content_stats():
    out = lossless_content(parse(flat(3, 4)), Config())
    assert out.stats["matches_total"] == 12 and out.stats["files_total"] == 3
