"""Test/build output compaction (compact.py) and its Bash/PowerShell hook."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from searchslim.compact import compact
from searchslim.hooks import handle
from searchslim.rules import NOTE_PREFIX

FIXTURES = Path(__file__).parent / "fixtures" / "compact"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def body(text: str) -> list[str]:
    return [ln for ln in text.splitlines() if not ln.startswith(NOTE_PREFIX)]


def assert_subsequence(out: str, raw: str) -> None:
    """Every kept line is a raw line, in the raw order (nothing rewritten)."""
    raw_lines = iter(raw.splitlines())
    for ln in body(out):
        assert any(ln == r for r in raw_lines), ln


# Real outputs captured from the runners (see tests/fixtures/compact).
REAL = {
    "pytest-v.txt": (["test_demo.py::test_bad FAILED", "E       AssertionError", "1 failed, 301 passed, 1 skipped"], "PASSED"),
    "pytest-ra.txt": (["FAILED test_demo.py::test_bad", "SKIPPED [1]", "error handling works", "test_err_output_passes"], "PASSED test_demo"),
    "vitest-verbose.txt": (["× s7.test.js > suite 7 > broken", "AssertionError", "Tests  1 failed | 321 passed", "deprecated api used"], "✓"),
    "go-v.txt": (["--- FAIL: TestMany/case77", "a_test.go:3: got 77 want 0", "=== RUN   TestMany/case77", "=== RUN   TestLog"], "--- PASS"),
    "cargo-test.txt": (["test tests::t50 ... FAILED", "left: 50", "test result: FAILED. 199 passed"], "... ok"),
}


@pytest.mark.parametrize("name", sorted(REAL))
def test_real_runner_output_keeps_failures_and_drops_passes(name):
    raw = fixture(name)
    out = compact(raw, trigger_tokens=0)
    assert out is not None and out.stats["tokens"] < out.stats["raw_tokens"] / 2
    must_keep, dropped = REAL[name]
    for text in must_keep:
        assert text in out.text, text
    assert dropped not in "\n".join(body(out.text))
    assert out.text.splitlines()[-1].startswith(f"{NOTE_PREFIX} not shown: ")
    assert_subsequence(out.text, raw)


def test_every_failure_line_of_the_raw_output_is_kept():
    raw = fixture("pytest-v.txt")
    kept = set(compact(raw, trigger_tokens=0).text.splitlines())
    for ln in raw.splitlines():
        if ln.startswith("E ") or "FAILED" in ln or "Error" in ln:
            assert ln in kept, ln


def test_pytest_passing_output_keeps_its_test_header():
    out = compact(fixture("pytest-ra.txt"), trigger_tokens=0).text.splitlines()
    i = out.index("error handling works")
    assert "test_err_output_passes" in out[i - 1] and "PASSES" in out[i - 2]


def test_non_tty_jest_output_is_already_compact():
    # Jest prints only failures and the summary when stdout is not a terminal.
    assert compact(fixture("jest.txt"), trigger_tokens=0) is None


def test_jest_verbose_pass_suites():
    suites = []
    for n in range(30):
        suites += [f"PASS src/s{n}.test.js", f"  suite {n}"] + [f"    ✓ case {i} (1 ms)" for i in range(8)] + [""]
    suites[5 * 11 + 3 : 5 * 11 + 3] = ["  console.warn", "    deprecated api used"]
    raw = "\n".join(
        suites
        + ["FAIL src/bad.test.js", "  suite bad", "    ✓ ok case (1 ms)", "    ✕ broken (3 ms)", "", "  ● suite bad › broken", "", "    expect(received).toBe(expected)", ""]
        + ["Test Suites: 1 failed, 30 passed, 31 total", "Tests:       1 failed, 241 passed, 242 total"]
    )
    out = compact(raw, trigger_tokens=0)
    lines = out.text.splitlines()
    assert "    ✕ broken (3 ms)" in lines and "  ● suite bad › broken" in lines
    assert "✓" not in "\n".join(body(out.text))
    # The warning of a passing suite stays under its own PASS header.
    i = lines.index("  console.warn")
    assert lines[i - 1].startswith("PASS src/s5.test.js") or lines[i - 2].startswith("PASS src/s5.test.js")
    assert sum(ln.startswith("PASS ") for ln in lines) == 1
    assert_subsequence(out.text, raw)


def test_dotnet_test():
    raw = "\n".join(
        [f"  Passed Ns.Tests.Case{i} [2 ms]" for i in range(300)]
        + ["  Failed Ns.Tests.Broken [5 ms]", "  Error Message:", "   Assert.Equal() Failure", "  Stack Trace:", "     at Ns.Tests.Broken() in /src/T.cs:line 12"]
        + ["", "Failed!  - Failed:     1, Passed:   300, Skipped:     0, Total:   301, Duration: 1 s - Ns.Tests.dll (net8.0)"]
    )
    out = compact(raw, trigger_tokens=0)
    assert "Passed Ns.Tests.Case" not in out.text
    for text in ("Failed Ns.Tests.Broken", "Assert.Equal() Failure", "T.cs:line 12", "Failed!  - Failed:     1"):
        assert text in out.text


def test_cargo_build_progress():
    raw = "\n".join([f"   Compiling crate{i} v1.{i}.0" for i in range(60)] + ["error[E0308]: mismatched types", " --> src/main.rs:3:5", "error: could not compile `app`"])
    out = compact(raw, trigger_tokens=0)
    assert "Compiling" not in "\n".join(body(out.text))
    assert "error[E0308]: mismatched types" in out.text and "60 cargo progress lines" in out.text


def test_log_repetition_keeps_first_and_last():
    raw = "\n".join(
        ["2026-09-30T01:00:00Z start"]
        + [f"2026-09-30T01:{i // 60:02d}:{i % 60:02d}Z healthcheck OK" for i in range(1, 400)]
        + ["2026-09-30T01:07:00Z ERROR db timeout"]
    )
    out = compact(raw, trigger_tokens=0)
    lines = out.text.splitlines()
    assert lines[1] == "2026-09-30T01:00:01Z healthcheck OK"
    assert lines[2] == f"{NOTE_PREFIX} 397 similar lines not shown"
    assert lines[3] == "2026-09-30T01:06:39Z healthcheck OK"
    assert lines[4] == "2026-09-30T01:07:00Z ERROR db timeout"


def test_lines_differing_in_data_never_collapse():
    # Only timestamps are ignored when comparing; counts and ids are data.
    raw = "\n".join(f"row {i}: value={i * 7}" for i in range(2000))
    assert compact(raw, trigger_tokens=0) is None


def test_other_output_passes_unchanged():
    # An agent's own summary (PowerShell Group-Object), search output mentioning
    # PASSED, and small outputs are not touched.
    summary = "\n".join(f"src/auth/file{i}.ts        {i}" for i in range(3000))
    grep = "\n".join(f"tests/t{i}.py:{i}:    assert result == 'PASSED'" for i in range(3000))
    assert compact(summary, trigger_tokens=0) is None
    assert compact(grep, trigger_tokens=0) is None
    assert compact(fixture("pytest-v.txt")) is not None
    assert compact(fixture("cargo-test.txt")) is None  # under the default trigger


def shell_event(stdout: str, stderr: str = "", tool: str = "Bash") -> dict:
    return {
        "hook_event_name": "PostToolUse",
        "tool_name": tool,
        "tool_input": {"command": "python -m pytest -v"},
        "tool_response": {"stdout": stdout, "stderr": stderr, "interrupted": False, "isImage": False},
    }


@pytest.mark.parametrize("tool", ["Bash", "PowerShell"])
def test_hook_compacts_shell_test_output_in_the_tools_shape(tool):
    raw = fixture("pytest-v.txt")
    out = handle(shell_event(raw, "some stderr", tool))["hookSpecificOutput"]
    assert out["hookEventName"] == "PostToolUse"
    updated = out["updatedToolOutput"]
    assert set(updated) == {"stdout", "stderr", "interrupted", "isImage"}
    assert updated["stderr"] == "some stderr" and updated["interrupted"] is False
    assert "test_bad FAILED" in updated["stdout"] and "PASSED" not in "\n".join(body(updated["stdout"]))


def test_hook_compacts_stderr_too():
    # jest and vitest write their report to stderr.
    out = handle(shell_event("", fixture("vitest-verbose.txt")))["hookSpecificOutput"]["updatedToolOutput"]
    assert out["stdout"] == "" and "✓" not in "\n".join(body(out["stderr"]))


def test_hook_leaves_other_shell_output_alone(monkeypatch):
    assert handle(shell_event("\n".join(f"line {i}" for i in range(5000)))) is None
    assert handle(shell_event("small output")) is None
    assert handle({**shell_event(""), "tool_response": "not a dict"}) is None
    monkeypatch.setenv("SEARCHSLIM_COMPACT", "off")
    assert handle(shell_event(fixture("pytest-v.txt"))) is None


def test_hook_trigger_from_env(monkeypatch):
    monkeypatch.setenv("SEARCHSLIM_COMPACT_TRIGGER_TOKENS", "100000")
    assert handle(shell_event(fixture("pytest-v.txt"))) is None
    monkeypatch.setenv("SEARCHSLIM_COMPACT_TRIGGER_TOKENS", "0")
    assert handle(shell_event(fixture("cargo-test.txt"))) is not None


def test_cli_compact():
    raw = fixture("pytest-v.txt")
    proc = subprocess.run([sys.executable, "-m", "searchslim", "compact", "--stats"], input=raw.encode(), capture_output=True)
    assert proc.returncode == 0
    assert "test_bad FAILED" in proc.stdout.decode() and "PASSED" not in proc.stdout.decode()
    assert json.loads(proc.stderr.decode())["dropped"]
    small = subprocess.run([sys.executable, "-m", "searchslim", "compact"], input=b"1 passed\n", capture_output=True)
    assert small.stdout == b"1 passed\n"


# --- wrapping test commands (PreToolUse) --------------------------------------

from searchslim.rewrite import is_test_command, rewrite_powershell, rewrite_test_command  # noqa: E402


@pytest.mark.parametrize(
    "command",
    ["pytest -v", "python -m pytest -q tests", "py.test", "uv run pytest -x", "npx jest", "npx vitest run",
     "npm test", "npm run test", "yarn test", "pnpm test", "go test ./... -v", "cargo test", "cargo nextest run",
     "dotnet test", "C:\\Python314\\python.exe -m pytest"],
)
def test_test_commands_are_recognized(command):
    assert is_test_command(command.split())


@pytest.mark.parametrize(
    "command",
    ["python script.py", "npx jest --watch", "vitest watch", "pytest --pdb", "npm install", "go build ./...",
     "cargo build", "rg pytest", "echo pytest", "npm run lint"],
)
def test_other_commands_are_not(command):
    assert not is_test_command(command.split())


def test_rewrite_test_command_forms():
    assert rewrite_test_command("pytest -v", runner="S") == "S run --compact -- pytest -v"
    assert (
        rewrite_test_command("cd app && CI=1 python -m pytest -q 2>&1", runner="S")
        == "cd app && CI=1 S run --compact -- python -m pytest -q 2>&1"
    )
    for command in ("pytest -v | tail -20", "pytest > out.txt", "pytest; echo done", "pytest $(ls)", "SEARCHSLIM=off pytest"):
        assert rewrite_test_command(command, runner="S") is None, command


def test_rewrite_powershell_test_command():
    out = rewrite_powershell("python -m pytest -v pyt 2>&1", python="C:\\Python314\\python.exe")
    assert out.endswith("& 'C:/Python314/python.exe' -m searchslim run --compact python -m pytest -v pyt 2>&1")
    assert rewrite_powershell("python -m pytest | Select-Object -Last 5", python="py") is None
    assert rewrite_powershell("pytest $args", python="py") is None


def test_hook_wraps_test_commands(monkeypatch):
    event = {"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {"command": f"{sys.executable} -m pytest -q"}}
    command = handle(event)["hookSpecificOutput"]["updatedInput"]["command"]
    assert " run --compact -- " in command
    monkeypatch.setenv("SEARCHSLIM_COMPACT", "off")
    assert handle(event) is None


def run_compact(tmp_path, *args, env=None):
    import os

    root = Path(__file__).resolve().parent.parent / "src"
    return subprocess.run(
        [sys.executable, "-m", "searchslim", "run", "--compact", "--", *args],
        cwd=tmp_path, capture_output=True, env={**os.environ, "PYTHONPATH": str(root), **(env or {})},
    )


def test_run_compact_keeps_a_failing_runs_exit_code_and_failure(tmp_path):
    (tmp_path / "test_many.py").write_text(
        "import pytest\n@pytest.mark.parametrize('i', range(300))\ndef test_ok(i):\n    assert i >= 0\n"
        "def test_bad():\n    assert {'a': 1} == {'a': 2}\n"
    )
    proc = run_compact(tmp_path, sys.executable, "-m", "pytest", "-v", "-p", "no:cacheprovider")
    out = proc.stdout.decode()
    assert proc.returncode == 1
    assert "test_many.py::test_bad FAILED" in out and "AssertionError" in out and "1 failed, 300 passed" in out
    assert "PASSED" not in "\n".join(body(out)) and "300 passing/skipped test lines" in out


def test_run_compact_passes_small_output_and_missing_commands(tmp_path):
    proc = run_compact(tmp_path, sys.executable, "-c", "import sys; print('1 passed'); sys.exit(3)")
    assert proc.returncode == 3 and proc.stdout.decode().strip() == "1 passed"
    assert run_compact(tmp_path, "no-such-runner-xyz").returncode == 127


def test_hook_compacts_the_persisted_full_output(tmp_path):
    raw = fixture("pytest-v.txt")
    saved = tmp_path / "out.txt"
    saved.write_text(raw, encoding="utf-8")
    event = shell_event(raw[:8000])  # Claude Code hands hooks a cut stdout over ~30 KB
    event["tool_response"].update(persistedOutputPath=str(saved), persistedOutputSize=len(raw))
    updated = handle(event)["hookSpecificOutput"]["updatedToolOutput"]
    assert "1 failed, 301 passed" in updated["stdout"] and "test_bad FAILED" in updated["stdout"]
    assert not any(k.startswith("persistedOutput") for k in updated)
