"""Checks against the installed Hermes, when the tests run inside its venv.

The probe runs in a child process with HERMES_HOME pointed at a temp dir, so
importing Hermes cannot touch the real Hermes home. Skipped when Hermes is not
importable (for example on a machine without it).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

PROBE = r'''
import inspect, json
from hermes_cli import plugins
from tools import approval, interrupt, approval_context
import toolsets
ctx = plugins.PluginContext
out = {
    "hooks": sorted(h for h in plugins.VALID_HOOKS if h in ("pre_tool_call", "post_approval_response")),
    "register_tool": list(inspect.signature(ctx.register_tool).parameters),
    "register_command": list(inspect.signature(ctx.register_command).parameters),
    "inject_message": list(inspect.signature(ctx.inject_message).parameters),
    "on_unload": hasattr(ctx, "on_unload"),
    "emit": hasattr(ctx, "emit"),
    "approval": list(inspect.signature(approval.request_tool_approval).parameters),
    "consume_yield": hasattr(interrupt, "consume_yield"),
    "is_interrupted": hasattr(interrupt, "is_interrupted"),
    "session_key": hasattr(approval_context, "get_current_session_key"),
    "delegation": "delegation" in toolsets.TOOLSETS,
}
try:
    from hermes_cli.plugin_events import broadcast_plugin_event
    out["broadcast"] = list(inspect.signature(broadcast_plugin_event).parameters)
except Exception as exc:
    out["broadcast"] = str(exc)
try:
    from plugins.plugin_storage import plugin_data_dir
    out["plugin_data_dir"] = True
except Exception:
    out["plugin_data_dir"] = False
print("PROBE" + json.dumps(out))
'''


def _hermes_root() -> Path | None:
    explicit = os.environ.get("HERMES_AGENT_DIR")
    candidates = [Path(explicit)] if explicit else []
    exe = Path(sys.executable).resolve()
    candidates += [p for p in exe.parents if p.name.endswith(".venv") or p.name == "venv"]
    for cand in candidates:
        root = cand if (cand / "hermes_cli").is_dir() else cand.parent
        if (root / "hermes_cli" / "plugins.py").is_file():
            return root
    return None


@pytest.fixture(scope="module")
def probe(tmp_path_factory):
    root = _hermes_root()
    if root is None:
        pytest.skip("Hermes is not installed next to this Python")
    home = tmp_path_factory.mktemp("hermes_home")
    env = dict(os.environ, HERMES_HOME=str(home), PYTHONDONTWRITEBYTECODE="1",
               PYTHONPATH=str(root) + os.pathsep + os.environ.get("PYTHONPATH", ""))
    done = subprocess.run([sys.executable, "-c", PROBE], cwd=str(home), env=env, capture_output=True,
                          text=True, timeout=180)
    line = next((ln for ln in done.stdout.splitlines() if ln.startswith("PROBE")), None)
    if line is None:
        pytest.skip(f"could not import Hermes: {done.stderr[-500:]}")
    return json.loads(line[len("PROBE"):])


def test_hooks_exist(probe):
    assert probe["hooks"] == ["post_approval_response", "pre_tool_call"]


def test_registration_api(probe):
    for name in ("name", "toolset", "schema", "handler", "emoji"):
        assert name in probe["register_tool"]
    for name in ("name", "handler", "description", "args_hint"):
        assert name in probe["register_command"]
    assert {"content", "role", "session_key"} <= set(probe["inject_message"])
    assert probe["on_unload"] and probe["emit"]


def test_runtime_api(probe):
    assert "rule_key" in probe["approval"]
    assert probe["consume_yield"] and probe["is_interrupted"] and probe["session_key"]
    assert probe["delegation"]
    assert probe["plugin_data_dir"]
    assert isinstance(probe["broadcast"], list) and len(probe["broadcast"]) >= 3
