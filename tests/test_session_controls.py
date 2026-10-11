"""Model configuration is server-driven and cannot replace human approval."""

from __future__ import annotations

import json
from dataclasses import replace

import pytest
from conftest import sub

controls = sub("session_controls")
approval = sub("approval")
FUSION = "fusion-frontier-medium-sidekick-economy-medium"


def test_discovery_selects_fusion_and_updates_thinking_without_a_prompt(harness):
    h = harness(scenario="config")
    result = h.plugin.session_options({"values": {"model": FUSION, "thought_level": "low"}})
    assert result["values"] == {"model": FUSION, "thought_level": "low"}
    assert [option["id"] for option in result["config_options"]] == ["model", "thought_level"]
    model = result["config_options"][0]
    fusion = next(choice for choice in model["options"] if choice["value"] == FUSION)
    assert fusion["family"] == "Fusion" and fusion["cost_summary"]
    assert "session/prompt" not in h.acp_methods()
    assert len(h.rec.gate_calls) == 0
    assert all(tree.poll() is not None for tree in h.spawned)


def test_selection_still_requires_approval_is_single_use_and_restores_permissions(harness):
    h = harness(scenario="config")
    selection = h.plugin.session_options({"values": {"model": FUSION, "thought_level": "low"}})
    args = {"prompt": "Fix the test", "launch_id": selection["launch_id"]}
    result = h.call("delegate_to_devin", args)
    assert result["ok"]
    run = h.manager.get(result["run_id"])
    assert run.model == FUSION and run.model_confirmed
    assert run.session_config["thought_level"] == "low"
    assert run.permission_mode == "normal"
    assert "Fusion (Frontier Medium + Economy Medium)" in h.rec.gate_calls[0][1]
    assert "thought_level: low" in h.rec.gate_calls[0][1]
    assert run.approval.via == "pre_tool_call"
    task_methods = h.acp_methods()
    assert task_methods[-2:] == ["session/set_mode", "session/prompt"]
    failed = h.call("delegate_to_devin", args)
    assert not failed["ok"] and failed["error"]["code"] == "selection_invalid"
    assert h.acp_methods().count("session/prompt") == 1


def test_selection_does_not_authorize_itself_when_gate_denies(harness):
    h = harness(scenario="config", gate_answer=False)
    selection = h.plugin.session_options({"values": {"model": FUSION}})
    result = h.call("delegate_to_devin", {"prompt": "Edit", "launch_id": selection["launch_id"]})
    assert not result["ok"] and "session/prompt" not in h.acp_methods()


def test_selected_workspace_and_settings_cannot_drift(harness, tmp_path):
    h = harness(scenario="config")
    result = h.plugin.session_options({})
    other = tmp_path / "other"
    other.mkdir()
    args = {"prompt": "Edit", "launch_id": result["launch_id"], "cwd": str(other)}
    assert h.call("delegate_to_devin", args)["error"]["code"] == "selection_invalid"
    h.settings["model"] = "other-model"
    args.pop("cwd")
    assert h.call("delegate_to_devin", args)["error"]["code"] == "selection_invalid"
    assert "session/prompt" not in h.acp_methods()


def test_old_ticket_cannot_approve_changed_settings(harness):
    h = harness(scenario="config")
    args = {"prompt": "Edit"}
    h.plugin.pre_tool_call(tool_name="delegate_to_devin", args=args, session_id="s1")
    h.settings["model"] = "base-medium"
    h.gate_answer = False
    result = json.loads(h.plugin.delegate(args, session_id="s1"))
    assert not result["ok"] and result["error"]["code"] == "not_approved"
    assert h.rec.gate_calls and "session/prompt" not in h.acp_methods()


def test_model_and_options_are_part_of_remembered_approval(harness):
    h = harness()
    cfg = h.config.load()
    chosen = controls.LaunchConfig.from_config(cfg, model=FUSION, session_options=(("thought_level", "high"),))
    assert approval.rule_key_for(cfg, "same", "/w") != approval.rule_key_for(chosen, "same", "/w")
    assert approval.rule_key_for(chosen, "same", "/w") != approval.rule_key_for(
        replace(chosen, session_options=(("thought_level", "low"),)), "same", "/w"
    )


@pytest.mark.parametrize("values", [{"mode": "bypass"}, {"thought_level": "low"}, {"model": "invented"}])
def test_invalid_or_permission_selections_are_refused(harness, values):
    h = harness(scenario="config")
    with pytest.raises(controls.ControlsError):
        h.plugin.session_options({"values": values})
    assert "session/prompt" not in h.acp_methods()
    assert all(tree.poll() is not None for tree in h.spawned)


def test_rejected_runtime_configuration_never_sends_a_prompt(harness, monkeypatch):
    h = harness(scenario="config")
    selection = h.plugin.session_options({"values": {"model": FUSION, "thought_level": "low"}})
    monkeypatch.setenv("FAKE_DEVIN_SCENARIO", "config_rejected")
    result = h.call("delegate_to_devin", {"prompt": "Edit", "launch_id": selection["launch_id"]})
    assert not result["ok"]
    assert "session/prompt" not in h.acp_methods()


def test_followup_retains_confirmed_model_even_if_default_changes(harness):
    h = harness(scenario="config")
    selection = h.plugin.session_options({"values": {"model": FUSION, "thought_level": "low"}})
    result = h.call("delegate_to_devin", {"prompt": "First", "launch_id": selection["launch_id"]})
    h.settings["model"] = "base-medium"
    child = h.plugin.follow_up(result["run_id"], "Second")
    assert child["ok"]
    run = h.manager.get(child["run"]["id"])
    assert run.settled.wait(10)
    assert run.state.value == "succeeded" and run.model == FUSION
    assert run.session_config["thought_level"] == "low"
    persisted = h.store.load(run.id)
    assert persisted["model"] == FUSION and persisted["session_config"]["thought_level"] == "low"


def test_unapproved_runtime_fallback_is_cancelled(harness, monkeypatch):
    h = harness(scenario="config")
    selection = h.plugin.session_options({"values": {"model": FUSION}})
    monkeypatch.setenv("FAKE_DEVIN_SCENARIO", "config_changed")
    result = h.call("delegate_to_devin", {"prompt": "Edit", "launch_id": selection["launch_id"]})
    assert not result["ok"]
    run = h.manager.get(result["run_id"])
    assert run.state.value == "cancelled" and "fallback" in run.cancel_reason
    assert run.model == FUSION and run.session_config["model"] == FUSION
    assert not run.model_confirmed


def test_expired_selection_is_not_reused(harness):
    h = harness(scenario="config")
    selection = h.plugin.session_options({})
    h.plugin.controls.clock = lambda: 10**12
    args = {"prompt": "Edit", "launch_id": selection["launch_id"]}
    assert h.call("delegate_to_devin", args)["error"]["code"] == "selection_invalid"


def test_grouped_choices_are_flattened_and_sensitive_metadata_is_omitted():
    result = controls.model_options(
        [
            {
                "id": "model",
                "category": "model",
                "type": "select",
                "currentValue": "m",
                "_meta": {"secret": "not-for-the-ui"},
                "options": [
                    {"name": "Family", "options": [{"name": "Model", "value": "m", "_meta": {"token": "hidden"}}]}
                ],
            },
            {"id": "mode", "category": "model", "type": "select", "currentValue": "bypass", "options": []},
        ]
    )
    assert len(result) == 1 and result[0]["options"] == [{"name": "Model", "value": "m", "group": "Family"}]
    assert "_meta" not in json.dumps(result) and "not-for-the-ui" not in json.dumps(result)


@pytest.mark.parametrize(
    "body", [{"cwd": 3}, {"values": []}, {"values": {"model": 3}}, {"model": FUSION}, {"target": "cloud"}]
)
def test_discovery_body_rejects_untyped_overrides(harness, body):
    h = harness(scenario="config")
    with pytest.raises(controls.ControlsError):
        h.plugin.session_options(body)
    assert not h.spawned


def test_defaults_are_remembered_only_after_approval_and_keep_other_targets(harness):
    h = harness(scenario="config", gate_answer=False)
    saved = h.store.root.parent / "session-preferences.json"
    saved.parent.mkdir(parents=True, exist_ok=True)
    saved.write_text(json.dumps({"cloud": {"devin_version": "remote-choice"}}))
    selection = h.plugin.session_options({"values": {"model": FUSION, "thought_level": "low"}})
    assert h.plugin.controls.defaults() == {}
    denied = h.call("delegate_to_devin", {"prompt": "Edit", "launch_id": selection["launch_id"]})
    assert not denied["ok"] and h.plugin.controls.defaults() == {}
    h.gate_answer = True
    approved = h.call("delegate_to_devin", {"prompt": "Edit", "launch_id": selection["launch_id"]})
    assert approved["ok"]
    assert json.loads(saved.read_text())["cloud"] == {"devin_version": "remote-choice"}
    reloaded = h.plugin.session_options({})
    assert reloaded["values"] == {"model": FUSION, "thought_level": "low"}
    explicit_reset = h.plugin.session_options({"values": {}})
    assert explicit_reset["values"]["model"] == "base-medium"


def test_stale_default_is_refused_instead_of_falling_back(harness):
    h = harness(scenario="config")
    saved = h.store.root.parent / "session-preferences.json"
    saved.parent.mkdir(parents=True, exist_ok=True)
    saved.write_text(json.dumps({"local": {"model": "removed-model"}}))
    with pytest.raises(controls.ControlsError, match="no longer available"):
        h.plugin.session_options({})
    assert "session/prompt" not in h.acp_methods()


def test_registry_has_a_bounded_number_of_single_use_selections(harness):
    h = harness()
    cfg = h.config.load()
    options = controls.model_options(
        [
            {
                "id": "model",
                "category": "model",
                "type": "select",
                "currentValue": "m",
                "options": [{"value": "m", "name": "M"}],
            }
        ]
    )
    tokens = [h.plugin.controls.issue(cfg, str(h.workdir), options) for _ in range(controls.MAX_SELECTIONS + 1)]
    with pytest.raises(controls.ControlsError):
        h.plugin.controls.resolve(tokens[0], cfg)
    assert h.plugin.controls.resolve(tokens[-1], cfg).cwd == str(h.workdir)


def test_server_canonical_model_id_after_thinking_change_is_the_launch_selection(harness):
    h = harness(scenario="config_canonical")
    selected = h.plugin.session_options({"values": {"model": FUSION, "thought_level": "high"}})
    canonical = "fusion-frontier-high-sidekick-economy-medium"
    assert selected["values"]["model"] == canonical
    result = h.call("delegate_to_devin", {"prompt": "First", "launch_id": selected["launch_id"]})
    assert result["ok"]
    run = h.manager.get(result["run_id"])
    assert run.model == canonical and run.model_confirmed
    assert run.session_config == {"model": canonical, "thought_level": "high"}


def test_empty_runtime_options_clear_confirmation_and_cancel_selected_run(harness):
    from types import SimpleNamespace

    backend = sub("backends").AcpBackend()
    backend._session_id = "selected-session"
    backend._config_ready = True
    backend._selected_values = {"model": FUSION}
    run = sub("models").Run(
        id="dv-test",
        prompt="p",
        title="t",
        cwd="/w",
        backend="acp",
        permission_mode="normal",
        model=FUSION,
        background=True,
        timeout_seconds=60,
        model_confirmed=True,
        session_config={"model": FUSION},
    )
    cancelled = []
    io = SimpleNamespace(run=run, emit=lambda *args, **kwargs: None, cancel=cancelled.append)
    update = {"sessionId": "foreign-session", "update": {"sessionUpdate": "config_option_update", "configOptions": []}}
    backend._on_update(io, update)
    assert run.model_confirmed and not cancelled
    update["sessionId"] = "selected-session"
    backend._on_update(io, update)
    assert not run.model_confirmed and run.session_config == {"model": FUSION} and cancelled


@pytest.mark.parametrize("kind", ["config_option_update", "current_mode_update"])
def test_runtime_permission_changes_cannot_widen_a_selected_run(harness, kind):
    from types import SimpleNamespace

    backend = sub("backends").AcpBackend()
    backend._session_id = "selected-session"
    backend._config_ready = True
    backend._selected_mode = "normal"
    backend._selected_values = {}
    run = sub("models").Run(
        id="dv-test",
        prompt="p",
        title="t",
        cwd="/w",
        backend="acp",
        permission_mode="normal",
        model="",
        background=True,
        timeout_seconds=60,
    )
    cancelled = []
    io = SimpleNamespace(run=run, emit=lambda *args, **kwargs: None, cancel=cancelled.append)
    backend._on_update(
        io,
        {
            "sessionId": "selected-session",
            "update": {
                "sessionUpdate": kind,
                "currentModeId": "bypass",
                "configOptions": [{"id": "mode", "currentValue": "bypass"}],
            },
        },
    )
    assert cancelled and "permission mode" in cancelled[0]


def test_unload_stops_an_in_progress_discovery_without_sending_a_prompt(harness):
    import threading
    import time

    h = harness(scenario="config_discovery_hang")
    errors = []

    def discover():
        try:
            h.plugin.session_options({})
        except controls.ControlsError as exc:
            errors.append(exc)

    worker = threading.Thread(target=discover)
    worker.start()
    deadline = time.monotonic() + 5
    while not h.spawned and time.monotonic() < deadline:
        time.sleep(0.01)
    assert h.spawned
    h.plugin.on_unload()
    worker.join(10)
    assert not worker.is_alive() and errors
    assert all(tree.poll() is not None for tree in h.spawned)
    assert "session/prompt" not in h.acp_methods()


def test_selected_models_require_an_advertised_permission_mode():
    from types import SimpleNamespace

    backend = sub("backends").AcpBackend()
    io = SimpleNamespace(run=SimpleNamespace(permission_mode="normal"))
    with pytest.raises(RuntimeError, match="permission modes"):
        backend._select_mode(io, None, {}, force=True)
