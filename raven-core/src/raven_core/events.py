"""Shared wire contract for Raven. Every client (t3code ACP bridge, iOS app) and
every engine module speaks these models. Change them only with a matching
update to AGENTS.md and all consumers."""

from __future__ import annotations

import time
import uuid
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


class Platform(StrEnum):
    macos = "macos"
    linux = "linux"
    windows = "windows"
    ios = "ios"


class Placement(StrEnum):
    auto = "auto"
    bare_metal = "bare-metal"
    vm = "vm"


class HMode(StrEnum):
    auto = "auto"
    local_runtime = "local-runtime"
    agents_api = "agents-api"
    models_loop = "models-loop"


class ExecutionProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    planner: Literal["local", "cloud"] = "local"
    model: str = Field(default="holo4-27b", pattern=r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,127}$")
    h_mode: HMode = HMode.auto
    platform_modes: dict[Platform, HMode] = Field(default_factory=dict)
    linux_target: Literal["apple-container", "devin-vm"] = "apple-container"
    linux_space: str = Field(default="raven-linux-apple", pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
    linux_fallback: bool = True
    windows_target: Literal["devin-vm"] = "devin-vm"
    ios_target: Literal["simulator", "device"] = "device"
    ios_device_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9:-]{1,128}$")

    def mode_for(self, platform: Platform) -> HMode:
        mode = self.platform_modes.get(platform, self.h_mode)
        if mode != HMode.auto:
            return mode
        return HMode.agents_api if platform in (Platform.linux, Platform.windows) else HMode.local_runtime


class AgentStatus(StrEnum):
    queued = "queued"
    running = "running"
    passed = "passed"
    failed = "failed"
    stopped = "stopped"
    error = "error"


class StopCause(StrEnum):
    user = "user"
    deadline = "deadline"
    shutdown = "shutdown"
    cancelled = "cancelled"


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
    placement: Placement | None = None
    execution: ExecutionProfile | None = None
    original_task: str | None = None
    planning_session_id: str | None = None
    repo_path: str | None = None


class Run(BaseModel):
    id: str = Field(default_factory=lambda: new_id("run"))
    spec: str
    platforms: list[Platform]
    status: RunStatus = RunStatus.queued
    agent_ids: list[str] = []
    created_at: float = Field(default_factory=time.time)
    execution: ExecutionProfile | None = None


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
    placement: Placement = Placement.auto
    # Whole-agent budget in seconds (a spec passes its timeout_s). Default: RAVEN_AGENT_TIMEOUT_S.
    timeout_s: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    execution: ExecutionProfile | None = None
    repo_path: str | None = None
    previous_agent_id: str | None = None
    repair: bool = False
    model: str | None = Field(default=None, pattern=r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,127}$")


class SendMessageRequest(BaseModel):
    text: str


class ApprovalDecision(BaseModel):
    option_id: str | None = None


class CreateRunRequest(BaseModel):
    spec: str
    platforms: list[Platform] | None = None
    placement: Placement = Placement.auto
    execution: ExecutionProfile | None = None
    model: str | None = Field(default=None, pattern=r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,127}$")


class VoiceSession(BaseModel):
    id: str
    url: str
    sample_rate: int = 16000
    encoding: str = "wav/pcm"
    bit_depth: int = 16
    channels: int = 1
