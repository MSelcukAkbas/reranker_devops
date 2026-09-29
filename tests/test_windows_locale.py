"""Repros from a real Windows run (cp1254 locale): nothing may be swallowed or garbled."""

import json
import os
import shutil
import subprocess
import sys

import pytest

from searchslim import cli

TURKISH = "// PUBLIC_PATHS kontrolü: DEĞİL Şu an 🎯"


def _env(**extra):
    env = {k: v for k, v in os.environ.items() if not k.startswith("SEARCHSLIM")}
    # cp1254 is the Turkish Windows code page: 0x9E (in UTF-8 'Ş'/'Ğ') is undefined there.
    env.update({"PYTHONIOENCODING": "cp1254", "PYTHONUTF8": "0", "SEARCHSLIM_SESSION": "off", "SEARCHSLIM_TRIGGER_TOKENS": "0", **extra})
    return env


def _searchslim(args, cwd, stdin=b"", **env):
    return subprocess.run([sys.executable, "-m", "searchslim", *args], cwd=cwd, input=stdin, capture_output=True, env=_env(**env))


@pytest.fixture
def repo(tmp_path):
    src = tmp_path / "services" / "gateway" / "src" / "plugins"
    src.mkdir(parents=True)
    (src / "auth.js").write_text(f"{TURKISH}\nconst PUBLIC_PATHS = [];\n" + "".join(f"PUBLIC_PATHS.push({i}) // DEĞİL\n" for i in range(80)), encoding="utf-8")
    return tmp_path


needs_rg = pytest.mark.skipif(shutil.which("rg") is None, reason="rg not installed")


@needs_rg
def test_run_keeps_turkish_output_under_cp1254(repo):
    for extra in ({}, {"SEARCHSLIM_MAX_TOKENS": "60"}):  # passthrough and reduced
        proc = _searchslim(["run", "--", "rg", "-n", "PUBLIC_PATHS", "services/gateway/src"], repo, **extra)
        assert proc.returncode == 0, proc.stderr
        out = proc.stdout.decode("utf-8")
        assert "kontrolü" in out and "DEĞİL" in out and "Şu" in out and "�" not in out


def _hook(repo, pattern):
    event = {"hook_event_name": "PostToolUse", "tool_name": "Grep", "tool_input": {"pattern": pattern, "path": "services/gateway/src", "output_mode": "content", "-n": True}, "cwd": str(repo)}
    proc = _searchslim(["hook"], repo, json.dumps(event, ensure_ascii=False).encode("utf-8"), SEARCHSLIM_MAX_TOKENS="60", SEARCHSLIM_RERANK="off")
    assert proc.returncode == 0
    assert proc.stdout, "hook skipped the event"
    return json.loads(proc.stdout)["hookSpecificOutput"]["updatedToolOutput"]["content"]


@needs_rg
def test_hook_sends_utf8_text_to_the_model(repo):
    reason = _hook(repo, "PUBLIC_PATHS")
    assert "kontrolü" in reason and "Ã" not in reason


@needs_rg
def test_hook_handles_turkish_pattern(repo):
    assert "DEĞİL" in _hook(repo, "DEĞİL")


def test_backslash_path_list_names_real_directories(tmp_path):
    paths = "".join(f"services\\svc{k}\\src\\f{i}.js\n" for k in range(4) for i in range(300))
    script = f"import sys; sys.stdout.buffer.write({paths.encode()!r})"
    proc = _searchslim(["run", "--kind", "paths", "--", sys.executable, "-c", script], tmp_path)
    note = proc.stdout.decode("utf-8").splitlines()[-1]
    assert "services/svc" in note and "./ (" not in note


def test_reduction_failure_prints_raw_output(monkeypatch, capfdbinary):
    def boom(*a, **k):
        raise UnicodeEncodeError("charmap", "x", 0, 1, "simulated")

    monkeypatch.setattr(cli, "slim", boom)
    raw = "a.js:1:" + TURKISH + "\n"
    rc = cli.main(["run", "--", sys.executable, "-c", f"import sys; sys.stdout.buffer.write({raw.encode()!r})"])
    out, err = capfdbinary.readouterr()
    assert rc == 0
    assert out.decode("utf-8") == raw
    assert b"showing raw output" in err
