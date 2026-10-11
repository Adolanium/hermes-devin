"""Newline-delimited JSON-RPC 2.0 over a child's stdin/stdout (the ACP transport).

One reader thread parses the child's stdout. Responses wake the caller waiting
on that id; notifications go to ``on_notification`` on the reader thread (keep
it fast); requests from the agent go to ``on_request`` on a fresh thread, since
answering one can mean waiting for a human.
"""

from __future__ import annotations

import json
import logging
import threading
from collections.abc import Callable
from typing import Any, BinaryIO

logger = logging.getLogger(__name__)

METHOD_NOT_FOUND = -32601
INTERNAL_ERROR = -32603

# Lines longer than this are dropped rather than buffered without bound.
MAX_LINE_BYTES = 8 * 1024 * 1024


class RpcError(Exception):
    def __init__(self, code: int, message: str, data: Any = None):
        super().__init__(f"{message} (code {code})")
        self.code = code
        self.message = message
        self.data = data


class PeerClosed(Exception):
    pass


class _Pending:
    __slots__ = ("event", "result", "error")

    def __init__(self) -> None:
        self.event = threading.Event()
        self.result: Any = None
        self.error: BaseException | None = None


class JsonRpcPeer:
    def __init__(
        self,
        reader: BinaryIO,
        writer: BinaryIO,
        *,
        on_notification: Callable[[str, dict], None],
        on_request: Callable[[str, dict], Any],
        on_closed: Callable[[], None] | None = None,
        on_garbage: Callable[[str], None] | None = None,
    ):
        self._reader = reader
        self._writer = writer
        self._on_notification = on_notification
        self._on_request = on_request
        self._on_closed = on_closed
        self._on_garbage = on_garbage
        self._pending: dict[int, _Pending] = {}
        self._lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._next_id = 0
        self.closed = threading.Event()
        self._thread = threading.Thread(target=self._read_loop, name="devin-acp-reader", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def join(self, timeout: float | None = None) -> None:
        self._thread.join(timeout)

    # -- outgoing -----------------------------------------------------------

    def _send(self, payload: dict) -> None:
        data = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
        with self._write_lock:
            if self.closed.is_set():
                raise PeerClosed("connection closed")
            try:
                self._writer.write(data)
                self._writer.flush()
            except (OSError, ValueError) as exc:
                raise PeerClosed(str(exc)) from exc

    def notify(self, method: str, params: dict) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def request_async(self, method: str, params: dict) -> tuple[int, _Pending]:
        with self._lock:
            self._next_id += 1
            rid = self._next_id
            pending = _Pending()
            self._pending[rid] = pending
        try:
            self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        except PeerClosed:
            with self._lock:
                self._pending.pop(rid, None)
            raise
        return rid, pending

    def wait(self, rid: int, pending: _Pending, timeout: float | None) -> Any:
        if not pending.event.wait(timeout):
            raise TimeoutError(f"no response to request {rid} within {timeout}s")
        if pending.error is not None:
            raise pending.error
        return pending.result

    def request(self, method: str, params: dict, timeout: float | None = 60.0) -> Any:
        rid, pending = self.request_async(method, params)
        try:
            return self.wait(rid, pending, timeout)
        finally:
            with self._lock:
                self._pending.pop(rid, None)

    # -- incoming -----------------------------------------------------------

    def _respond(self, rid: Any, result: Any = None, error: RpcError | None = None) -> None:
        msg: dict[str, Any] = {"jsonrpc": "2.0", "id": rid}
        if error is not None:
            msg["error"] = {"code": error.code, "message": error.message}
        else:
            msg["result"] = result
        try:
            self._send(msg)
        except PeerClosed:
            pass

    def _handle_request(self, rid: Any, method: str, params: dict) -> None:
        try:
            result = self._on_request(method, params)
        except RpcError as exc:
            self._respond(rid, error=exc)
        except Exception as exc:  # never leave the agent waiting
            logger.debug("devin acp: request handler for %s failed", method, exc_info=True)
            self._respond(rid, error=RpcError(INTERNAL_ERROR, f"client error: {exc}"))
        else:
            self._respond(rid, result=result)

    def _dispatch(self, msg: dict) -> None:
        if "method" in msg:
            method = str(msg.get("method"))
            params = msg.get("params") if isinstance(msg.get("params"), dict) else {}
            if "id" in msg and msg["id"] is not None:
                threading.Thread(
                    target=self._handle_request,
                    args=(msg["id"], method, params),
                    name=f"devin-acp-{method}",
                    daemon=True,
                ).start()
            else:
                try:
                    self._on_notification(method, params)
                except Exception:
                    logger.debug("devin acp: notification handler failed", exc_info=True)
            return
        rid = msg.get("id")
        with self._lock:
            pending = self._pending.get(rid) if isinstance(rid, int) else None
        if pending is None:
            return
        if "error" in msg and msg["error"] is not None:
            err = msg["error"] if isinstance(msg["error"], dict) else {}
            pending.error = RpcError(
                int(err.get("code", INTERNAL_ERROR)), str(err.get("message", "error")), err.get("data")
            )
        else:
            pending.result = msg.get("result")
        pending.event.set()

    def _read_loop(self) -> None:
        try:
            while True:
                line = self._reader.readline(MAX_LINE_BYTES + 1)
                if not line:
                    break
                if len(line) > MAX_LINE_BYTES:
                    # Drain the rest of the oversized line.
                    while line and not line.endswith(b"\n"):
                        line = self._reader.readline(MAX_LINE_BYTES)
                    if self._on_garbage:
                        self._on_garbage("[oversized line dropped]")
                    continue
                text = line.decode("utf-8", errors="replace").strip()
                if not text:
                    continue
                try:
                    msg = json.loads(text)
                except ValueError:
                    if self._on_garbage:
                        self._on_garbage(text)
                    continue
                if isinstance(msg, dict):
                    self._dispatch(msg)
        except (OSError, ValueError):
            pass
        finally:
            self.closed.set()
            with self._lock:
                pending = list(self._pending.values())
            for item in pending:
                if not item.event.is_set():
                    item.error = PeerClosed("agent closed the connection")
                    item.event.set()
            if self._on_closed:
                try:
                    self._on_closed()
                except Exception:
                    pass
