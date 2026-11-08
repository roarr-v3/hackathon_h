"""Failure report: failed agents + verdicts + screenshots + Linkup research (+ Devin)."""

from __future__ import annotations

import asyncio
import json
import os
import re
from typing import Any

from raven_core.events import Agent, AgentStatus, Event, Platform, Run
from raven_core.integrations import devin, linkup

MAX_RESEARCH = 3
_FAILED = {AgentStatus.failed, AgentStatus.error, AgentStatus.stopped}
_PRIVATE_KEY = re.compile(r"password|passwd|secret|api[_-]?key|access[_-]?token|authorization|cookie|credential", re.I)
_VERIFIED = re.compile(
    r"^\s*(?P<last>last\s+)?verified\s+(?:checkpoint|state)\s*:\s*(?P<state>.+)$", re.I | re.M,
)


def _safe(value: Any, depth: int = 0) -> Any:
    if isinstance(value, str):
        for name in ("DEVIN_API_KEY", "HAI_API_KEY", "LINKUP_API_KEY", "GLADIA_API_KEY", "RAVEN_TOKEN"):
            if secret := os.environ.get(name):
                value = value.replace(secret, "[redacted]")
        value = re.sub(r"(?i)(Bearer\s+)\S+", r"\1[redacted]", value)
        value = re.sub(r"(?i)((?:token|api_key|password|secret)=)[^&\s]+", r"\1[redacted]", value)
        return value[:1200]
    if value is None or isinstance(value, (int, float, bool)):
        return value
    if depth >= 3:
        return "[omitted]"
    if isinstance(value, dict):
        return {
            str(key)[:80]: "[redacted]" if _PRIVATE_KEY.search(str(key)) else _safe(item, depth + 1)
            for key, item in list(value.items())[:12]
        }
    if isinstance(value, list):
        return [_safe(item, depth + 1) for item in value[:8]]
    return None


def _category(agent: Agent, verdict: dict[str, Any], reason: str) -> str:
    explicit = verdict.get("category") or verdict.get("failure_category")
    if isinstance(explicit, str) and explicit.strip():
        return str(_safe(explicit.strip().lower()))[:80]
    if agent.status == AgentStatus.stopped:
        return "cancelled"
    if category := re.search(
        r"^\s*(?:failure\s+)?category:\s*(blocked|execution|timeout|assertion|cancelled)\b", reason, re.I | re.M,
    ):
        return category.group(1).lower()
    if re.search(r"\b(timed out|timeout|deadline)\b", reason, re.I):
        return "timeout"
    if verdict.get("blocked") is True or re.match(r"\s*VERDICT:\s*BLOCKED\b", reason, re.I):
        return "blocked"
    if agent.status == AgentStatus.error:
        return "execution"
    return "assertion" if agent.status == AgentStatus.failed else "unknown"


def _checkpoint(event: Event, screenshot: str | None) -> dict[str, Any] | None:
    data = event.data
    if event.type not in ("agent.message", "agent.action", "agent.verdict", "agent.screenshot"):
        return None
    if event.type == "agent.message" and data.get("role") != "agent":
        return None
    value = data.get("last_verified_checkpoint") or data.get("last_verified_state")
    explicit = value is not None
    if explicit:
        screenshot = None
    if value is None:
        value = data.get("checkpoint") or data.get("verification")
    state = None
    evidence = None
    if isinstance(value, dict):
        verified = value.get("verified") is True or value.get("status") == "verified"
        verified = verified or (value is data.get("verification") and value.get("passed") is True)
        if (
            value.get("verified") is False or value.get("passed") is False
            or value.get("status") in ("failed", "unverified", "unknown")
            or not (verified or explicit)
        ):
            return None
        state = value.get("state") or value.get("description") or value.get("observation")
        evidence = value.get("evidence")
        screenshot = value.get("screenshot") or value.get("screenshot_url") or screenshot
    elif isinstance(value, str) and (explicit or data.get("verified") is True):
        state = value
        evidence = data.get("evidence")
    elif event.type in ("agent.message", "agent.verdict"):
        text = data.get("text") if event.type == "agent.message" else data.get("reason")
        if isinstance(text, str) and (match := _VERIFIED.search(text)):
            state = match.group("state")
            if match.group("last"):
                screenshot = None
    if not isinstance(state, str) or not state.strip():
        return None
    if re.fullmatch(r"(?:none|unknown|unverified|not verified|n/a|no verified (?:state|checkpoint))[.!]?", state.strip(), re.I):
        return None
    return {
        "state": _safe(state.strip()), "verified": True,
        "event_id": event.id, "ts": event.ts,
        "evidence": _safe(evidence), "screenshot": _safe(screenshot) if isinstance(screenshot, str) else None,
    }


def _outcome(data: dict[str, Any]) -> Any:
    if data.get("error"):
        return {"status": "failed", "error": _safe(data["error"])}
    for key in ("outcome", "result"):
        if data.get(key) is not None:
            return _safe(data[key])
    if isinstance(data.get("success"), bool):
        return {"status": "succeeded" if data["success"] else "failed"}
    return _safe(data.get("status")) or "unknown"


def _without_prior_inputs(value: Any, inputs: list[str]) -> Any:
    if isinstance(value, str):
        return re.sub("|".join(re.escape(text) for text in inputs), "[prior-attempt input]", value) if inputs else value
    if isinstance(value, dict):
        return {key: _without_prior_inputs(item, inputs) for key, item in value.items()}
    if isinstance(value, list):
        return [_without_prior_inputs(item, inputs) for item in value]
    return value


def build_failure_context(
    agents: list[Agent], events: list[Event], *, platform: Platform | None = None
) -> dict[str, Any]:
    """Build per-agent attempt evidence; task is identity, not a retry instruction.

    Known prior typed inputs are placeholders. Callers scope agents and bind the original task when persisting.
    """
    failures = []
    for agent in sorted(agents, key=lambda item: item.created_at):
        if agent.status not in _FAILED or (platform is not None and agent.platform != platform):
            continue
        own = sorted(
            (
                event for event in events
                if event.agent_id == agent.id
                and (event.run_id is None or event.run_id == agent.run_id)
                and event.data.get("platform", agent.platform.value) == agent.platform.value
            ),
            key=lambda event: event.ts,
        )
        verdict_event = next((event for event in reversed(own) if event.type == "agent.verdict"), None)
        verdict = verdict_event.data if verdict_event else {}
        if verdict.get("passed") is True:
            continue
        if verdict_event:
            own = [event for event in own if event.ts <= verdict_event.ts]
        inputs = set()
        for event in own:
            if event.type == "agent.action" and event.data.get("action") not in ("done", "answer"):
                args = event.data.get("args")
                if isinstance(args, dict):
                    inputs.update(
                        str(_safe(value)) for key, value in args.items()
                        if key in ("text", "value", "message", "content", "input", "command")
                        and isinstance(value, str) and len(value) >= 3
                    )
        prior_inputs = sorted(inputs, key=len, reverse=True)
        reason = verdict.get("reason") or agent.verdict_reason or agent.status.value
        reason = reason if isinstance(reason, str) else agent.status.value
        shots = []
        actions = []
        checkpoint = None
        previous_shot = None
        for event in own:
            if event.type == "agent.screenshot" and isinstance(event.data.get("url"), str):
                previous_shot = str(_safe(event.data["url"]))
                shots.append({"url": previous_shot, "event_id": event.id, "ts": event.ts})
                if actions and actions[-1]["screenshot_after"] is None:
                    actions[-1]["screenshot_after"] = previous_shot
            elif event.type == "agent.action":
                actions.append({
                    "event_id": event.id, "ts": event.ts,
                    "action": _safe(event.data.get("action")) or "unknown",
                    "args": _safe(event.data.get("args") or {}),
                    "outcome": _outcome(event.data),
                    "screenshot_before": previous_shot,
                    "screenshot_after": None,
                })
            if verified := _checkpoint(event, previous_shot):
                if not (
                    checkpoint and checkpoint["state"] == verified["state"]
                    and not verified["evidence"] and not verified["screenshot"]
                ):
                    checkpoint = verified
        if not shots and agent.last_screenshot:
            shots.append({"url": _safe(agent.last_screenshot), "event_id": None, "ts": None})
        if checkpoint:
            checkpoint["state"] = _without_prior_inputs(checkpoint["state"], prior_inputs)
            checkpoint["evidence"] = _without_prior_inputs(checkpoint["evidence"], prior_inputs)
        for action in actions:
            action["args"] = _without_prior_inputs(action["args"], prior_inputs)
            action["outcome"] = _without_prior_inputs(action["outcome"], prior_inputs)
        safe_reason = _without_prior_inputs(_safe(reason), prior_inputs)
        failures.append({
            "agent_id": agent.id, "run_id": agent.run_id, "platform": agent.platform.value,
            "task": getattr(agent, "original_task", None) or agent.task,
            "created_at": agent.created_at, "status": agent.status.value,
            "verdict": {
                "passed": verdict.get("passed") if isinstance(verdict.get("passed"), bool) else None,
                "reason": safe_reason, "category": _category(agent, verdict, safe_reason),
                "event_id": verdict_event.id if verdict_event else None,
            },
            "last_verified_checkpoint": checkpoint,
            "recent_actions": actions[-6:],
            "screenshots": shots[-4:],
        })
    return {"version": 1, "failures": failures}


def _evidence_lines(failure: dict[str, Any]) -> list[str]:
    verdict = failure["verdict"]
    lines = [
        f"**Verdict:** {verdict['reason']}",
        f"**Category:** {verdict['category']}",
        f"**Attempt:** `{failure['agent_id']}` · run `{failure['run_id'] or 'standalone'}`",
        "",
    ]
    checkpoint = failure["last_verified_checkpoint"]
    if checkpoint:
        lines.append(f"**Last verified checkpoint:** {checkpoint['state']}")
        if checkpoint["evidence"]:
            lines.append(f"- Evidence: {checkpoint['evidence']}")
        if shot := _public(checkpoint["screenshot"]):
            lines.append(f"- Checkpoint screenshot: {shot}")
    else:
        lines.append("**Last verified checkpoint:** none recorded; actions alone do not verify a state.")
    if failure["recent_actions"]:
        lines.extend(["", "**Recent actions and outcomes:**"])
        for action in failure["recent_actions"]:
            args = json.dumps(action["args"], ensure_ascii=False)
            outcome = json.dumps(action["outcome"], ensure_ascii=False)
            lines.append(f"- `{action['action']} {args}` — outcome: {outcome}")
            if shot := _public(action["screenshot_after"]):
                lines.append(f"  - Screenshot after action (not proof of success): {shot}")
    if failure["screenshots"]:
        lines.extend(["", "**Recent screenshots:**"])
        lines.extend(f"- {_public(shot['url'])}" for shot in failure["screenshots"])
    return lines


def _public(url: str | None) -> str | None:
    if not isinstance(url, str) or not url:
        return None
    base = os.environ.get("RAVEN_PUBLIC_URL", "").rstrip("/")
    return f"{base}{url}" if base and url.startswith("/") else url


async def build_failure_report(
    run: Run,
    agents: list[Agent],
    events_by_agent: dict[str, list[Event]],
    *,
    create_devin: bool = False,
) -> dict[str, Any]:
    """Returns the `report.ready` data payload."""
    agents = [a for a in agents if a.run_id in (None, run.id)]
    context = build_failure_context(agents, [event for own in events_by_agent.values() for event in own])
    by_id = {item["agent_id"]: item for item in context["failures"]}
    failed = [a for a in agents if a.id in by_id]
    passed = [a for a in agents if a.status == AgentStatus.passed]

    research_targets = failed[:MAX_RESEARCH]
    results = await asyncio.gather(
        *[
            linkup.search(linkup.failure_query(a.platform.value, by_id[a.id]["verdict"]["category"]))
            for a in research_targets
        ],
        return_exceptions=True,
    )
    findings: list[tuple[Agent, dict[str, Any]]] = [
        (a, r) for a, r in zip(research_targets, results) if isinstance(r, dict)
    ]

    lines = [f"# Raven report — `{run.spec}` ({run.id})", ""]
    lines.append(
        f"**Result:** {run.status.value if hasattr(run.status, 'value') else run.status} · "
        f"{len(passed)}/{len(agents)} platforms passed"
    )
    lines.append("")
    lines.append("| Platform | Worker | Status | Reason |")
    lines.append("|---|---|---|---|")
    for a in agents:
        reason = (by_id[a.id]["verdict"]["reason"] if a.id in by_id else _safe(a.verdict_reason or ""))
        reason = reason.replace("|", "\\|").replace("\n", " ")[:200]
        lines.append(f"| {a.platform.value} | {a.worker} | {a.status.value} | {reason} |")
    lines.append("")

    for a in failed:
        lines.append(f"## ❌ {a.platform.value}: {a.title}")
        lines.append("")
        lines.append(f"**Task:** {_safe(a.task.strip())}")
        lines.append("")
        lines.extend(_evidence_lines(by_id[a.id]))
        lines.append("")

    sources: list[dict[str, Any]] = []
    if findings:
        lines.append("## Research (Linkup)")
        lines.append("")
        for a, f in findings:
            lines.append(f"### {a.platform.value} — _{f['query']}_")
            lines.append("")
            lines.append(f["answer"] or "_no answer_")
            lines.append("")
            for s in f["sources"][:5]:
                lines.append(f"- [{s['name'] or s['url']}]({s['url']})")
                sources.append({**s, "platform": a.platform.value})
            lines.append("")
    elif not linkup.available():
        lines.append("_Linkup research skipped (LINKUP_API_KEY not set)._")
        lines.append("")

    markdown = "\n".join(lines)
    devin_url = None
    if create_devin:
        if devin.available():
            sess = await devin.create_session(devin.fix_prompt(markdown))
            devin_url = sess.get("url") if sess else None
        if devin_url:
            markdown += f"\n## Devin\n\nFix session: {devin_url}\n"
        else:
            markdown += "\n_Devin session not created (no DEVIN_API_KEY or API error)._\n"

    return {
        "markdown": markdown, "linkup_sources": sources, "devin_session_url": devin_url,
        "failure_context": context,
    }


def failure_notes(run: Run, agents: list[Agent], events: list[Event]) -> str:
    """Local evidence that is available even if research providers are offline."""
    agents = [agent for agent in agents if agent.run_id in (None, run.id)]
    context = build_failure_context(agents, events)
    by_id = {item["agent_id"]: item for item in context["failures"]}
    lines = [f"# QA failure — {run.spec}", "", f"Run: `{run.id}`", "", "## Run timeline", ""]
    for event in sorted(events, key=lambda item: item.ts):
        if (
            event.run_id == run.id and event.type == "agent.message"
            and not event.agent_id and event.data.get("role") == "system"
        ):
            lines.append(f"- {_safe(event.data.get('text', ''))}")
    lines.extend(["", "## Agent evidence", ""])
    for agent in agents:
        lines.append(f"- **{agent.platform.value}** (`{agent.id}`, {agent.worker}): {agent.status.value}")
        if agent.id in by_id:
            lines.extend(["", *_evidence_lines(by_id[agent.id]), ""])
    lines.extend(["", "## Before rerunning", "",
                  "- Check the last screenshot and action against the expected screen.",
                  "- For a setup or tool error, verify the app, desktop connection, and permissions first.",
                  "- When a click or keystroke has no effect twice, record a different path in the YAML spec.",
                  "- For an observed failed assertion, preserve the evidence and investigate the app behavior.", ""])
    return "\n".join(lines)
