import shutil
import subprocess

import pytest

from searchslim import Config, Kind, slim
from searchslim.parsers import parse
from searchslim.rules import NOTE_PREFIX, build_blocks, dedupe_lines, estimate_tokens


def test_small_output_passes_through_unchanged():
    raw = "src/a.py:10:foo\nsrc/b.py:3:foo"
    assert slim(raw).text == raw


def test_duplicate_lines_from_repeated_searches_are_removed():
    raw = "src/a.py:10:foo\nsrc/a.py:10:foo\nsrc/a.py:11:foo2\n"
    out = slim(raw).text.splitlines()
    assert out[:2] == ["src/a.py:10:foo", "src/a.py:11:foo2"]
    assert out[2].startswith(NOTE_PREFIX)


def test_match_beats_context_for_same_line():
    lines = parse("a.py-5-x\na.py:5:x\n").lines
    (only,) = dedupe_lines(lines)
    assert only.is_match


def test_overlapping_context_groups_merge_into_one_block():
    # Two -C1 groups from separate searches that overlap on lines 11-12.
    raw = "a.py-10-a\na.py:11:b\na.py-12-c\n--\na.py-11-b\na.py:12:c\na.py-13-d\n"
    blocks = build_blocks(dedupe_lines(parse(raw).lines))
    (block,) = blocks["a.py"]
    assert (block.start, block.end) == (10, 13)
    assert block.matches == {11, 12}


def test_merge_gap_joins_nearby_blocks():
    lines = parse("a.py:1:x\na.py:4:y\n").lines
    assert len(build_blocks(lines, merge_gap=0)["a.py"]) == 2
    assert len(build_blocks(lines, merge_gap=2)["a.py"]) == 1


def _big_content(files=30, matches=20, context=2):
    out = []
    for f in range(files):
        for m in range(matches):
            n = m * 10 + 5
            for c in range(n - context, n):
                out.append(f"pkg/mod{f}.py-{c}-    context line {c}")
            out.append(f"pkg/mod{f}.py:{n}:    result = compute_value(x, y)  # match {m}")
            for c in range(n + 1, n + 1 + context):
                out.append(f"pkg/mod{f}.py-{c}-    context line {c}")
            out.append("--")
    return "\n".join(out)


def test_over_budget_output_fits_and_keeps_every_kept_match_anchored():
    raw = _big_content()
    reduced = slim(raw, config=Config(max_tokens=1500))
    assert estimate_tokens(reduced.text) <= 1500
    body, note = reduced.text.rsplit("\n", 1)
    assert note.startswith(NOTE_PREFIX)
    # Every body line is a path:line:text match line; nothing is rewritten.
    raw_lines = set(raw.splitlines())
    for ln in body.splitlines():
        assert ln in raw_lines
        assert ":" in ln and ln.split(":")[1].isdigit()


def test_note_accounts_for_every_dropped_match():
    reduced = slim(_big_content(), config=Config(max_tokens=1500))
    s = reduced.stats
    assert s["matches_total"] == 30 * 20
    assert s["matches_kept"] + sum(s["omitted"].values()) == s["matches_total"]
    assert "context lines dropped" in s["steps"]


def test_kept_files_are_a_prefix_of_original_order():
    reduced = slim(_big_content(), config=Config(max_tokens=1500))
    seen = []
    for ln in reduced.text.splitlines()[:-1]:
        path = ln.split(":")[0]
        if path not in seen:
            seen.append(path)
    assert seen == [f"pkg/mod{i}.py" for i in range(len(seen))]


def test_long_lines_are_clipped_not_dropped():
    raw = "min.js:1:" + "x" * 1000
    out = slim(raw, config=Config(max_line_chars=50)).text
    assert out.startswith("min.js:1:" + "x" * 50 + "…[+950 chars]")


def test_paths_dedupe_and_budget_note_by_directory():
    paths = ["./src/a.py", "src/a.py"] + [f"src/deep/f{i}.py" for i in range(500)]
    reduced = slim("\n".join(paths), config=Config(max_tokens=300))
    lines = reduced.text.splitlines()
    assert lines[0] == "src/a.py"
    assert lines.count("src/a.py") == 1
    assert lines[-1].startswith(NOTE_PREFIX)
    assert "src/deep/" in lines[-1]
    assert estimate_tokens(reduced.text) <= 300


def test_counts_merge_duplicates():
    reduced = slim("a.py:3\n./a.py:3\nb.py:1\n", kind=Kind.COUNT)
    assert reduced.text == "a.py:3\nb.py:1"


@pytest.mark.skipif(shutil.which("rg") is None, reason="rg not installed")
def test_real_rg_output_round_trips(tmp_path):
    src = tmp_path / "pkg"
    src.mkdir()
    for i in range(3):
        (src / f"m{i}.py").write_text("\n".join(f"line {n} target" if n % 7 == 0 else f"line {n}" for n in range(60)))
    raw = subprocess.run(
        ["rg", "-n", "-C", "2", "--no-heading", "target", "pkg"], cwd=tmp_path, capture_output=True, text=True
    ).stdout
    reduced = slim(raw)
    assert reduced.stats["matches_total"] == 3 * 9
    assert reduced.stats["matches_kept"] == 3 * 9
    assert set(reduced.text.splitlines()) <= set(raw.splitlines())


def test_framing_kept_around_trimmed_paths_with_note_last():
    paths = [f"src/d{i % 3}/f{i}.py" for i in range(500)]
    footer = "(Results are truncated. Consider using a more specific path or pattern.)"
    raw = "\n".join(["Found 500 files", *paths, footer]) + "\n"
    out = slim(raw, config=Config(max_tokens=200))
    lines = out.text.splitlines()
    assert lines[0] == "Found 500 files"
    assert lines[-2] == footer
    assert lines[-1].startswith(NOTE_PREFIX)
    assert "/500 paths shown" in lines[-1]
    assert out.stats["input"] == 500


def test_framing_only_passes_through():
    assert slim("No files found\n").text == "No files found"


def test_single_file_output_keeps_its_pathless_format_and_fills_the_budget():
    raw = "".join(f"{i}:    scope = compute_scope({i}, value_{i})\n" for i in range(1, 201))
    out = slim(raw, config=Config(max_tokens=800))
    lines = out.text.splitlines()
    body, note = lines[:-1], lines[-1]
    assert body[0] == "1:    scope = compute_scope(1, value_1)"
    assert all(line in raw.splitlines() for line in body)
    assert len(body) > 8  # not cut to the multi-file floor
    assert estimate_tokens(out.text) <= 800
    assert note.startswith(NOTE_PREFIX) and "this file" in note and "path/glob" not in note


def test_single_file_note_uses_default_path_name():
    raw = "".join(f"{i}:x = {i} * 12345678901234567890\n" for i in range(1, 400))
    out = slim(raw, config=Config(max_tokens=300), default_path="src/a.py")
    assert "src/a.py (" in out.text.splitlines()[-1]
    assert not out.text.startswith("src/a.py")


def test_cap_rises_above_floor_when_budget_allows():
    # One big file and a few small ones: a fixed cap of 8 would hide most of
    # the big file even though the budget has room for it.
    big = [f"big.go:{i}:func F{i}() error {{" for i in range(1, 41)]
    small = [f"s{j}.go:{i}:F{i}()" for j in range(3) for i in range(1, 4)]
    ctx = [f"big.go-{i}-// padding line for context {i}" for i in range(100, 160)]
    raw = "\n".join(big + small + ctx) + "\n"
    out = slim(raw, config=Config(max_tokens=600))
    kept_big = [ln for ln in out.text.splitlines() if ln.startswith("big.go:")]
    assert len(kept_big) > 8
    assert kept_big == big[: len(kept_big)]  # still a prefix in line order
    assert estimate_tokens(out.text) <= 600


def test_note_covers_every_omitted_file_by_directory():
    from searchslim.rules import rollup_dirs

    files = [f"pkg{k}/sub{j}/f.py" for k in range(4) for j in range(8)]
    groups = rollup_dirs([(f, 1) for f in files], limit=5)
    assert len(groups) <= 5
    assert sum(n for _, n in groups) == len(files)
    assert [d for d, _ in groups] == ["pkg0", "pkg1", "pkg2", "pkg3"]

    many = [(f"d{i}/f.py", 2) for i in range(30)]
    groups = rollup_dirs(many, limit=5)
    assert len(groups) == 5 and sum(n for _, n in groups) == 60
    assert groups[-1][0] == "+26 other dirs"


def test_unordered_input_still_names_the_dropped_evidence_dir():
    files = [f"src/_pytest/m{i}.py" for i in range(25)] + ["src/_pytest/config/__init__.py"]
    raw = "".join(f"{f}:{n}:    raise ValueError('x' * {n})\n" for f in files for n in range(1, 30))
    out = slim(raw, config=Config(max_tokens=500))
    note = out.text.splitlines()[-1]
    assert "src/_pytest/config/" in note


def test_one_file_with_path_asks_to_narrow_the_pattern():
    raw = "".join(f"src/f.py:{i}:scope = {i} * 1234567890\n" for i in range(1, 300))
    note = slim(raw, config=Config(max_tokens=300)).text.splitlines()[-1]
    assert "src/f.py (" in note and "Narrow the pattern" in note
