"""YAML test specs (see specs/*.yaml).

Format:
  name: str                      # defaults to the file stem
  description: str
  vars: {key: value}             # values may use {{builtins}} and earlier vars
  setup: {platform: str | [str]} # each command launches the app (see runner.py); prefix "sh:" to run plainly
  agents: [{platform, role?, title?, task, start_delay_s?}]
  timeout_s: int (default 180)

Template syntax: {{var}}. Builtins: random_word, random_number, timestamp
(each evaluated once per load_spec so every agent sees the same value).
"""

from __future__ import annotations

import random
import re
import time
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

from raven_core.events import Platform

_WORDS = (
    "amber anchor aurora basil birch cobalt comet coral crane dune ember falcon fjord garnet glacier "
    "harbor hazel indigo iris jade juniper kestrel lagoon lotus maple meadow nebula onyx opal orchid "
    "otter pepper pine quartz raven reef saffron sierra slate sparrow tango tundra umber velvet willow "
    "zephyr"
).split()

_TPL = re.compile(r"\{\{\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*\}\}")


class SpecAgent(BaseModel):
    platform: Platform
    task: str
    role: str | None = None
    title: str | None = None
    start_delay_s: float | None = None


class Spec(BaseModel):
    name: str
    description: str = ""
    vars: dict[str, str] = Field(default_factory=dict)
    setup: dict[Platform, list[str]] = Field(default_factory=dict)
    agents: list[SpecAgent] = Field(default_factory=list)
    timeout_s: int = 180

    @property
    def platforms(self) -> list[Platform]:
        out: list[Platform] = []
        for a in self.agents:
            if a.platform not in out:
                out.append(a.platform)
        return out


def _builtins() -> dict[str, str]:
    return {
        "random_word": f"{random.choice(_WORDS)}{random.randint(10, 99)}",
        "random_number": str(random.randint(1000, 9999)),
        "timestamp": str(int(time.time())),
    }


def render(text: str, ctx: dict[str, str]) -> str:
    return _TPL.sub(lambda m: ctx.get(m.group(1), m.group(0)), text)


def _render_all(obj: Any, ctx: dict[str, str]) -> Any:
    if isinstance(obj, str):
        return render(obj, ctx)
    if isinstance(obj, list):
        return [_render_all(x, ctx) for x in obj]
    if isinstance(obj, dict):
        return {k: _render_all(v, ctx) for k, v in obj.items()}
    return obj


def _spec_files(specs_dir: Path) -> list[Path]:
    specs_dir = Path(specs_dir)
    if not specs_dir.is_dir():
        return []
    return sorted([*specs_dir.glob("*.yaml"), *specs_dir.glob("*.yml")])


def _read(path: Path) -> dict:
    data = yaml.safe_load(path.read_text()) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path} is not a mapping")
    data.setdefault("name", path.stem)
    return data


def list_specs(specs_dir: Path) -> list[dict]:
    """[{name, description, platforms}] for every spec file (invalid files are skipped)."""
    out = []
    for f in _spec_files(specs_dir):
        try:
            d = _read(f)
            plats: list[str] = []
            for a in d.get("agents") or []:
                p = str(a.get("platform"))
                if p not in plats:
                    plats.append(p)
            out.append(
                {"name": str(d["name"]), "description": " ".join(str(d.get("description") or "").split()), "platforms": plats}
            )
        except Exception:  # noqa: BLE001
            continue
    return out


def _find(name: str, specs_dir: Path) -> Path:
    specs_dir = Path(specs_dir)
    for ext in (".yaml", ".yml"):
        p = specs_dir / f"{name}{ext}"
        if p.is_file():
            return p
    for f in _spec_files(specs_dir):
        try:
            if str(_read(f).get("name")) == name:
                return f
        except Exception:  # noqa: BLE001
            continue
    raise FileNotFoundError(f"spec {name!r} not found in {specs_dir}")


def load_spec(name: str, specs_dir: Path) -> Spec:
    """Load + render a spec by name (spec `name:` field or file stem)."""
    data = _read(_find(name, specs_dir))
    ctx = _builtins()
    for k, v in (data.get("vars") or {}).items():
        ctx[str(k)] = render(str(v), ctx)
    data["vars"] = {k: ctx[str(k)] for k in (data.get("vars") or {})}
    data = _render_all(data, ctx)
    setup = {}
    for plat, cmds in (data.get("setup") or {}).items():
        if cmds is None:
            continue
        setup[plat] = [str(c) for c in cmds] if isinstance(cmds, list) else [str(cmds)]
    data["setup"] = setup
    data["description"] = " ".join(str(data.get("description") or "").split())
    return Spec.model_validate(data)
