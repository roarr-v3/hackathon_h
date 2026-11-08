"""Idempotent sandbox provisioning for Raven (used by setup_sandboxes.sh).

  uv run python scripts/sandbox_setup.py linux        # create raven-linux + install bitchat-tui (+ bitchatx fallback)
  uv run python scripts/sandbox_setup.py macos        # create raven-mac (Lume VM; first pull ~23 GB)
  uv run python scripts/sandbox_setup.py windows      # check WINDOWS_WORKER_HOST reachability
  uv run python scripts/sandbox_setup.py shot linux   # save a screenshot to artifacts/spike-<platform>.png
  uv run python scripts/sandbox_setup.py smoke linux  # real Holo run: "open a terminal and type echo hello"
"""

from __future__ import annotations

import asyncio
import os
import sys
import time

from raven_core.engine.holo import load_env
from raven_core.env import artifacts_dir
from raven_core.events import Agent, Event, Platform
from raven_core.workers.cua import _patch_cua_embedded

_patch_cua_embedded()

LINUX = os.environ.get("RAVEN_LINUX_SANDBOX", "raven-linux")
MAC = os.environ.get("RAVEN_MACOS_SANDBOX", "raven-mac")

# Everything below runs *inside* the Linux sandbox (Ubuntu 24.04 + XFCE, cua-spacesd).
LINUX_INSTALL = r"""
set -e
SUDO=""; [ "$(id -u)" -eq 0 ] || SUDO="sudo -n"
export DEBIAN_FRONTEND=noninteractive
if ! command -v cc >/dev/null || ! dpkg -s libdbus-1-dev >/dev/null 2>&1; then
  $SUDO apt-get update -y
  $SUDO apt-get install -y --no-install-recommends build-essential pkg-config libdbus-1-dev libssl-dev \
      curl git ca-certificates xdotool xfce4-terminal
fi
if [ ! -x "$HOME/.cargo/bin/cargo" ]; then
  curl -fsSL https://sh.rustup.rs | sh -s -- -y --profile minimal
fi
. "$HOME/.cargo/env"
if ! command -v bitchat-tui >/dev/null; then
  cargo install --locked --git https://github.com/E8EgsqUO/bitchat-tui bitchat-tui \
    || cargo install --git https://github.com/E8EgsqUO/bitchat-tui \
    || cargo install bitchat-tui
  $SUDO ln -sf "$HOME/.cargo/bin/bitchat-tui" /usr/local/bin/bitchat-tui
fi
if ! command -v bitchatx >/dev/null; then
  (cargo install --locked --git https://github.com/derekross/bitchatx \
     && $SUDO ln -sf "$HOME/.cargo/bin/bitchatx" /usr/local/bin/bitchatx) || echo "bitchatx fallback failed (non-fatal)"
fi
echo RAVEN_INSTALL_OK
"""


async def _run_long(sb, script: str, label: str, timeout_s: int = 3600) -> bool:
    """Run a long script in the background inside the guest and poll a sentinel (shell.run caps timeouts)."""
    log = f"/tmp/raven-{label}.log"
    done = f"/tmp/raven-{label}.done"
    await sb.shell.run(f"cat > /tmp/raven-{label}.sh <<'RAVEN_EOF'\n{script}\nRAVEN_EOF")
    await sb.shell.run(f"rm -f {done}; (bash /tmp/raven-{label}.sh > {log} 2>&1; echo $? > {done}) &", background=True)
    t0 = time.time()
    while time.time() - t0 < timeout_s:
        r = await sb.shell.run(f"cat {done} 2>/dev/null; tail -n 2 {log} 2>/dev/null")
        lines = r.stdout.strip().splitlines()
        if lines and lines[0].strip().isdigit():
            ok = lines[0].strip() == "0"
            print(f"[{label}] finished rc={lines[0].strip()}")
            print((await sb.shell.run(f"tail -n 30 {log}")).stdout)
            return ok
        print(f"[{label}] {int(time.time() - t0)}s … {lines[-1] if lines else ''}"[:160], flush=True)
        await asyncio.sleep(15)
    print(f"[{label}] timed out")
    return False


async def _connect_or_create(name: str, image, **kw):
    from cua_sandbox import Sandbox

    try:
        sb = await asyncio.wait_for(_aw(Sandbox.connect(name)), 60)
        await sb.shell.run("true")
        print(f"[{name}] already exists")
        return sb
    except Exception as e:  # noqa: BLE001
        print(f"[{name}] not reachable ({type(e).__name__}); creating…")
    return await Sandbox.create(image, name=name, **kw)


async def _aw(x):
    return await x


async def linux() -> None:
    from cua_sandbox import Image

    sb = await _connect_or_create(LINUX, Image.linux())
    r = await sb.shell.run("command -v bitchat-tui && command -v bitchatx; echo; uname -a")
    print(r.stdout)
    if "bitchat-tui" not in r.stdout:
        ok = await _run_long(sb, LINUX_INSTALL, "install")
        if not ok:
            sys.exit("linux install failed; see log above")
    await sb.disconnect()


async def macos() -> None:
    from cua_sandbox import Image

    sb = await _connect_or_create(MAC, Image.macos(), runtime="lume")
    print((await sb.shell.run("sw_vers; ls /Applications | head -50")).stdout)
    await sb.disconnect()


async def windows() -> None:
    from raven_core.workers import get_worker

    w = get_worker(Platform.windows)
    rc, out, err = await w.shell("ver")
    print(rc, out, err)
    print("screen", await w.size())


async def shot(platform: str) -> None:
    from raven_core.workers import get_worker

    w = get_worker(Platform(platform))
    png = await w.screenshot()
    artifacts = artifacts_dir()
    artifacts.mkdir(exist_ok=True)
    p = artifacts / f"spike-{platform}.png"
    p.write_bytes(png)
    print("wrote", p, len(png), "bytes")


async def smoke(platform: str) -> None:
    from raven_core.engine import run_agent
    from raven_core.workers import get_worker

    agent = Agent(platform=Platform(platform), worker=platform, title="smoke", task="Open a terminal and type `echo hello`, press Enter, and verify 'hello' is printed.")

    async def emit(e: Event) -> None:
        print(e.type, {k: v for k, v in e.data.items()})

    await run_agent(
        agent,
        get_worker(Platform(platform)),
        emit,
        asyncio.Queue(),
        asyncio.Event(),
        artifacts_dir(),
        timeout_s=300,
        max_steps=20,
    )
    print("RESULT", agent.status, agent.verdict_reason)


def main() -> None:
    load_env()
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    cmd, *rest = sys.argv[1:]
    fn = {"linux": linux, "macos": macos, "windows": windows, "shot": shot, "smoke": smoke}[cmd]
    asyncio.run(fn(*rest))


if __name__ == "__main__":
    main()
