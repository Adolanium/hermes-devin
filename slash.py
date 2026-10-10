"""``/devin`` slash command: look at and stop runs without going through the model."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from .runner import RunRefused

if TYPE_CHECKING:  # pragma: no cover
    from .core import DevinPlugin

USAGE = ("/devin [list | status <run_id> | cancel <run_id|all> | doctor]\n"
         "  list    recent runs\n  status  one run's state and recent events\n"
         "  cancel  stop a run and every process it started\n  doctor  check settings, CLI and login")


def _fmt_row(row: dict) -> str:
    dur = row.get("duration_seconds")
    dur_txt = f"{int(dur)}s" if isinstance(dur, (int, float)) else "-"
    bg = "bg" if row.get("background") else "fg"
    return f"{row.get('id')}  {row.get('state'):<10} {bg} {dur_txt:>6}  {row.get('title') or ''}"


def make_handler(plugin: DevinPlugin):
    def handle(raw_args: str = "") -> str:
        parts = (raw_args or "").split()
        verb = parts[0].lower() if parts else "list"
        if verb in ("help", "-h", "--help"):
            return USAGE
        if verb == "list":
            data = json.loads(plugin.list_runs({"limit": 20}))
            rows = data.get("runs") or []
            return "\n".join(_fmt_row(r) for r in rows) if rows else "No Devin runs yet."
        if verb == "status" and len(parts) == 2:
            data = json.loads(plugin.run_status({"run_id": parts[1], "events": 8}))
            if not data.get("ok"):
                return data["error"]["message"]
            lines = [_fmt_row(data), f"cwd: {data.get('cwd')}  mode: {data.get('permission_mode')}"]
            if data.get("plan"):
                done = sum(1 for e in data["plan"] if e.get("status") == "completed")
                lines.append(f"plan: {done}/{len(data['plan'])} done")
            for event in data.get("events") or []:
                text = (event.get("text") or "").replace("\n", " ")[:160]
                lines.append(f"  [{event.get('kind')}] {text}")
            if data.get("error"):
                lines.append(f"error: {data['error'].get('message')}")
            return "\n".join(lines)
        if verb == "cancel" and len(parts) == 2:
            if parts[1] == "all":
                active = plugin.manager.active_runs()
                for run in active:
                    plugin.manager.cancel(run.id, "cancelled with /devin cancel all")
                return f"Stopping {len(active)} run(s)." if active else "No active runs."
            data = json.loads(plugin.cancel_run({"run_id": parts[1]}))
            return data.get("message") or data.get("note") or data.get("error", {}).get("message", "")
        if verb == "doctor":
            return doctor(plugin)
        return USAGE

    return handle


def doctor(plugin: DevinPlugin) -> str:
    lines = []
    cfg, err = plugin.config.try_load()
    if cfg is None:
        return f"settings: INVALID - {err}"
    lines.append(f"settings: ok (backend={cfg.backend}, permission_mode={cfg.permission_mode}, "
                 f"max_concurrent_runs={cfg.max_concurrent_runs})")
    try:
        argv = plugin.manager.resolve_cli(cfg)
    except RunRefused as exc:
        lines.append(f"cli: MISSING - {exc.message}")
        return "\n".join(lines)
    lines.append(f"cli: {argv[0]}")
    status = plugin.manager.check_auth(argv)
    state = {True: "logged in", False: "NOT logged in", None: "unknown"}[status.ok]
    lines.append(f"auth: {state} ({status.detail.splitlines()[0][:160] if status.detail else ''})")
    lines.append(f"active runs: {len(plugin.manager.active_runs())}")
    return "\n".join(lines)
