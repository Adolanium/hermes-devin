"""The read-only panel backend reads what the store writes."""

from __future__ import annotations

import importlib.util

from conftest import PLUGIN_DIR, sub

models = sub("models")
store_mod = sub("store")


def _api():
    spec = importlib.util.spec_from_file_location(
        "devin_panel_api_under_test", PLUGIN_DIR / "dashboard" / "plugin_api.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_lists_and_reads_stored_runs(tmp_path):
    st = store_mod.RunStore(tmp_path, throttle_seconds=0)
    run = models.Run(
        id=models.new_run_id(),
        prompt="secret prompt",
        title="t",
        cwd="/w",
        backend="acp",
        permission_mode="normal",
        model="",
        background=False,
        timeout_seconds=60,
        output=models.CappedText(1000),
    )
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


def test_run_lists_use_distinct_opaque_profile_scopes(tmp_path):
    api = _api()
    responses = []
    for name in ("default", "second"):
        root = tmp_path / name / "runs"
        responses.append(api.run_list_response(root))
    assert responses[0]["runs"] == responses[1]["runs"] == []
    assert responses[0]["profile_scope"] != responses[1]["profile_scope"]
    assert all(len(body["profile_scope"]) == 64 and str(tmp_path) not in str(body) for body in responses)


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
    while not h.manager.get(run_id).files_changed() and __import__("time").time() < deadline:
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


def test_live_model_controls_use_active_profile_registry_not_other_modules(monkeypatch):
    import sys
    import types

    api = _api()
    expected = types.SimpleNamespace(manager=object())
    foreign = types.SimpleNamespace(manager=object())
    current_module = types.SimpleNamespace(TOOLSET="delegation", get_plugin=lambda: expected)
    foreign_module = types.SimpleNamespace(TOOLSET="delegation", get_plugin=lambda: foreign)
    monkeypatch.setitem(sys.modules, "hermes_plugins.foreign", foreign_module)
    registry = {"devin": types.SimpleNamespace(enabled=True, module=current_module)}
    host = types.ModuleType("hermes_cli.plugins")
    host.get_plugin_manager = lambda: types.SimpleNamespace(_plugins=registry)
    loaded = []
    host.discover_plugins = lambda: loaded.append(True)
    parent = types.ModuleType("hermes_cli")
    parent.__path__ = []
    monkeypatch.setitem(sys.modules, "hermes_cli", parent)
    monkeypatch.setitem(sys.modules, "hermes_cli.plugins", host)
    assert api.live_plugin(load=True) is expected and loaded == [True]
    registry["devin"].enabled = False
    assert api.live_plugin() is None
    registry.clear()
    assert api.live_plugin() is None
