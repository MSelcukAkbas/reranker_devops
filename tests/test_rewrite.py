import shlex
import subprocess
import shutil

import pytest

from searchslim.rewrite import Prepared, prepare, rewrite_command

R = "SLIM"


def rw(cmd):
    return rewrite_command(cmd, runner=R)


@pytest.mark.parametrize(
    "cmd",
    [
        "rg foo src",
        "rg -n -C2 'a|b' src",
        "rg -l foo",
        "rg --files -g '*.py'",
        "grep -rn foo .",
        "grep -rl foo .",
        "fd -e py",
        "find . -name '*.py'",
        "rg foo *.py ~/x",
        "rg --sort modified foo",
        "git grep -n foo",
        "git grep foo -- '*.py'",
        "git ls-files",
        "git ls-files src",
        "tree src",
        "tree -L 3",
        "ls -R src",
        "ls -laR",
        "find / -name x.py 2>/dev/null",
        "rg foo src 2>&1",
    ],
)
def test_search_commands_are_wrapped_as_typed(cmd):
    # Anchor flags are added at run time (after glob expansion), not here.
    assert rw(cmd) == f"SLIM run -- {cmd}"


def test_cd_prefix_stays_outside():
    assert rw("cd /repo && rg foo") == "cd /repo && SLIM run -- rg foo"


def test_run_args_go_before_the_command():
    assert rewrite_command("rg foo", runner=R, run_args=["--max-tokens=500"]) == "SLIM run --max-tokens=500 -- rg foo"


@pytest.mark.parametrize(
    "cmd,kind",
    [
        ("rg -n foo src | head -50", "content"),
        ("rg foo src | head", "lines"),
        ("rg -l foo | sort", "paths"),
        ("rg -c foo | sort -t: -k2 -rn", "count"),
        ("grep -rn foo . | grep -v test", "content"),
        ("find . -name '*.py' | sort | head -n 20", "paths"),
        ("fd -e py 2>/dev/null | grep -v vendor", "paths"),
        ("git grep -n foo | tail -n 30", "content"),
        ("tree | head -100", "lines"),
        ("rg -n 'a|b' src | uniq", "content"),
        ("rg -n foo | grep -e bar", "content"),
    ],
)
def test_filter_pipelines_run_whole_through_the_shell(cmd, kind):
    assert rw(cmd) == f"SLIM run --kind={kind} --shell -- {shlex.quote(cmd)}"


@pytest.mark.parametrize(
    "cmd",
    [
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
        "rg foo | xargs cat",
        "rg foo | tail -f",
        "rg foo | head file",
        "rg foo | sort -o out",
        "rg foo | uniq -c",
        "rg foo | uniq - out",
        "rg foo | grep -c x",
        "rg foo | grep x file",
        "rg foo | grep -n x",
        "rg foo | wc -l",
        "rg foo || ls",
        "rg foo |& head",
        "ls | grep foo",
        "rg foo 2>err.txt",
        "rg foo >/dev/null 2>&1",
        "git log",
        "git grep -O foo",
        "git ls-files -z",
        "tree -o out.txt",
        "tree -J",
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


def test_prepare_anchors_multi_file_searches(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert prepare(["rg", "foo", "src"]).argv == ["rg", "--sort=path", "--with-filename", "--line-number", "foo", "src"]
    assert prepare(["rg", "--sort", "modified", "foo"]).argv == ["rg", "--with-filename", "--line-number", "--sort", "modified", "foo"]
    assert prepare(["rg", "-l", "foo"]).argv == ["rg", "--sort=path", "-l", "foo"]
    assert prepare(["grep", "-rn", "foo", "."]).argv == ["grep", "-H", "-n", "-rn", "foo", "."]
    assert prepare(["grep", "-rl", "foo", "."]).argv == ["grep", "-rl", "foo", "."]
    assert prepare(["git", "grep", "foo"]).argv == ["git", "grep", "-n", "foo"]
    assert prepare(["git", "grep", "-l", "foo"]).argv == ["git", "grep", "-l", "foo"]
    assert prepare(["tree"]).kind == "lines"
    assert prepare(["ls", "-R"]).kind == "lines"
    assert prepare(["fd", "x"]) == Prepared(["fd", "x"])


def test_prepare_keeps_single_file_searches_pathless(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "a.rs").write_text("fn x() {}\n")
    (tmp_path / "b.rs").write_text("fn y() {}\n")
    one = prepare(["rg", "-C2", "-g", "*.rs", "fn ", "a.rs"])
    assert one.argv == ["rg", "--sort=path", "--line-number", "-C2", "-g", "*.rs", "fn ", "a.rs"]
    assert one.default_path == "a.rs"
    assert prepare(["rg", "-e", "fn", "a.rs"]).default_path == "a.rs"
    assert prepare(["grep", "fn", "a.rs"]) == Prepared(["grep", "-n", "fn", "a.rs"], default_path="a.rs")
    # Two files, a directory, or an explicit -H still get a path on every line.
    assert prepare(["rg", "fn", "a.rs", "b.rs"]).default_path == ""
    assert prepare(["rg", "fn", "."]).default_path == ""
    assert "--with-filename" in prepare(["rg", "-H", "fn", "a.rs"]).argv


@pytest.mark.skipif(shutil.which("rg") is None, reason="rg not installed")
def test_single_file_search_under_budget_is_not_cut(tmp_path):
    # Regression: the path prefix used to push a single-file search over the
    # budget, which then cut it to a handful of matches.
    name = "crates/printer/src/standard.rs"
    (tmp_path / "crates/printer/src").mkdir(parents=True)
    body = "\n".join(f"    fn f_{i}(&self) {{" for i in range(150))
    (tmp_path / name).write_text(body + "\n")
    cmd = rewrite_command(f"rg 'fn ' {name}", run_args=["--max-tokens=2000"])
    out = subprocess.run(cmd, shell=True, cwd=tmp_path, capture_output=True, text=True).stdout
    assert out.count("\n") == 150
    assert out.startswith("1:    fn f_0(&self) {")
    assert "[searchslim]" not in out


@pytest.mark.skipif(shutil.which("rg") is None, reason="rg not installed")
def test_pipeline_output_is_reduced_and_keeps_exit_code(tmp_path):
    for i in range(30):
        (tmp_path / f"m{i:02d}.py").write_text("\n".join(f"target_{n} = {n}" for n in range(40)) + "\n")
    cmd = rewrite_command("rg -n target | grep -v 'target_1 '", run_args=["--max-tokens=300"])
    assert "--shell" in cmd
    proc = subprocess.run(cmd, shell=True, cwd=tmp_path, capture_output=True, text=True)
    assert proc.returncode == 0
    lines = proc.stdout.strip().splitlines()
    assert lines[-1].startswith("[searchslim]")
    assert all(":target_" in ln for ln in lines[:-1])
    miss = subprocess.run(rewrite_command("rg -n nothing_here | sort"), shell=True, cwd=tmp_path, capture_output=True, text=True)
    assert miss.stdout == ""


@pytest.mark.skipif(shutil.which("tree") is None, reason="tree not installed")
def test_tree_keeps_shallow_levels(tmp_path):
    for a in range(6):
        for b in range(6):
            d = tmp_path / f"pkg{a}" / f"sub{b}"
            d.mkdir(parents=True)
            for c in range(10):
                (d / f"file_with_a_long_name_{c}.py").write_text("")
    cmd = rewrite_command("tree", run_args=["--max-tokens=400"])
    out = subprocess.run(cmd, shell=True, cwd=tmp_path, capture_output=True, text=True, env={"LANG": "C.UTF-8", "PATH": "/usr/bin:/bin"}).stdout
    lines = out.strip().splitlines()
    assert "sub5" in out and "file_with_a_long_name_0.py" not in out
    assert "directories, 360 files" in out
    assert lines[-1].startswith("[searchslim]") and "deeper than level 2" in lines[-1] and "pkg0/sub0/ (10)" in lines[-1]
