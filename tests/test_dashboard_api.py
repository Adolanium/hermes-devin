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


def test_manifest_mounts_the_api_without_a_web_tab():
    import json

    manifest = json.loads((PLUGIN_DIR / "dashboard" / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["name"] == "devin" and manifest["api"] == "plugin_api.py"
    assert manifest["tab"]["hidden"] is True
    assert (PLUGIN_DIR / "dashboard" / manifest["entry"]).is_file()


def test_live_runs_win_and_can_be_cancelled(harness, tmp_path):
    h = harness(scenario="slow")
    started = h.call("delegate_to_devin", {"prompt": "x", "background": True})
    run_id = started["run_id"]
    api = _api()
    root = tmp_path / "data" / "runs"
    deadline = __import__("time").time() + 10
    while h.manager.get(run_id).state.value != "running" and __import__("time").time() < deadline:
        __import__("time").sleep(0.05)
    rows = api.list_runs(root, "active", manager=h.manager)
    assert [r["id"] for r in rows] == [run_id] and rows[0]["state"] == "running"
    assert rows[0]["files_changed"] == ["app.py"]
    detail = api.get_run(root, run_id, manager=h.manager)
    assert detail["live"] is True and "prompt" not in detail and "output" not in detail
    assert api.cancel_run(run_id, None)[0] == 409
    assert api.cancel_run("../x", h.manager)[0] == 400
    status, body = api.cancel_run(run_id, h.manager)
    assert status == 200 and body["run"]["state"] in ("cancelling", "cancelled")
    assert h.manager.get(run_id).settled.wait(20)
    assert h.manager.get(run_id).state.value == "cancelled"
    assert h.manager.get(run_id).cancel_reason == "cancelled from Hermes Desktop"
