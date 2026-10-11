"""The two ways of driving the Devin CLI.

``AcpBackend`` (default) runs ``devin acp`` and speaks the Agent Client
Protocol: JSON-RPC over stdio with a ``session/update`` notification for every
message chunk, tool call, plan change and usage report. That gives live,
structured progress, a protocol-level cancel (``session/cancel``) and a
``session/request_permission`` callback the plugin answers itself, so the
configured permission mode is enforced on this side too.

``PrintBackend`` runs ``devin -p --prompt-file <file>`` and treats stdout as
the answer. It exists for CLIs or environments where ACP is unavailable; its
progress is line-based ``message`` events, which fit the same event model.

Both backends only talk to a :class:`RunIO`, which owns the run record, the
event stream and cancellation, so a third backend (Devin's cloud API, say) can
be added without touching anything else.
"""

from __future__ import annotations

import difflib
import logging
import os
import re
import tempfile
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from .jsonrpc import METHOD_NOT_FOUND, JsonRpcPeer, PeerClosed, RpcError
from .models import ErrorCode, Run, RunError, RunState
from .session_controls import ControlsError, apply_options, model_options, option_values

logger = logging.getLogger(__name__)

ACP_PROTOCOL_VERSION = 1
CLIENT_INFO = {"name": "hermes-devin", "title": "Hermes Agent", "version": "0.1.0"}

_AUTH_PATTERNS = re.compile(
    r"not (?:logged|signed) in|unauthenticated|authentication required|auth(?:entication)? required|"
    r"please (?:log|sign) in|devin auth login|no (?:stored )?credentials|login required",
    re.IGNORECASE,
)
_TRUST_PATTERNS = re.compile(r"workspace trust|untrusted (?:directory|workspace)|not trusted", re.IGNORECASE)
# What `devin --print` writes to stderr for each action its permission mode
# would have asked about. It still exits 0.
_PRINT_REJECTED = re.compile(r"rejected a tool call that requires confirmation", re.IGNORECASE)

# Devin permission modes as the CLI spells them, for DEVIN_PERMISSION_MODE / --permission-mode.
CLI_PERMISSION_MODE = {
    "plan": "normal",
    "normal": "normal",
    "accept-edits": "accept-edits",
    "smart": "smart",
    "dangerous": "dangerous",
    "autonomous": "autonomous",
}

# Names that identify the matching ACP session mode advertised by the agent.
# Matched exactly against a mode's id or name, never as substrings: "auto"
# must not pick an "autonomous" mode.
_MODE_HINTS = {
    "plan": ("plan",),
    "normal": ("normal", "default", "auto"),
    "accept-edits": ("accept-edits", "accept_edits", "acceptedits", "accept edits"),
    "smart": ("smart",),
    "dangerous": ("bypass", "dangerous", "yolo"),
    "autonomous": ("autonomous",),
}

_EDIT_KINDS = {"edit", "delete", "move"}
_READ_KINDS = {"read", "search", "think", "fetch"}


@dataclass
class Outcome:
    state: RunState
    stop_reason: str = ""
    error: RunError | None = None
    exit_code: int | None = None
    kill_report: dict[str, Any] = field(default_factory=dict)


class RunIO(Protocol):
    run: Run
    argv: list[str]
    env: dict[str, str]
    cancel_event: threading.Event
    deadline: float
    grace: float
    trust_workspace: bool
    sandbox: bool
    model: str
    session_options: dict[str, str]
    permission_requests: str

    def emit(self, kind: str, text: str = "", **data: Any) -> None: ...
    def append_output(self, text: str) -> None: ...
    def mark_running(self) -> None: ...
    def ask_permission(self, title: str, kind: str, detail: dict) -> tuple[bool, str]: ...
    def record_permission(self, title: str, kind: str, allowed: bool, reason: str, *, request_id: str = "") -> None: ...
    def spawn(self, argv: list[str], *, stdin_pipe: bool) -> Any: ...
    def cancel_reason(self) -> str: ...
    def cancel(self, reason: str) -> None: ...


def looks_unauthenticated(text: str) -> bool:
    return bool(text and _AUTH_PATTERNS.search(text))


class _StderrTail:
    def __init__(self, stream: Any, limit: int = 8000):
        self._stream = stream
        self._limit = limit
        self.text = ""
        self._thread = threading.Thread(target=self._loop, name="devin-stderr", daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        try:
            while True:
                reader = getattr(self._stream, "read1", None) or self._stream.read
                chunk = reader(4096)
                if not chunk:
                    break
                self.text = (self.text + chunk.decode("utf-8", errors="replace"))[-self._limit :]
        except (OSError, ValueError):
            pass

    def join(self, timeout: float = 2.0) -> str:
        self._thread.join(timeout)
        return self.text.strip()


def _wait_or_stop(io: RunIO, done: Callable[[], bool], *, poll: float = 0.1) -> str:
    """Block until ``done()``; return ``done``, ``cancelled`` or ``timeout``."""
    while True:
        if done():
            return "done"
        if io.cancel_event.is_set():
            return "cancelled"
        if time.monotonic() >= io.deadline:
            return "timeout"
        io.cancel_event.wait(poll)


def _finish_stopped(io: RunIO, why: str, kill: dict[str, Any], stderr: str) -> Outcome:
    if why == "timeout":
        return Outcome(
            RunState.TIMED_OUT,
            error=RunError(
                ErrorCode.TIMEOUT.value,
                f"Devin did not finish within {io.run.timeout_seconds}s and was stopped",
                stderr_tail=stderr[-2000:],
            ),
            kill_report=kill,
        )
    reason = io.cancel_reason() or "cancelled"
    code = ErrorCode.INTERRUPTED if reason.startswith("interrupt") else ErrorCode.CANCELLED
    return Outcome(
        RunState.CANCELLED,
        stop_reason="cancelled",
        error=RunError(code.value, f"Run stopped: {reason}"),
        kill_report=kill,
    )


# --- ACP ----------------------------------------------------------------------


def _text_of(content: Any) -> str:
    if isinstance(content, dict):
        if content.get("type") == "text":
            return str(content.get("text") or "")
        if "content" in content:
            return _text_of(content["content"])
        return ""
    if isinstance(content, list):
        return "".join(_text_of(item) for item in content)
    return ""


def _merge_paths(paths: list[str], update: dict) -> list[str]:
    """Add the file paths a tool call touches: its ``locations`` and the
    ``path`` of any ``diff`` content (Devin reports edits only that way)."""
    found = [loc.get("path") for loc in update.get("locations") or [] if isinstance(loc, dict)]
    found += [
        item.get("path")
        for item in update.get("content") or []
        if isinstance(item, dict) and item.get("type") == "diff"
    ]
    merged = list(paths)
    for path in found:
        if isinstance(path, str) and path and path not in merged:
            merged.append(path)
    return merged[:20]


_DIFF_MAX_LINES = 160
_COMMAND_OUTPUT_CHARS = 4000


def unified_diff(path: str, old: str, new: str) -> dict[str, Any]:
    """A compact unified diff of one edit, capped at ``_DIFF_MAX_LINES`` lines."""
    name = os.path.basename(path) or path
    lines = list(difflib.unified_diff(old.splitlines(), new.splitlines(), f"a/{name}", f"b/{name}", n=2, lineterm=""))[
        2:
    ]
    added = sum(1 for line in lines if line.startswith("+"))
    removed = sum(1 for line in lines if line.startswith("-"))
    truncated = len(lines) > _DIFF_MAX_LINES
    return {"patch": "\n".join(lines[:_DIFF_MAX_LINES]), "added": added, "removed": removed, "truncated": truncated}


def _pick_option(options: list[dict], allow: bool) -> str | None:
    wanted = ("allow_once", "allow_always") if allow else ("reject_once", "reject_always")
    for kind in wanted:
        for opt in options:
            if isinstance(opt, dict) and opt.get("kind") == kind and opt.get("optionId"):
                return str(opt["optionId"])
    return None


class AcpBackend:
    name = "acp"

    def __init__(self) -> None:
        self._session_id = ""
        self._peer: JsonRpcPeer | None = None
        # Set by tool activity: Devin's next message text starts a new paragraph.
        self._paragraph_break = False
        # True while session/load replays an earlier conversation, which is not this run's output.
        self._replaying = False

    # -- argv / env -----------------------------------------------------------

    @staticmethod
    def build_argv(io: RunIO) -> list[str]:
        argv = list(io.argv)
        if io.sandbox and io.run.execution_target != "cloud":
            argv.append("--sandbox")
        argv.append("acp")
        if io.run.execution_target == "cloud":
            argv.append("--cloud")
        elif io.model:
            argv += ["--model", io.model]
        return argv

    # -- protocol callbacks -------------------------------------------------

    def _on_update(self, io: RunIO, params: dict) -> None:
        if params.get("sessionId") and self._session_id and params["sessionId"] != self._session_id:
            return
        if self._replaying:
            return
        update = params.get("update") if isinstance(params.get("update"), dict) else {}
        kind = update.get("sessionUpdate")
        run = io.run
        if kind == "config_option_update":
            for option in update.get("configOptions") or []:
                if isinstance(option, dict) and option.get("id") == "mode":
                    self._observe_mode(io, option.get("currentValue"))
            options = model_options(update.get("configOptions") or [], target=run.execution_target)
            if getattr(self, "_config_ready", False):
                required = getattr(self, "_selected_values", {})
                confirmed = option_values(options)
                if any(confirmed.get(key) != value for key, value in required.items()):
                    with run._lock:
                        run.model_confirmed = False
                    io.cancel("Devin changed the approved model configuration; no fallback was authorized")
                    return
            self._capture_config(io, options)
        elif kind == "agent_message_chunk":
            text = _text_of(update.get("content"))
            if text:
                if self._paragraph_break and run.output.value().strip():
                    text = "\n\n" + text.lstrip("\n")
                self._paragraph_break = False
                io.append_output(text)
                io.emit("message", text)
        elif kind == "agent_thought_chunk":
            text = _text_of(update.get("content"))
            if text:
                io.emit("thought", text)
        elif kind == "tool_call":
            self._paragraph_break = True
            call_id = str(update.get("toolCallId") or "")
            info = {
                "title": str(update.get("title") or ""),
                "kind": str(update.get("kind") or "other"),
                "status": str(update.get("status") or "pending"),
                "locations": _merge_paths([], update),
            }
            raw = update.get("rawInput") if isinstance(update.get("rawInput"), dict) else {}
            if info["kind"] == "execute" and isinstance(raw.get("command"), str):
                info["command"] = raw["command"][:2000]
            with run._lock:
                run.tool_calls[call_id] = info
            io.emit(
                "tool_call",
                info["title"],
                tool_call_id=call_id,
                tool_kind=info["kind"],
                status=info["status"],
                locations=info["locations"] or None,
            )
            self._capture_content(io, call_id, info, update)
        elif kind == "tool_call_update":
            call_id = str(update.get("toolCallId") or "")
            with run._lock:
                info = run.tool_calls.setdefault(
                    call_id, {"title": "", "kind": "other", "status": "pending", "locations": []}
                )
                for key in ("title", "kind", "status"):
                    if update.get(key):
                        info[key] = str(update[key])
                info["locations"] = _merge_paths(info.get("locations") or [], update)
            self._capture_content(io, call_id, info, update)
            if update.get("status") in ("completed", "failed"):
                io.emit("tool_update", info.get("title", ""), tool_call_id=call_id, status=update.get("status"))
        elif kind == "plan":
            entries = [
                {
                    "content": str(e.get("content") or ""),
                    "status": str(e.get("status") or ""),
                    "priority": str(e.get("priority") or ""),
                }
                for e in update.get("entries") or []
                if isinstance(e, dict)
            ][:100]
            with run._lock:
                run.plan = entries
            done = sum(1 for e in entries if e["status"] == "completed")
            io.emit("plan", f"plan: {done}/{len(entries)} done", entries=entries)
        elif kind == "current_mode_update":
            self._observe_mode(io, update.get("currentModeId"))
            io.emit("mode", str(update.get("currentModeId") or ""))
        elif kind == "usage_update":
            usage: dict[str, Any] = {}
            for src, dst in (("used", "context_used"), ("size", "context_size")):
                if isinstance(update.get(src), (int, float)):
                    usage[dst] = update[src]
            cost = update.get("cost")
            if isinstance(cost, dict) and isinstance(cost.get("amount"), (int, float)):
                usage["cost"] = {"amount": cost["amount"], "currency": str(cost.get("currency") or "")}
            if usage:
                with run._lock:
                    run.usage.update(usage)
                io.emit("usage", "", **usage)
        elif kind == "session_info_update":
            title = update.get("title")
            if isinstance(title, str) and title:
                io.emit("status", f"session title: {title}")

    def _load_session(self, io: RunIO, peer: JsonRpcPeer, session_id: str, timeout: float) -> dict:
        """Resume an earlier Devin session (a follow-up) without replaying it into this run.

        ``session/load`` replays the old conversation as ``session/update``
        notifications. They are dropped until a throwaway request comes back:
        the peer handles notifications in order on its reader thread, so by then
        every replayed update has been seen.
        """
        self._replaying = True
        try:
            try:
                session = peer.request(
                    "session/load",
                    {
                        "sessionId": session_id,
                        "cwd": "/" if io.run.execution_target == "cloud" else io.run.cwd,
                        "mcpServers": [],
                    },
                    timeout=timeout,
                )
            except RpcError as exc:
                raise RuntimeError(f"Devin could not resume session {session_id}: {exc.message}") from exc
            try:
                peer.request("_hermes_devin/replay_barrier", {}, timeout=10)
            except (RpcError, TimeoutError):
                pass
        finally:
            self._replaying = False
        session = dict(session) if isinstance(session, dict) else {}
        session.setdefault("sessionId", session_id)
        io.emit("status", f"resumed Devin session {session_id}")
        return session

    def _capture_content(self, io: RunIO, call_id: str, info: dict, update: dict) -> None:
        """Turn a tool call's content into live views: file diffs and shell output.

        Devin sends each edit as ``diff`` content (``oldText`` is absent for a new
        file) and a running command's output as a growing text snapshot, with the
        exit code in ``_meta.terminal_exit``.
        """
        run = io.run
        content = [item for item in update.get("content") or [] if isinstance(item, dict)]
        for item in content:
            path, new = item.get("path"), item.get("newText")
            if item.get("type") != "diff" or not isinstance(path, str) or not path or not isinstance(new, str):
                continue
            old = item.get("oldText") if isinstance(item.get("oldText"), str) else ""
            record = {
                "path": path,
                "tool_call_id": call_id,
                "new_file": item.get("oldText") is None,
                **unified_diff(path, old, new),
            }
            with run._lock:
                previous = run.diffs.get(path)
                if previous is not None and previous["patch"] == record["patch"]:
                    continue
                run.diffs.pop(path, None)
                run.diffs[path] = record
            verb = "Created" if record["new_file"] else "Edited"
            io.emit("diff", f"{verb} {os.path.basename(path)} (+{record['added']} -{record['removed']})", **record)

        if info.get("kind") != "execute":
            return
        texts = [
            _text_of(item.get("content"))
            for item in content
            if item.get("type") == "content"
            and isinstance(item.get("content"), dict)
            and item["content"].get("type") == "text"
        ]
        meta = update.get("_meta") if isinstance(update.get("_meta"), dict) else {}
        exit_info = meta.get("terminal_exit") if isinstance(meta.get("terminal_exit"), dict) else {}
        status = update.get("status") if isinstance(update.get("status"), str) else ""
        if not texts and "exit_code" not in exit_info and not info.get("command"):
            return
        with run._lock:
            command = run.commands.get(call_id)
            created = command is None
            if created:
                command = {
                    "tool_call_id": call_id,
                    "command": info.get("command") or info.get("title") or "",
                    "output": "",
                    "exit_code": None,
                    "status": "running",
                }
                run.commands[call_id] = command
                while len(run.commands) > 10:
                    run.commands.pop(next(iter(run.commands)))
            before = dict(command)
            if texts:
                command["output"] = texts[-1][-_COMMAND_OUTPUT_CHARS:]
            if "exit_code" in exit_info:
                command["exit_code"] = exit_info.get("exit_code")
            if status in ("completed", "failed"):
                command["status"] = status
            changed = command != before
            snapshot = dict(command)
        if changed or created:
            io.emit("terminal", snapshot["command"][:200], **snapshot)

    def _on_request(self, io: RunIO, method: str, params: dict) -> Any:
        if io.run.execution_target == "cloud":
            if method == "session/request_permission":
                return {"outcome": {"outcome": "cancelled"}}
            raise RpcError(METHOD_NOT_FOUND, "Cloud sessions cannot access local tools or files")
        if method == "session/request_permission":
            return self._answer_permission(io, params)
        # We advertise no fs/terminal capabilities and cannot answer questions
        # meant for a human in a delegated run.
        raise RpcError(METHOD_NOT_FOUND, f"{method} is not supported by this client")

    def _answer_permission(self, io: RunIO, params: dict) -> dict:
        tool_call = params.get("toolCall") if isinstance(params.get("toolCall"), dict) else {}
        options = [o for o in params.get("options") or [] if isinstance(o, dict)]
        title = str(tool_call.get("title") or "an action")
        kind = str(tool_call.get("kind") or "other")
        allowed, reason = io.ask_permission(title, kind, {"tool_call_id": tool_call.get("toolCallId")})
        option = _pick_option(options, allowed)
        if option is None:
            # No matching option: cancelling the prompt is the safe answer either way.
            return {"outcome": {"outcome": "cancelled"}}
        return {"outcome": {"outcome": "selected", "optionId": option}}

    # -- run ----------------------------------------------------------------

    def _capture_config(self, io: RunIO, raw: Any) -> None:
        options = model_options(raw, target=io.run.execution_target)
        if not options and raw is None:
            return
        values = option_values(options)
        with io.run._lock:
            run = io.run
            run.config_options = options
            run.session_config = values
            model_id = "devin_version" if run.execution_target == "cloud" else "model"
            run.model_confirmed = model_id in values
            if model_id in values:
                model = next(option for option in options if option["id"] == model_id)
                choice = next((item for item in model["options"] if item["value"] == values[model_id]), {})
                if values[model_id] != run.model or not run.model_label:
                    run.model_label = choice.get("name") or values[model_id]
                    run.model_cost_summary = ""
                run.model = values[model_id]
        io.emit("config", "model configuration confirmed", values=values)

    def _observe_mode(self, io: RunIO, value: Any) -> None:
        if not isinstance(value, str):
            return
        self._observed_mode = value
        self._mode_revision = getattr(self, "_mode_revision", 0) + 1
        selected = getattr(self, "_selected_mode", None)
        if getattr(self, "_config_ready", False) and selected and selected != value:
            io.cancel("Devin changed the configured permission mode; no override was authorized")

    def _select_mode(self, io: RunIO, peer: JsonRpcPeer, session: dict, *, force: bool = False) -> None:
        modes = session.get("modes") if isinstance(session.get("modes"), dict) else {}
        available = [m for m in modes.get("availableModes") or [] if isinstance(m, dict) and m.get("id")]
        if not available:
            if force:
                raise RuntimeError("Devin did not advertise permission modes after model selection. No task was sent.")
            return
        wanted = io.run.permission_mode
        hints = _MODE_HINTS.get(wanted, (wanted,))
        match = None
        for mode in available:
            ident = str(mode.get("id")).lower()
            name = str(mode.get("name") or "").lower()
            if any(h == ident or h == name for h in hints):
                match = str(mode["id"])
                break
        current = str(modes.get("currentModeId") or "")
        if match is None:
            # Staying in the agent's current mode could grant more than the
            # configured one (Devin starts in accept-edits), so fail closed.
            io.emit("mode", current, note=f"agent advertises no mode matching '{wanted}'")
            offered = ", ".join(str(m["id"]) for m in available)
            raise RuntimeError(
                f"Devin offers no session mode matching permission_mode '{wanted}' over ACP "
                f"(offered: {offered}); refusing to run in '{current or 'its default mode'}' instead. "
                "Use backend 'print', which passes --permission-mode, or pick a mode Devin offers."
            )
        if match != current or force:
            revision = getattr(self, "_mode_revision", 0)
            peer.request("session/set_mode", {"sessionId": self._session_id, "modeId": match}, timeout=30)
            if getattr(self, "_mode_revision", 0) != revision and self._observed_mode != match:
                raise RuntimeError("Devin did not confirm the configured permission mode. No task was sent.")
        self._selected_mode = match
        io.emit("mode", match)

    def execute(self, io: RunIO) -> Outcome:
        argv = self.build_argv(io)
        io.emit("status", "starting devin acp", argv=argv[1:])
        tree = io.spawn(argv, stdin_pipe=True)
        stderr = _StderrTail(tree.proc.stderr)
        peer = JsonRpcPeer(
            tree.proc.stdout,
            tree.proc.stdin,
            on_notification=lambda m, p: self._on_update(io, p) if m == "session/update" else None,
            on_request=lambda m, p: self._on_request(io, m, p),
            on_garbage=lambda line: io.emit("stderr", line[:500]),
        )
        self._peer = peer
        peer.start()
        try:
            return self._drive(io, tree, peer, stderr)
        finally:
            if io.run.execution_target == "cloud" and io.run.remote_state in ("running", "cancel_requested"):
                io.run.remote_state = "unknown"
            # Normal exit or not, nothing Devin started may outlive the run.
            if tree.poll() is None:
                try:
                    tree.proc.stdin.close()
                except Exception:
                    pass
                if tree.wait(min(5.0, io.grace or 0.5)) is None:
                    tree.stop(grace=0.5)
            tree.reap_descendants()
            peer.join(2)
            stderr.join(1)
            tree.close_streams()

    def _setup_error(self, exc: BaseException, stderr: _StderrTail, tree: Any) -> Outcome:
        tail = stderr.join(1) if tree.wait(1.0) is not None else stderr.text
        text = f"{exc} {tail}"
        if (
            isinstance(exc, RpcError)
            and (exc.code == -32000 or looks_unauthenticated(exc.message))
            or looks_unauthenticated(tail)
        ):
            return Outcome(
                RunState.FAILED,
                error=RunError(
                    ErrorCode.NOT_LOGGED_IN.value,
                    "Devin CLI is not logged in. Run 'devin auth login' in a terminal, then try again.",
                    stderr_tail=tail[-2000:],
                ),
            )
        if _TRUST_PATTERNS.search(text):
            return Outcome(
                RunState.FAILED,
                error=RunError(
                    ErrorCode.WORKSPACE_UNTRUSTED.value,
                    "Devin refused the working directory because it is not trusted. Trust it in Devin, "
                    "or set trust_workspace: true in the plugin settings.",
                    stderr_tail=tail[-2000:],
                ),
            )
        exit_code = tree.poll()
        if exit_code is not None:
            return Outcome(
                RunState.FAILED,
                exit_code=exit_code,
                error=RunError(
                    ErrorCode.EXIT_NONZERO.value,
                    f"devin acp exited with code {exit_code} during startup",
                    exit_code=exit_code,
                    stderr_tail=tail[-2000:],
                ),
            )
        return Outcome(
            RunState.FAILED,
            error=RunError(
                ErrorCode.PROTOCOL.value, f"ACP handshake with Devin failed: {exc}", stderr_tail=tail[-2000:]
            ),
        )

    def _drive(self, io: RunIO, tree: Any, peer: JsonRpcPeer, stderr: _StderrTail) -> Outcome:
        def budget() -> float:
            return max(1.0, min(120.0, io.deadline - time.monotonic()))

        try:
            init = peer.request(
                "initialize",
                {
                    "protocolVersion": ACP_PROTOCOL_VERSION,
                    "clientCapabilities": {"fs": {"readTextFile": False, "writeTextFile": False}, "terminal": False},
                    "clientInfo": CLIENT_INFO,
                },
                timeout=budget(),
            )
            if isinstance(init, dict):
                agent = init.get("agentInfo") or {}
                io.emit(
                    "status",
                    "connected",
                    protocol_version=init.get("protocolVersion"),
                    agent=agent.get("name") if isinstance(agent, dict) else None,
                    agent_version=agent.get("version") if isinstance(agent, dict) else None,
                )
            resume = io.run.resume_session_id
            cloud = io.run.execution_target == "cloud"
            if cloud and not io.run.cloud_permissions_ack:
                raise ControlsError("Cloud-managed permissions were not confirmed. No task was sent.")
            if resume:
                if cloud and not init.get("agentCapabilities", {}).get("loadSession"):
                    raise ControlsError("The Cloud server does not support reconnecting this session.")
                session = self._load_session(io, peer, resume, budget())
            else:
                session = peer.request(
                    "session/new", {"cwd": "/" if cloud else io.run.cwd, "mcpServers": []}, timeout=budget()
                )
            if not isinstance(session, dict) or not session.get("sessionId"):
                raise RpcError(-32603, "session/new returned no sessionId")
            self._session_id = str(session["sessionId"])
            with io.run._lock:
                io.run.devin_session_id = self._session_id
            self._config_ready = False
            values = dict(getattr(io, "session_options", {}))
            if io.model and model_options(session.get("configOptions")):
                values.setdefault("model", io.model)
            self._selected_values = values
            options = apply_options(
                peer,
                self._session_id,
                session.get("configOptions"),
                values,
                timeout=budget,
                target=io.run.execution_target,
            )
            self._capture_config(io, options)
            if any(io.run.session_config.get(key) != value for key, value in values.items()):
                raise ControlsError("Devin changed a requested model setting. No task was sent; choose again.")
            # Model setters must not be able to leave the agent in a wider mode.
            if not cloud:
                self._select_mode(io, peer, session, force=bool(values))
            if any(io.run.session_config.get(key) != value for key, value in values.items()):
                raise ControlsError("Devin changed model settings while applying permissions. No task was sent.")
            self._config_ready = True
        except (RpcError, PeerClosed, TimeoutError, RuntimeError, ControlsError) as exc:
            if io.cancel_event.is_set() or time.monotonic() >= io.deadline:
                kill = tree.stop(grace=io.grace).to_dict()
                return _finish_stopped(
                    io, "timeout" if not io.cancel_event.is_set() else "cancelled", kill, stderr.text
                )
            if isinstance(exc, ControlsError):
                return Outcome(RunState.FAILED, error=RunError(exc.code, str(exc)))
            if isinstance(exc, RuntimeError) and not isinstance(exc, (RpcError, PeerClosed, TimeoutError)):
                return Outcome(RunState.FAILED, error=RunError(ErrorCode.REFUSED.value, str(exc)))
            return self._setup_error(exc, stderr, tree)

        if io.cancel_event.is_set() or time.monotonic() >= io.deadline:
            kill = tree.stop(grace=io.grace).to_dict()
            return _finish_stopped(io, "cancelled" if io.cancel_event.is_set() else "timeout", kill, stderr.text)
        if cloud:
            io.run.remote_state = "running"
        io.mark_running()
        rid, pending = peer.request_async(
            "session/prompt",
            {
                "sessionId": self._session_id,
                "prompt": [{"type": "text", "text": io.run.prompt}],
            },
        )
        why = _wait_or_stop(io, pending.event.is_set)
        if why != "done":
            if cloud:
                io.run.remote_state = "cancel_requested"

            def soft() -> None:
                # ACP's cancel: the agent ends the turn with stopReason "cancelled".
                # Then end the connection (stdin EOF) so the agent process exits on
                # its own; whatever is still running afterwards is killed.
                peer.notify("session/cancel", {"sessionId": self._session_id})
                pending.event.wait(max(0.5, io.grace / 2))
                try:
                    tree.proc.stdin.close()
                except Exception:
                    pass

            # Let the agent wind down its turn before the tree is killed.
            kill = tree.stop(grace=io.grace, soft_stop=soft if not peer.closed.is_set() else None).to_dict()
            if cloud:
                confirmed = (
                    pending.event.is_set()
                    and pending.error is None
                    and isinstance(pending.result, dict)
                    and pending.result.get("stopReason") == "cancelled"
                )
                io.run.remote_state = "cancelled" if confirmed else "unknown"
                kill["remote_cancel_confirmed"] = confirmed
                if not confirmed:
                    return Outcome(
                        RunState.FAILED,
                        stop_reason="cloud_stop_unconfirmed",
                        kill_report=kill,
                        error=RunError(
                            "cloud_stop_unconfirmed",
                            "Local relay stopped, but Cloud cancellation was not confirmed. Remote work may still be running.",
                        ),
                    )
            return _finish_stopped(io, why, kill, stderr.text)

        if pending.error is not None:
            if cloud:
                io.run.remote_state = "unknown"
            err = pending.error
            tail = stderr.join(1) if tree.wait(0.5) is not None else stderr.text
            if isinstance(err, RpcError) and (err.code == -32000 or looks_unauthenticated(err.message)):
                return Outcome(
                    RunState.FAILED,
                    error=RunError(
                        ErrorCode.NOT_LOGGED_IN.value,
                        "Devin CLI is not logged in. Run 'devin auth login' in a terminal, then try again.",
                        stderr_tail=tail[-2000:],
                    ),
                )
            exit_code = tree.poll()
            if isinstance(err, PeerClosed) and exit_code not in (None, 0):
                return Outcome(
                    RunState.FAILED,
                    exit_code=exit_code,
                    error=RunError(
                        ErrorCode.EXIT_NONZERO.value,
                        f"devin acp exited with code {exit_code}",
                        exit_code=exit_code,
                        stderr_tail=tail[-2000:],
                    ),
                )
            return Outcome(
                RunState.FAILED,
                exit_code=exit_code,
                error=RunError(ErrorCode.PROTOCOL.value, f"Devin run failed: {err}", stderr_tail=tail[-2000:]),
            )

        result = pending.result if isinstance(pending.result, dict) else {}
        stop = str(result.get("stopReason") or "end_turn")
        usage = result.get("usage")
        if isinstance(usage, dict):
            clean = {k: v for k, v in usage.items() if isinstance(v, (int, float))}
            if clean:
                with io.run._lock:
                    io.run.usage.update({"tokens": clean})
                io.emit("usage", "", tokens=clean)
        if stop == "cancelled":
            if cloud:
                io.run.remote_state = "cancelled"
            return Outcome(
                RunState.CANCELLED,
                stop_reason=stop,
                error=RunError(ErrorCode.CANCELLED.value, "Devin reported the turn as cancelled"),
            )
        if cloud:
            io.run.remote_state = "idle"
        if stop == "refusal":
            return Outcome(
                RunState.FAILED,
                stop_reason=stop,
                error=RunError(ErrorCode.REFUSED.value, "Devin refused to continue this task"),
            )
        return Outcome(RunState.SUCCEEDED, stop_reason=stop)


# --- print mode ---------------------------------------------------------------------


class PrintBackend:
    name = "print"

    def __init__(self, temp_dir: str | None = None) -> None:
        self._temp_dir = temp_dir

    @staticmethod
    def build_argv(io: RunIO, prompt_file: str) -> list[str]:
        argv = list(io.argv)
        argv += [
            "--print",
            "--prompt-file",
            prompt_file,
            "--permission-mode",
            CLI_PERMISSION_MODE[io.run.permission_mode],
        ]
        if io.model:
            argv += ["--model", io.model]
        if io.sandbox:
            argv.append("--sandbox")
        if io.trust_workspace:
            argv += ["--respect-workspace-trust", "false"]
        return argv

    def execute(self, io: RunIO) -> Outcome:
        fd, prompt_file = tempfile.mkstemp(prefix="hermes-devin-", suffix=".md", dir=self._temp_dir)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(io.run.prompt)
            argv = self.build_argv(io, prompt_file)
            io.emit("status", "starting devin --print", argv=[a for a in argv[1:] if a != prompt_file])
            tree = io.spawn(argv, stdin_pipe=False)
            try:
                return self._drive(io, tree)
            finally:
                if tree.poll() is None:
                    tree.stop(grace=0.5)
                tree.reap_descendants()
                tree.close_streams()
        finally:
            try:
                os.unlink(prompt_file)
            except OSError:
                pass

    def _drive(self, io: RunIO, tree: Any) -> Outcome:
        stderr = _StderrTail(tree.proc.stderr)
        eof = threading.Event()

        def pump() -> None:
            try:
                while True:
                    line = tree.proc.stdout.readline(1 << 20)
                    if not line:
                        break
                    text = line.decode("utf-8", errors="replace")
                    io.append_output(text)
                    if text.strip():
                        io.emit("message", text.rstrip("\r\n"))
            except (OSError, ValueError):
                pass
            finally:
                eof.set()

        reader = threading.Thread(target=pump, name="devin-print-stdout", daemon=True)
        reader.start()
        io.mark_running()
        why = _wait_or_stop(io, lambda: eof.is_set() and tree.poll() is not None)
        if why != "done":
            kill = tree.stop(grace=io.grace).to_dict()
            reader.join(2)
            return _finish_stopped(io, why, kill, stderr.text)
        reader.join(2)
        tail = stderr.join(2)
        code = tree.poll()
        if code == 0:
            return self._success_or_denied(io, tail)
        out_tail = io.run.output.value()[-2000:]
        if looks_unauthenticated(tail) or looks_unauthenticated(out_tail):
            return Outcome(
                RunState.FAILED,
                exit_code=code,
                error=RunError(
                    ErrorCode.NOT_LOGGED_IN.value,
                    "Devin CLI is not logged in. Run 'devin auth login' in a terminal, then try again.",
                    exit_code=code,
                    stderr_tail=tail[-2000:],
                ),
            )
        if _TRUST_PATTERNS.search(tail) or _TRUST_PATTERNS.search(out_tail):
            return Outcome(
                RunState.FAILED,
                exit_code=code,
                error=RunError(
                    ErrorCode.WORKSPACE_UNTRUSTED.value,
                    "Devin refused the working directory because it is not trusted. Trust it in Devin, "
                    "or set trust_workspace: true in the plugin settings.",
                    exit_code=code,
                    stderr_tail=tail[-2000:],
                ),
            )
        return Outcome(
            RunState.FAILED,
            exit_code=code,
            error=RunError(
                ErrorCode.EXIT_NONZERO.value, f"devin exited with code {code}", exit_code=code, stderr_tail=tail[-2000:]
            ),
        )

    @staticmethod
    def _success_or_denied(io: RunIO, tail: str) -> Outcome:
        # Print mode cannot ask, so Devin rejects whatever its mode does not
        # cover and exits 0. Record each rejection; with no answer either, the
        # run did not do its task.
        rejected = len(_PRINT_REJECTED.findall(tail))
        mode = io.run.permission_mode
        for _ in range(rejected):
            io.record_permission(
                "an action that needed confirmation", "other", False, f"print mode cannot ask; permission mode '{mode}'"
            )
        if rejected and not io.run.output.value().strip():
            return Outcome(
                RunState.FAILED,
                stop_reason="exit",
                exit_code=0,
                error=RunError(
                    ErrorCode.PERMISSION_DENIED.value,
                    f"Devin stopped without an answer after {rejected} action(s) needed confirmation, "
                    f"which print mode cannot ask for. Permission mode '{mode}' does not allow them; "
                    "choose a wider mode such as accept-edits if Devin should act on its own.",
                    exit_code=0,
                    stderr_tail=tail[-2000:],
                ),
            )
        return Outcome(RunState.SUCCEEDED, stop_reason="exit", exit_code=0)


def permission_policy(mode: str, kind: str) -> bool | None:
    """Static answer for a permission prompt under ``mode``; ``None`` means ask a human.

    ``plan`` never allows anything that needed a prompt. ``accept-edits`` and
    ``smart`` pre-allow workspace edits (what those modes mean in Devin),
    ``dangerous`` allows everything. Everything else goes to a person.
    """
    kind = (kind or "other").lower()
    if mode == "plan":
        return True if kind in _READ_KINDS else False
    if mode == "dangerous":
        return True
    if mode in ("accept-edits", "smart") and kind in _EDIT_KINDS:
        return True
    return None
