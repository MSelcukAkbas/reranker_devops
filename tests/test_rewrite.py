import subprocess
import shutil

import pytest

from searchslim.rewrite import rewrite_command

R = "SLIM"


def rw(cmd):
    return rewrite_command(cmd, runner=R)


@pytest.mark.parametrize(
    "cmd,expected",
    [
        ("rg foo src", "SLIM run -- rg --sort=path --with-filename --line-number foo src"),
        ("rg -n -C2 'a|b' src", "SLIM run -- rg --sort=path --with-filename --line-number -n -C2 'a|b' src"),
        ("rg -l foo", "SLIM run -- rg --sort=path -l foo"),
        ("rg --files -g '*.py'", "SLIM run -- rg --sort=path --files -g '*.py'"),
        ("grep -rn foo .", "SLIM run -- grep -H -n -rn foo ."),
        ("grep -rl foo .", "SLIM run -- grep -rl foo ."),
        ("fd -e py", "SLIM run -- fd -e py"),
        ("find . -name '*.py'", "SLIM run -- find . -name '*.py'"),
        ("rg foo *.py ~/x", "SLIM run -- rg --sort=path --with-filename --line-number foo *.py ~/x"),
        ("rg --sort modified foo", "SLIM run -- rg --with-filename --line-number --sort modified foo"),
        ("cd /repo && rg foo", "cd /repo && SLIM run -- rg --sort=path --with-filename --line-number foo"),
    ],
)
def test_search_commands_are_wrapped(cmd, expected):
    assert rw(cmd) == expected


@pytest.mark.parametrize(
    "cmd",
    [
        "rg foo | head",
        "rg foo > out.txt",
        "rg foo; ls",
        "cd a && rg foo && ls",
        "rg $(cat pat) src",
        'rg "$PAT" src',
        "rg foo src &",
        "find . -name '*.pyc' -delete",
        "find . -exec cat {} +",
        "fd -x rm",
        "rg -o foo",
        "rg --replace bar foo",
        "grep -q foo file",
        "SEARCHSLIM=off rg foo",
        "ls -la",
        "python3 -m pytest",
        "rg 'unbalanced",
    ],
)
def test_other_commands_are_left_alone(cmd):
    assert rw(cmd) is None


@pytest.mark.skipif(shutil.which("rg") is None, reason="rg not installed")
def test_rewritten_command_runs_and_keeps_exit_code(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\ntarget = 2\n")
    hit = subprocess.run(rewrite_command("rg target"), shell=True, cwd=tmp_path, capture_output=True, text=True)
    assert hit.returncode == 0
    assert hit.stdout.strip() == "a.py:2:target = 2"
    miss = subprocess.run(rewrite_command("rg nothing_here"), shell=True, cwd=tmp_path, capture_output=True, text=True)
    assert miss.returncode == 1
    assert miss.stdout == ""
