"""Devin for Hermes Agent.

Delegate coding tasks from Hermes to the local Devin CLI, with human approval
before every run, live progress, background runs and clean shutdown of the
whole Devin process tree.

Hermes imports this directory as a package (``hermes_plugins.<id>``), so the
relative imports below work without touching ``sys.path``.
"""

from __future__ import annotations

import hashlib
import logging
from typing import Any

from . import schemas
from .config import ConfigSource
from .core import DevinPlugin
from .events import EventBus
from .runner import RunManager
from .store import RunStore, default_data_dir

logger = logging.getLogger(__name__)

__version__ = "0.1.0"

TOOLSET = "delegation"

_plugin: DevinPlugin | None = None


def get_plugin() -> DevinPlugin | None:
    """The live plugin instance (for embedding and diagnostics)."""
    return _plugin


def _make_injector(ctx: Any):
    def inject(content: str, session_key: str | None) -> bool:
        try:
            return bool(ctx.inject_message(content, role="user", session_key=session_key))
        except Exception:
            logger.debug("devin: inject_message failed", exc_info=True)
            return False

    return inject


def build(
    ctx: Any, *, config: ConfigSource | None = None, store: RunStore | None = None, broadcast: Any = "default"
) -> DevinPlugin:
    """Assemble the plugin objects for ``ctx``. Split out of ``register`` for tests."""
    config = config or ConfigSource()
    cfg, _err = config.try_load()
    persist = cfg.persist_runs if cfg is not None else True
    if store is None:
        store = RunStore(default_data_dir() if persist else None)
    emit = getattr(ctx, "emit", None)
    bus_kwargs: dict[str, Any] = {"plugin_id": getattr(ctx, "plugin_id", "devin") or "devin", "emit": emit}
    data_dir = default_data_dir()
    if data_dir is not None:
        bus_kwargs["profile_scope"] = hashlib.sha256(str((data_dir / "runs").resolve()).encode()).hexdigest()
    if broadcast != "default":
        bus_kwargs["broadcast"] = broadcast
    bus = EventBus(**bus_kwargs)
    manager = RunManager(bus=bus, store=store, injector=_make_injector(ctx))
    try:
        store.mark_orphans(set())
    except Exception:
        logger.debug("devin: orphan sweep failed", exc_info=True)
    return DevinPlugin(config=config, manager=manager)


def register(ctx: Any) -> None:
    """Entry point. Hermes calls this once per process on every frontend."""
    global _plugin
    plugin = build(ctx)
    _plugin = plugin

    for name, handler in plugin.handlers().items():
        ctx.register_tool(
            name=name,
            toolset=TOOLSET,
            schema=schemas.SCHEMAS[name],
            handler=handler,
            emoji=schemas.EMOJI.get(name, ""),
        )

    ctx.register_hook("pre_tool_call", plugin.pre_tool_call)
    ctx.register_hook("post_approval_response", plugin.post_approval_response)

    try:
        from .slash import make_handler

        ctx.register_command(
            "devin",
            make_handler(plugin),
            description="List, inspect or stop Devin runs",
            args_hint="[list | status <id> | cancel <id|all> | doctor]",
        )
    except Exception as exc:
        logger.warning("devin: slash command not registered: %s", exc)

    import atexit

    def _at_exit() -> None:
        plugin.controls.close()
        plugin.manager.shutdown("Hermes exited", 5.0)

    atexit.register(_at_exit)

    def _unload() -> None:
        atexit.unregister(_at_exit)
        plugin.on_unload()

    on_unload = getattr(ctx, "on_unload", None)
    if callable(on_unload):
        on_unload(_unload)

    cfg, err = plugin.config.try_load()
    if cfg is None:
        logger.warning("devin: settings are invalid, runs will be refused: %s", err)
    else:
        logger.info("devin plugin ready: backend=%s permission_mode=%s", cfg.backend, cfg.permission_mode)
