"""Minimal newline-delimited JSON-RPC 2.0 peer (ACP stdio transport)."""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import threading
from typing import Any, Awaitable, Callable

log = logging.getLogger("raven.acp.rpc")

Handler = Callable[[str, dict[str, Any], bool], Awaitable[Any]]


class RpcError(Exception):
    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data


class MethodNotFound(RpcError):
    def __init__(self, method: str) -> None:
        super().__init__(-32601, f"Method not found: {method}")


class Connection:
    def __init__(self, write: Callable[[bytes], None], handler: Handler) -> None:
        self._write = write
        self._handler = handler
        self._lock = threading.Lock()
        self._tasks: set[asyncio.Task[Any]] = set()

    # ------------------------------------------------------------------ out
    def send(self, obj: dict[str, Any]) -> None:
        data = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"
        with self._lock:
            try:
                self._write(data)
            except (BrokenPipeError, ValueError, OSError):
                log.debug("stdout closed")

    def notify(self, method: str, params: dict[str, Any]) -> None:
        self.send({"jsonrpc": "2.0", "method": method, "params": params})

    # ------------------------------------------------------------------ in
    def feed(self, line: bytes | str) -> None:
        line = line.strip()
        if not line:
            return
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            self.send({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}})
            return
        if not isinstance(msg, dict) or "method" not in msg:
            return  # responses to requests we never send
        is_request = "id" in msg and msg["id"] is not None
        t = asyncio.get_running_loop().create_task(
            self._dispatch(msg.get("id"), msg["method"], msg.get("params") or {}, is_request)
        )
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)

    async def _dispatch(self, mid: Any, method: str, params: dict[str, Any], is_request: bool) -> None:
        try:
            result = await self._handler(method, params, is_request)
            if is_request:
                self.send({"jsonrpc": "2.0", "id": mid, "result": {} if result is None else result})
        except RpcError as e:
            if is_request:
                err: dict[str, Any] = {"code": e.code, "message": e.message}
                if e.data is not None:
                    err["data"] = e.data
                self.send({"jsonrpc": "2.0", "id": mid, "error": err})
        except asyncio.CancelledError:
            if is_request:
                self.send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32800, "message": "cancelled"}})
            raise
        except Exception as e:
            log.exception("handler error for %s", method)
            if is_request:
                self.send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32603, "message": f"{type(e).__name__}: {e}"}})

    async def serve(self, read_line: Callable[[], Awaitable[bytes]]) -> None:
        """Read until EOF (empty bytes)."""
        while True:
            line = await read_line()
            if not line:
                break
            self.feed(line)

    async def drain(self, timeout: float = 2.0) -> None:
        if self._tasks:
            await asyncio.wait(list(self._tasks), timeout=timeout)

    def cancel_all(self) -> None:
        for t in list(self._tasks):
            t.cancel()


def stdin_line_reader(loop: asyncio.AbstractEventLoop) -> Callable[[], Awaitable[bytes]]:
    """Read stdin lines on a daemon thread (works for pipes, ttys and files)."""
    q: asyncio.Queue[bytes] = asyncio.Queue()
    stdin = sys.stdin.buffer

    def pump() -> None:
        try:
            while True:
                line = stdin.readline()
                loop.call_soon_threadsafe(q.put_nowait, line)
                if not line:
                    return
        except Exception:
            loop.call_soon_threadsafe(q.put_nowait, b"")

    threading.Thread(target=pump, name="acp-stdin", daemon=True).start()
    return q.get
