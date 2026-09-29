"""Claude Code's PowerShell tool (Windows): search commands are wrapped like Bash ones."""

import shutil

import pytest

from searchslim.hooks import handle
from searchslim.rewrite import PS_UTF8, rewrite_powershell

PY = "C:/Python314/python.exe"
RUNNER = f"& '{PY}' -m searchslim"


def rw(cmd):
    return rewrite_powershell(cmd, python=PY, run_args=["--max-tokens=2000"])


def test_rg_goes_through_run_without_double_dash():
    out = rw('rg -n "require\\(" services\\gateway\\src')
    assert out == f"{PS_UTF8}{RUNNER} run '--max-tokens=2000' rg -n \"require\\(\" services\\gateway\\src"


def test_select_string_and_recursive_listing_are_piped_into_filter():
    out = rw("Get-ChildItem -Recurse -Include *.js services | Select-String -Pattern 'process.env'")
    assert out == (
        f"{PS_UTF8}Get-ChildItem -Recurse -Include *.js services | Select-String -Pattern 'process.env'"
        f" | Out-String -Stream -Width 4096 | {RUNNER} filter '--max-tokens=2000'"
    )
    assert rw("Select-String -Path src\\*.js -Pattern foo").endswith(f"{RUNNER} filter '--max-tokens=2000'")
    assert rw("gci -Recurse -Name services").endswith("filter '--max-tokens=2000' --kind=paths")
    assert rw("Get-ChildItem services -Recurse").endswith("filter '--max-tokens=2000' --kind=lines")


@pytest.mark.parametrize("cmd", [
    "Get-ChildItem services",                                   # not recursive: small
    "rg foo; Remove-Item x",                                    # statement separator
    "rg foo > out.txt",                                         # redirect
    'rg "$env:X" src',                                          # interpolation
    "Get-ChildItem -Recurse | ForEach-Object { $_.FullName }",  # script block
    "Get-ChildItem -Recurse | Remove-Item",                     # other cmdlet
    "rg --files | Select-String foo",                           # rg in a pipeline
    "rg -r x foo",                                              # rg replace mode
    "$env:SEARCHSLIM='off'; rg foo",
    "rg 'unbalanced",
])
def test_anything_else_is_left_alone(cmd):
    assert rw(cmd) is None


def test_quoted_run_args_survive():
    out = rewrite_powershell("rg foo", python=PY, run_args=["--transcript=C:\\Users\\o'neil\\t.jsonl"])
    assert "'--transcript=C:\\Users\\o''neil\\t.jsonl'" in out


@pytest.mark.skipif(shutil.which("rg") is None, reason="rg not installed")
def test_hook_rewrites_the_powershell_tool():
    out = handle({"hook_event_name": "PreToolUse", "tool_name": "PowerShell", "tool_input": {"command": "rg -n foo src", "description": "d"}})
    upd = out["hookSpecificOutput"]["updatedInput"]
    assert upd["description"] == "d" and " -m searchslim run " in upd["command"] and upd["command"].endswith(" rg -n foo src")
