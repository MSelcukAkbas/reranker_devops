"""CLI behaviour that differs by platform: encoding, Windows paths, default ranking."""

import os
import subprocess
import sys

from searchslim import Config, slim
from searchslim.rerank import LexicalScorer, Query
from searchslim.rules import group_path, rollup_dirs

WIN_FILES = [f".\\projeler\\skill{k}\\scripts\\m{i}.py" for k in range(3) for i in range(20)]
WIN_RAW = "".join(f"{f}:{n}:def fn_{n}():\n" for f in WIN_FILES for n in range(1, 12))


def _cli(args, stdin=b"", env_extra=None):
    # cp1252 stands in for a Windows locale; the CLI must still speak UTF-8.
    env = {k: v for k, v in os.environ.items() if k != "SEARCHSLIM_RERANK"}
    env.update({"PYTHONIOENCODING": "cp1252", "PYTHONUTF8": "0", **(env_extra or {})})
    return subprocess.run([sys.executable, "-m", "searchslim", *args], input=stdin, capture_output=True, env=env)


def test_group_path_normalizes_windows_separators():
    assert group_path(".\\a\\b.py") == "a/b.py"
    assert group_path("./a/b.py") == "a/b.py"
    assert group_path("C:\\x\\y.py") == "C:/x/y.py"
    groups = rollup_dirs([(f, 1) for f in WIN_FILES], limit=5)
    assert [d for d, _ in groups] == ["projeler/skill0/scripts", "projeler/skill1/scripts", "projeler/skill2/scripts"]


def test_windows_paths_note_names_real_directories_and_keeps_shown_paths():
    for scorer in (None, LexicalScorer()):
        out = slim(WIN_RAW, config=Config(max_tokens=400), scorer=scorer, query=Query(pattern="def "))
        lines = out.text.splitlines()
        assert all(ln.startswith(".\\projeler\\") for ln in lines[:-1])  # original spelling
        note = lines[-1]
        assert "projeler/skill" in note and "./ (" not in note


def test_run_reads_and_writes_utf8_whatever_the_locale():
    text = "a.md:1:## 🎯 Hedef Sistem — Ne İstiyorsun? ğüşıöç\n"
    script = f"import sys; sys.stdout.buffer.write({text.encode('utf-8')!r})"
    proc = _cli(["run", "--", sys.executable, "-c", script])
    assert proc.returncode == 0
    assert proc.stdout.decode("utf-8") == text


def test_filter_reads_utf8_stdin():
    text = "a.md:1:Şu an orkestratör → ajan\n"
    proc = _cli(["filter"], stdin=text.encode("utf-8"))
    assert proc.stdout.decode("utf-8") == text


def test_run_ranks_by_default_and_env_turns_it_off():
    script = f"import sys; sys.stdout.write({WIN_RAW!r})"
    ranked = _cli(["run", "--max-tokens", "400", "--", sys.executable, "-c", script])
    assert "ranked by relevance (lexical)" in ranked.stdout.decode("utf-8").splitlines()[-1]
    plain = _cli(["run", "--max-tokens", "400", "--", sys.executable, "-c", script], env_extra={"SEARCHSLIM_RERANK": "off"})
    assert "ranked by relevance" not in plain.stdout.decode("utf-8")
    assert "files dropped from the end" in plain.stdout.decode("utf-8")
