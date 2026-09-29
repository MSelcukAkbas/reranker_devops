import json

from searchslim.models import Kind
from searchslim.parsers import detect_kind, parse


def test_content_match_and_context_lines():
    raw = "src/a.py-9-def f():\nsrc/a.py:10:    return foo\nsrc/a.py-11-\n--\nsrc/b.py:3:foo = 1\n"
    result = parse(raw)
    assert result.kind is Kind.CONTENT
    got = [(ln.path, ln.number, ln.is_match) for ln in result.lines]
    assert got == [
        ("src/a.py", 9, False),
        ("src/a.py", 10, True),
        ("src/a.py", 11, False),
        ("src/b.py", 3, True),
    ]
    assert result.lines[1].text == "    return foo"
    assert not result.unparsed


def test_context_line_with_dash_digit_path_uses_known_path():
    raw = "lib/v-2-api.py:5:x\nlib/v-2-api.py-6-y\n"
    lines = parse(raw).lines
    assert [(ln.path, ln.number, ln.text) for ln in lines] == [
        ("lib/v-2-api.py", 5, "x"),
        ("lib/v-2-api.py", 6, "y"),
    ]


def test_match_text_containing_colons():
    lines = parse("a/b.py:7:url = 'http://x:80/y'\n").lines
    assert lines[0].path == "a/b.py"
    assert lines[0].number == 7
    assert lines[0].text == "url = 'http://x:80/y'"


def test_heading_mode():
    raw = "src/a.py\n3:foo\n4-bar\n\nsrc/b.py\n10:foo\n"
    lines = parse(raw).lines
    assert [(ln.path, ln.number, ln.is_match) for ln in lines] == [
        ("src/a.py", 3, True),
        ("src/a.py", 4, False),
        ("src/b.py", 10, True),
    ]


def test_bare_lines_need_default_path():
    lines = parse("3:foo\n4-bar\n", kind=Kind.CONTENT, default_path="x.py").lines
    assert [(ln.path, ln.number, ln.is_match) for ln in lines] == [("x.py", 3, True), ("x.py", 4, False)]


def test_rg_json():
    events = [
        {"type": "begin", "data": {"path": {"text": "a.py"}}},
        {"type": "context", "data": {"path": {"text": "a.py"}, "lines": {"text": "ctx\n"}, "line_number": 1}},
        {"type": "match", "data": {"path": {"text": "a.py"}, "lines": {"text": "hit\n"}, "line_number": 2}},
        {"type": "end", "data": {}},
    ]
    raw = "\n".join(json.dumps(e) for e in events)
    lines = parse(raw).lines
    assert [(ln.path, ln.number, ln.text, ln.is_match) for ln in lines] == [
        ("a.py", 1, "ctx", False),
        ("a.py", 2, "hit", True),
    ]


def test_detect_paths_and_counts():
    assert detect_kind("src/a.py\nsrc/b.py\n") is Kind.PATHS
    assert detect_kind("src/a.py:3\nsrc/b.py:12\n") is Kind.COUNT
    assert detect_kind("src/a.py:3:x\n") is Kind.CONTENT
    assert detect_kind("") is Kind.PATHS


def test_unparseable_lines_are_kept():
    result = parse("src/a.py:1:x\nsome warning text here\n")
    assert result.unparsed == ["some warning text here"]
