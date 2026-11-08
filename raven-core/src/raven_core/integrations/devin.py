"""Devin CLI planning, completed v3 structured plans, and the legacy v1 fix API."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import signal
import uuid
from collections.abc import Awaitable, Callable
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

import httpx

from raven_core.integrations import http_client
from raven_core.env import REPO_ROOT, artifacts_dir, data_dir

log = logging.getLogger("raven.devin")
spawn = asyncio.create_subprocess_exec
POLL_S = 1.0
CLEANUP_S = 2.0
MAX_OUTPUT_BYTES = 256_000
MAX_WIRE_BYTES = 2_000_000
Approve = Callable[[dict[str, Any]], Awaitable[str | None]]
_TOOL_KINDS = {"read", "search", "think", "edit", "execute", "delete", "move", "fetch", "switch_mode", "other"}
_TOOL_NAMES = {
    "read", "grep", "glob", "search", "code_search", "read_file", "list_directory",
    "exec", "edit", "write", "fetch", "write_plan", "exit_plan_mode", "provide_structured_output",
    "mcp_list_servers", "mcp_list_tools", "apply_patch",
}
_LISTING_TOOLS = {"mcp_list_servers", "mcp_list_tools"}
_DISABLED_TOOLS = (
    "write_plan", "exit_plan_mode", "todo_write", "ask_user_question", "skill", "request_scope",
    "web_search", "webfetch",
)


class DevinError(RuntimeError):
    def __init__(self, message: str, *, code: str, session_id: str | None = None):
        super().__init__(message)
        self.code = code
        self.session_id = session_id


@dataclass(frozen=True)
class PlannerReply:
    output: str | dict[str, Any]
    session_id: str | None = None


def _session_id(value: Any, *, cloud: bool = False) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", value):
        raise DevinError("Devin returned an invalid session ID.", code="invalid_response")
    if cloud and not value.startswith("devin-"):
        raise DevinError("Cloud planning requires a v3 Devin session ID (devin-…).", code="configuration")
    return value


async def _read_output(stream: asyncio.StreamReader) -> bytes:
    chunks: list[bytes] = []
    size = 0
    while chunk := await stream.read(16_384):
        size += len(chunk)
        if size > MAX_OUTPUT_BYTES:
            raise DevinError("Devin planner output exceeded the size limit.", code="invalid_response")
        chunks.append(chunk)
    return b"".join(chunks)


def _signal_process(proc: asyncio.subprocess.Process, *, kill: bool = False) -> None:
    try:
        if os.name == "posix" and proc.pid:
            os.killpg(proc.pid, signal.SIGKILL if kill else signal.SIGTERM)
        elif proc.returncode is None:
            proc.kill() if kill else proc.terminate()
    except ProcessLookupError:
        pass


async def _stop_process(proc: asyncio.subprocess.Process, grace_s: float | None = None) -> None:
    grace_s = CLEANUP_S if grace_s is None else grace_s
    _signal_process(proc)
    try:
        await asyncio.wait_for(proc.wait(), grace_s / 2)
    except TimeoutError:
        _signal_process(proc, kill=True)
        try:
            await asyncio.wait_for(proc.wait(), grace_s / 2)
        except TimeoutError:
            log.warning("Devin planner did not exit after kill")


def _state_root() -> Path:
    return (data_dir() or artifacts_dir()) / "devin"


def _private_dir(path: Path) -> None:
    if path.is_symlink():
        raise DevinError("Devin session storage must not contain symbolic links.", code="configuration")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)


def _write_private(path: Path, value: dict[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _session_file(session_id: str) -> Path:
    name = hashlib.sha256(_session_id(session_id).encode()).hexdigest()
    return _state_root() / ".devin" / "sessions" / f"{name}.json"


def local_session_workspace(session_id: str) -> str:
    """Return only a Raven-owned session's stable workspace; never search other CLI sessions."""
    saved = _load_local_session(session_id)
    return str(saved.get("cwd") or saved["workspace"])


def _load_local_session(session_id: str) -> dict[str, Any]:
    path = _session_file(session_id)
    try:
        if path.is_symlink() or path.stat().st_size > 8192:
            raise ValueError
        data = json.loads(path.read_text(encoding="utf-8"))
        key = data.get("workspace_key")
        if data.get("session_id") != session_id or not isinstance(key, str) or not re.fullmatch(r"[a-f0-9]{64}", key):
            raise ValueError
        workspace = _state_root() / "workspaces" / key
        if workspace.is_symlink() or not workspace.is_dir():
            raise ValueError
        return {**data, "workspace": workspace}
    except (OSError, ValueError, AttributeError):
        raise DevinError(
            "This local session has no valid Raven workspace mapping; create a Raven plan first.",
            code="session_unavailable", session_id=session_id,
        ) from None


@contextmanager
def _workspace_lock(workspace: Path):
    lock_path = workspace / ".devin" / "raven.lock"
    if lock_path.is_symlink():
        raise DevinError("Invalid Devin workspace lock.", code="configuration")
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        try:
            if os.name == "posix":
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            else:
                import msvcrt
                if not os.fstat(fd).st_size:
                    os.write(fd, b"0")
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError:
            raise DevinError("The task's Devin workspace is already in use.", code="session_busy") from None
        yield
    finally:
        os.close(fd)


def _local_config(workspace: Path, *, fixing: bool) -> Path:
    denied = ["mcp__*", "Fetch(http://*/*)", "Fetch(https://*/*)", "Write(**/.devin/**)", "Write(/**/.devin/**)"]
    denied.extend(f"Read(/**/{pattern})" for pattern in (".env*", "*.pem", "*.key", "secrets*", "credentials*"))
    if not fixing:
        denied.extend(["edit", "exec", "Write(/**)", "Write(**)"])
    path = workspace / ".devin" / ("fix.json" if fixing else "planner.json")
    _write_private(path, {
        "permissions": {
            "allow": ["read", "grep", "glob"], "deny": denied,
            "ask": ["edit", "exec", "Write(/**)", "Write(**)"] if fixing else [],
        },
        "permission_mode": "normal",
        "subagents_enabled": False, "auto_update": False, "otel": {"enabled": False},
        "disabled_tools": [*_DISABLED_TOOLS] if fixing else ["exec", "edit", "write", "apply_patch", *_DISABLED_TOOLS],
        "read_config_from": {
            name: False for name in
            ("agents_standard", "cursor", "windsurf", "claude", "copilot", "opencode", "zed", "cloud")
        },
    })
    return path


def _tool_name(tool: dict[str, Any]) -> str:
    metadata = tool.get("_meta")
    name = metadata.get("cognition.ai/inferenceToolName") if isinstance(metadata, dict) else None
    # Titles, arguments and arbitrary provider names can contain source paths or credentials.
    return name if isinstance(name, str) and name in _TOOL_NAMES else "unknown"


def _tool_description(tool: dict[str, Any]) -> str:
    kind = tool.get("kind")
    kind = kind if isinstance(kind, str) and kind in _TOOL_KINDS else "unknown"
    return f"kind={kind}, name={_tool_name(tool)}"


def _merge_tool(previous: dict[str, Any], update: dict[str, Any]) -> dict[str, Any]:
    details = {**previous, **update}
    if isinstance(previous.get("_meta"), dict) and isinstance(update.get("_meta"), dict):
        details["_meta"] = {**previous["_meta"], **update["_meta"]}
    return details


class _ACPClient:
    def __init__(self, proc, *, fixing: bool, approve: Approve | None, repo: str | None):
        self.proc = proc
        self.fixing = fixing
        self.approve = approve
        self.repo = Path(repo) if repo else None
        self.session_id: str | None = None
        self.mode: str | None = None
        self.capture = False
        self.parts: list[str] = []
        self.message_id: str | None = None
        self.bytes_read = 0
        self.output_size = 0
        self.next_id = 0
        self.tool_calls: dict[str, dict[str, Any]] = {}
        self.approved: set[str] = set()
        self.pending_rejections: dict[str, dict[str, Any]] = {}

    def scope_error(self, tool: dict[str, Any], *, unconfirmed: bool = False) -> DevinError:
        detail = " The CLI did not confirm rejection before ending the turn." if unconfirmed else ""
        return DevinError(
            f"Devin attempted a tool outside the authorized task scope ({_tool_description(tool)}).{detail}",
            code="blocked",
        )

    async def send(self, value: dict[str, Any]) -> None:
        self.proc.stdin.write((json.dumps(value, ensure_ascii=False) + "\n").encode())
        await self.proc.stdin.drain()

    async def cancel(self) -> None:
        if self.session_id:
            await self.send({"jsonrpc": "2.0", "method": "session/cancel", "params": {"sessionId": self.session_id}})

    async def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self.next_id += 1
        mid = self.next_id
        await self.send({"jsonrpc": "2.0", "id": mid, "method": method, "params": params})
        while True:
            try:
                line = await self.proc.stdout.readline()
            except ValueError:
                raise DevinError("Devin ACP message exceeded the size limit.", code="invalid_response") from None
            self.bytes_read += len(line)
            if len(line) > MAX_OUTPUT_BYTES or self.bytes_read > MAX_WIRE_BYTES:
                raise DevinError("Devin ACP transcript exceeded the size limit.", code="invalid_response")
            if not line:
                raise DevinError(
                    "Devin ACP exited before completing the request; check CLI authentication and workspace trust.",
                    code="planner_failed",
                )
            try:
                message = json.loads(line)
            except ValueError:
                raise DevinError("Devin ACP returned invalid protocol data; review authentication and workspace trust.",
                                 code="invalid_response") from None
            if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
                raise DevinError("Devin ACP returned an invalid protocol message.", code="invalid_response")
            if "method" in message:
                if "id" in message:
                    await self.permission(message)
                elif message["method"] == "session/update":
                    self.update(message.get("params") or {})
                continue
            if message.get("id") != mid:
                raise DevinError("Devin ACP returned an unexpected response ID.", code="invalid_response")
            if "error" in message:
                raise DevinError(
                    f"Devin ACP rejected {method}; review workspace trust, authentication, and session permissions.",
                    code="blocked",
                )
            result = message.get("result")
            if not isinstance(result, dict):
                raise DevinError("Devin ACP returned an invalid result.", code="invalid_response")
            if method == "session/prompt" and self.pending_rejections:
                raise self.scope_error(next(iter(self.pending_rejections.values())), unconfirmed=True)
            return result

    async def permission(self, message: dict[str, Any]) -> None:
        params = message.get("params") or {}
        method = message.get("method")
        if method != "session/request_permission":
            await self.send({
                "jsonrpc": "2.0", "id": message["id"],
                "error": {"code": -32601, "message": "Client operation is not available"},
            })
            raise DevinError("Devin requested a client capability Raven did not advertise.", code="blocked")
        selected = None
        options = params.get("options") or []
        tool = params.get("toolCall") or {}
        if not isinstance(tool, dict) or not isinstance(options, list):
            raise DevinError("Devin requested an invalid permission.", code="invalid_response")
        tool_id = tool.get("toolCallId")
        if not isinstance(tool_id, str) or not tool_id:
            raise DevinError("Devin requested a permission without a valid tool ID.", code="invalid_response")
        details = _merge_tool(self.tool_calls.get(tool_id, {}), tool)
        allowed = {
            option.get("optionId") for option in options if isinstance(option, dict)
            and option.get("kind") == "allow_once" and isinstance(option.get("optionId"), str)
        }
        if (
            self.fixing and self.approve and params.get("sessionId") == self.session_id
            and details.get("kind") in ("read", "search", "edit", "execute")
            and allowed and (details.get("kind") != "edit" or self.edit_in_scope(details))
        ):
            try:
                selected = await self.approve({**params, "toolCall": details})
            except asyncio.CancelledError:
                await self.send({"jsonrpc": "2.0", "id": message["id"], "result": {"outcome": {"outcome": "cancelled"}}})
                raise
        if not isinstance(selected, str) or selected not in allowed:
            await self.send({"jsonrpc": "2.0", "id": message["id"], "result": {"outcome": {"outcome": "cancelled"}}})
            raise DevinError(
                f"Devin requires explicit approval ({_tool_description(details)}). "
                "Review workspace trust or authorize this operation through an approval UI.",
                code="approval_required",
            )
        self.approved.add(tool_id)
        await self.send({
            "jsonrpc": "2.0", "id": message["id"],
            "result": {"outcome": {"outcome": "selected", "optionId": selected}},
        })

    def edit_in_scope(self, tool: dict[str, Any]) -> bool:
        locations = tool.get("locations")
        if not self.repo or not isinstance(locations, list) or not locations:
            return False
        for location in locations:
            try:
                path = Path(location["path"])
                if not path.is_absolute():
                    return False
                relative = path.resolve().relative_to(self.repo)
                if any(part in (".git", ".devin", "specs") or part.startswith(".env") for part in relative.parts):
                    return False
            except (KeyError, TypeError, ValueError, OSError):
                return False
        return True

    def update(self, params: dict[str, Any]) -> None:
        if not self.capture:
            return
        if params.get("sessionId") != self.session_id:
            raise DevinError("Devin sent output from a different session.", code="invalid_response")
        update = params.get("update")
        if not isinstance(update, dict):
            raise DevinError("Devin sent an invalid session update.", code="invalid_response")
        kind = update.get("sessionUpdate")
        if kind == "current_mode_update" and update.get("currentModeId") != self.mode:
            raise DevinError("Devin attempted to change the authorized session mode.", code="blocked")
        if kind in ("tool_call", "tool_call_update"):
            tool_id = update.get("toolCallId")
            if not isinstance(tool_id, str) or not tool_id:
                raise DevinError("Devin sent a tool update without a valid tool ID.", code="invalid_response")
            details = _merge_tool(self.tool_calls.get(tool_id, {}), update)
            self.tool_calls[tool_id] = details
            tool_kind = details.get("kind")
            permitted = ("read", "search", "think", "edit", "execute") if self.fixing else ("read", "search", "think")
            running = details.get("status") in ("in_progress", "completed")
            if not self.fixing and _tool_name(details) == "exec":
                # ACP's initial tool_call is a proposal, not evidence of execution. Devin
                # emits it even for exec calls denied by our unchanged "exec" config rule.
                # Let the planner observe that denial and continue with repository reads
                # or the supplied snapshot. Never approve or execute even a read-only shell.
                status = details.get("status")
                if tool_kind != "execute":
                    raise self.scope_error(details)
                if status in (None, "pending"):
                    self.pending_rejections[tool_id] = details
                elif status == "failed" and (details.get("_meta") or {}).get("cognition.ai/rejected") is True:
                    self.pending_rejections.pop(tool_id, None)
                else:
                    raise self.scope_error(details)
            elif (tool_kind not in permitted and not (tool_kind is None and _tool_name(details) in _LISTING_TOOLS)) or (
                not self.fixing and _tool_name(details) in ("edit", "write", "fetch", "write_plan", "exit_plan_mode")
            ):
                raise self.scope_error(details)
            if self.fixing and tool_kind in ("edit", "execute") and running:
                if tool_id not in self.approved:
                    raise DevinError(
                        f"Devin attempted a modification without per-operation approval ({_tool_description(details)}).",
                        code="blocked",
                    )
            if kind == "tool_call":
                self.parts = []
                self.message_id = None
        elif kind == "agent_message_chunk":
            content = update.get("content") or {}
            if content.get("type") != "text" or not isinstance(content.get("text"), str):
                raise DevinError("Devin returned non-text output for a structured task.", code="invalid_response")
            message_id = update.get("messageId")
            if message_id is not None and message_id != self.message_id:
                self.parts = []
                self.message_id = message_id
            text = content["text"]
            self.output_size += len(text.encode())
            if self.output_size > MAX_OUTPUT_BYTES:
                raise DevinError("Devin output exceeded the size limit.", code="invalid_response")
            self.parts.append(text)


async def _close_acp(proc, rpc: _ACPClient | None, *, cancel: bool) -> None:
    try:
        async with asyncio.timeout(CLEANUP_S / 2):
            if rpc and cancel:
                await rpc.cancel()
            if proc.stdin:
                proc.stdin.close()
            await proc.wait()
            return
    except (TimeoutError, OSError, RuntimeError):
        pass
    await _stop_process(proc, CLEANUP_S / 2)


async def _finish_local_cleanup(proc, rpc: _ACPClient | None, stderr: asyncio.Task | None, *, cancel: bool) -> None:
    """Reap the process and its reader before propagating even repeated cancellation."""
    async def cleanup() -> None:
        try:
            if proc:
                await _close_acp(proc, rpc, cancel=cancel)
        finally:
            if stderr:
                stderr.cancel()
                await asyncio.gather(stderr, return_exceptions=True)

    task = asyncio.create_task(cleanup())
    cancellation = None
    try:
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError as error:
                cancellation = cancellation or error
        task.result()
    finally:
        # A first Stop may arrive during cleanup of an otherwise successful turn.
        if cancellation is not None:
            raise cancellation


async def _local_turn(
    prompt: str, *, repo_path: str | None, session_id: str | None, timeout_s: float,
    task_key: str, fixing: bool = False, approve: Approve | None = None,
    on_session: Callable[[str], None] | None = None,
) -> PlannerReply:
    repo = str(Path(repo_path).expanduser().resolve(strict=True)) if repo_path else None
    key = hashlib.sha256(f"{task_key}\0{repo or ''}".encode()).hexdigest()
    root = _state_root()
    for directory in (root, root / ".devin", root / ".devin" / "sessions", root / "workspaces"):
        _private_dir(directory)
    workspace = root / "workspaces" / key
    cwd = repo or str(REPO_ROOT)
    if session_id:
        saved = _load_local_session(session_id)
        if saved.get("workspace_key") != key or saved.get("repo_path") != repo or saved.get("task_key") != task_key:
            raise DevinError("The Devin session belongs to a different task or repository.", code="session_mismatch",
                             session_id=session_id)
        workspace = saved["workspace"]
        cwd = str(saved.get("cwd") or workspace)
    elif fixing:
        raise DevinError("A fix must resume the planning session.", code="configuration")
    _private_dir(workspace)
    _private_dir(workspace / ".devin")
    proc = None
    rpc = None
    stderr = None
    finished = False
    with _workspace_lock(workspace):
        config = _local_config(workspace, fixing=fixing)
        args = [
            os.environ.get("DEVIN_CLI") or "devin",
            "--config", str(config), "--permission-mode", "normal", "--respect-workspace-trust", "true",
            "acp", "--model", os.environ.get("DEVIN_PLANNING_MODEL") or os.environ.get("DEVIN_MODEL") or "opus",
        ]
        try:
            async with asyncio.timeout(timeout_s):
                proc = await spawn(
                    *args, cwd=cwd,
                    env={**os.environ, "DEVIN_PERMISSION_MODE": "normal", "OTEL_LOG_USER_PROMPTS": "0", "OTEL_LOG_TOOL_DETAILS": "0"},
                    stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                    start_new_session=os.name == "posix", limit=MAX_OUTPUT_BYTES + 1,
                )
                stderr = asyncio.create_task(_read_output(proc.stderr))
                rpc = _ACPClient(proc, fixing=fixing, approve=approve, repo=repo)
                initialized = await rpc.request("initialize", {
                    "protocolVersion": 1,
                    "clientCapabilities": {"fs": {"readTextFile": False, "writeTextFile": False}, "terminal": False},
                    "clientInfo": {"name": "raven-planner", "version": "1"},
                })
                if initialized.get("protocolVersion") != 1 or (initialized.get("agentCapabilities") or {}).get("loadSession") is not True:
                    raise DevinError("Devin must support ACP v1 and session/load before Raven can retain a planning session.",
                                     code="unsupported_protocol")
                params: dict[str, Any] = {"cwd": cwd, "mcpServers": []}
                if session_id:
                    rpc.session_id = session_id
                    state = await rpc.request("session/load", {**params, "sessionId": session_id})
                else:
                    state = await rpc.request("session/new", params)
                    session_id = _session_id(state.get("sessionId"))
                    rpc.session_id = session_id
                    path = _session_file(session_id)
                    if path.exists():
                        raise DevinError("Devin returned a session ID that is already registered.", code="invalid_response")
                    _write_private(path, {
                        "version": 1, "session_id": session_id, "workspace_key": key,
                        "task_key": task_key, "repo_path": repo, "cwd": cwd,
                    })
                if on_session:
                    on_session(session_id)
                modes = (state.get("modes") or {}).get("availableModes") or []
                wanted = "normal" if fixing else "plan"
                selected = next((
                    mode.get("id") for mode in modes if isinstance(mode, dict)
                    and (str(mode.get("name", "")).lower() == wanted or mode.get("id") == wanted)
                    and isinstance(mode.get("id"), str)
                ), None)
                if not selected:
                    raise DevinError(f"Devin ACP must advertise its documented {wanted.title()} mode; no task was submitted.",
                                     code="unsupported_mode")
                rpc.mode = selected
                await rpc.request("session/set_mode", {"sessionId": session_id, "modeId": selected})
                rpc.capture = True
                result = await rpc.request("session/prompt", {
                    "sessionId": session_id, "prompt": [{"type": "text", "text": prompt}],
                })
                if result.get("stopReason") != "end_turn":
                    raise DevinError("Devin did not complete the requested turn.", code="blocked")
                finished = True
                return PlannerReply("".join(rpc.parts).strip(), session_id)
        except FileNotFoundError:
            raise DevinError("Devin CLI was not found; install it or set DEVIN_CLI.", code="configuration") from None
        except OSError:
            raise DevinError("Devin ACP could not access its workspace or start.", code="configuration") from None
        except TimeoutError:
            raise DevinError(f"Devin ACP exceeded its deadline. Review authentication and workspace trust in {cwd}.",
                             code="timeout", session_id=session_id) from None
        except DevinError as error:
            error.session_id = error.session_id or session_id
            raise
        finally:
            await _finish_local_cleanup(proc, rpc, stderr, cancel=not finished)


async def local_plan(
    prompt: str, *, repo_path: str | None, session_id: str | None, timeout_s: float,
    task_key: str, on_session: Callable[[str], None] | None = None,
) -> PlannerReply:
    """ACP v1 session/new returns the real ID; session/load reuses only Raven-owned mappings."""
    return await _local_turn(prompt, repo_path=repo_path, session_id=session_id, timeout_s=timeout_s,
                             task_key=task_key, on_session=on_session)


async def _json_request(client: httpx.AsyncClient, method: str, url: str, **kwargs: Any) -> dict[str, Any]:
    try:
        async with client.stream(method, url, **kwargs) as response:
            if not 200 <= response.status_code < 300:
                raise DevinError(
                    f"Devin cloud planning request failed (HTTP {response.status_code}).",
                    code="transport",
                )
            chunks = []
            size = 0
            async for chunk in response.aiter_bytes():
                size += len(chunk)
                if size > MAX_OUTPUT_BYTES:
                    raise DevinError("Devin cloud planning response exceeded the size limit.", code="invalid_response")
                chunks.append(chunk)
    except httpx.TimeoutException:
        raise DevinError("Devin cloud planning request timed out.", code="timeout") from None
    except httpx.HTTPError:
        raise DevinError("Devin cloud planning request failed.", code="transport") from None
    try:
        data = json.loads(b"".join(chunks))
    except ValueError:
        raise DevinError("Devin cloud planning returned invalid JSON.", code="invalid_response") from None
    if not isinstance(data, dict):
        raise DevinError("Devin cloud planning returned an invalid response.", code="invalid_response")
    return data


async def _archive(client: httpx.AsyncClient, url: str, headers: dict[str, str]) -> None:
    try:
        async with asyncio.timeout(CLEANUP_S):
            async with client.stream("POST", f"{url}/archive", headers=headers, timeout=CLEANUP_S) as response:
                if not 200 <= response.status_code < 300:
                    log.warning("Could not stop Devin planner session (HTTP %s)", response.status_code)
    except Exception:
        log.warning("Could not stop Devin planner session within cleanup deadline")


async def cloud_plan(
    prompt: str,
    schema: dict[str, Any],
    *,
    request_id: str,
    session_id: str | None,
    timeout_s: float,
    on_session: Callable[[str], None] | None = None,
) -> PlannerReply:
    """Poll v3 GET structured_output and completion; archive on cancellation/failure."""
    key = os.environ.get("DEVIN_API_KEY")
    org = os.environ.get("DEVIN_ORG_ID")
    if not key or not org:
        raise DevinError(
            "Cloud planning requires DEVIN_API_KEY (v3 service-user/PAT) and DEVIN_ORG_ID.",
            code="configuration",
        )
    base = (os.environ.get("DEVIN_API_V3_BASE") or "https://api.devin.ai/v3").rstrip("/")
    parts = urlsplit(base)
    if (
        parts.scheme not in ("https", "http") or not parts.netloc or parts.username
        or parts.password or parts.query or parts.fragment
    ):
        raise DevinError("DEVIN_API_V3_BASE must be an HTTP(S) base URL without credentials or query.", code="configuration")
    sessions_url = f"{base}/organizations/{quote(org, safe='')}/sessions"
    headers = {"Authorization": f"Bearer {key}"}
    sid = _session_id(session_id, cloud=True) if session_id else None
    resumed = sid is not None
    started = False
    deadline = asyncio.get_running_loop().time() + timeout_s
    try:
        max_acu = int(os.environ.get("DEVIN_PLANNING_MAX_ACU") or "2")
        if max_acu <= 0:
            raise ValueError
    except ValueError:
        raise DevinError("DEVIN_PLANNING_MAX_ACU must be a positive integer.", code="configuration") from None

    async with http_client(timeout=min(timeout_s, 30.0)) as client:
        try:
            async with asyncio.timeout(timeout_s):
                if sid:
                    started = True
                    await _json_request(
                        client, "POST", f"{sessions_url}/{sid}/messages",
                        headers=headers, json={"message": prompt},
                    )
                else:
                    body: dict[str, Any] = {
                        "prompt": prompt,
                        "title": "Raven QA planning",
                        "devin_mode": "normal",
                        "bypass_approval": False,
                        "memory_enabled": False,
                        "max_acu_limit": max_acu,
                        "structured_output_schema": schema,
                        "structured_output_required": True,
                        "resumable": True,
                    }
                    if placement := os.environ.get("DEVIN_PLANNING_PLATFORM"):
                        body["platform"] = placement
                    if repos := os.environ.get("DEVIN_CLOUD_REPOS"):
                        body["repos"] = [repo.strip() for repo in repos.split(",") if repo.strip()]
                    created = await _json_request(client, "POST", sessions_url, headers=headers, json=body)
                    sid = _session_id(created.get("session_id"), cloud=True)
                    started = True
                if on_session:
                    on_session(sid)
                while True:
                    data = await _json_request(
                        client, "GET", f"{sessions_url}/{sid}", headers=headers,
                        timeout=max(0.001, min(30.0, deadline - asyncio.get_running_loop().time())),
                    )
                    if data.get("session_id") != sid:
                        raise DevinError("Devin returned a different planning session.", code="invalid_response")
                    status, detail = data.get("status"), data.get("status_detail")
                    output = data.get("structured_output")
                    fresh = isinstance(output, dict) and output.get("request_id") == request_id
                    if status == "error" or status == "suspended":
                        raise DevinError(
                            "Devin cloud planning stopped before completion; check session quota and availability.",
                            code="planner_failed",
                        )
                    if status == "exit" or detail == "finished":
                        if fresh:
                            return PlannerReply(output, sid)
                        if not resumed or status == "exit":
                            raise DevinError(
                                "Devin completed without a fresh structured QA plan.", code="invalid_response",
                            )
                    elif detail in ("waiting_for_user", "waiting_for_approval"):
                        raise DevinError(
                            "Devin planning needs user input or approval before it can produce a plan.", code="blocked",
                        )
                    elif status not in ("new", "claimed", "running", "resuming"):
                        raise DevinError("Devin returned an unknown planning session state.", code="invalid_response")
                    await asyncio.sleep(POLL_S)
        except BaseException as error:
            if sid and started:
                await asyncio.shield(_archive(client, f"{sessions_url}/{sid}", headers))
            if isinstance(error, TimeoutError):
                raise DevinError("Devin cloud planning exceeded its deadline.", code="timeout", session_id=sid) from None
            if isinstance(error, DevinError):
                error.session_id = sid
            raise


async def follow_up_session(
    session_id: str,
    prompt: str,
    *,
    mode: str = "local",
    repo_path: str | None = None,
    task_key: str,
    request_id: str,
    timeout_s: float = 300,
    approve: Approve | None = None,
) -> PlannerReply:
    """Resume a fix turn, never create a session. approve returns an explicitly chosen allow_once option ID.

    Cloud keeps the original structured-plan schema and retrieves the fresh fix report from documented session messages.
    Local fixes require an approval UI; callers must show full operations/paths, not trust agent-authored titles.
    """
    _session_id(session_id, cloud=mode == "cloud")
    if mode == "local":
        if not repo_path or approve is None:
            raise DevinError(
                "Local fixes require the original repository and an explicit per-operation approval callback.",
                code="approval_required", session_id=session_id,
            )
        return await _local_turn(
            prompt, repo_path=repo_path, session_id=session_id, timeout_s=timeout_s,
            task_key=task_key, fixing=True, approve=approve,
        )
    if mode != "cloud":
        raise DevinError("Devin follow-up mode must be local or cloud.", code="configuration", session_id=session_id)
    try:
        async with asyncio.timeout(timeout_s):
            reply = await cloud_plan(
                prompt, {}, request_id=request_id, session_id=session_id, timeout_s=timeout_s,
            )
            base = (os.environ.get("DEVIN_API_V3_BASE") or "https://api.devin.ai/v3").rstrip("/")
            org = quote(os.environ.get("DEVIN_ORG_ID") or "", safe="")
            url = f"{base}/organizations/{org}/sessions/{session_id}/messages"
            headers = {"Authorization": f"Bearer {os.environ.get('DEVIN_API_KEY') or ''}"}
            after = None
            seen = set()
            final = None
            async with http_client(timeout=min(timeout_s, 30.0)) as client:
                for _ in range(20):
                    params: dict[str, Any] = {"first": 100}
                    if after:
                        params["after"] = after
                    page = await _json_request(client, "GET", url, headers=headers, params=params)
                    items = page.get("items")
                    if not isinstance(items, list):
                        raise DevinError("Devin returned invalid session messages.", code="invalid_response")
                    for item in items:
                        if not isinstance(item, dict) or item.get("source") != "devin" or not isinstance(item.get("message"), str):
                            continue
                        text = item["message"].strip()
                        if fenced := re.fullmatch(r"```(?:json)?\s*\n?(.*?)\n?```", text, re.I | re.S):
                            text = fenced.group(1)
                        try:
                            value = json.loads(text)
                        except ValueError:
                            continue
                        if isinstance(value, dict) and value.get("request_id") == request_id and "fix" in value:
                            final = value
                    if not page.get("has_next_page"):
                        if not final or final.get("plan") != reply.output.get("plan"):
                            raise DevinError("Devin completed without a fresh fix report matching the unchanged QA plan.",
                                             code="invalid_response")
                        return PlannerReply(final, session_id)
                    after = page.get("end_cursor")
                    if not isinstance(after, str) or not after or after in seen:
                        raise DevinError("Devin returned an invalid session-message cursor.", code="invalid_response")
                    seen.add(after)
                raise DevinError("Devin session history exceeded the bounded follow-up retrieval limit.", code="invalid_response")
    except TimeoutError:
        raise DevinError("Devin follow-up exceeded its deadline.", code="timeout", session_id=session_id) from None
    except DevinError as error:
        error.session_id = session_id
        raise


def available() -> bool:
    return bool(os.environ.get("DEVIN_API_KEY"))


async def create_session(prompt: str) -> dict[str, Any] | None:
    """Return {"session_id", "url"} or None if no key / failure."""
    key = os.environ.get("DEVIN_API_KEY")
    if not key:
        return None
    base = (os.environ.get("DEVIN_API_BASE") or "https://api.devin.ai/v1").rstrip("/")
    try:
        async with http_client(timeout=60) as client:
            r = await client.post(
                f"{base}/sessions",
                json={"prompt": prompt, "idempotent": False},
                headers={"Authorization": f"Bearer {key}"},
            )
        if r.status_code >= 400:
            log.warning("devin create session failed: HTTP %s", r.status_code)
            return None
        data = r.json()
    except Exception as e:
        log.warning("devin create session error: %s", type(e).__name__)
        return None
    if not isinstance(data, dict):
        return None
    sid = data.get("session_id") or data.get("id")
    if sid is not None and not isinstance(sid, str):
        return None
    url = data.get("url") or (f"https://app.devin.ai/sessions/{sid.removeprefix('devin-')}" if sid else None)
    return {"session_id": sid, "url": url}


def fix_prompt(report_markdown: str) -> str:
    return (
        "Raven (a cross-platform test runner driving computer-use agents on macOS, Linux, "
        "Windows and iOS) found a failing cross-platform test of the bitchat clients "
        "(Nostr geohash channels; no Bluetooth available in the test VMs).\n\n"
        "Investigate the failure below, find the root cause in the relevant bitchat client "
        "(e.g. github.com/permissionlesstech/bitchat or the bitchat-tui client), and open a PR "
        "with a fix. If it is a test/environment problem rather than an app bug, explain what "
        "to change in the test instead.\n\n"
        "---\n\n" + report_markdown
    )
