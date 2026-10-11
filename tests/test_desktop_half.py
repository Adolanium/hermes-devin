"""Render smoke test for the Desktop half (desktop/plugin.js), run under Node.

It needs Node and a ``node_modules`` with ``react`` and ``react-dom``: the one
from a Hermes Agent install (``HERMES_DESKTOP_NODE_MODULES``, or the usual
install locations). Without them the test is skipped.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent


def _node_modules() -> Path | None:
    candidates = [os.environ.get("HERMES_DESKTOP_NODE_MODULES", "")]
    for root in (os.environ.get("HERMES_AGENT_DIR", ""),
                 str(Path(os.environ.get("LOCALAPPDATA", "")) / "hermes" / "hermes-agent"),
                 str(Path.home() / ".hermes" / "hermes-agent")):
        if root:
            candidates += [str(Path(root) / "node_modules"), str(Path(root) / "apps" / "desktop" / "node_modules")]
    for candidate in candidates:
        if candidate and (Path(candidate) / "react").is_dir() and (Path(candidate) / "react-dom").is_dir():
            return Path(candidate)
    return None


def test_desktop_half_renders():
    node = shutil.which("node")
    modules = _node_modules()
    if node is None or modules is None:
        pytest.skip("needs node and a node_modules with react and react-dom")
    done = subprocess.run([node, str(HERE / "desktop_smoke.mjs"), str(modules)],
                          capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stdout + done.stderr
