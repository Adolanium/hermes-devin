"""Shared fixtures. The plugin directory is imported as a package, the same
way Hermes imports it, and Devin is always the fake in ``fake_devin.py``."""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import threading
from pathlib import Path
from typing import Any

import pytest

PLUGIN_DIR = Path(__file__).resolve().parent.parent
FAKE_DEVIN = Path(__file__).resolve().parent / "fake_devin.py"
PKG_NAME = "hermes_devin_under_test"


def _load_package():
    if PKG_NAME in sys.modules:
        return sys.modules[PKG_NAME]
    spec = importlib.util.spec_from_file_location(
        PKG_NAME, PLUGIN_DIR / "__init__.py", submodule_search_locations=[str(PLUGIN_DIR)])
    module = importlib.util.module_from_spec(spec)
    sys.modules[PKG_NAME] = module
    spec.loader.exec_module(module)
    return module


pkg = _load_package()


def load_yaml(path: Path) -> Any:
    """Parse YAML with whatever the Hermes environment ships (PyYAML or ruamel)."""
    text = path.read_text(encoding="utf-8")
    try:
        import yaml  # type: ignore

        return yaml.safe_load(text)
    except ImportError:
        pass
    try:
        from ruamel.yaml import YAML  # type: ignore

        return YAML(typ="safe").load(text)
    except ImportError:
        pytest.skip("no YAML parser available")


def sub(name: str):
    return sys.modules[f"{PKG_NAME}.{name}"] if f"{PKG_NAME}.{name}" in sys.modules else \
        importlib.import_module(f"{PKG_NAME}.{name}")


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    # Keep every temp file (prompt files, logs) inside pytest's basetemp.
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    for var in ("FAKE_DEVIN_SCENARIO", "FAKE_DEVIN_AUTH", "FAKE_DEVIN_LOG", "FAKE_DEVIN_PIDFILE",
                "DEVIN_PERMISSION_MODE", "TERMINAL_CWD"):
        monkeypatch.delenv(var, raising=False)
    yield


class Recorder:
    def __init__(self) -> None:
        self.events: list[dict] = []
        self.injected: list[tuple[str, Any]] = []
        self.gate_calls: list[tuple[str, str, str]] = []
        self.lock = threading.Lock()

    def listener(self, payload: dict) -> None:
        with self.lock:
            self.events.append(payload)

    def types(self) -> list[str]:
        with self.lock:
            return [e["type"] for e in self.events]


class Harness:
    def __init__(self, tmp_path: Path, monkeypatch, settings: dict | None = None, *, scenario: str = "ok",
                 auth: str = "ok", gate_answer: bool = True, interrupted=None, platform: str | None = None,
                 cli_override: list[str] | None = None):
        config_mod = sub("config")
        core = sub("core")
        runner = sub("runner")
        events = sub("events")
        store_mod = sub("store")
        self.tmp = tmp_path
        self.workdir = tmp_path / "work"
        self.workdir.mkdir(exist_ok=True)
        self.log = tmp_path / "devin-log.jsonl"
        self.settings = {"default_cwd": str(self.workdir), "cancel_grace_seconds": 1.0}
        self.settings.update(settings or {})
        monkeypatch.setenv("FAKE_DEVIN_SCENARIO", scenario)
        monkeypatch.setenv("FAKE_DEVIN_AUTH", auth)
        monkeypatch.setenv("FAKE_DEVIN_LOG", str(self.log))
        self.rec = Recorder()
        self.gate_answer = gate_answer
        self.bus = events.EventBus(plugin_id="devin", emit=None, broadcast=None, chatty_interval=0.0)
        self.bus.subscribe(self.rec.listener)
        self.store = store_mod.RunStore(tmp_path / "data", throttle_seconds=0.0)
        self.spawned: list[Any] = []

        def spawn(argv, **kw):
            tree = sub("process").ProcessTree(argv, **kw)
            self.spawned.append(tree)
            return tree

        self.manager = runner.RunManager(
            bus=self.bus, store=self.store, spawn=spawn,
            injector=lambda text, key: (self.rec.injected.append((text, key)) or True),
            temp_dir=str(tmp_path),
            cli_override=cli_override if cli_override is not None else [sys.executable, str(FAKE_DEVIN)],
        )
        self.config = config_mod.ConfigSource(reader=lambda: self.settings, platform=platform)
        self._interrupt = threading.Event()
        self._yield = threading.Event()
        self.plugin = core.DevinPlugin(
            config=self.config, manager=self.manager, gate=self.gate,
            interrupted=interrupted or self._interrupt.is_set, session_key=lambda: "sess-key-1",
            yield_requested=self._take_yield)

    def gate(self, tool: str, reason: str, rule_key: str) -> dict:
        self.rec.gate_calls.append((tool, reason, rule_key))
        if self.gate_answer:
            return {"approved": True}
        return {"approved": False, "message": "BLOCKED: user denied"}

    def _take_yield(self) -> bool:
        if self._yield.is_set():
            self._yield.clear()
            return True
        return False

    def yield_after(self, seconds: float) -> None:
        threading.Timer(seconds, self._yield.set).start()

    def interrupt_after(self, seconds: float) -> None:
        threading.Timer(seconds, self._interrupt.set).start()

    def call(self, tool: str, args: dict, *, via_hook: bool = True, session_id: str = "s1") -> dict:
        """Run a tool the way Hermes does: pre_tool_call first, then the handler."""
        if tool == "delegate_to_devin" and via_hook:
            directive = self.plugin.pre_tool_call(tool_name=tool, args=args, task_id="t1", session_id=session_id)
            if directive and directive.get("action") == "block":
                return json.loads(directive["message"])
            if directive and directive.get("action") == "approve":
                verdict = self.gate(tool, directive.get("message", ""), directive.get("rule_key", ""))
                self.plugin.post_approval_response(pattern_key="plugin_rule:" + directive["rule_key"],
                                                   choice="once" if verdict.get("approved") else "deny")
                if not verdict.get("approved"):
                    return {"ok": False, "error": {"code": "blocked", "message": verdict["message"]}}
        handler = self.plugin.handlers()[tool]
        return json.loads(handler(args, task_id="t1", session_id=session_id))

    def log_entries(self) -> list[dict]:
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines() if line.strip()]

    def acp_methods(self) -> list[str]:
        return [e["msg"].get("method") for e in self.log_entries() if e.get("mode") == "acp" and e["msg"].get("method")]

    def close(self) -> None:
        self.manager.shutdown("test teardown", wait=10)


@pytest.fixture
def harness(tmp_path, monkeypatch):
    made: list[Harness] = []

    def make(settings: dict | None = None, **kwargs) -> Harness:
        h = Harness(tmp_path, monkeypatch, settings, **kwargs)
        made.append(h)
        return h

    yield make
    for h in made:
        h.close()
