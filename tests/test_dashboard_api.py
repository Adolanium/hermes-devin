"""The read-only panel backend reads what the store writes."""

from __future__ import annotations

import importlib.util

from conftest import PLUGIN_DIR, sub

models = sub("models")
store_mod = sub("store")


def _api():
    spec = importlib.util.spec_from_file_location("devin_panel_api_under_test", PLUGIN_DIR / "dashboard" / "plugin_api.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_lists_and_reads_stored_runs(tmp_path):
    st = store_mod.RunStore(tmp_path, throttle_seconds=0)
    run = models.Run(id=models.new_run_id(), prompt="secret prompt", title="t", cwd="/w", backend="acp",
                     permission_mode="normal", model="", background=False, timeout_seconds=60,
                     output=models.CappedText(1000))
    run.output.append("result text")
    st.save(run.snapshot())
    api = _api()
    root = tmp_path / "runs"
    rows = api.list_runs(root)
    assert [r["id"] for r in rows] == [run.id]
    assert "prompt" not in rows[0] and "output" not in rows[0]
    assert api.list_runs(root, "finished") == []
    assert api.list_runs(root, "active")[0]["state"] == "created"
    detail = api.get_run(root, run.id)
    assert "output" not in detail and "prompt" not in detail
    assert api.get_run(root, run.id, include_output=True)["output"] == "result text"
    assert api.get_run(root, "../etc") is None
    assert api.list_runs(None) == []
