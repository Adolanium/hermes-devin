"""Run persistence.

Each run is written as ``<data dir>/runs/<run id>.json`` (the full snapshot)
when it changes state, and at most every few seconds while it streams. That
gives a history across restarts and a read path for a Desktop panel backend
running in another process (``dashboard/plugin_api.py`` reads these files).

Writes are atomic (temp file + replace). Only the newest ``history_limit``
finished runs are kept.
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .models import ACTIVE_STATES, RunState

logger = logging.getLogger(__name__)

_RUN_ID = re.compile(r"^dv-[0-9a-f]{6,32}$")


def default_data_dir() -> Path | None:
    try:
        from plugins.plugin_storage import plugin_data_dir
    except Exception:
        return None
    try:
        return plugin_data_dir("devin")
    except Exception:
        logger.debug("devin: no plugin data dir", exc_info=True)
        return None


def valid_run_id(run_id: str) -> bool:
    return bool(_RUN_ID.match(run_id or ""))


class RunStore:
    def __init__(self, root: Path | None, *, throttle_seconds: float = 3.0):
        self.root = Path(root) / "runs" if root is not None else None
        self._throttle = throttle_seconds
        self._last_write: dict[str, float] = {}
        self._final: set[str] = set()
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return self.root is not None

    def save(self, snapshot: dict[str, Any], *, force: bool = False) -> None:
        if self.root is None:
            return
        run_id = str(snapshot.get("id") or "")
        if not valid_run_id(run_id):
            return
        now = time.monotonic()
        terminal = snapshot.get("state") not in {s.value for s in ACTIVE_STATES}
        snapshot = dict(snapshot)
        snapshot.setdefault("owner_pid", os.getpid())
        # Writes are serialized, and once a finished snapshot is on disk a late
        # streaming snapshot of the same run can no longer overwrite it.
        with self._lock:
            if not terminal and run_id in self._final:
                return
            if not force and now - self._last_write.get(run_id, 0.0) < self._throttle:
                return
            self._last_write[run_id] = now
            try:
                self.root.mkdir(parents=True, exist_ok=True)
                fd, tmp = tempfile.mkstemp(prefix=f".{run_id}.", suffix=".tmp", dir=str(self.root))
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump(snapshot, fh, ensure_ascii=False)
                os.replace(tmp, self.root / f"{run_id}.json")
            except Exception:
                logger.debug("devin: could not persist run %s", run_id, exc_info=True)
                return
            if terminal:
                self._final.add(run_id)

    def load_all(self) -> list[dict[str, Any]]:
        if self.root is None or not self.root.is_dir():
            return []
        out = []
        for path in self.root.glob("dv-*.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            if isinstance(data, dict) and valid_run_id(str(data.get("id"))):
                out.append(data)
        out.sort(key=lambda d: d.get("created_at") or 0, reverse=True)
        return out

    def load(self, run_id: str) -> dict[str, Any] | None:
        if self.root is None or not valid_run_id(run_id):
            return None
        path = self.root / f"{run_id}.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return None
        return data if isinstance(data, dict) else None

    def mark_orphans(self, live_ids: set[str], *, pid_alive: Callable[[int], bool] | None = None) -> int:
        """Runs left 'active' by a process that is gone can never finish; record that.

        Several Hermes processes (CLI, gateway, Desktop) can share one data
        directory, so a run is only orphaned when the process that owns it is
        no longer alive.
        """
        if pid_alive is None:
            from .process import pid_alive as _alive

            pid_alive = _alive
        me = os.getpid()
        count = 0
        for data in self.load_all():
            if data.get("id") in live_ids:
                continue
            owner = data.get("owner_pid")
            if isinstance(owner, int) and owner != me and owner > 0:
                try:
                    if pid_alive(owner):
                        continue
                except Exception:
                    continue
            try:
                state = RunState(data.get("state"))
            except ValueError:
                continue
            if state in ACTIVE_STATES:
                data["state"] = RunState.FAILED.value
                data["error"] = {
                    "code": "orphaned",
                    "message": "Hermes stopped while this run was active, so its result was never collected.",
                }
                data["finished_at"] = data.get("finished_at") or time.time()
                self.save(data, force=True)
                count += 1
        return count

    def prune(self, keep: int) -> None:
        if self.root is None:
            return
        finished = [d for d in self.load_all() if d.get("state") not in {s.value for s in ACTIVE_STATES}]
        for data in finished[keep:]:
            try:
                (self.root / f"{data['id']}.json").unlink()
            except OSError:
                pass
            with self._lock:
                self._last_write.pop(str(data.get("id")), None)
                self._final.discard(str(data.get("id")))
