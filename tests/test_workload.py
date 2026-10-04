import json
import sys
from argparse import Namespace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "benchmark"))

import live  # noqa: E402
import workload  # noqa: E402


def _args(**kw):
    return Namespace(**{"repeat": 1, "compact_trigger": workload.DEFAULT_COMPACT_TRIGGER_TOKENS, **kw})


def test_workload_covers_every_category_with_fixtures():
    spec, tasks = workload.load_workload()
    assert {t["category"] for t in tasks} == set(spec["categories"])
    for t in tasks:
        for _, path in workload.steps(t):
            assert path.exists(), path
    for t in spec["live"]:
        assert t["category"] in spec["categories"] and t["evidence"]
        if t.get("patch"):
            assert (workload.ROOT / t["patch"]).exists()


def test_hook_defaults_keep_every_evidence_and_location():
    spec, tasks = workload.load_workload()
    config = workload.hook_config()
    rows = [workload.run_task(t, spec, config, workload.estimate_tokens, _args()) for t in tasks]
    for r in rows:
        assert r.ev_found == r.ev_total, r.task
        assert r.loc_found == r.loc_total, (r.task, r.missing[:3])
        assert r.tokens <= r.raw_tokens, r.task


def test_shell_locations_ignore_passing_status_lines():
    raw = "t.py::test_error_case PASSED [ 50%]\nt.py:12: AssertionError\nE   assert 1 == 2\n"
    found, total, missing = workload.shell_locations(raw, "t.py:12: AssertionError\n")
    assert (found, total, missing) == (1, 2, ["E   assert 1 == 2"])


def test_live_counts_build_calls_and_compensatory_searches():
    events = [
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "a", "name": "Grep", "input": {"pattern": "process.env"}},
            {"type": "tool_use", "id": "b", "name": "Bash", "input": {"command": "cd app && cargo test -p globset"}},
        ]}},
        {"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "a", "content": "a.js:1:x\n[searchslim] not shown: 3 lines."},
            {"type": "tool_result", "tool_use_id": "b", "content": "test result: ok"},
        ]}},
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "c", "name": "Bash", "input": {"command": "rg -n 'process\\.env\\.PORT' services"}},
            {"type": "tool_use", "id": "d", "name": "Bash", "input": {"command": "cd app && cargo test -p globset"}},
        ]}},
        {"type": "result", "subtype": "success", "result": "ok", "num_turns": 3, "total_cost_usd": 0.2,
         "usage": {"input_tokens": 10, "cache_read_input_tokens": 900, "cache_creation_input_tokens": 90, "output_tokens": 50}},
    ]
    row = live.parse_transcript([json.dumps(e) for e in events])
    assert row["search_calls"] == 2 and row["build_calls"] == 2
    assert row["re_searches"] == 1 and row["re_searches_after_reduced"] == 1
    assert row["reruns"] == 1 and row["hook_answered"] == 1
    assert (row["uncached_input_tokens"], row["cache_read_tokens"], row["cache_write_tokens"]) == (10, 900, 90)


def test_live_evidence_kinds():
    assert live.cited("TestVersionFlagExecuted fails", {"mention": "testversionflagexecuted"})
    assert live.cited("PORT, HOST and DEBUG", {"regex": r"\b[A-Z]{4,}\b", "min": 2})
    assert not live.cited("PORT only", {"regex": r"\b[A-Z]{4,}\b", "min": 2})
    assert live.cited(r"see crates\core\main.rs:116", {"path": "crates/core/main.rs", "line": 116})


def test_live_paired_summary():
    rows = []
    for rep in range(3):
        for mode, cost in (("off", 1.0), ("on", 0.8)):
            rows.append({"task": "t", "rep": rep, "mode": mode, "category": "env", "success": True, "cited": 1, "critical": 1,
                         "turns": 3, "search_calls": 2, "cost_usd": cost, "wall_ms": 1000, "tool_output_tokens": 100 if mode == "off" else 60})
    text = live.summarize(rows, ["off", "on"])
    assert "Paired on vs off: 3 pairs" in text
    assert "| cost_usd | 3.000 | 2.400 | -20% | -20% .. -20% |" in text
    assert "| env | 3 | 3/3 | -20% | -40% | +0% | 0/0 |" in text
