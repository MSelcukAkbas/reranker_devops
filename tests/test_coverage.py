from pathlib import Path

import pytest

from searchslim import Config, Kind, estimate_tokens, parse, slim
from searchslim.coverage import split_note
from searchslim.rerank import LexicalScorer, Query
from searchslim.rules import NOTE_PREFIX
from searchslim.session import SessionStore

FIXTURES = Path(__file__).resolve().parent.parent / "benchmark" / "fixtures"


def _raw(files: int, matches: int, width: int = 40) -> str:
    return "\n".join(
        f"src/pkg{f % 4}/mod{f:02d}.py:{n * 3 + 1}:    value_{n} = target({n}) " + "x" * width
        for f in range(files)
        for n in range(matches)
    )


def _covered(index: str, path: str) -> bool:
    """The file is named in the index, or sits under a directory row."""
    for row in index.splitlines()[1:]:
        name = row.split()[0]
        if name == path or (name.endswith("/") and path.startswith(name)):
            return True
    return False


def test_small_output_passes_through_unchanged():
    raw = "src/a.py:10:foo\nsrc/b.py:3:foo"
    assert slim(raw, config=Config(view="coverage")).text == raw


def test_output_that_fits_after_context_drop_keeps_plain_view():
    raw = "\n".join(f"a.py-{n}-ctx {'y' * 60}" if n % 2 else f"a.py:{n}:hit" for n in range(1, 120))
    out = slim(raw, config=Config(max_tokens=600, view="coverage"))
    assert out.stats.get("view") != "coverage"


@pytest.mark.parametrize("scorer", [None, LexicalScorer()])
def test_index_covers_every_file_and_evidence_lines_come_from_input(scorer):
    raw = _raw(files=12, matches=30)
    out = slim(raw, config=Config(max_tokens=1500, view="coverage"), scorer=scorer, query=Query(pattern="target"))
    body, index = split_note(out.text)
    assert index.startswith(NOTE_PREFIX) and "Coverage: 12/12 matching files indexed" in index
    assert out.text.startswith(NOTE_PREFIX)  # index first, evidence after
    raw_lines = set(raw.splitlines())
    assert body and all(ln in raw_lines for ln in body.splitlines())
    for f in range(12):
        assert _covered(index, f"src/pkg{f % 4}/mod{f:02d}.py")
    assert estimate_tokens(out.text) <= 1500


def test_index_rows_give_count_span_and_expanded():
    raw = _raw(files=6, matches=40)
    out = slim(raw, config=Config(max_tokens=1500, view="coverage"))
    _, index = split_note(out.text)
    rows = index.splitlines()[1:]
    assert rows[0].split()[:4] == ["src/pkg0/mod00.py", "40", "matches", "L1-118"]
    assert "expanded)" in rows[0]
    assert out.stats["files_indexed"] == 6


def test_many_files_roll_up_by_directory_and_stay_in_budget():
    raw = _raw(files=300, matches=3, width=10)
    out = slim(raw, config=Config(max_tokens=2000, view="coverage"))
    body, index = split_note(out.text)
    assert "Coverage: 300/300 matching files indexed (by directory where many)" in index
    assert out.stats["index_rolled_up"]
    for f in range(300):
        assert _covered(index, f"src/pkg{f % 4}/mod{f:02d}.py")
    # Directory rows count every file and match that is not named on its own.
    named = [r for r in index.splitlines()[1:] if not r.split()[0].endswith("/")]
    dir_files = sum(int(r.split()[1]) for r in index.splitlines()[1:] if r.split()[0].endswith("/"))
    assert dir_files + len(named) == 300
    assert estimate_tokens(out.text) <= 2000
    assert parse(body, kind=Kind.CONTENT).lines


def test_definition_line_is_marked():
    raw = "\n".join([f"src/a.py:{n}:    refresh_token(x) {'z' * 50}" for n in range(2, 80)] + ["src/a.py:90:def refresh_token(x):"])
    raw += "\n" + "\n".join(f"src/b.py:{n}:refresh_token() {'z' * 50}" for n in range(1, 80))
    out = slim(raw, config=Config(max_tokens=800, view="coverage"))
    _, index = split_note(out.text)
    assert "def L90" in index.splitlines()[1]


def test_notes_view_is_unchanged():
    raw = _raw(files=12, matches=30)
    notes = slim(raw, config=Config(max_tokens=1500))
    assert notes.text.splitlines()[-1].startswith(NOTE_PREFIX)
    assert not notes.text.startswith(NOTE_PREFIX)


def test_session_repeat_keeps_the_full_index(tmp_path):
    raw = _raw(files=8, matches=40)
    store = SessionStore("s1", root=tmp_path)
    config = Config(max_tokens=1500, view="coverage")
    first = slim(raw, config=config, session=store)
    second = slim(raw, config=config, session=store)
    for out in (first, second):
        assert "Coverage: 8/8 matching files indexed" in out.text.splitlines()[0]
    assert second.stats.get("seen_lines_skipped")
    assert split_note(first.text)[0] != split_note(second.text)[0]  # pages forward


@pytest.mark.parametrize("fixture", sorted(p.name for p in FIXTURES.glob("*.txt")))
def test_fixtures_stay_in_budget_and_subset(fixture):
    raw = (FIXTURES / fixture).read_text(encoding="utf-8")
    raw_lines = set(raw.splitlines()) | {ln[2:] for ln in raw.splitlines() if ln.startswith("./")}
    for budget in (800, 2000):
        out = slim(raw, config=Config(max_tokens=budget, view="coverage"), scorer=LexicalScorer(), query=Query(pattern="x"))
        if out.stats.get("view") != "coverage":
            continue
        body, _ = split_note(out.text)
        assert all(ln in raw_lines or "…[+" in ln for ln in body.splitlines())
        assert estimate_tokens(out.text) <= budget
