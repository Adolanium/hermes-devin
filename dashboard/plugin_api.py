"""Backend for the Devin runs panel in Hermes Desktop (``desktop/plugin.js``).

``dashboard/manifest.json`` makes Hermes mount ``router`` at
``/api/plugins/devin/``; the Desktop half reaches it with ``ctx.rest``. Hermes
only imports this module while the plugin is in ``plugins.enabled``.

Runs are read from two places:

* the live run manager, when the agent half runs in this same process (the
  ``hermes serve`` backend Desktop talks to). That gives fresh data and lets
  the panel stop a run;
* the snapshots the plugin writes to
  ``<HERMES_HOME>/plugin-data/devin/runs/<run id>.json`` (see ``store.py``), for
  history and for runs owned by another process.

Live updates come from the ``plugin.devin.run.*`` events (see
docs/run-data-and-events.md); this API is the initial load and the fallback.

Hermes imports this file on its own, not as part of the plugin package, so it
has no relative imports.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

_RUN_ID = re.compile(r"^dv-[0-9a-f]{6,32}$")
_ACTIVE = {"created", "starting", "running", "cancelling"}
_SUMMARY_KEYS = (
    "id",
    "title",
    "state",
    "background",
    "cwd",
    "permission_mode",
    "backend",
    "parent_run_id",
    "created_at",
    "started_at",
    "finished_at",
    "updated_at",
    "duration_seconds",
    "tool_calls",
    "files_changed",
    "permission_requests",
    "pending_permissions",
    "last_activity",
    "usage",
    "error",
    "schema_version",
    "model",
    "model_label",
    "model_cost_summary",
    "model_confirmed",
    "session_config",
    "execution_target",
    "remote_state",
    "cloud_url",
)
_DETAIL_EVENTS = 60


def runs_dir() -> Path | None:
    try:
        from plugins.plugin_storage import plugin_data_dir
    except Exception:
        return None
    return plugin_data_dir("devin") / "runs"


def live_plugin(*, load: bool = False) -> Any | None:
    """The agent half loaded in this process (a ``DevinPlugin``), if any."""
    try:
        from hermes_cli.plugins import discover_plugins, get_plugin_manager
    except ImportError:
        modules = [
            module
            for name, module in list(sys.modules.items())
            if name.startswith("hermes_plugins.") and name.count(".") == 1
        ]
    else:
        # The host registry owns profile scoping, enabled state and trust checks.
        if load:
            discover_plugins()
        manager = get_plugin_manager()
        modules = [loaded.module for loaded in manager._plugins.values() if loaded.enabled]
    for module in modules:
        if getattr(module, "TOOLSET", None) != "delegation" or not callable(getattr(module, "get_plugin", None)):
            continue
        try:
            plugin = module.get_plugin()
        except Exception:
            continue
        if plugin is not None and getattr(plugin, "manager", None) is not None:
            return plugin
    return None


def live_manager() -> Any | None:
    """The run manager of the agent half loaded in this process, if any."""
    plugin = live_plugin()
    return plugin.manager if plugin is not None else None


_FOLLOW_UP_STATUS = {
    "not_found": 404,
    "busy": 409,
    "not_resumable": 409,
    "concurrency_limit": 429,
    "invalid_arguments": 400,
    "invalid_settings": 409,
}


def follow_up(run_id: str, prompt: Any, plugin: Any | None) -> tuple[int, dict[str, Any]]:
    """Start a follow-up run in the same Devin session. Returns ``(status code, body)``."""
    if not _RUN_ID.match(run_id or "") or not isinstance(prompt, str):
        return 400, {"detail": "expected a run id and a prompt"}
    if plugin is None:
        return 409, {"detail": "The Devin agent half is not loaded in this Hermes process."}
    result = plugin.follow_up(run_id, prompt)
    if result.get("ok"):
        return 200, {"ok": True, "run": _summary(result["run"])}
    error = result.get("error") or {}
    return _FOLLOW_UP_STATUS.get(error.get("code"), 409), {"detail": error.get("message") or "follow-up refused"}


def _load(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(data, dict) or not _RUN_ID.match(str(data.get("id") or "")):
        return None
    return data


def _summary(data: dict[str, Any]) -> dict[str, Any]:
    out = {k: data.get(k) for k in _SUMMARY_KEYS if k in data}
    if "cloud_url" in out and not re.fullmatch(
        r"https://app\.devin\.ai/sessions/[a-f0-9]{32}", str(out["cloud_url"] or "")
    ):
        out["cloud_url"] = ""
    return out


def run_list_response(root: Path | None, state: str = "all", limit: int = 50, manager: Any = None) -> dict:
    scope = hashlib.sha256(str(root.resolve()).encode()).hexdigest() if root is not None else ""
    return {"runs": list_runs(root, state, limit, manager), "live": manager is not None, "profile_scope": scope}


def list_runs(
    root: Path | None, state: str = "all", limit: int = 50, manager: Any | None = None
) -> list[dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}
    if root is not None and root.is_dir():
        for data in (_load(p) for p in root.glob("dv-*.json")):
            if data:
                rows[data["id"]] = _summary(data)
    if manager is not None:
        for run in manager.list():
            rows[run.id] = _summary(run.summary())
    out = list(rows.values())
    if state == "active":
        out = [d for d in out if d.get("state") in _ACTIVE]
    elif state == "finished":
        out = [d for d in out if d.get("state") not in _ACTIVE]
    out.sort(key=lambda d: d.get("created_at") or 0, reverse=True)
    return out[: max(1, min(limit, 200))]


def get_run(
    root: Path | None, run_id: str, *, include_output: bool = False, manager: Any | None = None
) -> dict[str, Any] | None:
    if not _RUN_ID.match(run_id or ""):
        return None
    run = manager.get(run_id) if manager is not None else None
    if run is not None:
        data = run.snapshot(events=_DETAIL_EVENTS, include_output=include_output)
        data["live"] = True
    else:
        data = _load(root / f"{run_id}.json") if root is not None else None
        if data is None:
            return None
        data["events"] = (data.get("events") or [])[-_DETAIL_EVENTS:]
        data["live"] = False
    data.pop("prompt", None)
    if not include_output:
        data.pop("output", None)
    return data


def cancel_run(run_id: str, manager: Any | None) -> tuple[int, dict[str, Any]]:
    """Stop a run owned by this process. Returns ``(status code, body)``."""
    if not _RUN_ID.match(run_id or ""):
        return 400, {"detail": "invalid run id"}
    run = manager.cancel(run_id, "cancelled from Hermes Desktop") if manager is not None else None
    if run is None:
        return 409, {
            "detail": "This run is not owned by this Hermes process, so it cannot be stopped from here. "
            f"Ask Hermes to cancel {run_id}, or run /devin cancel {run_id}."
        }
    return 200, {"ok": True, "run": _summary(run.summary())}


_ASK_ID = re.compile(r"^[0-9a-f]{6,32}$")


def answer_permission(run_id: str, ask_id: str, allow: Any, manager: Any | None) -> tuple[int, dict[str, Any]]:
    """Answer one of Devin's requests waiting on the run card. Returns ``(status code, body)``."""
    if not _RUN_ID.match(run_id or "") or not _ASK_ID.match(ask_id or "") or not isinstance(allow, bool):
        return 400, {"detail": "expected a run id, a request id and allow: true or false"}
    outcome = manager.answer_permission(run_id, ask_id, allow) if manager is not None else "no_run"
    if outcome == "no_run":
        return 409, {"detail": f"Run {run_id} is not owned by this Hermes process."}
    if outcome == "no_request":
        return 404, {"detail": "This request was already answered or has expired."}
    return 200, {"ok": True, "allowed": allow}


def devin_mark_candidates() -> list[Path]:
    """Where an installed Devin desktop app keeps its logo (the hexagon mark)."""
    roots: list[Path] = []
    if os.environ.get("DEVIN_APP_DIR"):
        roots.append(Path(os.environ["DEVIN_APP_DIR"]))
    if os.environ.get("LOCALAPPDATA"):
        roots.append(Path(os.environ["LOCALAPPDATA"]) / "Programs" / "Devin" / "resources" / "app")
    roots += [
        Path("/Applications/Devin.app/Contents/Resources/app"),
        Path("/usr/share/devin/resources/app"),
        Path("/opt/Devin/resources/app"),
    ]
    return [root / "out" / "media" / "letterpress-dark.svg" for root in roots]


def devin_mark(candidates: list[Path] | None = None) -> str | None:
    """Legacy installed-app mark endpoint; None when the app is absent.

    The Desktop half uses its bundled mark instead, without needing this endpoint.
    """
    for path in candidates if candidates is not None else devin_mark_candidates():
        try:
            if not path.is_file() or path.stat().st_size > 64_000:
                continue
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        if text.lstrip().startswith("<svg") and "<script" not in text.lower():
            return text
    return None


try:
    from fastapi import APIRouter, HTTPException
except Exception:  # pragma: no cover - only inside the Hermes dashboard
    router = None
else:
    router = APIRouter()

    @router.get("/brand/devin")
    def _devin_mark() -> dict[str, Any]:
        return {"svg": devin_mark()}

    @router.get("/runs")
    def _runs(state: str = "all", limit: int = 50) -> dict[str, Any]:
        if state not in ("all", "active", "finished"):
            raise HTTPException(status_code=400, detail="state must be all, active or finished")
        return run_list_response(runs_dir(), state, limit, live_manager())

    @router.get("/runs/{run_id}")
    def _run(run_id: str, output: bool = False) -> dict[str, Any]:
        data = get_run(runs_dir(), run_id, include_output=output, manager=live_manager())
        if data is None:
            raise HTTPException(status_code=404, detail="run not found")
        return data

    @router.post("/runs/{run_id}/permissions/{ask_id}")
    def _answer(run_id: str, ask_id: str, body: dict) -> dict[str, Any]:
        status, payload = answer_permission(run_id, ask_id, (body or {}).get("allow"), live_manager())
        if status != 200:
            raise HTTPException(status_code=status, detail=payload["detail"])
        return payload

    @router.post("/runs/{run_id}/follow-up")
    def _follow_up(run_id: str, body: dict) -> dict[str, Any]:
        status, payload = follow_up(run_id, (body or {}).get("prompt"), live_plugin())
        if status != 200:
            raise HTTPException(status_code=status, detail=payload["detail"])
        return payload

    @router.post("/session-options")
    def _session_options(body: dict) -> dict[str, Any]:
        plugin = live_plugin(load=True)
        if plugin is None:
            raise HTTPException(
                status_code=409,
                detail="The Devin agent plugin is not loaded in this profile. Check that it is enabled.",
            )
        try:
            return plugin.session_options(body)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            # Discovery errors can contain CLI stderr; never return raw transport logs.
            raise HTTPException(
                status_code=409, detail="Could not load model choices. Check CLI login and workspace access."
            ) from exc

    @router.post("/runs/{run_id}/reconnect")
    def _reconnect(run_id: str) -> dict[str, Any]:
        if not _RUN_ID.fullmatch(run_id):
            raise HTTPException(status_code=400, detail="Expected a run id.")
        plugin = live_plugin(load=True)
        if plugin is None:
            raise HTTPException(status_code=409, detail="The Devin agent plugin is not loaded in this profile.")
        result = plugin.reconnect(run_id)
        if not result.get("ok"):
            raise HTTPException(
                status_code=409, detail=(result.get("error") or {}).get("message") or "Reconnect failed."
            )
        return result

    @router.post("/runs/{run_id}/cancel")
    def _cancel(run_id: str) -> dict[str, Any]:
        status, body = cancel_run(run_id, live_manager())
        if status != 200:
            raise HTTPException(status_code=status, detail=body["detail"])
        return body
