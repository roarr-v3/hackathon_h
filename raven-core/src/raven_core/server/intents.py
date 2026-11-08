"""Tiny keyword/regex intent parser shared by /voice/command and the ACP bridge.

Examples
    "run bitchat geohash interop on linux and mac"  -> run(spec, [linux, macos])
    "/run bitchat-geohash-interop on linux,macos"   -> run
    "stop all"                                       -> stop_all
    "start a linux agent to open a terminal"         -> agent(linux, "open a terminal")
    "on linux: open a terminal and run uname -a"     -> agent(linux, ...)
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field

from raven_core.events import Placement, Platform
from raven_core.placement import requested

_PLATFORM_RE = re.compile(
    r"\b(mac\s?os|os\s?x|macbook|mac|linux|ubuntu|debian|windows|win(?:dows)?\s?11|win|ios|iphone|ipad)\b",
    re.I,
)
_ALIAS = {
    "macos": Platform.macos,
    "mac os": Platform.macos,
    "osx": Platform.macos,
    "os x": Platform.macos,
    "mac": Platform.macos,
    "macbook": Platform.macos,
    "linux": Platform.linux,
    "ubuntu": Platform.linux,
    "debian": Platform.linux,
    "windows": Platform.windows,
    "win": Platform.windows,
    "ios": Platform.ios,
    "iphone": Platform.ios,
    "ipad": Platform.ios,
}
_PLAT_WORD = r"(?:mac\s?os|os\s?x|mac|linux|ubuntu|windows|win|ios|iphone)"
_PLACEMENT_PREFIX_RE = re.compile(
    r"^(?:(?:in|on|inside|using)\s+(?:an?\s+|the\s+)?(?:vm|virtual\s+machine|sandbox|container)"
    r"|bare[\s-]?metal|(?:on\s+)?this\s+(?:mac|machine|computer))\s*[:,\-]?\s*",
    re.I,
)


@dataclass
class Intent:
    kind: str  # run | agent | stop_all | list | specs | help | unknown
    text: str = ""
    spec: str | None = None
    platforms: list[Platform] | None = None
    platform: Platform | None = None
    task: str | None = None
    placement: Placement = Placement.auto
    notes: list[str] = field(default_factory=list)


def _alias(word: str) -> Platform | None:
    w = re.sub(r"\s+", " ", word.lower()).strip()
    if w in _ALIAS:
        return _ALIAS[w]
    if w.startswith("win"):
        return Platform.windows
    return _ALIAS.get(w.replace(" ", ""))


def parse_platforms(text: str) -> list[Platform]:
    out: list[Platform] = []
    for m in _PLATFORM_RE.finditer(text or ""):
        p = _alias(m.group(1))
        if p is not None and p not in out:
            out.append(p)
    return out


def _norm(s: str) -> str:
    s = re.sub(r"\b(test|tests|spec|suite|the)\b", " ", s.lower())
    return re.sub(r"[^a-z0-9]+", "", s)


def match_spec(query: str, names: list[str]) -> str | None:
    q = _norm(query)
    if not q:
        return None
    normed = {k: n for n in names if (k := _norm(n))}
    if q in normed:
        return normed[q]
    contains = [n for k, n in normed.items() if len(q) >= 4 and (q in k or k in q)]
    if len(contains) >= 1:
        return sorted(contains, key=len)[0]
    close = difflib.get_close_matches(q, list(normed), n=1, cutoff=0.6)
    return normed[close[0]] if close else None


def parse_command(text: str, spec_names: list[str]) -> Intent:
    raw = (text or "").strip()
    t = raw.lstrip("/").strip().rstrip(".!?").strip()
    low = t.lower()

    if re.fullmatch(
        r"(?:please\s+)?(?:stop|cancel|abort|kill|halt)(?:\s+(?:all|everything|it|now|"
        r"all agents|the agents|agents|the run|all runs|runs))*",
        low,
    ):
        return Intent("stop_all", text=raw)
    if low in ("help", "?", "commands"):
        return Intent("help", text=raw)
    if low in ("specs", "list specs", "tests", "list tests", "suites"):
        return Intent("specs", text=raw)
    if low in ("status", "agents", "list", "list agents", "ls"):
        return Intent("list", text=raw)

    m = re.match(r"(?:please\s+)?(?:run|test|execute|launch)\s+(?:the\s+)?(?P<rest>.+)$", t, re.I | re.S)
    if m:
        rest = m.group("rest").strip()
        spec_part, plat_part = rest, ""
        parts = re.split(r"\s+on\s+", rest, flags=re.I)
        if len(parts) > 1:
            spec_part, plat_part = " on ".join(parts[:-1]), parts[-1]
        spec = match_spec(spec_part, spec_names)
        if spec is None and not plat_part:
            spec = match_spec(rest, spec_names)
        if spec is not None:
            plats = parse_platforms(plat_part) if plat_part else []
            return Intent("run", text=raw, spec=spec, platforms=plats or None, placement=requested(raw))

    agent_patterns = [
        rf"(?:please\s+)?(?:start|spawn|launch|open|create|new)\s+(?:an?\s+|the\s+)?(?P<plat>{_PLAT_WORD})\s+agent\b\s*(?:to|that|and|which|:|,|-)?\s*(?P<task>.+)$",
        rf"(?:on|in|using)\s+(?:the\s+)?(?P<plat>{_PLAT_WORD})(?:\s+(?:vm|machine|box|agent))?\s*[:,\-]\s*(?P<task>.+)$",
        rf"(?P<plat>{_PLAT_WORD})\s*:\s*(?P<task>.+)$",
        rf"(?:on|in)\s+(?:the\s+)?(?P<plat>{_PLAT_WORD})\s+(?P<task>.+)$",
    ]
    for pat in agent_patterns:
        m = re.match(pat, t, re.I | re.S)
        if m:
            p = _alias(m.group("plat"))
            task = _PLACEMENT_PREFIX_RE.sub("", m.group("task").strip()).strip()
            if p is not None and task:
                return Intent("agent", text=raw, platform=p, task=task, placement=requested(raw))

    return Intent("unknown", text=raw)


HELP = """**Raven commands**
- `run <spec> [on linux,macos,windows]` — run a YAML test suite on several platforms
- `on <platform>: <task>` — start one computer-use agent (e.g. `on linux: open a terminal and run uname -a`)
- `stop` — stop all running agents
- `specs` — list test suites · `status` — list agents
- Tests run on this machine when it is the same OS (bare metal); add `in a vm` to use a VM instead (you can watch it live)
Anything else is sent to the running agent (or starts one on the default platform)."""
