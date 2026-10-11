"""Cloud uses ACP's own controls without inheriting Local permission promises."""

import json
import time

import pytest
from conftest import sub

controls = sub("session_controls")
approval = sub("approval")


def pick(h, **kwargs):
    return h.plugin.session_options({"target": "cloud", "values": {}, "cloud_permissions_ack": True, **kwargs})


def test_cloud_discovery_does_not_prompt_upload_or_leak_metadata(harness):
    h = harness({"model": "local-only", "sandbox": True})
    menu = pick(h, cloud_permissions_ack=False)
    assert set(menu["values"]) == controls.CLOUD_CONTROLS
    assert menu["permission_mode"] == "cloud-managed"
    assert "private.invalid" not in json.dumps(menu)
    assert "session/prompt" not in h.acp_methods()
    for entry in h.log_entries():
        if entry.get("mode") != "acp":
            continue
        assert "--cloud" in entry["argv"] and "--model" not in entry["argv"] and "--sandbox" not in entry["argv"]
        assert entry["env_mode"] is None
        if entry["msg"].get("method") == "session/new":
            assert entry["msg"]["params"] == {"cwd": "/", "mcpServers": []}
    assert all(tree.poll() is not None for tree in h.spawned)
    denied = h.call("delegate_to_devin", {"prompt": "hello", "launch_id": menu["launch_id"]})
    assert denied["error"]["code"] == "cloud_consent_required"
    assert not h.rec.gate_calls


def test_cloud_selection_needs_hermes_approval_and_has_separate_defaults(harness):
    h = harness(scenario="config")
    local = h.plugin.session_options({"values": {}})
    local_cfg = h.plugin.controls.resolve(local["launch_id"], h.config.load()).config
    h.plugin.controls.remember(local_cfg)
    cloud = pick(h, values={"devin_version": "devin_lite", "repos": "test/app", "platform": "macos"})
    result = h.call("delegate_to_devin", {"prompt": "hello", "launch_id": cloud["launch_id"]})
    assert result["ok"], result
    run = h.manager.get(result["run_id"])
    assert run.execution_target == "cloud" and run.permission_mode == "normal" and run.cloud_permissions_ack
    assert run.model == "devin_lite" and run.remote_state == "idle" and run.model_confirmed
    assert "Cloud manages permissions" in h.rec.gate_calls[0][1]
    assert "session/set_mode" not in h.acp_methods()
    assert h.plugin.controls.defaults()["model"] == "base-medium"
    assert h.plugin.controls.defaults("cloud")["devin_version"] == "devin_lite"
    assert run.cloud_url() == "https://app.devin.ai/sessions/" + "1" * 32
    assert "never-save" not in json.dumps(run.snapshot()) and "private.invalid" not in json.dumps(run.snapshot())
    child_result = h.plugin.follow_up(run.id, "more")
    assert child_result["ok"], child_result
    child = h.manager.get(child_result["run"]["id"])
    assert child.settled.wait(20)
    assert child.state.value == "succeeded", child.error
    assert "model" not in child.session_config
    assert child.execution_target == "cloud" and child.remote_state == "idle"
    assert child.devin_session_id == run.devin_session_id
    assert "REPLAYED" not in child.output.value()
    assert all(
        e["msg"]["params"]["cwd"] == "/"
        for e in h.log_entries()
        if e.get("mode") == "acp" and e["msg"].get("method") == "session/load"
    )


@pytest.mark.parametrize("scenario,expected", [("slow", "cancelled"), ("hang", "unknown")])
def test_cloud_stop_distinguishes_remote_ack_from_relay_exit(harness, scenario, expected):
    h = harness({"cancel_grace_seconds": 1}, scenario=scenario)
    menu = pick(h)
    result = h.call("delegate_to_devin", {"prompt": "wait", "launch_id": menu["launch_id"], "background": True})
    run = h.manager.get(result["run_id"])
    deadline = time.monotonic() + 10
    while run.remote_state != "running" and time.monotonic() < deadline:
        time.sleep(0.02)
    assert run.remote_state == "running"
    time.sleep(0.1)
    h.manager.cancel(run.id, "test stop")
    assert run.settled.wait(15)
    assert run.remote_state == expected
    assert run.kill_report["remote_cancel_confirmed"] is (expected == "cancelled")
    assert run.state.value == ("cancelled" if expected == "cancelled" else "failed")
    cancels = [
        e["msg"] for e in h.log_entries() if e.get("mode") == "acp" and e["msg"].get("method") == "session/cancel"
    ]
    assert cancels and "id" not in cancels[-1]


def test_local_and_cloud_approval_identity_cannot_be_reused(harness):
    h = harness()
    cfg = h.config.load()
    cloud = controls.LaunchConfig.from_config(cfg, execution_target="cloud", cloud_permissions_ack=True)
    assert approval.rule_key_for(cfg, "same", "/w") != approval.rule_key_for(cloud, "same", "/w")


def test_reconnect_only_loads_and_unknown_activity_blocks_followups(harness):
    h = harness()
    menu = pick(h)
    result = h.call("delegate_to_devin", {"prompt": "hello", "launch_id": menu["launch_id"]})
    run = h.manager.get(result["run_id"])
    before = h.acp_methods().count("session/prompt")
    assert h.plugin.reconnect(run.id)["connection"] == "reachable"
    assert h.acp_methods().count("session/prompt") == before
    run.remote_state = "unknown"
    assert h.plugin.follow_up(run.id, "more")["error"]["code"] == "cloud_state_unknown"
    assert h.plugin.reconnect(run.id)["ok"]
    assert run.remote_state == "unknown"
    assert h.plugin.reconnect("dv-0000000000")["ok"] is False


def test_cloud_disconnect_and_config_changes_are_not_hidden(harness):
    crashed = harness(scenario="crash")
    menu = pick(crashed)
    result = crashed.call("delegate_to_devin", {"prompt": "hello", "launch_id": menu["launch_id"]})
    assert not result["ok"] and crashed.manager.get(result["run_id"]).remote_state == "unknown"
    changed = harness(scenario="config_changed")
    menu = pick(changed)
    result = changed.call("delegate_to_devin", {"prompt": "hello", "launch_id": menu["launch_id"]})
    run = changed.manager.get(result["run_id"])
    assert not result["ok"] and run.remote_state == "cancelled"
    assert run.session_config["devin_version"] == "devin-auto"
    assert not run.model_confirmed


@pytest.mark.parametrize("values", [{"model": "base-medium"}, {"mode": "bypass"}, {"platform": "unknown"}])
def test_cloud_cannot_take_local_controls_or_invented_values(harness, values):
    h = harness()
    with pytest.raises(controls.ControlsError):
        pick(h, values=values)
    assert "session/prompt" not in h.acp_methods()


def test_cloud_orphan_keeps_remote_activity_unknown(tmp_path):
    store = sub("store").RunStore(tmp_path)
    record = {"id": "dv-1234567890", "state": "running", "execution_target": "cloud", "remote_state": "running"}
    store.save(record, force=True)
    assert store.mark_orphans(set()) == 1
    assert store.load(record["id"])["remote_state"] == "unknown"


def test_cloud_followup_never_falls_back_to_local_without_saved_settings(harness):
    h = harness()
    menu = pick(h)
    result = h.call("delegate_to_devin", {"prompt": "hello", "launch_id": menu["launch_id"]})
    run = h.manager.get(result["run_id"])
    run.session_config = {}
    assert h.plugin.follow_up(run.id, "more")["error"]["code"] == "not_resumable"


def test_cloud_ignores_local_workspace_limits(harness, tmp_path):
    # allowed_roots that do not cover Hermes' own working directory must not block
    # Cloud: it never works in a local folder.
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    h = harness({"allowed_roots": [str(elsewhere)]}, scenario="config")
    with pytest.raises(Exception) as refused:
        h.plugin.session_options({"values": {}})
    assert getattr(refused.value, "code", None) == "cwd_not_allowed"
    menu = pick(h, values={"devin_version": "devin_lite", "repos": "test/app", "platform": "macos"})
    assert menu["cwd"] == "/"
    result = h.call("delegate_to_devin", {"prompt": "hello", "launch_id": menu["launch_id"]})
    assert result["ok"], result
    assert h.manager.get(result["run_id"]).execution_target == "cloud"
