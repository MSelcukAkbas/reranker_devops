import re
import json
import subprocess
import sys
from types import SimpleNamespace

from searchslim import Config, slim
from searchslim.parsers import parse
from searchslim.rerank import (
    ClaudeScorer,
    LexicalScorer,
    Query,
    parse_ranking,
    pattern_and_paths,
    query_from_transcript,
    reduce_ranked,
    run_for_benchmark,
    split_units,
    terms,
)
from searchslim.rules import NOTE_PREFIX, build_blocks, dedupe_lines, estimate_tokens


def _big_go_file():
    """Many small methods in one file, with the two that matter far down."""
    out = []
    n = 1
    for i in range(120):
        name = "Execute" if i == 100 else "ExecuteC" if i == 101 else f"Helper{i}"
        out.append(f"command.go:{n}:func (c *Command) {name}() error {{")
        n += 7
    out.append("command_test.go:5:func (c *Command) fakeForTest() {")
    return "\n".join(out)


def test_terms_split_identifiers_and_stem():
    assert terms("ExecuteC max_columns checks") == ["executec", "execut", "max", "column", "check"]


def test_split_units_gives_each_match_cluster_its_nearest_context():
    raw = "a.py-1-x\na.py:2:hit one\na.py-3-y\na.py-4-z\na.py-5-w\na.py:6:hit two\na.py-7-v\n"
    units = split_units(build_blocks(dedupe_lines(parse(raw).lines)))
    assert [sorted(u.lines) for u in units] == [[1, 2, 3, 4], [5, 6, 7]]
    assert [u.matches for u in units] == [{2}, {6}]


def test_lexical_rerank_finds_definitions_rules_would_cut():
    raw = _big_go_file()
    query = Query("Trace command execution from Execute().", "Find Execute and ExecuteC.", "func")
    rules_only = slim(raw, config=Config(max_tokens=600)).text
    ranked = slim(raw, config=Config(max_tokens=600), scorer=LexicalScorer(), query=query).text
    assert "Execute() error" not in rules_only
    assert "command.go:701:func (c *Command) Execute() error {" in ranked
    assert "command.go:708:func (c *Command) ExecuteC() error {" in ranked
    assert estimate_tokens(ranked) <= 600


def test_rerank_output_lines_all_come_from_the_input():
    raw = _big_go_file()
    out = slim(raw, config=Config(max_tokens=500), scorer=LexicalScorer(), query=Query("execute")).text
    raw_lines = set(raw.splitlines())
    body = [ln for ln in out.splitlines() if not ln.startswith(NOTE_PREFIX)]
    assert body and all(ln in raw_lines for ln in body)
    note = out.splitlines()[-1]
    assert note.startswith(NOTE_PREFIX) and "ranked by relevance (lexical)" in note


def test_rerank_is_a_no_op_when_rules_drop_nothing():
    raw = "a.py:1:x\nb.py:2:y"
    assert slim(raw, scorer=LexicalScorer(), query=Query("anything")).text == raw


def test_tests_and_changelogs_rank_below_code_unless_asked():
    raw = "\n".join(
        [f"History.md:{i}:  * fix redirect status {i}" for i in range(1, 40)]
        + [f"test/redirect.js:{i}:  it('should redirect', function () {{" for i in range(1, 40)]
        + ["lib/response.js:946:res.redirect = function redirect(url) {"]
    )
    out = slim(raw, config=Config(max_tokens=200), scorer=LexicalScorer(), query=Query("Change res.redirect default status")).text
    assert out.splitlines()[0] == "lib/response.js:946:res.redirect = function redirect(url) {"


def test_paths_are_ranked_too():
    paths = [f"src/pkg/mod{i}.py" for i in range(300)] + ["src/_pytest/warnings.py"]
    out = slim("\n".join(paths), config=Config(max_tokens=150), scorer=LexicalScorer(), query=Query("which modules import warnings")).text
    assert out.splitlines()[0] == "src/_pytest/warnings.py"


def test_parse_ranking_ignores_unknown_and_repeated_ids():
    assert parse_ranking('sure: {"ranking": [3, 9, 3, "x", 1]}', {1, 2, 3}) == [3, 1]
    assert parse_ranking("no json here", {1}) == []


class _FakeClient:
    def __init__(self, reply):
        self.reply = reply
        self.calls = []
        self.messages = self

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text=self.reply)],
            usage=SimpleNamespace(input_tokens=321, output_tokens=12),
        )


def test_claude_scorer_orders_by_model_ranking_and_reports_usage():
    raw = "\n".join(f"m.py:{i}:line {i}" for i in range(1, 400))
    # Dense matches are cut into units of 5 lines: unit 50 = lines 251-255, unit 1 = lines 6-10.
    client = _FakeClient('{"ranking": [50, 1]}')
    reduced = slim(raw, config=Config(max_tokens=300), scorer=ClaudeScorer(client=client, max_candidates=400), query=Query("x"))
    body = reduced.text.splitlines()
    assert {"m.py:251:line 251", "m.py:6:line 6"} <= set(body)
    assert reduced.stats["model_usage"]["input_tokens"] == 321
    assert client.calls[0]["model"] == "claude-haiku-4-5"
    assert "[50]" in client.calls[0]["messages"][0]["content"]


def test_query_from_transcript(tmp_path):
    entries = [
        {"type": "user", "message": {"role": "user", "content": "Why does -p no:X fail?"}},
        {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": "Searching for UsageError."}]}},
        {"type": "user", "message": {"role": "user", "content": [{"type": "tool_result", "content": "..."}]}},
    ]
    path = tmp_path / "t.jsonl"
    path.write_text("\n".join(json.dumps(e) for e in entries) + "\nnot json\n")
    q = query_from_transcript(str(path), "raise")
    assert (q.intent, q.subtask, q.pattern) == ("Why does -p no:X fail?", "Searching for UsageError.", "raise")
    assert query_from_transcript(str(tmp_path / "missing")).intent == ""


def test_pattern_and_paths():
    assert pattern_and_paths(["rg", "-n", "-C2", "raise ", "src"]) == ("raise ", ["src"])
    assert pattern_and_paths(["rg", "-n", "-g", "*.py", "-e", "x", "a", "b"]) == ("x", ["a", "b"])
    assert pattern_and_paths(["rg", "--files"]) == ("", [])


def test_benchmark_entry_keeps_single_file_format():
    raw = "\n".join(f"{i}:    scope = {i}" for i in range(1, 300)) + "\n991:        Scope.from_user(scope_str)"
    payload = {"intent": "fix scope parsing", "subtask": "find Scope.from_user", "cmd": ["rg", "-n", "scope", "src/fixtures.py"], "raw": raw, "max_tokens": 300}
    text, _ = run_for_benchmark(payload, LexicalScorer())
    body = [ln for ln in text.splitlines() if not ln.startswith(NOTE_PREFIX)]
    assert "991:        Scope.from_user(scope_str)" in body
    assert set(body) <= set(raw.splitlines())


def test_bench_model_cli_contract():
    raw = _big_go_file()
    payload = json.dumps({"intent": "Execute", "subtask": "", "cmd": ["rg", "-n", "func", "."], "raw": raw, "rules": "", "max_tokens": 500})
    proc = subprocess.run([sys.executable, "-m", "searchslim", "bench-model"], input=payload, capture_output=True, text=True)
    assert proc.returncode == 0
    assert "command.go:701:func (c *Command) Execute() error {" in proc.stdout
    assert json.loads(proc.stderr.strip().splitlines()[-1]) == {}


def test_ranked_paths_note_covers_every_omitted_path():
    raw = "\n".join(f"pkg{d}/sub{s}/file_{i}.py" for d in range(15) for s in range(3) for i in range(5))
    result = parse(raw)
    out = reduce_ranked(result, Query(intent="file_3"), LexicalScorer(), Config(max_tokens=200))
    note = out.text.splitlines()[-1]
    shown = len(out.text.splitlines()) - 1
    counted = sum(int(n) for n in re.findall(r"\((\d+)\)", note))
    assert shown + counted == 225
