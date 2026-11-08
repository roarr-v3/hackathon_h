"""Raven host: runs INSIDE a test machine and drives that machine with H's desktop agent.

Standalone on purpose (stdlib + `hai-agents[desktop]` only) so it can be copied into any
machine (Cua Linux container, Cua macOS VM, Windows PC, Devin cloud VM, this Mac).

    python raven_host.py --task "..." [--mode local-runtime|agents-api] [--model holo4-27b]

Both modes pass a resolved inline `h/holo-desktop` spec with an unclaimed
`user_device` desktop. The official SDK binds its local desktop bridge to this
session; a catalog name alone must never select an unrelated H cloud desktop.

Protocol with raven-core:
- stdout: one JSON object per line, {"kind": ..., ...}. Nothing else is printed to stdout.
- stdin:  each line is a steering message forwarded to the running session.
- screenshots are written to --shots-dir and reported by path.
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import copy
import io
import json
import os
import platform
import signal
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

AGENT = "h/holo-desktop"
ENV_KEYS = ("HAI_API_KEY", "HAI_MODEL", "HAI_BASE_URL", "HAI_API_BASE_URL")


def out(kind: str, **data: Any) -> None:
    text = json.dumps({"kind": kind, "ts": time.time(), **data}, default=str)
    if secret := os.environ.get("HAI_API_KEY"):
        text = text.replace(secret, "[redacted]")
    sys.stdout.write(text + "\n")
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


def bound_desktop_spec(spec: dict) -> dict:
    """Bind only the resolved official desktop spec to *this* machine.

    `Client` auto-bridges inline user_device environments with no session_id.
    Refuse unknown/mixed environments rather than running against another target.
    """
    spec = copy.deepcopy(spec)
    environments = spec.get("environments")
    if not isinstance(environments, list) or len(environments) != 1:
        raise ValueError("H desktop spec must contain exactly one resolved desktop environment")
    env = environments[0]
    if not isinstance(env, dict) or env.get("kind") != "desktop" or spec.get("subagents"):
        raise ValueError("H desktop spec is not an exclusively bound desktop agent")
    env["host"] = "user_device"
    env.pop("session_id", None)
    return spec


def preflight(mode: str, expected_platform: str | None) -> dict:
    """No API client, login, model call, runtime download or provisioning."""
    result: dict[str, Any] = {"ok": False, "system": sys.platform, "arch": platform.machine()}
    expected = {"linux": "linux", "windows": "win32", "macos": "darwin", "ios": "darwin"}.get(expected_platform)
    if expected and not sys.platform.startswith(expected):
        return {**result, "category": "configuration"}
    try:
        import hai_agents  # noqa: F401
        if mode == "local-runtime":
            from hai_agents_local.runtime.manifest import MANIFEST, platform_key

            try:
                supported = platform_key() in MANIFEST
            except RuntimeError:
                supported = False
            if not supported:
                return {**result, "category": "unsupported-runtime"}
        from hai_drivers.desktop.local import LocalDesktopDriver
    except (ImportError, SystemExit):
        return {**result, "category": "dependency"}
    except Exception:
        return {**result, "category": "desktop-unavailable"}
    driver = None
    try:
        if sys.platform == "darwin":
            from hai_agents_local.desktop import ensure_macos_input_permissions

            ensure_macos_input_permissions(prompt=False)
        driver = LocalDesktopDriver()
        width, height = driver.get_screen_size()
        image = driver.screenshot_b64()
        if not image or width <= 0 or height <= 0:
            raise RuntimeError("no desktop")
        return {**result, "ok": True, "width": width, "height": height, "mode": mode}
    except (Exception, SystemExit):
        return {**result, "category": "desktop-unavailable"}
    finally:
        if driver is not None:
            driver.close()


def native_action(request: dict) -> Any:
    """A narrow native-driver RPC for the explicitly selected Models fallback."""
    from hai_drivers.desktop.local import LocalDesktopDriver

    driver = LocalDesktopDriver()
    try:
        op = request["operation"]
        if op == "screenshot":
            return driver.screenshot_b64()
        if op == "size":
            return driver.get_screen_size()
        if op == "click":
            return driver.click(int(request["x"]), int(request["y"]), button=request.get("button", "left"))
        if op == "double_click":
            return driver.double_click(int(request["x"]), int(request["y"]))
        if op == "type_text":
            return driver.write(str(request["text"]))
        if op == "key":
            return driver.hotkey(request["keys"])
        if op == "scroll":
            driver.mouse_move_to(int(request["x"]), int(request["y"]))
            for amount, positive, negative in ((int(request["dx"]), "right", "left"), (int(request["dy"]), "down", "up")):
                if amount:
                    driver.scroll_by_n_clicks(positive if amount > 0 else negative, abs(amount))
            return None
        raise ValueError("unsupported native action")
    finally:
        driver.close()


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
    ap.add_argument("--task")
    ap.add_argument("--mode", default=os.environ.get("RAVEN_H_MODE", "local-runtime"),
                    choices=["local-runtime", "agents-api", "models-loop"])
    ap.add_argument("--model", default=os.environ.get("HAI_MODEL") or None)
    ap.add_argument("--max-steps", type=int, default=60)
    ap.add_argument("--max-time", type=float, default=600)
    ap.add_argument("--shots-dir", default=os.environ.get("RAVEN_SHOTS_DIR", "/tmp/raven-shots"))
    ap.add_argument("--no-cancel-on-eof", dest="cancel_on_eof", action="store_false",
                    help="keep running when stdin closes (manual runs with </dev/null)")
    ap.add_argument("--preflight", action="store_true", help="check guest capabilities without any API calls")
    ap.add_argument("--expected-platform", choices=["linux", "windows", "macos", "ios"])
    ap.add_argument("--driver-json", action="store_true", help="one native-driver command on stdin; no agent decisions")
    ap.add_argument("--env-stdin", action="store_true", help="read the H environment envelope before steering lines")
    args = ap.parse_args(argv)

    if args.preflight:
        # Desktop imports sometimes print diagnostics; keep the protocol clean and
        # return only a safe category, not arbitrary driver exceptions.
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            report = preflight(args.mode, args.expected_platform)
        print(json.dumps(report), flush=True)
        return 0 if report["ok"] else 78
    if args.driver_json:
        request = json.load(sys.stdin)
        with contextlib.redirect_stdout(io.StringIO()):
            result = native_action(request)
        print(json.dumps({"result": result}), flush=True)
        return 0
    if not args.task or args.mode == "models-loop":
        ap.error("a task and an official H harness mode are required")
    if args.env_stdin:
        envelope = json.loads(sys.stdin.readline(65537))
        if not isinstance(envelope, dict) or set(envelope) - set(ENV_KEYS) or not all(isinstance(v, str) for v in envelope.values()):
            raise ValueError("invalid H environment envelope")
        os.environ.update(envelope)
    args.model = args.model or os.environ.get("HAI_MODEL") or None
    # Never read holo's stored login or another secret store. Raven supplies env.
    if not os.environ.get("HAI_API_KEY"):
        raise RuntimeError("HAI_API_KEY is not configured")
    os.environ["HAI_AUTO_BRIDGE"] = "1"
    if base := os.environ.get("HAI_BASE_URL"):
        os.environ["HAI_API_BASE_URL"] = base

    from hai_agents import Client

    shots_dir = Path(args.shots_dir)
    if args.shots_dir.startswith("/tmp/"):
        shots_dir = Path(tempfile.gettempdir()) / args.shots_dir.removeprefix("/tmp/")
    shots = Shots(shots_dir)
    options: dict[str, Any] = {"api_key": os.environ["HAI_API_KEY"]}
    if base := os.environ.get("HAI_API_BASE_URL"):
        options["base_url"] = base
    cloud = Client(**options)
    spec = bound_desktop_spec(cloud.agents.get_agent(AGENT, resolve=True).model_dump(mode="json", exclude_none=True))
    params: dict[str, Any] = {"agent": spec, "messages": args.task,
                              "max_steps": args.max_steps, "max_time_s": args.max_time}
    if args.model:
        params["overrides"] = {"agent.model": args.model}
    if args.mode == "local-runtime":
        client = Client.local()
    else:
        client = cloud

    handle = client.start_session(**params)
    if "desktop" not in client.sessions.bridged_kinds(handle.id):
        handle.cancel()
        client.close()
        raise RuntimeError("H session did not bind its desktop bridge to this test machine")
    out("started", session_id=handle.id, mode=args.mode, model=args.model, agent=AGENT)

    def steer() -> None:
        for line in sys.stdin:
            if line.strip():
                try:
                    handle.send_message(line.strip())
                    out("steered", text=line.strip())
                except Exception as e:  # noqa: BLE001
                    out("error", error=f"steer failed ({type(e).__name__})")
        if args.cancel_on_eof:
            # Raven closed stdin (stop, or the authenticated transport died): free the H slot.
            try:
                handle.cancel()
            finally:
                client.close()
                out("result", status="interrupted", answer=None, outcome=None, error="stopped by raven-core")
                os._exit(130)

    threading.Thread(target=steer, daemon=True).start()
    signal.signal(signal.SIGTERM, signal.default_int_handler)
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
        client.close()
        if client is not cloud:
            cloud.close()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as error:
        # SDK/transport exceptions can contain headers/URLs. Do not log their body.
        out("result", status="error", answer=None, outcome=None,
            error=f"H host execution failed ({type(error).__name__}); no target fallback was attempted")
        sys.exit(1)
