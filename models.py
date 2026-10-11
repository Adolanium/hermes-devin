"""Run data model.

A :class:`Run` is the single record of one Devin invocation: what was asked,
where, under which permission mode, who approved it, what happened while it ran
and how it ended. Everything a status view needs is on it, and
:meth:`Run.snapshot` turns it into plain JSON so the same shape can go to the
model, to disk and over the event stream to a Desktop panel.

State machine::

    created -> starting -> running -> succeeded
        |          |          |   \\-> failed
        |          |          |   \\-> timed_out
        |          |          \\-> cancelling -> cancelled
        \\----------\\--> failed / cancelled / timed_out

Terminal states are final; :meth:`Run.transition` refuses anything else so a
late reader thread cannot resurrect a cancelled run.
"""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

SCHEMA_VERSION = 1


class RunState(str, Enum):
    CREATED = "created"
    STARTING = "starting"
    RUNNING = "running"
    CANCELLING = "cancelling"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"

    @property
    def terminal(self) -> bool:
        return self in TERMINAL_STATES


TERMINAL_STATES = frozenset({RunState.SUCCEEDED, RunState.FAILED, RunState.CANCELLED, RunState.TIMED_OUT})
ACTIVE_STATES = frozenset({RunState.CREATED, RunState.STARTING, RunState.RUNNING, RunState.CANCELLING})

_ALLOWED: dict[RunState, frozenset[RunState]] = {
    RunState.CREATED: frozenset({RunState.STARTING, RunState.FAILED, RunState.CANCELLED, RunState.CANCELLING}),
    RunState.STARTING: frozenset({RunState.RUNNING, RunState.CANCELLING, RunState.FAILED, RunState.CANCELLED,
                                  RunState.TIMED_OUT}),
    RunState.RUNNING: frozenset({RunState.CANCELLING, RunState.SUCCEEDED, RunState.FAILED, RunState.TIMED_OUT,
                                 RunState.CANCELLED}),
    RunState.CANCELLING: frozenset({RunState.CANCELLED, RunState.TIMED_OUT, RunState.FAILED}),
}


class InvalidTransition(RuntimeError):
    pass


class ErrorCode(str, Enum):
    CLI_NOT_FOUND = "cli_not_found"
    NOT_LOGGED_IN = "not_logged_in"
    TIMEOUT = "timeout"
    EXIT_NONZERO = "exit_nonzero"
    PROTOCOL = "protocol_error"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"
    WORKSPACE_UNTRUSTED = "workspace_untrusted"
    REFUSED = "refused"
    PERMISSION_DENIED = "permission_denied"
    SPAWN_FAILED = "spawn_failed"
    INTERNAL = "internal_error"


@dataclass
class RunError:
    code: str
    message: str
    exit_code: int | None = None
    stderr_tail: str = ""

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.exit_code is not None:
            out["exit_code"] = self.exit_code
        if self.stderr_tail:
            out["stderr_tail"] = self.stderr_tail
        return out


@dataclass
class ApprovalRecord:
    """Who allowed the run to start, and how.

    ``via`` is ``pre_tool_call`` when the Hermes approval gate ran from this
    plugin's hook, ``direct_gate`` when the handler had to ask the gate itself
    (the call did not come through the normal tool path).
    """

    via: str
    rule_key: str
    summary: str
    requested_at: float
    decided_at: float
    choice: str = "approved"
    decided_by: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "via": self.via, "rule_key": self.rule_key, "summary": self.summary,
            "requested_at": self.requested_at, "decided_at": self.decided_at,
            "choice": self.choice, "decided_by": self.decided_by,
        }


@dataclass
class PermissionDecision:
    """One permission prompt Devin raised during a run and how it was answered."""

    request_id: str
    title: str
    kind: str
    decision: str  # allowed | denied | cancelled
    reason: str
    at: float

    def to_dict(self) -> dict[str, Any]:
        return {"request_id": self.request_id, "title": self.title, "kind": self.kind,
                "decision": self.decision, "reason": self.reason, "at": self.at}


@dataclass
class ProgressEvent:
    """One normalized progress item.

    ``kind`` is a small closed vocabulary so a UI can switch on it:
    ``message`` (agent text), ``thought``, ``tool_call``, ``tool_update``,
    ``plan``, ``mode``, ``usage``, ``permission``, ``status``, ``stderr``.
    ``data`` carries the kind-specific fields.
    """

    seq: int
    at: float
    kind: str
    text: str = ""
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"seq": self.seq, "at": self.at, "kind": self.kind}
        if self.text:
            out["text"] = self.text
        if self.data:
            out["data"] = self.data
        return out


def new_run_id() -> str:
    return "dv-" + uuid.uuid4().hex[:10]


class CappedText:
    """Accumulates text, keeping the head and the tail when it grows too big.

    The tail matters most (an agent's closing summary is at the end), so most
    of the budget goes there.
    """

    def __init__(self, limit: int):
        self.limit = max(100, int(limit))
        self._head_limit = self.limit // 5
        self._head: list[str] = []
        self._head_len = 0
        self._tail = ""
        self.total = 0

    def append(self, text: str) -> None:
        if not text:
            return
        self.total += len(text)
        if self._head_len < self._head_limit:
            take = text[: self._head_limit - self._head_len]
            self._head.append(take)
            self._head_len += len(take)
            text = text[len(take):]
            if not text:
                return
        tail_limit = self.limit - self._head_limit
        self._tail = (self._tail + text)[-tail_limit:]

    @property
    def truncated(self) -> bool:
        return self.total > self.limit

    def value(self) -> str:
        head = "".join(self._head)
        if not self.truncated:
            return head + self._tail
        dropped = self.total - len(head) - len(self._tail)
        return f"{head}\n\n[... {dropped} characters omitted ...]\n\n{self._tail}"


def clip(text: str, limit: int) -> tuple[str, bool]:
    """Keep ``text`` under ``limit`` chars, favouring the end. Returns (text, clipped)."""
    if len(text) <= limit:
        return text, False
    head = limit // 5
    tail = limit - head
    return f"{text[:head]}\n\n[... {len(text) - head - tail} characters omitted ...]\n\n{text[-tail:]}", True


@dataclass
class Run:
    id: str
    prompt: str
    title: str
    cwd: str
    backend: str
    permission_mode: str
    model: str
    background: bool
    timeout_seconds: int
    session_id: str = ""
    task_id: str = ""
    session_key: str = ""
    state: RunState = RunState.CREATED
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None
    updated_at: float = field(default_factory=time.time)
    pid: int | None = None
    devin_session_id: str = ""
    stop_reason: str = ""
    exit_code: int | None = None
    approval: ApprovalRecord | None = None
    permissions: list[PermissionDecision] = field(default_factory=list)
    usage: dict[str, Any] = field(default_factory=dict)
    error: RunError | None = None
    cancel_reason: str = ""
    kill_report: dict[str, Any] = field(default_factory=dict)
    events: list[ProgressEvent] = field(default_factory=list)
    events_dropped: int = 0
    max_events: int = 500
    tool_calls: dict[str, dict[str, Any]] = field(default_factory=dict)
    plan: list[dict[str, Any]] = field(default_factory=list)
    # Latest change per file (path -> unified diff record) and recent shell commands, for live views.
    diffs: dict[str, dict[str, Any]] = field(default_factory=dict)
    commands: dict[str, dict[str, Any]] = field(default_factory=dict)
    output: CappedText = field(default_factory=lambda: CappedText(200_000))
    notified: bool = False
    _seq: int = 0
    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False)
    done: threading.Event = field(default_factory=threading.Event, repr=False)
    # Set by the run manager once a finished run is fully recorded (persisted,
    # events published, completion notice sent). Waiters use this one.
    settled: threading.Event = field(default_factory=threading.Event, repr=False)

    # -- state ----------------------------------------------------------

    def transition(self, new: RunState) -> RunState:
        """Move to ``new``; raise :class:`InvalidTransition` when not allowed."""
        with self._lock:
            old = self.state
            if new == old:
                return old
            if new not in _ALLOWED.get(old, frozenset()):
                raise InvalidTransition(f"run {self.id}: {old.value} -> {new.value} is not allowed")
            self.state = new
            now = time.time()
            self.updated_at = now
            if new == RunState.RUNNING and self.started_at is None:
                self.started_at = now
            if new.terminal:
                self.finished_at = now
                self.done.set()
            return old

    def try_transition(self, new: RunState) -> bool:
        try:
            self.transition(new)
            return True
        except InvalidTransition:
            return False

    @property
    def active(self) -> bool:
        return self.state in ACTIVE_STATES

    # -- progress -------------------------------------------------------

    def add_event(self, kind: str, text: str = "", **data: Any) -> ProgressEvent:
        with self._lock:
            self._seq += 1
            event = ProgressEvent(seq=self._seq, at=time.time(), kind=kind, text=text[:4000],
                                  data={k: v for k, v in data.items() if v is not None})
            self.events.append(event)
            overflow = len(self.events) - self.max_events
            if overflow > 0:
                del self.events[:overflow]
                self.events_dropped += overflow
            self.updated_at = event.at
            return event

    def duration_seconds(self) -> float | None:
        start = self.started_at or self.created_at
        end = self.finished_at or time.time()
        return round(end - start, 2)

    # -- views ----------------------------------------------------------

    def summary(self) -> dict[str, Any]:
        """Small view for listings and event payloads."""
        with self._lock:
            out: dict[str, Any] = {
                "id": self.id, "title": self.title, "state": self.state.value,
                "background": self.background, "cwd": self.cwd,
                "permission_mode": self.permission_mode, "backend": self.backend,
                "created_at": self.created_at, "started_at": self.started_at,
                "finished_at": self.finished_at, "updated_at": self.updated_at,
                "duration_seconds": self.duration_seconds(),
                "tool_calls": len(self.tool_calls),
                "files_changed": self.files_changed()[:50],
                "permission_requests": len(self.permissions),
                "last_activity": self._last_activity(),
            }
            if self.usage:
                out["usage"] = dict(self.usage)
            if self.error:
                out["error"] = self.error.to_dict()
            return out

    def files_changed(self) -> list[str]:
        """Paths of files Devin edited, deleted or moved, in first-touched order."""
        files: list[str] = []
        with self._lock:
            for info in self.tool_calls.values():
                if info.get("kind") in ("edit", "delete", "move"):
                    for path in info.get("locations") or []:
                        if path not in files:
                            files.append(path)
        return files

    def _last_activity(self) -> str:
        for event in reversed(self.events):
            if event.kind in ("tool_call", "tool_update", "status", "plan", "permission"):
                return event.text[:200]
        return ""

    def snapshot(self, *, events: int | None = None, include_output: bool = True,
                 output_limit: int | None = None) -> dict[str, Any]:
        """Full JSON view. ``events`` limits how many recent progress events are included."""
        with self._lock:
            data = self.summary()
            data.update({
                "schema_version": SCHEMA_VERSION,
                "prompt": self.prompt,
                "model": self.model,
                "timeout_seconds": self.timeout_seconds,
                "session_id": self.session_id,
                "devin_session_id": self.devin_session_id,
                "pid": self.pid,
                "stop_reason": self.stop_reason,
                "exit_code": self.exit_code,
                "approval": self.approval.to_dict() if self.approval else None,
                "permissions": [p.to_dict() for p in self.permissions],
                "plan": list(self.plan),
                "diffs": [dict(d) for d in list(self.diffs.values())[-30:]],
                "commands": [dict(c) for c in list(self.commands.values())[-10:]],
                "cancel_reason": self.cancel_reason,
                "kill_report": dict(self.kill_report),
                "events_dropped": self.events_dropped,
                "output_chars": self.output.total,
                "output_truncated": self.output.truncated,
            })
            chosen = self.events if events is None else (self.events[-events:] if events > 0 else [])
            data["events"] = [e.to_dict() for e in chosen]
            if include_output:
                text = self.output.value()
                if output_limit is not None:
                    text, clipped = clip(text, output_limit)
                    data["output_truncated"] = data["output_truncated"] or clipped
                data["output"] = text
            return data
