"""Agent engine: `run_agent(agent, worker, emit, inbox, stop, artifacts_dir)`.

`worker` is either a `hosts.Host` (H's desktop agent runs inside the machine: the default)
or a `workers.Worker` (fallback: our own Holo4 Models-API loop drives it through Cua).
Extra keyword-only options are forwarded (timeout_s, model, ...).
"""

from __future__ import annotations

from typing import Any

from raven_core.engine.holo import build_tools, load_env, make_client
from raven_core.engine.holo import run_agent as run_models_loop


async def run_agent(agent, worker, emit, inbox, stop, artifacts_dir, **kw: Any) -> None:
    from raven_core.hosts import Host

    if isinstance(worker, Host):
        from raven_core.engine.h_agent import run_h_agent

        allowed = {k: v for k, v in kw.items() if k in ("timeout_s", "model", "mode", "max_steps")}
        return await run_h_agent(agent, worker, emit, inbox, stop, artifacts_dir, **allowed)
    return await run_models_loop(agent, worker, emit, inbox, stop, artifacts_dir, **kw)


__all__ = ["run_agent", "run_models_loop", "make_client", "load_env", "build_tools"]
