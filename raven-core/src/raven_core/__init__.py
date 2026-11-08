"""Raven core: cross-platform testing with computer-use agents.

CLI:
    raven-core serve [--port 8787] [--host 0.0.0.0]   HTTP/WS API (AGENTS.md)
    raven-core acp                                    ACP stdio agent (for t3code)
    raven-core run <spec> [--platforms linux,macos]   run a spec in-process, print events
    raven-core agent <platform> "<task>"              one agent on the running server, followed live
"""

from __future__ import annotations


def _setup_logging() -> None:
    import logging
    import os
    import sys

    logging.basicConfig(
        level=(os.environ.get("RAVEN_LOG_LEVEL") or "INFO").upper(),
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    for noisy in ("httpx", "httpcore", "websockets"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def _fmt_event(ev) -> str:  # type: ignore[no-untyped-def]
    import json

    d = ev.data or {}
    who = (ev.agent_id or ev.run_id or "")[-6:]
    if ev.type == "agent.message":
        body = f"{d.get('role')}: {d.get('text')}"
    elif ev.type == "agent.action":
        body = f"{d.get('action')} {json.dumps(d.get('args') or {})}" + (
            f"  # {d.get('thought')}" if d.get("thought") else ""
        )
    elif ev.type == "agent.screenshot":
        body = str(d.get("url"))
    elif ev.type == "agent.verdict":
        body = f"{'PASSED' if d.get('passed') else 'FAILED'} — {d.get('reason')}"
    elif ev.type in ("agent.status", "run.status"):
        body = str(d.get("status"))
    elif ev.type == "agent.created":
        a = d.get("agent") or {}
        body = f"[{a.get('platform')}] {a.get('title')} on {a.get('worker')}"
    elif ev.type == "run.created":
        r = d.get("run") or {}
        body = f"{r.get('spec')} on {','.join(r.get('platforms') or [])}"
    elif ev.type == "report.ready":
        body = "\n" + str(d.get("markdown")) + (
            f"\nDevin: {d['devin_session_url']}" if d.get("devin_session_url") else ""
        )
    else:
        body = json.dumps(d)[:300]
    return f"{ev.type:<17} {who:>6}  {body}"


async def _run_cli(spec: str, platforms: str | None, as_json: bool) -> int:
    import asyncio

    from raven_core.events import CreateRunRequest, Platform
    from raven_core.server import Raven

    raven = Raven()
    raven.store.bind_loop(asyncio.get_running_loop())
    raven.settings.artifacts_dir.mkdir(parents=True, exist_ok=True)
    q = raven.store.bus.subscribe()
    plats = [Platform(p.strip()) for p in platforms.split(",") if p.strip()] if platforms else None
    run = raven.create_run(CreateRunRequest(spec=spec, platforms=plats))
    final: str | None = None
    try:
        while True:
            timeout = 120.0 if final == "failed" else None
            try:
                ev = await asyncio.wait_for(q.get(), timeout=timeout)
            except (TimeoutError, asyncio.TimeoutError):
                break
            print(ev.model_dump_json() if as_json else _fmt_event(ev), flush=True)
            if ev.type == "run.status" and ev.run_id == run.id and ev.data.get("status") in ("passed", "failed", "stopped"):
                final = ev.data["status"]
                if final != "failed" or not raven.settings.reports:
                    break
            if ev.type == "report.ready" and ev.run_id == run.id and ev.agent_id is None and final == "failed":
                break
    except (KeyboardInterrupt, asyncio.CancelledError):
        raven.stop_all()
    finally:
        await raven.shutdown()
    return 0 if final == "passed" else 1


async def _agent_cli(platform: str, task: str, server: str) -> int:
    """Start one agent on the running server and stream its events until it finishes."""
    import asyncio
    import json

    import httpx
    import websockets

    from raven_core.events import Event

    async with httpx.AsyncClient(base_url=server, timeout=30) as http:
        agent = (await http.post("/agents", json={"platform": platform, "task": task})).raise_for_status().json()
        print(f"agent {agent['id']} on {agent['worker']}  (screenshots: artifacts/{agent['id']}/)", flush=True)
        ws_url = server.replace("http", "ws", 1).rstrip("/") + f"/ws/events?agent_id={agent['id']}"
        status = "unknown"
        try:
            async with websockets.connect(ws_url, max_size=None) as ws:
                async for raw in ws:
                    ev = Event.model_validate(json.loads(raw))
                    if ev.agent_id != agent["id"]:
                        continue
                    print(_fmt_event(ev), flush=True)
                    if ev.type == "agent.status" and ev.data.get("status") in ("passed", "failed", "stopped", "error"):
                        status = ev.data["status"]
                        break
        except (KeyboardInterrupt, asyncio.CancelledError):
            await http.post(f"/agents/{agent['id']}/stop")
            status = "stopped"
    return 0 if status == "passed" else 1


def main(argv: list[str] | None = None) -> None:
    import argparse
    import asyncio
    import os
    import sys

    from raven_core.server.config import load_env

    load_env()
    p = argparse.ArgumentParser(prog="raven-core", description="Raven cross-platform test orchestrator")
    sub = p.add_subparsers(dest="cmd")
    s = sub.add_parser("serve", help="run the HTTP/WS API")
    s.add_argument("--port", type=int, default=None)
    s.add_argument("--host", default=os.environ.get("RAVEN_HOST") or "0.0.0.0")
    sub.add_parser("acp", help="ACP stdio agent (t3code 'ACP → Local command')")
    r = sub.add_parser("run", help="run a spec in-process and print events")
    r.add_argument("spec")
    r.add_argument("--platforms", default=None, help="comma separated, e.g. linux,macos")
    r.add_argument("--json", action="store_true", help="print raw Event JSON lines")
    a = sub.add_parser("agent", help="start one agent on the running server and follow it")
    a.add_argument("platform", choices=["linux", "macos", "windows", "ios"])
    a.add_argument("task")
    a.add_argument("--server", default=os.environ.get("RAVEN_URL") or f"http://localhost:{os.environ.get('RAVEN_PORT') or 8787}")
    args = p.parse_args(argv)
    _setup_logging()

    if args.cmd in (None, "serve"):
        import uvicorn

        from raven_core.server import create_app

        port = getattr(args, "port", None) or int(os.environ.get("RAVEN_PORT") or 8787)
        host = getattr(args, "host", None) or "0.0.0.0"
        os.environ["RAVEN_PORT"] = str(port)
        app = create_app()
        app.state.serve_background = True
        uvicorn.run(app, host=host, port=port, log_level="info")
    elif args.cmd == "acp":
        from raven_core.acp import serve_stdio

        try:
            asyncio.run(serve_stdio())
        except KeyboardInterrupt:
            pass
    elif args.cmd == "run":
        sys.exit(asyncio.run(_run_cli(args.spec, args.platforms, args.json)))
    elif args.cmd == "agent":
        try:
            sys.exit(asyncio.run(_agent_cli(args.platform, args.task, args.server)))
        except KeyboardInterrupt:
            sys.exit(130)
