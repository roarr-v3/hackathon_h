"""Explicit Raven target preparation. The default command is read-only.

    uv run python scripts/sandbox_setup.py inspect
    uv run python scripts/sandbox_setup.py linux              # existing Apple Space
    uv run python scripts/sandbox_setup.py linux --create     # opt-in Apple creation
    uv run python scripts/sandbox_setup.py macos              # native, never a VM
    uv run python scripts/sandbox_setup.py macos --vm --create # explicit legacy Lume
    uv run python scripts/sandbox_setup.py windows            # bounded Devin VM
    uv run python scripts/sandbox_setup.py shot linux
    uv run python scripts/sandbox_setup.py smoke linux        # real H session

This script does not install a container engine, delete a machine, silently create
after connection/auth failure, or install/replace the application under test.
"""

from __future__ import annotations

import argparse
import asyncio
import json

from raven_core.env import artifacts_dir, load_env
from raven_core.events import Agent, Event, ExecutionProfile, HMode, Placement, Platform
from raven_core.workers import get_worker, inspect_targets, prepare_worker


async def run(args: argparse.Namespace) -> None:
    profile = ExecutionProfile(
        h_mode=HMode(args.mode), linux_space=args.space, linux_fallback=not args.no_fallback,
    )
    if args.command == "inspect":
        print(json.dumps(await inspect_targets(profile), indent=2))
        return
    platform = Platform(args.platform if args.command in ("shot", "smoke") else args.command)
    if args.vm and platform != Platform.macos:
        raise ValueError("--vm is only the explicit legacy macOS Lume route")
    if args.create and platform != Platform.linux and not (platform == Platform.macos and args.vm):
        raise ValueError("--create is only for a local Apple Space or explicit macOS Lume VM")

    async def say(text: str) -> None:
        print(text, flush=True)

    if args.vm:
        from raven_core.hosts.cua import CuaHost

        worker = CuaHost(platform, args.mac_space, mode=profile.mode_for(platform))
    else:
        worker = get_worker(platform, placement=Placement.auto, execution=profile)
    if args.create:
        from raven_core.machines import ensure_space

        await ensure_space(platform, worker.name, say, create=True)
    worker = await prepare_worker(worker, execution=profile, say=say)
    try:
        if args.command == "shot":
            png = await worker.screenshot()
            folder = artifacts_dir()
            folder.mkdir(parents=True, exist_ok=True)
            path = folder / f"spike-{platform.value}.png"
            path.write_bytes(png)
            print("wrote", path, len(png), "bytes")
        elif args.command == "smoke":
            from raven_core.engine import run_agent
            from raven_core.hosts import Host

            agent = Agent(
                platform=platform, worker=worker.name, execution=profile, title="smoke",
                task="Inspect the desktop, open a terminal, type `echo hello`, press Enter and verify hello is visible.",
            )

            async def emit(event: Event) -> None:
                print(event.type, event.data)

            options = {"mode": profile.mode_for(platform).value} if isinstance(worker, Host) else {}
            await run_agent(
                agent, worker, emit, asyncio.Queue(), asyncio.Event(), artifacts_dir(),
                timeout_s=300, max_steps=20, **options,
            )
            print("RESULT", agent.status, agent.verdict_reason)
        else:
            print(f"Prepared {platform.value}: {worker.name}")
    finally:
        # Disconnect only; Devin archives owned sessions to avoid leaving a paid VM
        # running. Explicitly attached sessions and local Spaces are never deleted.
        await worker.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", nargs="?", default="inspect", choices=["inspect", "linux", "macos", "windows", "ios", "shot", "smoke"])
    parser.add_argument("platform", nargs="?", choices=[p.value for p in Platform])
    parser.add_argument("--mode", choices=[m.value for m in HMode], default="auto")
    parser.add_argument("--space", default="raven-linux-apple")
    parser.add_argument("--mac-space", default="raven-mac")
    parser.add_argument("--no-fallback", action="store_true")
    parser.add_argument("--create", action="store_true", help="explicit creation, never used by inspection or failed connection recovery")
    parser.add_argument("--vm", action="store_true")
    args = parser.parse_args()
    if args.command in ("shot", "smoke") and not args.platform:
        parser.error("shot/smoke needs a platform")
    load_env()
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
