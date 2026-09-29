import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "benchmark"))

import live  # noqa: E402


def test_cited_matches_paths_lines_and_ranges():
    ev = {"path": "src/_pytest/fixtures.py", "line": 1129}
    assert live.cited("It is in `src/_pytest/fixtures.py:1129`.", ev)
    assert live.cited("see fixtures.py:1130", ev)  # within tolerance
    assert live.cited("fixtures.py:1120-1135 holds it", ev)
    assert not live.cited("fixtures.py:900", ev)
    assert not live.cited("other_fixtures.py:1129", ev)
    assert live.cited("look at src/_pytest/fixtures.py", {"path": "src/_pytest/fixtures.py"})


def test_is_search_call():
    assert live.is_search_call("Grep", {})
    assert live.is_search_call("Bash", {"command": "rg -n foo src"})
    assert live.is_search_call("Bash", {"command": "/usr/bin/find . -name x"})
    assert not live.is_search_call("Bash", {"command": "ls src"})
    assert not live.is_search_call("Read", {"file_path": "a"})


def test_parse_transcript_counts_calls_and_result():
    events = [
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "t1", "name": "Grep", "input": {"pattern": "x"}},
            {"type": "tool_use", "id": "t2", "name": "Read", "input": {"file_path": "a.py"}},
        ]}},
        {"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": "searchslim ran this search and reduced the output.\na.py:1:x"},
            {"type": "tool_result", "tool_use_id": "t2", "content": [{"type": "text", "text": "file body"}]},
        ]}},
        {"type": "result", "subtype": "success", "result": "a.py:1", "num_turns": 2, "total_cost_usd": 0.1,
         "duration_ms": 1500, "usage": {"input_tokens": 10, "cache_read_input_tokens": 90, "output_tokens": 5}},
    ]
    row = live.parse_transcript([json.dumps(e) for e in events])
    assert row["search_calls"] == 1 and row["read_calls"] == 1
    assert row["hook_answered"] == 1
    assert row["input_tokens"] == 100 and row["answer"] == "a.py:1" and not row["is_error"]


def test_hook_settings_points_at_src(tmp_path):
    cmd = live.hook_settings(tmp_path)["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
    assert str(tmp_path) in cmd and cmd.endswith("-m searchslim hook")
