"""Failure report: failed agents + verdicts + screenshots + Linkup research (+ Devin)."""

from __future__ import annotations

import asyncio
import os
from typing import Any

from raven_core.events import Agent, AgentStatus, Event, Run
from raven_core.integrations import devin, linkup

MAX_RESEARCH = 3


def _public(url: str | None) -> str | None:
    if not url:
        return None
    base = os.environ.get("RAVEN_PUBLIC_URL", "").rstrip("/")
    return f"{base}{url}" if base and url.startswith("/") else url


def _last_messages(events: list[Event], n: int = 4) -> list[str]:
    out = [
        f"{e.data.get('role', 'agent')}: {e.data.get('text', '')}"
        for e in events
        if e.type == "agent.message" and e.data.get("text")
    ]
    return [m[:400] for m in out[-n:]]


def _last_actions(events: list[Event], n: int = 6) -> list[str]:
    out = []
    for e in events:
        if e.type == "agent.action":
            out.append(f"{e.data.get('action')} {e.data.get('args') or {}}")
    return [a[:200] for a in out[-n:]]


async def build_failure_report(
    run: Run,
    agents: list[Agent],
    events_by_agent: dict[str, list[Event]],
    *,
    create_devin: bool = False,
) -> dict[str, Any]:
    """Returns the `report.ready` data payload."""
    failed = [a for a in agents if a.status != AgentStatus.passed]
    passed = [a for a in agents if a.status == AgentStatus.passed]

    research_targets = failed[:MAX_RESEARCH]
    results = await asyncio.gather(
        *[
            linkup.search(linkup.failure_query(a.platform.value, a.verdict_reason or a.status.value))
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
        reason = (a.verdict_reason or "").replace("|", "\\|").replace("\n", " ")[:200]
        lines.append(f"| {a.platform.value} | {a.worker} | {a.status.value} | {reason} |")
    lines.append("")

    for a in failed:
        lines.append(f"## ❌ {a.platform.value}: {a.title}")
        lines.append("")
        lines.append(f"**Task:** {a.task.strip()}")
        lines.append("")
        lines.append(f"**Verdict:** {a.verdict_reason or a.status.value}")
        shot = _public(a.last_screenshot)
        if shot:
            lines.append("")
            lines.append(f"**Last screenshot:** {shot}")
        evs = events_by_agent.get(a.id, [])
        acts = _last_actions(evs)
        if acts:
            lines.append("")
            lines.append("**Last actions:**")
            lines.extend(f"- `{x}`" for x in acts)
        msgs = _last_messages(evs)
        if msgs:
            lines.append("")
            lines.append("**Last messages:**")
            lines.extend(f"> {m}" for m in msgs)
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

    return {"markdown": markdown, "linkup_sources": sources, "devin_session_url": devin_url}
