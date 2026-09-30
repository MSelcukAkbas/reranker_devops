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
    # Jest prints only failures and the summary when stdout is not a terminal;
    # the one thing left to drop is the failure repeated under its summary.
    raw = fixture("jest.txt")
    out = compact(raw, trigger_tokens=0)
    assert set(out.stats["dropped"]) == {"failure lines repeated in jest's summary"}
    assert out.text.count("● suite 7 › broken") == 1 and out.text.count("FAIL ./s7.test.js") == 2
    assert_subsequence(out.text, raw)


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
    assert rewrite_test_command("cd 'my app'; pytest", runner="S") == "cd 'my app'; S run --compact -- pytest"
    for command in ("pytest -v | tail -20", "cd $(x) && pytest", "cd a && cd b && pytest", "pytest > out.txt", "pytest; echo done", "pytest $(ls)", "SEARCHSLIM=off pytest"):
        assert rewrite_test_command(command, runner="S") is None, command


def test_rewrite_powershell_test_command():
    out = rewrite_powershell("python -m pytest -v pyt 2>&1", python="C:\\Python314\\python.exe")
    assert out.endswith("& 'C:/Python314/python.exe' -m searchslim run --compact python -m pytest -v pyt 2>&1")
    assert rewrite_powershell("python -m pytest | Select-Object -Last 5", python="py") is None
    for command, prefix in (
        ("Set-Location pyt; python -m pytest -v 2>&1", "Set-Location pyt; "),
        ('cd "C:\\a b\\pyt" && pytest -v', 'cd "C:\\a b\\pyt" && '),
        ("Set-Location -Path pyt; npm test", "Set-Location -Path pyt; "),
    ):
        out = rewrite_powershell(command, python="py")
        assert out.startswith("$OutputEncoding") and f"; {prefix}& 'py' -m searchslim run --compact " in out, command
    for command in ("cd $d; pytest", "cd pyt; pytest; Remove-Item x", "cd pyt; Get-Date"):
        assert rewrite_powershell(command, python="py") is None, command
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


# --- build/lint diagnostics and other build output ----------------------------

import random  # noqa: E402
import re  # noqa: E402

from searchslim.compact import ANSI_RE  # noqa: E402
from searchslim.rewrite import is_build_command  # noqa: E402

# Real outputs (tsc 6.0, ESLint 10, gcc 13, cargo 1.9x, go 1.24 vet, jest 30
# --coverage), paths anonymized. dotnet-build.txt is hand-built in MSBuild's
# format (no .NET SDK where the fixtures were made): warnings, then the same
# warnings and error again under `Build FAILED.`.
DIAGNOSTIC_FIXTURES = ["tsc.txt", "tsc-pretty.txt", "eslint.txt", "gcc.txt", "cargo-build.txt", "go-vet.txt", "dotnet-build.txt"]

# Independent of compact.py: where each tool puts a diagnostic's location.
_ONE_LINE = [
    re.compile(r"^\s*(?P<path>[^\s(][^(]*?)(?P<pos>\(\d+,\d+\)): (?P<msg>(?:error|warning) .*)$"),  # tsc, MSBuild
    re.compile(r"^(?P<path>\S+?):(?P<pos>\d+:\d+) - (?P<msg>error .*)$"),  # tsc --pretty
    re.compile(r"^(?P<path>\S+?):(?P<pos>\d+:\d+): (?P<msg>(?:warning|error): .*)$"),  # gcc
    re.compile(r"^(?P<path>\S+\.go):(?P<pos>\d+:\d+): (?P<msg>.*)$"),  # go vet
]
_ESLINT_ROW = re.compile(r"^\s+(\d+:\d+)\s+((?:error|warning)\s+\S.*)$")
_RUST_HEAD = re.compile(r"^(?:error|warning)(?:\[\w+\])?: \S")
_RUST_LOC = re.compile(r"^\s*--> (\S+?):(\d+:\d+)$")
_NOTE = re.compile(
    rf"^{re.escape(NOTE_PREFIX)} (?P<n>\d+) more places with this same diagnostic \((?P<msg>.*)\)(?:, under (?P<under>\S+))?: (?P<where>.*)$"
)
_POS = re.compile(r"^(\(\d+(,\d+)*\)|\d+(:\d+)?)$")


def locations(text: str) -> set[tuple[str, str, str]]:
    """(path, position, message) of every diagnostic the text shows or lists."""
    found = set()
    lines = [ANSI_RE.sub("", ln) for ln in text.splitlines()]
    heading = None
    for i, ln in enumerate(lines):
        note = _NOTE.match(ln)
        if note:
            places = []
            for entry in note["where"].split("; "):
                words = entry.split(" ")
                k = len(words)
                while k > 1 and _POS.match(words[k - 1]):
                    k -= 1
                path = (note["under"] or "") + " ".join(words[:k])
                places += [(path, pos, note["msg"]) for pos in words[k:]]
            assert len(places) == int(note["n"]), ln
            found.update(places)
            continue
        row = _ESLINT_ROW.match(ln)
        if row and heading:
            found.add((heading, row[1], re.sub(r"\s{2,}", "  ", row[2].strip())))
            continue
        heading = ln.strip() if ln.strip() and not ln[0].isspace() and i + 1 < len(lines) and _ESLINT_ROW.match(lines[i + 1]) else None
        if _RUST_HEAD.match(ln) and i + 1 < len(lines) and _RUST_LOC.match(lines[i + 1]):
            loc = _RUST_LOC.match(lines[i + 1])
            found.add((loc[1], loc[2], ln))
            continue
        for pattern in _ONE_LINE:
            m = pattern.match(ln)
            if m:
                found.add((m["path"], m["pos"], m["msg"]))
                break
    return found


@pytest.mark.parametrize("name", DIAGNOSTIC_FIXTURES)
def test_every_diagnostic_location_of_the_raw_output_is_kept_or_listed(name):
    raw = fixture(name)
    out = compact(raw, trigger_tokens=0)
    assert out is not None and "diagnostics" in out.stats["tools"]
    assert out.stats["tokens"] < out.stats["raw_tokens"] * 0.4, out.stats
    expected = locations(raw)
    assert len(expected) >= 30
    assert locations(out.text) == expected
    assert_subsequence(out.text, raw)
    # Error-level diagnostics that are not repeated are never touched.
    kept = set(out.text.splitlines())
    for ln in raw.splitlines():
        if "error" in ln and raw.count(ln.split(": ", 1)[-1]) == 1 and not ln.startswith(" "):
            assert ln in kept, ln


def test_diagnostic_invariant_holds_on_shuffled_mixed_output():
    # Lines of every tool shuffled together: nothing crashes and no location is lost.
    # (ESLint is left out: its rows mean something only under their file heading.)
    pool = [ln for name in DIAGNOSTIC_FIXTURES if name != "eslint.txt" for ln in fixture(name).splitlines()]
    for seed in range(20):
        rng = random.Random(seed)
        raw = "\n".join(rng.sample(pool, 600))
        out = compact(raw, trigger_tokens=0)
        text = out.text if out else raw
        assert locations(text) == locations(raw), seed


def test_first_diagnostic_of_a_group_stays_whole_with_its_code_frame():
    out = compact(fixture("gcc.txt"), trigger_tokens=0).text.splitlines()
    i = out.index("m0.c:7:11: warning: comparison of integer expressions of different signedness: 'int' and 'unsigned int' [-Wsign-compare]")
    assert out[i + 1] == "    7 |     if (a < u) return b;" and out[i + 2].strip() == "|           ^"
    assert out[i + 3].startswith(f"{NOTE_PREFIX} 39 more places with this same diagnostic (warning: comparison")
    assert "m0.c 14:11 21:11 28:11 35:11; m1.c 7:11" in out[i + 3]


def test_rustc_errors_with_different_labels_never_group():
    # Same header, different `expected X, found Y` labels: all three stay whole.
    blocks = []
    for n, (want, got) in enumerate([("i32", "&str"), ("u8", "String"), ("i32", "&str"), ("bool", "i32")] * 60):
        blocks += ["error[E0308]: mismatched types", f" --> src/lib.rs:{n + 1}:9", "  |", f"{n + 1} |     let x: {want} = v;", f"  |            ---   ^ expected `{want}`, found `{got}`", ""]
    raw = "\n".join(blocks)
    out = compact(raw, trigger_tokens=0)
    assert locations(out.text) == locations(raw)
    notes = [ln for ln in out.text.splitlines() if "more places" in ln]
    assert len(notes) == 3  # one group per distinct label, i32/&str merged
    assert sum(ln.startswith("error[E0308]") for ln in out.text.splitlines()) == 3


def test_tsc_chained_messages_are_part_of_the_diagnostic():
    lines = []
    for n in range(80):
        lines += [f"src/a{n}.ts(3,5): error TS2345: Argument of type 'string' is not assignable to parameter of type 'Opts'.",
                  f"  Type 'string' has no properties in common with type '{'Opts' if n % 2 else 'Config'}'."]
    out = compact("\n".join(lines), trigger_tokens=0)
    notes = [ln for ln in out.text.splitlines() if "more places" in ln]
    assert len(notes) == 2 and all("39 more places" in ln for ln in notes)
    kept = out.text.splitlines()
    assert "  Type 'string' has no properties in common with type 'Config'." in kept
    assert "  Type 'string' has no properties in common with type 'Opts'." in kept


def test_msbuild_summary_repeats_are_shown_once():
    out = compact(fixture("dotnet-build.txt"), trigger_tokens=0)
    lines = out.text.splitlines()
    error = [ln for ln in lines if "error CS0246" in ln]
    assert len(error) == 1
    assert "Build FAILED." in lines and "    1 Error(s)" in lines and "Time Elapsed 00:00:04.87" in lines
    assert out.stats["dropped"]["duplicate diagnostic lines"] >= 70
    # Every CS8618 names a different property: none is grouped.
    assert sum("CS8618" in ln for ln in lines) == 8


def test_eslint_groups_go_after_the_first_files_list():
    out = compact(fixture("eslint.txt"), trigger_tokens=0).text.splitlines()
    first = out.index("/home/dev/web/src/api/m0.js")
    notes = [i for i, ln in enumerate(out) if ln.startswith(NOTE_PREFIX) and "more places" in ln]
    assert notes and all(ESLINT_ROW_OR_NOTE(ln) for ln in out[first + 1 : notes[-1] + 1])
    assert ", under /home/dev/web/src/: " in out[notes[0]]
    assert any(ln.startswith("✖ 593 problems (465 errors, 128 warnings)") for ln in out[-5:])


def ESLINT_ROW_OR_NOTE(line: str) -> bool:
    return bool(_ESLINT_ROW.match(line)) or line.startswith(NOTE_PREFIX)


def test_messages_repeated_twice_are_left_alone():
    raw = "\n".join(f"src/m{n}.ts({n + 1},1): error TS2304: Cannot find name 'x{n // 2}'." for n in range(400))
    assert compact(raw, trigger_tokens=0) is None


def test_search_output_is_not_mistaken_for_diagnostics():
    # rg output of lines that look like compiler messages keeps its own shape
    # unless a message truly repeats with a location: then it is only grouped.
    grep = "\n".join(f"docs/errors.md:{n}:error: see section {n}" for n in range(1, 2000))
    assert compact(grep, trigger_tokens=0) is None


def test_jest_coverage_and_summary_repeat():
    raw = fixture("jest-coverage.txt")
    out = compact(raw, trigger_tokens=0)
    lines = out.text.splitlines()
    assert "  m1.cjs  |     100 |      100 |     100 |     100 |                   " not in lines
    assert "  m0.cjs  |      75 |       50 |     100 |     100 | 2                 " in lines
    assert any(ln.startswith("All files |") for ln in lines)
    assert sum(ln == "    Expected: 3" for ln in lines) == 1  # summary copy dropped
    assert lines.count("FAIL src/store/m3.test.js") == 2  # both headers stay
    assert "Tests:       1 failed, 449 passed, 450 total" in lines
    assert_subsequence(out.text, raw)


def test_pytest_cov_full_rows():
    rows = [f"src/pkg/mod{n}.py{' ' * 10}{10 + n}{' ' * 6}0{' ' * 4}100%" for n in range(300)]
    raw = "\n".join(["Name                 Stmts   Miss  Cover", "-" * 40] + rows[:150] + ["src/pkg/bad.py          40     12    70%"] + rows[150:] + ["-" * 40, "TOTAL                 9999     12    99%"])
    out = compact(raw, trigger_tokens=0)
    assert "src/pkg/bad.py          40     12    70%" in out.text and "TOTAL                 9999     12    99%" in out.text
    assert "mod7.py" not in out.text and out.stats["dropped"]["fully covered coverage rows"] == 300


def test_maven_surefire_passing_classes():
    lines = []
    for n in range(120):
        lines += [f"[INFO] Running com.shop.T{n}Test", f"[INFO] Tests run: 4, Failures: 0, Errors: 0, Skipped: 0, Time elapsed: 0.0{n % 10} s - in com.shop.T{n}Test"]
    lines += ["[INFO] Running com.shop.BadTest", "[ERROR] Tests run: 2, Failures: 1, Errors: 0, Skipped: 0, Time elapsed: 0.1 s <<< FAILURE! - in com.shop.BadTest",
              "[ERROR] com.shop.BadTest.total  Time elapsed: 0.01 s  <<< FAILURE!", "org.opentest4j.AssertionFailedError: expected: <3> but was: <2>",
              "[INFO] Results:", "[ERROR] Tests run: 482, Failures: 1, Errors: 0, Skipped: 0"]
    out = compact("\n".join(lines), trigger_tokens=0)
    assert "Running com.shop.T5Test" not in out.text
    for text in ("[INFO] Running com.shop.BadTest", "<<< FAILURE! - in com.shop.BadTest", "AssertionFailedError", "Tests run: 482, Failures: 1"):
        assert text in out.text


def test_gradle_and_bundle_progress():
    raw = "\n".join(
        [f"> Task :app:compile{n} UP-TO-DATE" for n in range(40)]
        + [f"dist/assets/chunk-{n:03d}-a1b2c3.js   {n}.21 kB │ gzip: 1.{n % 10}0 kB" for n in range(60)]
        + ["> Task :app:test FAILED", "FAILURE: Build failed with an exception."]
    )
    out = compact(raw, trigger_tokens=0)
    assert out.stats["dropped"] == {"gradle progress lines": 40, "bundle asset lines": 60}
    assert "> Task :app:test FAILED" in out.text and "FAILURE: Build failed" in out.text


@pytest.mark.parametrize(
    "command",
    ["tsc", "tsc -p . --noEmit", "npx tsc", "npx eslint src", "eslint .", "npm run build", "npm run lint", "pnpm build",
     "yarn lint", "pnpm run typecheck", "npx next build", "npx vite build", "go build ./...", "go vet ./...",
     "cargo build", "cargo clippy", "cargo check", "dotnet build", "mvn -q test", "./gradlew build", "mypy src",
     "python -m mypy src", "ruff check .", "uv run mypy ."],
)
def test_build_commands_are_recognized(command):
    assert is_build_command(command.split())


@pytest.mark.parametrize(
    "command",
    ["tsc -w", "tsc --watch", "npm run dev", "npm start", "vite", "npx vite", "next dev", "go run .", "cargo run",
     "dotnet run", "npm install", "yarn", "webpack serve", "ruff format", "npx prettier --write .", "go test ./...",
     "mvn exec:java", "npm run build -- --watch"],
)
def test_other_commands_are_not_builds(command):
    assert not is_build_command(command.split())


def test_build_commands_are_wrapped():
    assert rewrite_test_command("cd web && npx tsc --noEmit 2>&1", runner="S") == "cd web && S run --compact -- npx tsc --noEmit 2>&1"
    assert rewrite_test_command("npm run build | tail -5", runner="S") is None
    out = rewrite_powershell("Set-Location web; npm run build 2>&1", python="py")
    assert out.endswith("; Set-Location web; & 'py' -m searchslim run --compact npm run build 2>&1")


def test_run_compact_groups_a_failing_builds_diagnostics(tmp_path):
    script = tmp_path / "build.py"
    script.write_text(
        "import sys\n"
        "for n in range(300):\n"
        "    print(f'src/m{n % 30}.ts({n + 1},5): error TS2304: Cannot find name \\'expect\\'.')\n"
        "print('src/app.ts(9,1): error TS2322: Type \\'string\\' is not assignable to type \\'number\\'.')\n"
        "print('Found 301 errors in 31 files.')\n"
        "sys.exit(2)\n"
    )
    proc = run_compact(tmp_path, sys.executable, str(script))
    out = proc.stdout.decode()
    assert proc.returncode == 2
    assert "src/app.ts(9,1): error TS2322" in out and "Found 301 errors in 31 files." in out
    assert "299 more places with this same diagnostic (error TS2304: Cannot find name 'expect'.)" in out
    raw = subprocess.run([sys.executable, str(script)], capture_output=True).stdout.decode()
    assert locations(out) == locations(raw) and len(locations(raw)) == 301
