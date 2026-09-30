"""Evidence invariants of the lossless view over every benchmark fixture.

Whatever level the lossless view picks short of L3, the output has exactly
the raw output's match locations (L0/L1 also every line and its text), each
path of a file list and each path:count of a count list. Checked at the hook
defaults and at budgets tight enough to force L1 without context and L2, on
the fixtures as captured and rewritten with Windows absolute paths.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from searchslim import Config, Kind, detect_kind, estimate_tokens, parse, slim
from searchslim.lossless import match_locations
from searchslim.rerank import pattern_and_paths
from searchslim.rules import LOSSLESS_MAX_TOKENS, LOSSLESS_TRIGGER_TOKENS

import json

ROOT = Path(__file__).resolve().parent.parent / "benchmark"
FIXTURES = sorted((ROOT / "fixtures").glob("*.txt"))
TASKS = {t["id"]: t for t in json.loads((ROOT / "tasks.json").read_text(encoding="utf-8"))["tasks"]}
WIN_ROOT = "C:\\Users\\dev\\projects\\repo\\"

CONFIGS = {
    "hook": Config(view="lossless", max_tokens=LOSSLESS_MAX_TOKENS, trigger_tokens=LOSSLESS_TRIGGER_TOKENS),
    "tight": Config(view="lossless", max_tokens=1500, trigger_tokens=0),
    "tighter": Config(view="lossless", max_tokens=800, trigger_tokens=0),
}


def _pattern(fixture: Path) -> str:
    task = TASKS.get(fixture.stem.split(".")[0])
    if not task or "cmd" not in task:
        return ""
    return pattern_and_paths(task["cmd"])[0]


def _norm(p: str) -> str:
    return p[2:] if p.startswith("./") else p


def _windows(raw: str) -> str:
    """The same output as Claude Code on Windows would show it: absolute paths with `\\`."""
    kind = detect_kind(raw)
    res = parse(raw, kind=kind)
    win = lambda p: WIN_ROOT + _norm(p).replace("/", "\\") if p else p  # noqa: E731
    if kind is Kind.CONTENT:
        return "\n".join(
            f"{win(ln.path)}{':' if ln.is_match else '-'}{ln.number}{':' if ln.is_match else '-'}{ln.text}"
            if ln.path else f"{ln.number}{':' if ln.is_match else '-'}{ln.text}"
            for ln in res.lines
        )
    if kind is Kind.COUNT:
        return "\n".join(f"{win(c.path)}:{c.count}" for c in res.counts)
    return "\n".join(win(p) for p in res.paths)


def _cases():
    for f in FIXTURES:
        raw = f.read_text(encoding="utf-8")
        for variant, text in (("raw", raw), ("windows", _windows(raw))):
            for name in CONFIGS:
                yield pytest.param(text, name, _pattern(f), id=f"{f.stem}-{variant}-{name}")


@pytest.mark.parametrize("raw,config_name,pattern", list(_cases()))
def test_lossless_levels_keep_every_location(raw, config_name, pattern):
    out = slim(raw, config=CONFIGS[config_name], pattern=pattern)
    level = out.stats.get("level")
    if out.stats.get("view") != "lossless":
        # L3 (ranked coverage) only past EMERGENCY_FACTOR x max_tokens.
        assert estimate_tokens(raw) > 3 * CONFIGS[config_name].max_tokens
        return
    assert level in ("L0", "L1", "L1-matches", "L2"), level
    assert estimate_tokens(out.text) <= estimate_tokens(raw)
    kind = detect_kind(raw)
    if kind is Kind.CONTENT:
        assert match_locations(out.text) == match_locations(raw)
        if level in ("L0", "L1"):
            full = lambda t: {(_norm(ln.path), ln.number, ln.text.strip(), ln.is_match) for ln in parse(t, kind=Kind.CONTENT).lines}  # noqa: E731
            assert full(out.text) == full(raw)
    elif kind is Kind.PATHS:
        assert {_norm(p) for p in parse(out.text).paths} == {_norm(p) for p in parse(raw).paths}
    else:
        assert {(_norm(c.path), c.count) for c in parse(out.text).counts} == {(_norm(c.path), c.count) for c in parse(raw).counts}


PROJECTION_CASES = {
    "definition": ("def |func |fn ", [
        "def handler_{i}(request):", "    async def method_{i}(self):", "func (s *Server) Handle{i}(w http.ResponseWriter) {",
        "pub fn search_{i}(&self) -> Result<()> {", "export function render{i}(props) {", "class Model{i}(Base):",
    ]),
    "route": ("router\\.(get|post)", [
        "router.get('/api/v1/items/{i}', auth, listItems{i});", "router.post(\"/api/v1/orders/{i}\", createOrder{i});",
        "app.delete('/api/v1/users/{i}', removeUser);",
    ]),
    "config key": ("config\\.get", [
        "const host = config.get('db.host.{i}');", "timeout = settings['REQUEST_TIMEOUT_{i}']", "port = config.get(\"server.port\")",
    ]),
    "dependency": ("\"\\^|version", [
        '    "package-{i}": "^1.2.{i}",', '    "@scope/lib-{i}": "~3.0.0",',
    ]),
}


@pytest.mark.parametrize("rec", sorted(PROJECTION_CASES))
@pytest.mark.parametrize("sep", ["/", "\\"])
def test_projection_keeps_every_location(rec, sep):
    pattern, templates = PROJECTION_CASES[rec]
    rows = []
    for f in range(160):
        for k, t in enumerate(templates):
            if (f + k) % 2:
                continue
            rows.append(f"services{sep}svc{f % 7}{sep}src{sep}module_{f:03d}.x:{k * 5 + 3}:{t.replace('{i}', str(f % 11))}")
    rows.append(f"services{sep}svc0{sep}README.md:1:unrelated text that no recognizer reads at all")
    raw = "\n".join(rows)
    for budget in (1500, 2500):
        out = slim(raw, config=Config(view="lossless", max_tokens=budget, trigger_tokens=0), pattern=pattern)
        if out.stats.get("view") != "lossless":
            continue
        if out.stats["level"] == "L2":
            assert out.stats["recognizer"] == rec
        assert match_locations(out.text) == match_locations(raw)
