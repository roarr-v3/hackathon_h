"""In-memory registry of agents/runs, per-agent event logs and the WS event bus.

Everything that changes agent/run state goes through `Store.emit(event)`, so the
event stream and the REST view can never disagree. `emit` is safe to call from
the event loop thread or from worker threads (it hops onto the loop)."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from raven_core.events import (
    Agent,
    AgentStatus,
    Event,
    Run,
    RunStatus,
)

log = logging.getLogger("raven.store")

REPLAY_SIZE = 200
TERMINAL_AGENT = {
    AgentStatus.passed,
    AgentStatus.failed,
    AgentStatus.stopped,
    AgentStatus.error,
}
TERMINAL_RUN = {RunStatus.passed, RunStatus.failed, RunStatus.stopped}


class EventBus:
    """Fan-out of events to subscribers + ring buffer for replay on connect."""

    def __init__(self, replay: int = REPLAY_SIZE) -> None:
        self._ring: deque[Event] = deque(maxlen=replay)
        self._subs: set[asyncio.Queue[Event]] = set()

    def publish(self, event: Event) -> None:
        self._ring.append(event)
        for q in list(self._subs):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:  # slow consumer: drop rather than block the engine
                pass

    def subscribe(self, maxsize: int = 2000) -> asyncio.Queue[Event]:
        q: asyncio.Queue[Event] = asyncio.Queue(maxsize=maxsize)
        self._subs.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[Event]) -> None:
        self._subs.discard(q)

    def recent(self) -> list[Event]:
        return list(self._ring)


class StopSignal(asyncio.Event):
    """An asyncio.Event that remembers why the agent was told to stop, so its verdict can say
    "QA attempt timed out ..." instead of "Stopped by user" when the runner pulled the plug."""

    def __init__(self) -> None:
        super().__init__()
        self.reason: str | None = None
        self.cause: str = "user"

    def set(self, reason: str | None = None, *, cause: str = "user") -> None:  # type: ignore[override]
        if not self.is_set():
            self.reason = reason
            self.cause = cause
        super().set()


@dataclass
class AgentHandle:
    """Runtime control for a running agent (kept so /messages and /stop work)."""

    inbox: asyncio.Queue[str]
    stop: StopSignal
    task: asyncio.Task[Any] | None = None
    first_event: int = 0


@dataclass
class Store:
    persist_path: Path | None = None
    agents: dict[str, Agent] = field(default_factory=dict)
    runs: dict[str, Run] = field(default_factory=dict)
    agent_events: dict[str, list[Event]] = field(default_factory=dict)
    run_events: dict[str, list[Event]] = field(default_factory=dict)
    reports: dict[str, dict[str, Any]] = field(default_factory=dict)
    handles: dict[str, AgentHandle] = field(default_factory=dict)
    bus: EventBus = field(default_factory=EventBus)
    loop: asyncio.AbstractEventLoop | None = None

    def __post_init__(self) -> None:
        self._persist_lock = threading.Lock()

    # ------------------------------------------------------------------ registry
    def add_agent(self, agent: Agent) -> Agent:
        self.agents[agent.id] = agent
        self.agent_events.setdefault(agent.id, [])
        if agent.run_id and agent.run_id in self.runs:
            run = self.runs[agent.run_id]
            if agent.id not in run.agent_ids:
                run.agent_ids.append(agent.id)
        return agent

    def add_run(self, run: Run) -> Run:
        self.runs[run.id] = run
        self.run_events.setdefault(run.id, [])
        return run

    def agents_for_run(self, run_id: str) -> list[Agent]:
        return [a for a in self.agents.values() if a.run_id == run_id]

    # ------------------------------------------------------------------ events
    def bind_loop(self, loop: asyncio.AbstractEventLoop | None = None) -> None:
        self.loop = loop or asyncio.get_running_loop()

    def emit(self, event: Event) -> Event:
        """Apply + record + broadcast. Thread-safe."""
        loop = self.loop
        if loop is not None and not loop.is_closed():
            try:
                running = asyncio.get_running_loop()
            except RuntimeError:
                running = None
            if running is not loop:
                loop.call_soon_threadsafe(self._emit_now, event)
                return event
        self._emit_now(event)
        return event

    def _emit_now(self, event: Event) -> None:
        if event.agent_id and not event.run_id:
            agent = self.agents.get(event.agent_id)
            if agent is not None:
                event.run_id = agent.run_id
        self._apply(event)
        if event.agent_id:
            self.agent_events.setdefault(event.agent_id, []).append(event)
        if event.run_id:
            self.run_events.setdefault(event.run_id, []).append(event)
        self.bus.publish(event)
        self._persist(event)

    def _apply(self, ev: Event) -> None:
        d = ev.data or {}
        try:
            if ev.type == "agent.created" and "agent" in d:
                raw = d["agent"]
                if ev.agent_id not in self.agents:
                    agent = raw if isinstance(raw, Agent) else Agent.model_validate(raw)
                    self.add_agent(agent)
                if isinstance(raw, Agent):
                    d["agent"] = raw.model_dump(mode="json")
            elif ev.type == "run.created" and "run" in d:
                raw = d["run"]
                run_id = raw.id if isinstance(raw, Run) else raw.get("id")
                if run_id not in self.runs:
                    run = raw if isinstance(raw, Run) else Run.model_validate(raw)
                    self.add_run(run)
                if isinstance(raw, Run):
                    d["run"] = raw.model_dump(mode="json")
            elif ev.type == "agent.status":
                agent = self.agents.get(ev.agent_id or "")
                if agent is not None:
                    agent.status = AgentStatus(d.get("status"))
            elif ev.type == "agent.message" and d.get("phase") == "planning" and d.get("state") == "ready":
                agent = self.agents.get(ev.agent_id or "")
                if agent is not None:
                    agent.task = d.get("task") or agent.task
                    agent.planning_session_id = d.get("planning_session_id")
            elif ev.type == "agent.message" and d.get("phase") == "target" and d.get("state") == "ready":
                agent = self.agents.get(ev.agent_id or "")
                if agent is not None:
                    agent.worker = d.get("worker") or agent.worker
                    agent.placement = d.get("placement")
            elif ev.type == "agent.screenshot":
                agent = self.agents.get(ev.agent_id or "")
                if agent is not None and d.get("url"):
                    agent.last_screenshot = d["url"]
            elif ev.type == "agent.verdict":
                agent = self.agents.get(ev.agent_id or "")
                if agent is not None:
                    agent.verdict_reason = d.get("reason")
                    if agent.status not in (AgentStatus.stopped, AgentStatus.error):
                        agent.status = (
                            AgentStatus.passed if d.get("passed") else AgentStatus.failed
                        )
            elif ev.type == "run.status":
                run = self.runs.get(ev.run_id or "")
                if run is not None:
                    run.status = RunStatus(d.get("status"))
            elif ev.type == "report.ready" and ev.run_id:
                self.reports[ev.run_id] = d
        except Exception:  # never let a malformed event kill the engine
            log.exception("failed to apply event %s", ev.type)

    def load(self) -> None:
        if self.persist_path is None or not self.persist_path.is_file():
            return
        with self.persist_path.open(encoding="utf-8") as f:
            for line in f:
                try:
                    event = Event.model_validate_json(line)
                except ValueError:
                    continue
                self._apply(event)
                if event.agent_id:
                    self.agent_events.setdefault(event.agent_id, []).append(event)
                if event.run_id:
                    self.run_events.setdefault(event.run_id, []).append(event)
        for agent in list(self.agents.values()):
            if agent.status not in TERMINAL_AGENT:
                self._emit_now(Event(type="agent.message", agent_id=agent.id, data={
                    "role": "system", "text": "Raven restarted before this agent finished.",
                }))
                self._emit_now(Event(type="agent.status", agent_id=agent.id, data={"status": AgentStatus.stopped.value}))
        for run in list(self.runs.values()):
            if run.status not in TERMINAL_RUN:
                self._emit_now(Event(type="run.status", run_id=run.id, data={"status": RunStatus.stopped.value}))

    def _persist(self, event: Event) -> None:
        if self.persist_path is None:
            return
        try:
            line = event.model_dump_json()
            with self._persist_lock:
                self.persist_path.parent.mkdir(parents=True, exist_ok=True)
                with self.persist_path.open("a", encoding="utf-8") as f:
                    f.write(line + "\n")
        except Exception:
            log.debug("event persistence failed", exc_info=True)


def coerce_event(
    event_or_type: Any,
    data: dict[str, Any] | None = None,
    *,
    agent_id: str | None = None,
    run_id: str | None = None,
    **kwargs: Any,
) -> Event:
    """Accept whatever shape the engine hands to `emit` and return an Event."""
    if isinstance(event_or_type, Event):
        ev = event_or_type
    elif isinstance(event_or_type, dict):
        ev = Event.model_validate(event_or_type)
    else:
        payload = dict(data or {})
        payload.update(kwargs)
        ev = Event(type=event_or_type, data=payload)
    if ev.agent_id is None and agent_id is not None:
        ev.agent_id = agent_id
    if ev.run_id is None and run_id is not None:
        ev.run_id = run_id
    # make data JSON-friendly (enums / models)
    ev.data = json.loads(json.dumps(ev.data, default=_json_default))
    return ev


def _json_default(o: Any) -> Any:
    if hasattr(o, "model_dump"):
        return o.model_dump(mode="json")
    if hasattr(o, "value"):
        return o.value
    if isinstance(o, Path):
        return str(o)
    if isinstance(o, (set, tuple)):
        return list(o)
    return str(o)


def default_persist_path(artifacts_dir: Path) -> Path | None:
    if os.environ.get("RAVEN_PERSIST", "1") in ("0", "false", "no"):
        return None
    return artifacts_dir / "events.jsonl"
