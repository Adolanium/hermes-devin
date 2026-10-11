"""Human approval before any Devin run starts.

Primary path: the plugin's ``pre_tool_call`` hook answers a
``delegate_to_devin`` call with ``{"action": "approve", ...}``. Hermes then runs
its own approval gate (CLI prompt, gateway buttons, Desktop dialog), and only
calls the tool handler if a person said yes. The model cannot skip it, and
Hermes fails closed when nobody is present to answer.

The hook also leaves a single-use *ticket* for that exact call. The handler
consumes it. If the handler runs without a ticket (another plugin dispatching
the tool directly, or a host path that skipped hooks), it asks the same gate
itself through ``tools.approval.request_tool_approval``. No ticket and no gate
means no run.

The hook also blocks calls that could never run (bad settings, Devin CLI
missing, concurrency limit reached), so the user is not asked to approve
something that will fail anyway.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .config import DevinConfig
from .models import ApprovalRecord

logger = logging.getLogger(__name__)

TOOL_NAME = "delegate_to_devin"
ALLOW_CHOICES = {"once", "session", "always", "approved", "allow", "yes"}
TICKET_TTL_SECONDS = 15 * 60
EXCERPT_CHARS = 1500


def fingerprint(args: dict[str, Any], cfg: DevinConfig | None = None) -> str:
    """Stable hash of the arguments a ticket was issued for."""
    relevant = {k: args.get(k) for k in ("prompt", "cwd", "background", "timeout_seconds", "title", "launch_id")}
    if cfg is not None:
        from .session_controls import config_fingerprint

        relevant["config"] = config_fingerprint(cfg)
    blob = json.dumps(relevant, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def rule_key_for(cfg: DevinConfig, prompt: str, cwd: str) -> str:
    """Grain for Hermes' "always/session" allowlist.

    The permission mode and backend are always part of it, so approving a
    normal-mode run never pre-approves a dangerous one. ``approval_scope: run``
    (default) also hashes the prompt, so a remembered approval only covers the
    identical request; ``workspace`` covers any prompt in that directory.
    """
    from .session_controls import config_fingerprint

    parts = [cfg.permission_mode, cfg.backend, cwd.lower() if cwd else "", config_fingerprint(cfg)]
    if cfg.approval_scope == "run":
        parts.append(prompt)
    digest = hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()[:16]
    scope = "run" if cfg.approval_scope == "run" else "ws"
    return f"devin:{scope}:{cfg.permission_mode}:{digest}"


def approval_message(cfg: DevinConfig, *, prompt: str, cwd: str, background: bool, timeout: int) -> str:
    options = getattr(cfg, "session_options", ())
    label = getattr(cfg, "model_label", "") or cfg.model or "configured CLI default"
    pricing = getattr(cfg, "model_cost_summary", "")
    excerpt = " ".join(prompt.split())
    if len(excerpt) > EXCERPT_CHARS:
        # Say how much is hidden, so a long prompt cannot tuck instructions out of view unnoticed.
        excerpt = (
            f"{excerpt[:EXCERPT_CHARS]}... [prompt continues: {len(prompt)} characters in total, "
            f"first {EXCERPT_CHARS} shown]"
        )
    mode_note = {
        "plan": "read-only planning",
        "normal": "Devin asks before edits and commands",
        "accept-edits": "file edits auto-approved, commands need approval",
        "smart": "edits auto-approved, other actions judged by Devin's safety model",
        "dangerous": "EVERY action auto-approved, including destructive commands",
        "autonomous": "sandboxed, everything but writes auto-approved",
    }.get(cfg.permission_mode, cfg.permission_mode)
    return (
        f"Start a Devin run in {cwd or '(current directory)'}; "
        f"permission mode '{cfg.permission_mode}' ({mode_note}); "
        f"{'background' if background else 'foreground'}, timeout {timeout}s, backend {cfg.backend}. "
        f"Model: {label}"
        f"{'; ' + ', '.join(f'{key}: {value}' for key, value in options if key != 'model') if options else ''}. "
        f"{'Catalog pricing: ' + pricing + '. ' if pricing else 'Pricing unavailable; usage may incur charges. '}"
        f"Task: {excerpt}"
    )


@dataclass
class Ticket:
    scope: str
    fp: str
    rule_key: str
    summary: str
    created: float
    choice: str = ""
    decided_by: str = ""
    decided_at: float = 0.0


class ApprovalLedger:
    def __init__(self, clock: Callable[[], float] = time.time, ttl: float = TICKET_TTL_SECONDS):
        self._clock = clock
        self._ttl = ttl
        self._tickets: list[Ticket] = []
        self._lock = threading.Lock()

    def _expire(self) -> None:
        cutoff = self._clock() - self._ttl
        self._tickets = [t for t in self._tickets if t.created >= cutoff]

    def issue(self, scope: str, fp: str, rule_key: str, summary: str) -> Ticket:
        ticket = Ticket(scope=scope, fp=fp, rule_key=rule_key, summary=summary, created=self._clock())
        with self._lock:
            self._expire()
            # A repeated identical call replaces its older ticket, so at most one
            # ticket per (scope, arguments) exists at any time.
            self._tickets = [t for t in self._tickets if not (t.scope == scope and t.fp == fp)]
            self._tickets.append(ticket)
        return ticket

    def record_decision(self, rule_key: str, choice: str, decided_by: str = "") -> None:
        """Called from ``post_approval_response``. A denial voids the tickets for that rule."""
        allowed = str(choice or "").lower() in ALLOW_CHOICES
        with self._lock:
            if not allowed:
                self._tickets = [t for t in self._tickets if t.rule_key != rule_key]
                return
            for ticket in self._tickets:
                if ticket.rule_key == rule_key and not ticket.choice:
                    ticket.choice = str(choice)
                    ticket.decided_by = decided_by
                    ticket.decided_at = self._clock()

    def consume(self, scope: str, fp: str) -> Ticket | None:
        with self._lock:
            self._expire()
            for index, ticket in enumerate(self._tickets):
                if ticket.fp == fp and ticket.scope == scope:
                    return self._tickets.pop(index)
        return None

    def __len__(self) -> int:
        with self._lock:
            return len(self._tickets)


def scope_of(session_id: str = "", task_id: str = "") -> str:
    return str(session_id or task_id or "")


def ticket_record(ticket: Ticket, now: float) -> ApprovalRecord:
    return ApprovalRecord(
        via="pre_tool_call",
        rule_key=ticket.rule_key,
        summary=ticket.summary,
        requested_at=ticket.created,
        decided_at=ticket.decided_at or now,
        choice=ticket.choice or "approved",
        decided_by=ticket.decided_by,
    )


def default_gate(tool_name: str, reason: str, rule_key: str) -> dict:
    """Hermes' human-approval gate. Missing Hermes means no approval."""
    try:
        from tools.approval import request_tool_approval
    except Exception:
        return {"approved": False, "message": "BLOCKED: the Hermes approval gate is unavailable"}
    return request_tool_approval(tool_name, reason, rule_key=rule_key)


def ask_gate(gate: Callable[[str, str, str], dict], reason: str, rule_key: str) -> tuple[bool, str, str]:
    """Run ``gate`` fail-closed. Returns (approved, message, choice)."""
    try:
        result = gate(TOOL_NAME, reason, rule_key) or {}
    except Exception as exc:
        logger.warning("devin: approval gate failed: %s", exc)
        return False, f"BLOCKED: approval gate failed ({exc})", "error"
    if result.get("approved"):
        return True, "", str(result.get("choice") or result.get("outcome") or "approved")
    return False, str(result.get("message") or "BLOCKED: approval was not granted"), "denied"
