"""Devin v3 test machines: H local runtime first, Devin computer use as fallback."""

from __future__ import annotations

import asyncio
import os
from typing import Any, Awaitable, Callable, Literal
from urllib.parse import quote

from pydantic import BaseModel, ConfigDict, Field

from raven_core.events import Agent, AgentStatus, Event
from raven_core.hosts import PASS_ENV, Host
from raven_core.integrations import http_client
from raven_core.workers.base import WorkerUnavailable


class CloudResult(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    verdict: Literal["PASSED", "FAILED", "ERROR"]
    driver: Literal["h", "devin"]
    fallback_reason: str | None
    report_markdown: str = Field(pattern=r"^VERDICT: (PASSED|FAILED|ERROR)")
    steps: list[str]


RESULT_SCHEMA = {"$schema": "http://json-schema.org/draft-07/schema#", **CloudResult.model_json_schema()}
INITIAL_POLL_S = 15.0


def selected(platform) -> bool:
    return str(platform) in ("linux", "windows") and os.environ.get(
        f"RAVEN_HOST_{str(platform).upper()}", ""
    ).strip() == "devin-cloud"


def timeout_seconds() -> float:
    return float(os.environ.get("RAVEN_DEVIN_TIMEOUT_S", "1800"))


class DevinCloudHost(Host):
    kind = "devin-cloud"

    def __init__(self, platform):
        super().__init__(platform, f"devin-cloud-{platform}")
        if not os.environ.get("DEVIN_API_KEY") or not os.environ.get("DEVIN_ORG_ID"):
            raise WorkerUnavailable("devin-cloud requires DEVIN_API_KEY and DEVIN_ORG_ID")

    async def run(
        self, agent: Agent, emit: Callable[[Event], Awaitable[None]], inbox: asyncio.Queue[str], stop: asyncio.Event
    ) -> None:
        secrets = {k: os.environ[k] for k in (*PASS_ENV, "DEVIN_API_KEY") if os.environ.get(k)}

        def clean(text: str) -> str:
            for value in sorted(secrets.values(), key=len, reverse=True):
                text = text.replace(value, "[REDACTED]")
            return text

        async def send(kind, data):
            def sanitize(value: Any) -> Any:
                if isinstance(value, str):
                    return clean(value)
                if isinstance(value, dict):
                    return {clean(k): sanitize(v) for k, v in value.items()}
                if isinstance(value, list):
                    return [sanitize(v) for v in value]
                return value

            safe = sanitize(data)
            await emit(Event(type=kind, agent_id=agent.id, run_id=agent.run_id, data=safe))

        async def message(text):
            await send("agent.message", {"role": "system", "text": text})

        async def status(value):
            agent.status = value
            await send("agent.status", {"status": value.value})

        async def finish(value, reason):
            agent.verdict_reason = clean(reason)
            await send("agent.verdict", {"passed": value == AgentStatus.passed, "reason": reason})
            await status(value)

        steering = None
        await status(AgentStatus.running)
        try:
            key, org = os.environ.get("DEVIN_API_KEY"), os.environ.get("DEVIN_ORG_ID")
            if not key or not org:
                raise WorkerUnavailable("devin-cloud requires DEVIN_API_KEY and DEVIN_ORG_ID")
            base = os.environ.get("DEVIN_API_BASE_V3", "https://api.devin.ai/v3").rstrip("/")
            endpoint = f"{base}/organizations/{quote(org, safe='')}/sessions"
            prompt = clean(
                "Run this QA spec on your assigned desktop VM. Try H first, fall back to Devin.\n"
                "Install uv if needed, then install H with: uv tool install 'hai-agents[cli,desktop]>=1.4.3'.\n"
                "Run: holo run --local --agent h/holo-desktop --quiet <complete-spec-text>. "
                "This is local-runtime mode, NOT hosted agents-api. Use holo run --help if needed. "
                "Use the supplied session secrets as environment variables; never print their values or "
                "include them in reports, commands shown to users, or structured output. HAI_MODEL and "
                "HAI_BASE_URL, when supplied, configure H. Configure desktop dependencies as needed.\n"
                "If H cannot install or run (unsupported runtime/platform, authentication error, etc.), "
                "do not wait for interactive login or request credentials. Instead, "
                "execute the SAME spec yourself using your own computer use. Do not fall back merely "
                "because H found an application failure. Set driver=h when H executes the test, otherwise "
                "driver=devin and explain the runtime failure in fallback_reason (no credentials). "
                "For H, fallback_reason must be null. Never claim a pass without verification.\n"
                "Submit final structured output matching the supplied schema. report_markdown must start "
                "with VERDICT: PASSED, VERDICT: FAILED, or VERDICT: ERROR matching verdict, followed by "
                "Setup / Task / Expected Result / Verification sections and visible evidence. "
                "Use ERROR if the test cannot be executed. steps contains short execution summaries.\n\n"
                + agent.task
            )
            payload = {
                "prompt": prompt, "platform": agent.platform.value,
                "session_secrets": [{"key": k, "value": v, "sensitive": True}
                                    for k, v in secrets.items() if k in PASS_ENV],
                "structured_output_schema": RESULT_SCHEMA, "structured_output_required": True,
                "title": f"Raven QA · {agent.platform.value}", "tags": ["raven", "qa"],
                "max_acu_limit": float(os.environ.get("RAVEN_DEVIN_MAX_ACU", "10")),
            }
            async with http_client() as client:
                client.headers["Authorization"] = f"Bearer {key}"

                async def request(method, url, *, expect_json=True, **kwargs):
                    try:
                        response = await client.request(method, url, **kwargs)
                        if response.is_error:
                            raise WorkerUnavailable(f"Devin API HTTP {response.status_code}")
                        if not expect_json:
                            return {}
                        data = response.json()
                        if not isinstance(data, dict):
                            raise ValueError
                        return data
                    except WorkerUnavailable:
                        raise
                    except Exception:
                        raise WorkerUnavailable("Devin API request failed (details withheld)") from None

                async with asyncio.timeout(timeout_seconds()):
                    if stop.is_set():
                        await finish(AgentStatus.stopped, "Stopped before creating cloud session")
                        return
                    session = await request("POST", endpoint, json=payload)
                    session_id = session.get("session_id")
                    if not isinstance(session_id, str) or not session_id:
                        raise WorkerUnavailable("Devin API returned no session ID")
                    url = session.get("url")
                    url = url if isinstance(url, str) and url.startswith("https://app.devin.ai/") else None
                    await message(f"Devin cloud session: {url or session_id}")

                    async def pump_inbox():
                        while True:
                            text = await inbox.get()
                            try:
                                await request("POST", f"{endpoint}/{quote(session_id, safe='')}/messages",
                                              json={"message": clean(text)}, expect_json=False)
                            except WorkerUnavailable as exc:
                                await message(f"Cloud steering failed: {exc}")
                            else:
                                await message("Steering sent to Devin cloud")

                    steering = asyncio.create_task(pump_inbox())
                    delay = INITIAL_POLL_S
                    last_state = None
                    while True:
                        if stop.is_set():
                            await finish(AgentStatus.stopped, "Stopped locally; cloud session may still be running")
                            return
                        data = await request("GET", f"{endpoint}/{quote(session_id, safe='')}")
                        state = (data.get("status"), data.get("status_detail"))
                        if state != last_state:
                            await message(f"Devin cloud status: {state[0]} / {state[1]}")
                            last_state = state
                        output = data.get("structured_output")
                        if output is not None:
                            result = CloudResult.model_validate(output)
                            if not result.report_markdown.startswith(f"VERDICT: {result.verdict}"):
                                raise WorkerUnavailable("Cloud report has no matching VERDICT header")
                            if result.driver == "devin" and not result.fallback_reason:
                                raise WorkerUnavailable("Cloud fallback has no reason")
                            if result.driver == "h" and result.fallback_reason is not None:
                                raise WorkerUnavailable("H cloud result must not have a fallback reason")
                            await message(f"Driver: {result.driver}; fallback: {result.fallback_reason or 'none'}")
                            for step in result.steps:
                                await message(step)
                            await send("report.ready", {"markdown": result.report_markdown, "linkup_sources": [],
                                                        "devin_session_url": url})
                            await finish({"PASSED": AgentStatus.passed, "FAILED": AgentStatus.failed,
                                          "ERROR": AgentStatus.error}[result.verdict], result.report_markdown)
                            return
                        if state[0] in ("exit", "error", "suspended", "finished", "expired", "blocked") or state[1] in (
                            "finished", "waiting_for_user", "waiting_for_approval",
                        ):
                            raise WorkerUnavailable("Cloud session ended or blocked without structured output")
                        try:
                            await asyncio.wait_for(stop.wait(), delay)
                        except TimeoutError:
                            pass
                        delay = min(delay * 1.3, 30)
        except asyncio.CancelledError:
            await finish(AgentStatus.stopped, "Cloud QA cancelled locally; cloud session may still be running")
            raise
        except TimeoutError:
            await finish(AgentStatus.error, "Cloud QA timed out; cloud session may still be running")
        except WorkerUnavailable as exc:
            await finish(AgentStatus.error, str(exc))
        except Exception:
            await finish(AgentStatus.error, "Invalid cloud QA response or configuration (details withheld)")
        finally:
            if steering is not None:
                steering.cancel()
                await asyncio.gather(steering, return_exceptions=True)
