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
    return handle(event, Config(max_tokens=800))["hookSpecificOutput"]["updatedToolOutput"]


@needs_rg
def test_hook_repeat_grep_then_compaction_resets(repo):
    first, second = _grep(repo), _grep(repo)
    assert "already shown by an earlier search" in second
    assert not set(body_lines(first)[1:]) & set(body_lines(second)[1:])  # [0] is the header
    handle({"hook_event_name": "PreCompact", "session_id": "sess"})
    assert _grep(repo) == first


@needs_rg
def test_bash_run_uses_the_session_across_tools(repo):
    first = _grep(repo)
    cmd = [sys.executable, "-m", "searchslim", "run", "--max-tokens=800", "--session=sess", "--",
           "rg", "--sort=path", "--with-filename", "--line-number", "target"]
    out = subprocess.run(cmd, cwd=repo, capture_output=True, text=True, env=os.environ).stdout
    assert "already shown by an earlier search" in out
    assert not set(body_lines(first)[1:]) & set(body_lines(out))


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
