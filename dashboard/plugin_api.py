"""Read-only backend for a future Devin runs panel.

When a panel UI is added, a ``dashboard/manifest.json`` with ``"api":
"plugin_api.py"`` makes Hermes mount ``router`` at ``/api/plugins/devin/``.
Until then this module is inert: Hermes only imports it through a manifest.

It reads the run snapshots the plugin writes to
``<HERMES_HOME>/plugin-data/devin/runs/<run id>.json`` (see ``store.py``), so it
works from the dashboard process without access to the agent process. Live
updates come from the ``plugin.devin.run.*`` events (see docs/run-data-and-events.md).

Hermes imports this file on its own, not as part of the plugin package, so it
has no relative imports.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

_RUN_ID = re.compile(r"^dv-[0-9a-f]{6,32}$")
_ACTIVE = {"created", "starting", "running", "cancelling"}
_SUMMARY_KEYS = ("id", "title", "state", "background", "cwd", "permission_mode", "backend", "created_at",
                 "started_at", "finished_at", "updated_at", "duration_seconds", "tool_calls",
                 "permission_requests", "last_activity", "usage", "error", "schema_version")


def runs_dir() -> Path | None:
    try:
        from plugins.plugin_storage import plugin_data_dir
    except Exception:
        return None
    return plugin_data_dir("devin") / "runs"


def _load(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(data, dict) or not _RUN_ID.match(str(data.get("id") or "")):
        return None
    return data


def list_runs(root: Path | None, state: str = "all", limit: int = 50) -> list[dict[str, Any]]:
    if root is None or not root.is_dir():
        return []
    rows = [d for d in (_load(p) for p in root.glob("dv-*.json")) if d]
    if state == "active":
        rows = [d for d in rows if d.get("state") in _ACTIVE]
    elif state == "finished":
        rows = [d for d in rows if d.get("state") not in _ACTIVE]
    rows.sort(key=lambda d: d.get("created_at") or 0, reverse=True)
    return [{k: d.get(k) for k in _SUMMARY_KEYS if k in d} for d in rows[:max(1, min(limit, 200))]]


def get_run(root: Path | None, run_id: str, *, include_output: bool = False) -> dict[str, Any] | None:
    if root is None or not _RUN_ID.match(run_id or ""):
        return None
    data = _load(root / f"{run_id}.json")
    if data is None:
        return None
    if not include_output:
        data.pop("prompt", None)
        data.pop("output", None)
    return data


try:
    from fastapi import APIRouter, HTTPException
except Exception:  # pragma: no cover - only inside the Hermes dashboard
    router = None
else:
    router = APIRouter()

    @router.get("/runs")
    def _runs(state: str = "all", limit: int = 50) -> dict[str, Any]:
        if state not in ("all", "active", "finished"):
            raise HTTPException(status_code=400, detail="state must be all, active or finished")
        return {"runs": list_runs(runs_dir(), state, limit)}

    @router.get("/runs/{run_id}")
    def _run(run_id: str, output: bool = False) -> dict[str, Any]:
        data = get_run(runs_dir(), run_id, include_output=output)
        if data is None:
            raise HTTPException(status_code=404, detail="run not found")
        return data
