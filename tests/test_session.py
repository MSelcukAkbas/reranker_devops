import json
import os
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from multiprocessing import get_context

import pytest

from searchslim import slim
from searchslim.hooks import handle
from searchslim.install import install
from searchslim.rules import Config, NOTE_PREFIX
from searchslim.session import COMPACT_AT, SessionStore, line_key

needs_rg = pytest.mark.skipif(shutil.which("rg") is None, reason="rg not installed")


@pytest.fixture(autouse=True)
def cache_dir(tmp_path, monkeypatch):
    root = tmp_path / "cache"
    monkeypatch.setenv("SEARCHSLIM_CACHE_DIR", str(root))
    return root


def big_output(files=12, per_file=12, tag="x"):
    return "\n".join(
        f"src/m{f:02d}.py:{n * 10}:    result_{n} = handle({tag}, value_{f}_{n})"
        for f in range(files)
        for n in range(1, per_file + 1)
    )


def body_lines(text):
    return [ln for ln in text.splitlines() if ln and not ln.startswith(NOTE_PREFIX)]


def test_store_roundtrip_and_clear(cache_dir):
    store = SessionStore("abc/../def")  # unsafe characters are neutralised
    assert store.dir.parent == cache_dir
    store.record(["k1", "k2"])
    store.record(["k2", "k3"])
    assert store.seen() == {"k1", "k2", "k3"}
    store.clear()
    assert store.seen() == set()


def test_store_compacts_without_losing_keys():
    store = SessionStore("s")
    for i in range(COMPACT_AT + 5):
        store.record([f"k{i}"])
    assert store.seen() == {f"k{i}" for i in range(COMPACT_AT + 5)}
    assert len(list(store.dir.glob("*.keys"))) < COMPACT_AT


def _record_many(args):
    root, worker = args
    os.environ["SEARCHSLIM_CACHE_DIR"] = root
    store = SessionStore("parallel")
    for i in range(20):
        store.record([f"w{worker}-{i}"])


def test_concurrent_writers_keep_every_key(cache_dir):
    # Separate processes, like parallel hook calls; enough writes to trigger compaction races.
    with get_context("spawn").Pool(8) as pool:
        pool.map(_record_many, [(str(cache_dir), w) for w in range(8)])
    assert SessionStore("parallel").seen() == {f"w{w}-{i}" for w in range(8) for i in range(20)}


def test_repeat_search_shows_unseen_lines_and_references_the_rest(tmp_path):
    raw = big_output()
    config = Config(max_tokens=600)
    store = SessionStore("s1")
    first = slim(raw, config=config, session=store, cwd=str(tmp_path))
    second = slim(raw, config=config, session=store, cwd=str(tmp_path))

    raw_set = set(raw.splitlines())
    first_body, second_body = body_lines(first.text), body_lines(second.text)
    # Nothing invented, nothing re-sent, and the repeat pages into what was dropped.
    assert set(second_body) <= raw_set
    assert not set(first_body) & set(second_body)
    assert second_body
    note = second.text.splitlines()[-1]
    assert note.startswith(NOTE_PREFIX) and "already shown by an earlier search" in note
    assert "src/m00.py:10-" not in note  # line numbers, not a range of consecutive numbers here
    assert "src/m00.py:10," in note
    assert second.stats["seen_lines_skipped"] == len(first_body)


def test_small_output_passes_through_even_if_seen(tmp_path):
    raw = "a.py:1:foo\na.py:2:bar"
    store = SessionStore("s2")
    assert slim(raw, session=store, cwd=str(tmp_path)).text == raw
    assert slim(raw, session=store, cwd=str(tmp_path)).text == raw


def test_changed_line_text_counts_as_new(tmp_path):
    config = Config(max_tokens=600)
    store = SessionStore("s3")
    slim(big_output(tag="x"), config=config, session=store, cwd=str(tmp_path))
    again = slim(big_output(tag="y"), config=config, session=store, cwd=str(tmp_path))
    assert "seen_lines_skipped" not in again.stats


def test_same_file_from_another_cwd_is_the_same_line(tmp_path):
    store = SessionStore("s4")
    store.record([line_key(str(tmp_path / "src" / "a.py"), 3, "x")])
    raw = "\n".join(["a.py:3:x"] + [f"a.py:{n}:{'y' * 80}" for n in range(10, 60)])
    out = slim(raw, config=Config(max_tokens=300), session=store, cwd=str(tmp_path / "src"))
    assert out.stats["seen_lines_skipped"] == 1 and "a.py:3:x" not in body_lines(out.text)


def test_bash_rewrite_passes_the_session():
    out = handle({"tool_name": "Bash", "session_id": "abc-123", "tool_input": {"command": "rg foo"}})
    assert "--session=abc-123 -- rg" in out["hookSpecificOutput"]["updatedInput"]["command"]


def test_session_off_switch(monkeypatch):
    monkeypatch.setenv("SEARCHSLIM_SESSION", "off")
    out = handle({"tool_name": "Bash", "session_id": "abc", "tool_input": {"command": "rg foo"}})
    assert "--session" not in out["hookSpecificOutput"]["updatedInput"]["command"]


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    for i in range(30):
        body = "\n".join(f"    value_{n} = compute(target, {n})" if n % 3 == 0 else f"    other_{n} = 0" for n in range(60))
        (root / f"mod{i:02d}.py").write_text(body + "\n")
    return root


def _grep(repo, session_id="sess"):
    event = {
        "hook_event_name": "PostToolUse", "tool_name": "Grep", "cwd": str(repo), "session_id": session_id,
        "tool_input": {"pattern": "target", "output_mode": "content"},
    }
    return handle(event, Config(max_tokens=800))["hookSpecificOutput"]["updatedToolOutput"]["content"]


@needs_rg
def test_hook_repeat_grep_then_compaction_resets(repo):
    first, second = _grep(repo), _grep(repo)
    assert "already shown by an earlier search" in second
    assert not set(body_lines(first)) & set(body_lines(second))
    handle({"hook_event_name": "PreCompact", "session_id": "sess"})
    assert _grep(repo) == first


@needs_rg
def test_bash_run_uses_the_session_across_tools(repo):
    first = _grep(repo)
    cmd = [sys.executable, "-m", "searchslim", "run", "--max-tokens=800", "--session=sess", "--",
           "rg", "--sort=path", "--with-filename", "--line-number", "target"]
    out = subprocess.run(cmd, cwd=repo, capture_output=True, text=True, env=os.environ).stdout
    assert "already shown by an earlier search" in out
    assert not set(body_lines(first)) & set(body_lines(out))


@needs_rg
def test_parallel_hook_processes(repo, tmp_path):
    """Many hook processes at once on one session: all answer, none fail, all fast."""
    event = json.dumps({
        "hook_event_name": "PostToolUse", "tool_name": "Grep", "cwd": str(repo), "session_id": "par",
        "tool_input": {"pattern": "target", "output_mode": "content"},
    })
    env = {**os.environ, "SEARCHSLIM_MAX_TOKENS": "800"}

    def call(_):
        start = time.perf_counter()
        proc = subprocess.run([sys.executable, "-m", "searchslim", "hook"], input=event, capture_output=True, text=True, env=env)
        return proc, time.perf_counter() - start

    with ThreadPoolExecutor(12) as pool:
        results = list(pool.map(call, range(12)))
    for proc, _ in results:
        assert proc.returncode == 0
        assert json.loads(proc.stdout)["hookSpecificOutput"]["updatedToolOutput"]
    assert max(t for _, t in results) < 10  # generous: CI machines vary; see benchmark for real numbers


def test_install_adds_precompact(tmp_path):
    path = tmp_path / "settings.json"
    install(path)
    data = json.loads(path.read_text())
    assert "PreCompact" in data["hooks"] and "PreToolUse" in data["hooks"]


def test_subagent_calls_use_their_own_store():
    main = handle({"tool_name": "Bash", "session_id": "s", "tool_input": {"command": "rg foo"}})
    sub = handle({"tool_name": "Bash", "session_id": "s", "agent_id": "a1", "tool_input": {"command": "rg foo"}})
    assert "--session=s --" in main["hookSpecificOutput"]["updatedInput"]["command"]
    assert "--session=s.a1 --" in sub["hookSpecificOutput"]["updatedInput"]["command"]


def test_split_seen_keeps_anchor_for_new_context():
    from searchslim import parse
    from searchslim.session import split_seen

    seen = {line_key("/t/a.py", n, t) for n, t in [(10, "hit"), (9, "before"), (11, "after"), (50, "hit2")]}
    raw = "\n".join([
        "a.py-8-older", "a.py-9-before", "a.py:10:hit", "a.py-11-after",  # new line 8 -> keep up to match 10
        "--", "a.py-49-x", "a.py:50:hit2",  # line 49 is new: keep 49-50
        "--", "a.py:70:new",
    ])
    seen.add(line_key("/t/a.py", 49, "x"))  # now the 49-50 run is fully seen
    rest, shown = split_seen(parse(raw), seen, "/t")
    assert [(ln.number, ln.is_match) for ln in rest.lines] == [(8, False), (9, False), (10, True), (70, True)]
    assert [ln.number for ln in shown] == [50]


# --- content cache vs model-visible evidence -----------------------------------


def _write_file(path, lines):
    path.write_text("\n".join(lines) + "\n")
    return str(path)


def test_compaction_clears_visible_evidence_but_keeps_the_content_cache(tmp_path):
    a = _write_file(tmp_path / "a.py", ["x = 1"])
    store = SessionStore("c1")
    store.record_lines([(a, 1, "x = 1")])
    assert store.seen() == {line_key(a, 1, "x = 1")}
    handle({"hook_event_name": "PreCompact", "session_id": "c1"})
    assert store.seen() == set()
    assert list(store.content.glob("*.fp"))  # disk facts outlive the agent's context
    store.record_lines([(a, 1, "x = 1")])
    assert store.seen() == {line_key(a, 1, "x = 1")}


def test_edit_hook_invalidates_only_that_files_lines(tmp_path):
    a = _write_file(tmp_path / "a.py", ["x = 1"])
    b = _write_file(tmp_path / "b.py", ["y = 2"])
    store = SessionStore("e1")
    store.record_lines([(a, 1, "x = 1"), (b, 1, "y = 2")])
    for tool, key in (("Edit", "file_path"), ("Write", "file_path"), ("MultiEdit", "file_path"), ("NotebookEdit", "notebook_path")):
        assert handle({"hook_event_name": "PostToolUse", "tool_name": tool, "session_id": "e1",
                       "cwd": str(tmp_path), "tool_input": {key: "a.py"}}) is None
    assert store.seen() == {line_key(b, 1, "y = 2")}
    # Shown again after the edit: counts again.
    store.record_lines([(a, 1, "x = 1")])
    assert line_key(a, 1, "x = 1") in store.seen()


def test_edit_in_a_subagent_or_pre_tool_use_leaves_the_main_store(tmp_path):
    a = _write_file(tmp_path / "a.py", ["x = 1"])
    store = SessionStore("e2")
    store.record_lines([(a, 1, "x = 1")])
    handle({"hook_event_name": "PreToolUse", "tool_name": "Edit", "session_id": "e2", "tool_input": {"file_path": a}})
    handle({"hook_event_name": "PostToolUse", "tool_name": "Edit", "session_id": "e2", "agent_id": "sub",
            "tool_input": {"file_path": a}})
    assert store.seen() == {line_key(a, 1, "x = 1")}


def test_file_changed_on_disk_lapses_its_evidence(tmp_path):
    a = _write_file(tmp_path / "a.py", ["x = 1", "z = 3"])
    b = _write_file(tmp_path / "b.py", ["y = 2"])
    store = SessionStore("d1")
    store.record_lines([(a, 1, "x = 1"), (b, 1, "y = 2")])
    # e.g. `sed -i` from Bash: no Edit event, but the content hash differs.
    _write_file(tmp_path / "a.py", ["x = 1", "z = 4"])
    # Rewritten with the same bytes (a checkout, a formatter with nothing to do): still true.
    _write_file(tmp_path / "b.py", ["y = 2"])
    assert store.seen() == {line_key(b, 1, "y = 2")}


def test_known_hash_is_reused_when_size_and_mtime_match(tmp_path, monkeypatch):
    a = _write_file(tmp_path / "a.py", ["x = 1"])
    old = time.time() - 60
    os.utime(a, (old, old))
    store = SessionStore("d2")
    store.record_lines([(a, 1, "x = 1")])
    opened = []
    real_open = open

    def spy(path, *args, **kwargs):
        opened.append(str(path))
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr("builtins.open", spy)
    assert store.seen() == {line_key(a, 1, "x = 1")}
    assert a not in opened


def test_invalidation_survives_merging_the_parts(tmp_path):
    a = _write_file(tmp_path / "a.py", ["x = 1"])
    store = SessionStore("m1")
    store.record_lines([(a, 1, "x = 1")])
    store.invalidate(a)
    for i in range(COMPACT_AT + 2):
        store.record([f"k{i}"])
    assert len(list(store.visible.glob("*.keys"))) < COMPACT_AT
    assert line_key(a, 1, "x = 1") not in store.seen()
    assert {f"k{i}" for i in range(COMPACT_AT + 2)} <= store.seen()


def test_persisted_output_records_only_its_preview(tmp_path):
    raw = "\n".join(f"a.py:{n}:{'v' * 60} {n}" for n in range(1, 400))
    store = SessionStore("p1", visible_chars=5000)
    store.record_output(raw, "", str(tmp_path))
    seen = store.seen()
    assert line_key(str(tmp_path / "a.py"), 1, f"{'v' * 60} 1") in seen
    assert line_key(str(tmp_path / "a.py"), 300, f"{'v' * 60} 300") not in seen
    assert 0 < len(seen) < 40


def test_lossless_view_never_leaves_out_seen_lines(tmp_path):
    raw = "\n".join(
        f"src/pkg/mod{f:02d}.py:{n}:    result = handle(target, value_{f}_{n})"
        for f in range(10) for n in range(1, 15)
    )
    config = Config(view="lossless", max_tokens=4800, trigger_tokens=100)
    store = SessionStore("l1")
    first = slim(raw, config=config, session=store, cwd=str(tmp_path))
    second = slim(raw, config=config, session=store, cwd=str(tmp_path))
    assert store.seen()  # recorded
    assert second.text == first.text and "already shown" not in second.text


@needs_rg
def test_hook_edit_between_greps_shows_that_file_again(repo):
    first = _grep(repo, "ed")
    handle({"hook_event_name": "PostToolUse", "tool_name": "Edit", "session_id": "ed", "cwd": str(repo),
            "tool_input": {"file_path": str(repo / "mod00.py")}})
    second = _grep(repo, "ed")
    assert any(ln.startswith("mod00.py") for ln in body_lines(first))
    assert any(ln.startswith("mod00.py") for ln in body_lines(second))
    assert "mod00.py:" not in second.splitlines()[-1].split("not repeated:", 1)[-1]


@needs_rg
def test_hook_records_a_grep_result_it_lets_through(repo):
    event = {
        "hook_event_name": "PostToolUse", "tool_name": "Grep", "cwd": str(repo), "session_id": "pt",
        "tool_input": {"pattern": "target", "output_mode": "content", "path": "mod03.py"},
        "tool_response": {"mode": "content", "content": "3:    value_3 = compute(target, 3)", "numLines": 1},
    }
    assert handle(event, Config(max_tokens=800, trigger_tokens=800, view="lossless")) is None
    assert SessionStore("pt").seen() == {line_key(str(repo / "mod03.py"), 3, "    value_3 = compute(target, 3)")}
