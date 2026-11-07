"""Raven host: runs INSIDE a test machine and drives that machine with H's desktop agent.

Standalone on purpose (stdlib + `hai-agents[desktop]` only) so it can be copied into any
machine (Cua Linux container, Cua macOS VM, Windows PC, Devin cloud VM, this Mac).

    python raven_host.py --task "..." [--mode local-runtime|agents-api] [--model holo4-27b]

Mirrors what H's own `holo` MCP server does (hai_agents_cli/mcp_server.py: run_agent):
local runtime gets the resolved `h/holo-desktop` spec inline; cloud gets the agent name.

Protocol with raven-core:
- stdout: one JSON object per line, {"kind": ..., ...}. Nothing else is printed to stdout.
- stdin:  each line is a steering message forwarded to the running session.
- screenshots are written to --shots-dir and reported by path.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any

AGENT = "h/holo-desktop"


def out(kind: str, **data: Any) -> None:
    sys.stdout.write(json.dumps({"kind": kind, "ts": time.time(), **data}, default=str) + "\n")
    sys.stdout.flush()


def _get(obj: Any, name: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


class Shots:
    def __init__(self, folder: Path) -> None:
        self.folder = folder
        self.folder.mkdir(parents=True, exist_ok=True)
        self.n = 0

    def save(self, image: Any) -> str | None:
        src, typ = _get(image, "source"), _get(image, "type")
        if not src:
            return None
        if typ == "url" and not str(src).startswith("data:"):
            return str(src)
        if str(src).startswith("data:"):
            src = str(src).split(",", 1)[1]
        ext = "jpg" if "jpeg" in str(_get(image, "media_type") or "") else "png"
        path = self.folder / f"{self.n:03d}.{ext}"
        self.n += 1
        path.write_bytes(base64.b64decode(src))
        return str(path)


def emit_event(ev: Any, shots: Shots) -> None:
    etype = _get(ev, "type")
    data = _get(ev, "data")
    if etype == "AgentEvent":
        kind = _get(data, "kind")
        if kind == "policy_event":
            for req in _get(data, "tool_reqs") or []:
                out("action", tool=_get(req, "tool_name"), args=_get(req, "args") or {},
                    thought=_get(data, "content") or _get(data, "reasoning_content"))
            if not _get(data, "tool_reqs") and _get(data, "content"):
                out("message", text=_get(data, "content"))
        elif kind == "observation_event":
            if _get(data, "image") is not None and (p := shots.save(_get(data, "image"))):
                out("screenshot", path=p)
            elif _get(data, "text"):
                out("observation", text=str(_get(data, "text"))[:2000])
        elif kind == "answer_event":
            out("answer", answer=_get(data, "answer"), outcome=_get(data, "outcome"))
        elif kind == "message_event":
            for item in _get(data, "content") or []:
                if isinstance(item, str):
                    out("message", text=item)
                elif (p := shots.save(item)):
                    out("screenshot", path=p)
        elif kind == "tool_result":
            out("tool_result", tool=_get(_get(data, "tool_req"), "tool_name"),
                result=str(_get(data, "result"))[:500])
    elif etype == "LiveViewUrlEvent":
        out("live_view", url=_get(data, "live_view_url"))
    elif etype == "AgentErrorEvent":
        out("error", error=_get(data, "error"))
    elif etype == "AgentCompletionEvent":
        out("completion", reason=_get(data, "reason"))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--task", required=True)
    ap.add_argument("--mode", default=os.environ.get("RAVEN_H_MODE", "local-runtime"),
                    choices=["local-runtime", "agents-api"])
    ap.add_argument("--model", default=os.environ.get("HAI_MODEL") or None)
    ap.add_argument("--max-steps", type=int, default=60)
    ap.add_argument("--max-time", type=float, default=600)
    ap.add_argument("--shots-dir", default=os.environ.get("RAVEN_SHOTS_DIR", "/tmp/raven-shots"))
    args = ap.parse_args(argv)

    from hai_agents import Client

    shots = Shots(Path(args.shots_dir))
    cloud = Client()
    params: dict[str, Any] = {"agent": AGENT, "messages": args.task,
                              "max_steps": args.max_steps, "max_time_s": args.max_time}
    if args.model:
        params["overrides"] = {"agent.model": args.model}
    if args.mode == "local-runtime":
        spec = cloud.agents.get_agent(AGENT, resolve=True).model_dump(mode="json", exclude_none=True)
        params["agent"] = spec
        client = Client.local()
    else:
        client = cloud

    handle = client.start_session(**params)
    out("started", session_id=handle.id, mode=args.mode, model=args.model, agent=AGENT)

    def steer() -> None:
        for line in sys.stdin:
            if line.strip():
                try:
                    handle.send_message(line.strip())
                    out("steered", text=line.strip())
                except Exception as e:  # noqa: BLE001
                    out("error", error=f"steer failed: {e}")

    threading.Thread(target=steer, daemon=True).start()
    try:
        for ev in handle.stream(until="terminal"):
            emit_event(ev, shots)
        result = handle.wait_for_completion()
        out("result", status=_get(result, "status"), answer=_get(result, "answer"),
            outcome=_get(result, "outcome"), error=_get(result, "error"))
        return 0
    except KeyboardInterrupt:
        out("result", status="interrupted", answer=None, outcome=None, error="interrupted")
        return 130
    finally:
        try:
            handle.cancel()
        except Exception:  # noqa: BLE001
            pass


if __name__ == "__main__":
    sys.exit(main())
