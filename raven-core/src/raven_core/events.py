"""Shared wire contract for Raven. Every client (t3code ACP bridge, iOS app) and
every engine module speaks these models. Change them only with a matching
update to AGENTS.md and all consumers."""

from __future__ import annotations

import time
import uuid
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


class Platform(StrEnum):
    macos = "macos"
    linux = "linux"
    windows = "windows"
    ios = "ios"


class AgentStatus(StrEnum):
    queued = "queued"
    running = "running"
    passed = "passed"
    failed = "failed"
    stopped = "stopped"
    error = "error"


class RunStatus(StrEnum):
    queued = "queued"
    running = "running"
    passed = "passed"
    failed = "failed"
    stopped = "stopped"


class Agent(BaseModel):
    id: str = Field(default_factory=lambda: new_id("agt"))
    run_id: str | None = None
    platform: Platform
    worker: str
    title: str
    task: str
    status: AgentStatus = AgentStatus.queued
    created_at: float = Field(default_factory=time.time)
    last_screenshot: str | None = None
    verdict_reason: str | None = None


class Run(BaseModel):
    id: str = Field(default_factory=lambda: new_id("run"))
    spec: str
    platforms: list[Platform]
    status: RunStatus = RunStatus.queued
    agent_ids: list[str] = []
    created_at: float = Field(default_factory=time.time)


EventType = Literal[
    "run.created",
    "run.status",
    "agent.created",
    "agent.status",
    "agent.message",
    "agent.action",
    "agent.screenshot",
    "agent.verdict",
    "report.ready",
]


class Event(BaseModel):
    """One message on GET /ws/events. `data` shapes per type:

    run.created       {"run": Run}
    run.status        {"status": RunStatus}
    agent.created     {"agent": Agent}
    agent.status      {"status": AgentStatus}
    agent.message     {"role": "agent"|"user"|"system", "text": str}
    agent.action      {"action": str, "args": dict, "thought": str|None}
    agent.screenshot  {"url": "/artifacts/<path>.png", "width": int, "height": int}
    agent.verdict     {"passed": bool, "reason": str}
    report.ready      {"markdown": str, "linkup_sources": list, "devin_session_url": str|None}
    """

    id: str = Field(default_factory=lambda: new_id("evt"))
    type: EventType
    ts: float = Field(default_factory=time.time)
    run_id: str | None = None
    agent_id: str | None = None
    data: dict[str, Any] = {}


class CreateAgentRequest(BaseModel):
    platform: Platform
    task: str
    title: str | None = None


class SendMessageRequest(BaseModel):
    text: str


class CreateRunRequest(BaseModel):
    spec: str
    platforms: list[Platform] | None = None


class VoiceSession(BaseModel):
    id: str
    url: str
    sample_rate: int = 16000
    encoding: str = "wav/pcm"
    bit_depth: int = 16
    channels: int = 1
