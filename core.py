"""Tool handlers and hooks, wired to one :class:`RunManager`.

Kept free of Hermes imports at module level so the whole thing can be driven
from tests with fakes for the approval gate, interrupt flag and injector.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time
from collections.abc import Callable
from typing import Any

from . import schemas
from .approval import (
    TOOL_NAME,
    ApprovalLedger,
    approval_message,
    ask_gate,
    default_gate,
    fingerprint,
    rule_key_for,
    scope_of,
    ticket_record,
)
from .config import ConfigSource, DevinConfig
from .models import ApprovalRecord, Run, clip
from .runner import RunManager, RunRefused, resolve_cwd

logger = logging.getLogger(__name__)

_FORBIDDEN_ARGS = {"permission_mode", "mode", "allow_dangerous", "dangerous", "yolo", "bypass", "sandbox",
                   "trust_workspace", "model", "cli_path"}


def _json(data: dict[str, Any]) -> str:
    return json.dumps(data, ensure_ascii=False, default=str)


def _session_platform() -> str:
    """The frontend of the current session, read the way Hermes' prompt builder reads it."""
    platform = os.environ.get("HERMES_PLATFORM") or os.environ.get("HERMES_SESSION_PLATFORM")
    if platform:
        return platform.strip().lower()
    get_session_env = getattr(sys.modules.get("gateway.session_context"), "get_session_env", None)
    try:
        return str((get_session_env("HERMES_SESSION_PLATFORM") if get_session_env else "") or "").strip().lower()
    except Exception:
        return ""


def _with_card(view: dict[str, Any], run_id: str) -> dict[str, Any]:
    """In Hermes Desktop, offer the transcript directive that renders a live run card.

    Other frontends would show the directive as raw text, so it is only added there.
    """
    if _session_platform() == "desktop":
        view["card"] = f'::devin-run{{id="{run_id}"}}'
    return view


def _error(code: str, message: str, **extra: Any) -> str:
    return _json({"ok": False, "error": {"code": code, "message": message}, **extra})


def default_interrupted() -> bool:
    try:
        from tools.interrupt import is_interrupted
    except Exception:
        return False
    return bool(is_interrupted())


def default_yield_requested() -> bool:
    try:
        from tools.interrupt import consume_yield
    except Exception:
        return False
    return bool(consume_yield(threading.current_thread().ident))


def default_session_key() -> str:
    try:
        from tools.approval_context import get_current_session_key
    except Exception:
        return ""
    key = get_current_session_key(default="")
    return "" if key in ("", "default") else str(key)


class ArgError(ValueError):
    pass


def _parse_delegate_args(cfg: DevinConfig, args: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(args, dict):
        raise ArgError("arguments must be an object")
    smuggled = sorted(k for k in args if k in _FORBIDDEN_ARGS)
    if smuggled:
        raise ArgError(
            f"{', '.join(smuggled)} cannot be set per call: Devin's permission mode, model and sandbox come "
            "only from the plugin settings (plugins.entries.devin.settings)."
        )
    unknown = sorted(k for k in args if k not in {"prompt", "cwd", "background", "timeout_seconds", "title"})
    if unknown:
        raise ArgError(f"unknown argument(s): {', '.join(unknown)}")
    prompt = args.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        raise ArgError("prompt is required and must be a non-empty string")
    if len(prompt) > cfg.max_prompt_chars:
        raise ArgError(f"prompt is {len(prompt)} characters; the limit is max_prompt_chars={cfg.max_prompt_chars}")
    background = args.get("background", cfg.default_background)
    if not isinstance(background, bool):
        raise ArgError("background must be true or false")
    default_timeout = cfg.background_timeout_seconds if background else cfg.timeout_seconds
    timeout = args.get("timeout_seconds", default_timeout)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
        raise ArgError("timeout_seconds must be a number")
    timeout = int(max(10, min(int(timeout), cfg.max_timeout_seconds)))
    cwd = args.get("cwd")
    if cwd is not None and not isinstance(cwd, str):
        raise ArgError("cwd must be a string")
    title = args.get("title") or ""
    if not isinstance(title, str):
        raise ArgError("title must be a string")
    return {"prompt": prompt, "background": background, "timeout": timeout, "cwd": cwd,
            "title": " ".join(title.split())[:120]}


class DevinPlugin:
    def __init__(self, *, config: ConfigSource, manager: RunManager, ledger: ApprovalLedger | None = None,
                 gate: Callable[[str, str, str], dict] = default_gate,
                 interrupted: Callable[[], bool] = default_interrupted,
                 session_key: Callable[[], str] = default_session_key,
                 yield_requested: Callable[[], bool] = default_yield_requested):
        self.config = config
        self.manager = manager
        self.ledger = ledger or ApprovalLedger()
        self.gate = gate
        self.interrupted = interrupted
        self.session_key = session_key
        self.yield_requested = yield_requested

    # -- hooks -----------------------------------------------------------------

    def pre_tool_call(self, tool_name: str = "", args: dict | None = None, task_id: str = "",
                      session_id: str = "", **_: Any) -> dict | None:
        """Ask for human approval of every Devin run, or block one that cannot run."""
        if tool_name != TOOL_NAME:
            return None
        args = args if isinstance(args, dict) else {}
        cfg, err = self.config.try_load()
        if cfg is None:
            return {"action": "block", "message": _error("config_invalid", f"Devin plugin settings: {err}")}
        try:
            parsed = _parse_delegate_args(cfg, args)
            cwd = resolve_cwd(cfg, parsed["cwd"])
            self.manager.resolve_cli(cfg)
        except ArgError as exc:
            return {"action": "block", "message": _error("invalid_arguments", str(exc))}
        except RunRefused as exc:
            return {"action": "block", "message": _error(exc.code, exc.message)}
        busy = self.manager.capacity_error(cfg)
        if busy:
            return {"action": "block", "message": _error("concurrency_limit", busy)}
        rule_key = rule_key_for(cfg, parsed["prompt"], cwd)
        summary = approval_message(cfg, prompt=parsed["prompt"], cwd=cwd, background=parsed["background"],
                                   timeout=parsed["timeout"])
        self.ledger.issue(scope_of(session_id, task_id), fingerprint(args), rule_key, summary)
        return {"action": "approve", "message": summary, "rule_key": rule_key}

    def post_approval_response(self, pattern_key: str = "", choice: str = "", **kwargs: Any) -> None:
        key = str(pattern_key or "")
        if key.startswith("plugin_rule:devin:"):
            self.ledger.record_decision(key[len("plugin_rule:"):], str(choice or ""),
                                        str(kwargs.get("decided_by") or ""))

    def on_unload(self) -> None:
        self.manager.shutdown("the Devin plugin was unloaded")

    # -- delegate ----------------------------------------------------------------

    def _approve(self, cfg: DevinConfig, args: dict, parsed: dict, cwd: str, task_id: str,
                 session_id: str) -> ApprovalRecord | str:
        now = time.time()
        ticket = self.ledger.consume(scope_of(session_id, task_id), fingerprint(args))
        if ticket is not None:
            return ticket_record(ticket, now)
        # No ticket from our hook: this call did not pass through the normal tool
        # path. Ask the same human gate directly, fail closed.
        rule_key = rule_key_for(cfg, parsed["prompt"], cwd)
        summary = approval_message(cfg, prompt=parsed["prompt"], cwd=cwd, background=parsed["background"],
                                   timeout=parsed["timeout"])
        ok, message, choice = ask_gate(self.gate, summary, rule_key)
        if not ok:
            return message or "approval was not granted"
        return ApprovalRecord(via="direct_gate", rule_key=rule_key, summary=summary, requested_at=now,
                              decided_at=time.time(), choice=choice)

    def delegate(self, args: dict, task_id: str = "", session_id: str = "", **_: Any) -> str:
        cfg, err = self.config.try_load()
        if cfg is None:
            return _error("config_invalid", f"Devin plugin settings: {err}")
        try:
            parsed = _parse_delegate_args(cfg, args)
            cwd = resolve_cwd(cfg, parsed["cwd"])
        except ArgError as exc:
            return _error("invalid_arguments", str(exc))
        except RunRefused as exc:
            return _error(exc.code, exc.message)
        busy = self.manager.capacity_error(cfg)
        if busy:
            return _error("concurrency_limit", busy)
        try:
            argv = self.manager.preflight(cfg)
        except RunRefused as exc:
            return _error(exc.code, exc.message)
        approval = self._approve(cfg, args, parsed, cwd, task_id, session_id)
        if isinstance(approval, str):
            return _error("not_approved", approval)
        try:
            run = self.manager.start(
                cfg, prompt=parsed["prompt"], cwd=cwd, background=parsed["background"],
                timeout_seconds=parsed["timeout"], title=parsed["title"], approval=approval,
                session_id=str(session_id or ""), task_id=str(task_id or ""),
                session_key=self.session_key() if parsed["background"] else "", argv=argv,
                desktop=_session_platform() == "desktop",
            )
        except RunRefused as exc:
            return _error(exc.code, exc.message)
        if parsed["background"]:
            return _json(_with_card({
                "ok": True, "run_id": run.id, "state": run.state.value, "background": True,
                "cwd": run.cwd, "permission_mode": run.permission_mode,
                "message": (f"Devin is working in the background as {run.id}. The result will be posted to "
                            "this conversation when it finishes; use devin_run_status to check progress."),
            }, run.id))
        how = self.manager.wait_foreground(run, interrupted=self.interrupted, gate=self.gate,
                                           yield_requested=self.yield_requested, session_key=self.session_key)
        if how == "detached":
            return _json(_with_card({
                "ok": True, "run_id": run.id, "state": run.state.value, "background": True,
                "cwd": run.cwd, "permission_mode": run.permission_mode,
                "message": (f"Devin run {run.id} was moved to the background so the conversation can continue. "
                            "Its result will be posted here when it finishes. From now on Devin's permission "
                            "prompts are denied, because nobody is waiting to answer them."),
            }, run.id))
        return _json(_with_card(self._result_view(run, cfg.max_result_chars), run.id))

    # -- companions ---------------------------------------------------------------

    def _lookup(self, run_id: Any) -> tuple[Run | None, dict | None]:
        if not isinstance(run_id, str) or not run_id:
            return None, None
        run = self.manager.get(run_id)
        if run is not None:
            return run, None
        return None, self.manager.store.load(run_id)

    def _result_view(self, run: Run, limit: int) -> dict[str, Any]:
        snap = run.snapshot(events=0, output_limit=limit)
        view = {
            "ok": snap["state"] == "succeeded",
            "run_id": run.id, "state": snap["state"], "title": snap["title"],
            "cwd": snap["cwd"], "permission_mode": snap["permission_mode"],
            "duration_seconds": snap["duration_seconds"], "stop_reason": snap["stop_reason"],
            "output": snap["output"], "output_truncated": snap["output_truncated"],
            "output_chars": snap["output_chars"], "tool_calls": snap["tool_calls"],
            "files_changed": snap["files_changed"],
            "permissions": snap["permissions"][-20:],
        }
        for key in ("usage", "error"):
            if snap.get(key):
                view[key] = snap[key]
        if snap["state"] in ("cancelled", "timed_out"):
            view["cancel_reason"] = snap["cancel_reason"]
            view["kill_report"] = snap["kill_report"]
            view["note"] = ("Devin and every process it started were stopped. Work done before the stop may "
                            "be left in the working directory.")
        return view

    def list_runs(self, args: dict, **_: Any) -> str:
        args = args if isinstance(args, dict) else {}
        state = args.get("state") or "all"
        limit = args.get("limit") or 10
        if state not in ("active", "finished", "all") or isinstance(limit, bool) or not isinstance(limit, int):
            return _error("invalid_arguments", "state must be active|finished|all and limit an integer")
        limit = max(1, min(50, limit))
        rows: dict[str, dict] = {r.id: r.summary() for r in self.manager.list()}
        for data in self.manager.store.load_all():
            rows.setdefault(data["id"], {k: data.get(k) for k in (
                "id", "title", "state", "background", "cwd", "permission_mode", "backend", "created_at",
                "started_at", "finished_at", "duration_seconds", "error")})
        active = {"created", "starting", "running", "cancelling"}
        items = sorted(rows.values(), key=lambda r: r.get("created_at") or 0, reverse=True)
        if state == "active":
            items = [r for r in items if r.get("state") in active]
        elif state == "finished":
            items = [r for r in items if r.get("state") not in active]
        return _json({"ok": True, "runs": items[:limit], "total": len(items)})

    def run_status(self, args: dict, **_: Any) -> str:
        args = args if isinstance(args, dict) else {}
        events = args.get("events", 10)
        if isinstance(events, bool) or not isinstance(events, int):
            events = 10
        run, stored = self._lookup(args.get("run_id"))
        if run is None and stored is None:
            return _error("not_found", f"No Devin run {args.get('run_id')!r}")
        if run is not None:
            snap = run.snapshot(events=max(0, min(100, events)), include_output=False)
            with run._lock:
                recent_tools = list(run.tool_calls.values())[-10:]
            snap["recent_tool_calls"] = recent_tools
            tail, _ = clip(run.output.value(), 1500)
            snap["output_tail"] = tail[-1500:]
        else:
            snap = dict(stored)
            snap.pop("output", None)
            snap["events"] = (snap.get("events") or [])[-max(0, min(100, events)):] if events else []
        snap.pop("prompt", None)
        return _json({"ok": True, **snap})

    def run_result(self, args: dict, **_: Any) -> str:
        args = args if isinstance(args, dict) else {}
        cfg, _err = self.config.try_load()
        default_limit = cfg.max_result_chars if cfg else 12_000
        limit = args.get("max_chars", default_limit)
        if isinstance(limit, bool) or not isinstance(limit, int):
            limit = default_limit
        ceiling = cfg.max_output_chars if cfg else 200_000
        limit = max(500, min(limit, ceiling))
        run, stored = self._lookup(args.get("run_id"))
        if run is None and stored is None:
            return _error("not_found", f"No Devin run {args.get('run_id')!r}")
        if run is not None:
            view = self._result_view(run, limit)
            if run.active:
                view["note"] = "Run is still active; this is the output so far."
            return _json(view)
        text, clipped = clip(str(stored.get("output") or ""), limit)
        return _json({"ok": stored.get("state") == "succeeded", "run_id": stored.get("id"),
                      "state": stored.get("state"), "title": stored.get("title"), "output": text,
                      "output_truncated": bool(clipped or stored.get("output_truncated")),
                      "error": stored.get("error"), "note": "Loaded from run history."})

    def cancel_run(self, args: dict, **_: Any) -> str:
        args = args if isinstance(args, dict) else {}
        run_id = args.get("run_id")
        run = self.manager.get(run_id) if isinstance(run_id, str) else None
        if run is None:
            return _error("not_found", f"No active Devin run {run_id!r}")
        if not run.active:
            return _json({"ok": True, "run_id": run.id, "state": run.state.value, "note": "Run already finished."})
        self.manager.cancel(run.id, "cancelled by devin_cancel_run")
        run.settled.wait(max(15.0, (self.config.try_load()[0] or DevinConfig()).cancel_grace_seconds * 4))
        snap = run.snapshot(events=0, include_output=False)
        return _json({"ok": not run.active, "run_id": run.id, "state": snap["state"],
                      "kill_report": snap["kill_report"],
                      "message": "Devin and its child processes were stopped." if not run.active
                      else "Stop requested; the run has not exited yet."})

    # -- registry glue ---------------------------------------------------------------

    def follow_up(self, run_id: str, prompt: str) -> dict[str, Any]:
        """Continue a finished run's Devin session with a message typed on its Desktop run card.

        Typing and sending the message is the user's own approval, so no gate is asked.
        The follow-up is a new background run that resumes the same Devin session
        (ACP ``session/load``), so Devin keeps the earlier context. Returns a JSON-able
        dict with ``ok`` and either ``run`` or ``error``.
        """
        def refuse(code: str, message: str) -> dict[str, Any]:
            return {"ok": False, "error": {"code": code, "message": message}}

        cfg, err = self.config.try_load()
        if cfg is None:
            return refuse("invalid_settings", err or "invalid settings")
        text = prompt.strip() if isinstance(prompt, str) else ""
        if not text:
            return refuse("invalid_arguments", "The follow-up is empty.")
        if len(text) > cfg.max_prompt_chars:
            return refuse("invalid_arguments", f"The follow-up is longer than {cfg.max_prompt_chars} characters.")
        run, stored = self._lookup(run_id)
        parent = run.summary() if run is not None else (stored or {})
        if run is not None:
            parent.update(devin_session_id=run.devin_session_id, session_id=run.session_id,
                          session_key=run.session_key)
        if not parent:
            return refuse("not_found", f"No Devin run {run_id}.")
        if parent.get("state") in ("created", "starting", "running", "cancelling"):
            return refuse("busy", "Devin is still working on this run. Send the follow-up when it finishes.")
        if parent.get("backend") != "acp" or not parent.get("devin_session_id"):
            return refuse("not_resumable", "Only runs on the ACP backend keep a Devin session to continue.")
        if cfg.backend != "acp":
            return refuse("not_resumable", "Follow-ups need backend: acp in the plugin settings.")
        try:
            cwd = resolve_cwd(cfg, str(parent.get("cwd") or ""))
            argv = self.manager.preflight(cfg)
            now = time.time()
            approval = ApprovalRecord(via="desktop_card", rule_key="", choice="sent",
                                      summary="Follow-up typed and sent on the run card in Hermes Desktop",
                                      requested_at=now, decided_at=now, decided_by="user")
            child = self.manager.start(
                cfg, prompt=text, cwd=cwd, background=True, timeout_seconds=cfg.background_timeout_seconds,
                title=f"Follow-up: {text[:70]}", approval=approval, argv=argv, desktop=True,
                session_id=str(parent.get("session_id") or ""), session_key=str(parent.get("session_key") or ""),
                parent_run_id=str(parent["id"]), resume_session_id=str(parent["devin_session_id"]),
            )
        except RunRefused as exc:
            return refuse(exc.code, exc.message)
        return {"ok": True, "run": child.summary()}

    def handlers(self) -> dict[str, Callable[..., str]]:
        return {
            schemas.DELEGATE: self.delegate,
            schemas.LIST: self.list_runs,
            schemas.STATUS: self.run_status,
            schemas.RESULT: self.run_result,
            schemas.CANCEL: self.cancel_run,
        }
