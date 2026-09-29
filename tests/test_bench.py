import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "benchmark"))

import bench  # noqa: E402
from searchslim import Kind, detect_kind  # noqa: E402


def test_content_evidence_statuses():
    ev = {"path": "a.py", "line": 10}
    kept = "a.py:10:def f():\n"
    assert bench.evidence_status(ev, kept, Kind.CONTENT) == "kept"
    assert bench.evidence_status(ev, "./a.py-10-def f():\n", Kind.CONTENT) == "kept"
    # file still in the body, line dropped: one narrowed search away
    assert bench.evidence_status(ev, "a.py:3:x\n", Kind.CONTENT) == "recoverable"
    noted = "b.py:1:x\n[searchslim] 9 -> 1 lines. Omitted matches: ./a.py (4), c.py (2). Narrow the search."
    assert bench.evidence_status(ev, noted, Kind.CONTENT) == "recoverable"
    gone = "b.py:1:x\n[searchslim] 9 -> 1 lines. Omitted matches: c.py (2), +3 more files (7 matches)."
    assert bench.evidence_status(ev, gone, Kind.CONTENT) == "lost"


def test_single_file_output_uses_default_path():
    ev = {"path": "src/f.py", "line": 7}
    assert bench.evidence_status(ev, "7:scope = 1\n", Kind.CONTENT, "src/f.py") == "kept"


def test_path_and_count_evidence():
    assert bench.evidence_status({"path": "src/a.py"}, "src/a.py\nsrc/b.py\n", Kind.PATHS) == "kept"
    note = "src/x.py\n[searchslim] 1/40 paths shown. Omitted: src/ (39)."
    assert bench.evidence_status({"path": "src/a.py"}, note, Kind.PATHS) == "recoverable"
    assert bench.evidence_status({"path": "t/a.py"}, "t/a.py:5\n", Kind.COUNT) == "kept"


def test_validate_subset_rejects_invented_lines():
    raw = "a.py:1:x = 1\na.py:2:y = 2\n"
    assert bench.validate_subset("a.py:2:y = 2\n[searchslim] note", raw) == []
    assert bench.validate_subset("a.py:2:y = 3\n", raw) == ["a.py:2:y = 3"]
    clipped = "a.py:9:" + "z" * 10
    assert bench.validate_subset(clipped + "…[+5 chars]", clipped + "zzzzz") == []


def test_capture_cmd_sorts_rg_only():
    assert bench.capture_cmd({"cmd": ["rg", "-n", "x"]}) == ["rg", "--sort", "path", "-n", "x"]
    assert bench.capture_cmd({"cmd": ["find", "src"]}) == ["find", "src"]


def test_fixtures_hold_all_critical_evidence():
    """Every committed fixture must contain its task's critical evidence (raw mode keeps everything)."""
    spec = bench.load_spec()
    for task in spec["tasks"]:
        raw = (bench.FIXTURES / f"{task['id']}.txt").read_text(encoding="utf-8")
        bench.resolve_lines(task, raw)
        kind = detect_kind(raw)
        for ev in task["evidence"]:
            if ev.get("critical"):
                assert bench.evidence_status(ev, raw, kind, bench.default_path(task)) == "kept", (task["id"], ev)


def test_run_with_model_cmd(tmp_path, capsys):
    # A stand-in reranker that returns the rules output unchanged and reports usage.
    model = tmp_path / "model.py"
    model.write_text(
        "import json, sys\n"
        "p = json.load(sys.stdin)\n"
        "sys.stdout.write(p['rules'])\n"
        "sys.stderr.write(json.dumps({'input_tokens': 100, 'output_tokens': 10}) + '\\n')\n"
    )
    out = tmp_path / "rows.jsonl"
    rc = bench.main(
        ["run", "--tokenizer", "chars", "--repeat", "1", "--only", "cobra-execute", "--model-cmd", f"{sys.executable} {model}", "--jsonl", str(out)]
    )
    assert rc == 0
    rows = [json.loads(ln) for ln in out.read_text().splitlines()]
    assert [r["mode"] for r in rows] == ["raw", "rules", "rules+model"]
    assert rows[2]["cost_usd"] > rows[1]["cost_usd"]
    assert "rules+model" in capsys.readouterr().out
