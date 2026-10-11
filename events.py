"""Event emission for run lifecycle and progress.

Every change to a run produces one event with a stable envelope::

    {"schema_version": 1, "type": "run.progress", "run_id": "dv-...", "seq": 7,
     "at": 1760000000.0, "run": {<Run.summary()>}, "event": {<ProgressEvent>}}

Types: ``run.created``, ``run.state``, ``run.progress``, ``run.permission``,
``run.usage``, ``run.finished``.

The bus fans each event out to:

* in-process listeners (tests, the slash command, anything embedding the plugin);
* ``ctx.emit`` -> ``devin:<type>`` on the Hermes plugin event bus, for other plugins;
* ``broadcast_plugin_event`` -> ``plugin.devin.<type>`` on the Desktop/TUI
  gateway event stream, which a Desktop half subscribes to with
  ``host.onEvent('plugin.devin.run.progress', ...)``.

Delivery is best effort and never raises into a run. Chatty kinds (message and
thought chunks) are coalesced per run so a fast stream does not flood clients.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from typing import Any

from .models import SCHEMA_VERSION, ProgressEvent, Run

logger = logging.getLogger(__name__)

EVENT_TYPES = ("run.created", "run.state", "run.progress", "run.permission", "run.usage", "run.finished")
_CHATTY = {"message", "thought"}

Listener = Callable[[dict[str, Any]], None]


def _default_broadcast(plugin_id: str, event: str, payload: dict) -> None:
    try:
        from hermes_cli.plugin_events import broadcast_plugin_event
    except Exception:
        return
    broadcast_plugin_event(plugin_id, event, payload)


class EventBus:
    def __init__(
        self,
        *,
        plugin_id: str = "devin",
        emit: Callable[[str, dict], Any] | None = None,
        broadcast: Callable[[str, str, dict], None] | None = _default_broadcast,
        chatty_interval: float = 0.5,
    ):
        self.plugin_id = plugin_id
        self._emit = emit
        self._broadcast = broadcast
        self._listeners: list[Listener] = []
        self._lock = threading.Lock()
        self._chatty_interval = chatty_interval
        self._last_chatty: dict[str, float] = {}

    def subscribe(self, listener: Listener) -> Callable[[], None]:
        with self._lock:
            self._listeners.append(listener)

        def unsubscribe() -> None:
            with self._lock:
                if listener in self._listeners:
                    self._listeners.remove(listener)

        return unsubscribe

    def publish(self, type_: str, run: Run, event: ProgressEvent | None = None, **extra: Any) -> None:
        if event is not None and event.kind in _CHATTY:
            now = time.monotonic()
            with self._lock:
                last = self._last_chatty.get(run.id, 0.0)
                if now - last < self._chatty_interval:
                    return
                self._last_chatty[run.id] = now
        payload: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "type": type_,
            "run_id": run.id,
            "at": time.time(),
            "run": run.summary(),
        }
        if event is not None:
            payload["seq"] = event.seq
            payload["event"] = event.to_dict()
        payload.update(extra)
        if type_ == "run.finished":
            with self._lock:
                self._last_chatty.pop(run.id, None)
        with self._lock:
            listeners = list(self._listeners)
        for listener in listeners:
            try:
                listener(payload)
            except Exception:
                logger.debug("devin: event listener failed", exc_info=True)
        if self._emit is not None:
            try:
                self._emit(type_, payload)
            except Exception:
                logger.debug("devin: ctx.emit failed", exc_info=True)
        if self._broadcast is not None:
            try:
                self._broadcast(self.plugin_id, type_, payload)
            except Exception:
                logger.debug("devin: desktop broadcast failed", exc_info=True)
