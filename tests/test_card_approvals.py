"""Devin's requests in background runs started from Hermes Desktop are answered on the run card."""

from __future__ import annotations

import importlib.util
import time

from conftest import PLUGIN_DIR, sub

runner = sub("runner")


def _wait_pending(run, timeout: float = 10.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        with run._lock:
            pending = list(run.pending_permissions.values())
        if pending:
            return pending[0]
        time.sleep(0.02)
    raise AssertionError("no pending request appeared")


def _api():
    spec = importlib.util.spec_from_file_location("devin_api_cards", PLUGIN_DIR / "dashboard" / "plugin_api.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _start_background(h):
    started = h.call("delegate_to_devin", {"prompt": "x", "background": True})
    return h.manager.get(started["run_id"])


def test_background_desktop_run_asks_on_the_card(harness, monkeypatch):
    monkeypatch.setenv("HERMES_SESSION_PLATFORM", "desktop")
    h = harness(scenario="permission")
    run = _start_background(h)
    first = _wait_pending(run)
    assert first["via"] == "card" and "rm -rf build" in first["title"] and first["expires_at"] > time.time()
    assert run.summary()["pending_permissions"][0]["ask_id"] == first["ask_id"]
    assert any(p.get("event", {}).get("kind") == "permission_request" for p in h.rec.events)
    assert h.manager.answer_permission(run.id, first["ask_id"], True) == "answered"
    assert h.manager.answer_permission(run.id, first["ask_id"], True) == "no_request"
    deadline = time.time() + 10
    while time.time() < deadline:
        second = _wait_pending(run)
        if second["ask_id"] != first["ask_id"]:
            break
    assert "Edit main.py" in second["title"]
    assert h.manager.answer_permission(run.id, second["ask_id"], False) == "answered"
    assert run.settled.wait(20)
    out = run.output.value()
    assert "permission outcome: selected:yes" in out and "edit outcome: n" in out
    assert [(p.decision, p.reason) for p in run.permissions] == [
        ("allowed", "allowed on the run card in Hermes Desktop"),
        ("denied", "denied on the run card in Hermes Desktop")]
    assert run.pending_permissions == {}
    assert len(h.rec.gate_calls) == 1  # only the run approval


def test_unanswered_card_request_is_denied_after_the_timeout(harness, monkeypatch):
    monkeypatch.setenv("HERMES_SESSION_PLATFORM", "desktop")
    original = runner._RunIO.__init__

    def short(self, *args, **kwargs):
        original(self, *args, **kwargs)
        self.card_timeout = 0.3

    monkeypatch.setattr(runner._RunIO, "__init__", short)
    h = harness(scenario="permission")
    run = _start_background(h)
    assert run.settled.wait(20)
    assert [p.decision for p in run.permissions] == ["denied", "denied"]
    assert run.permissions[0].reason == "no answer on the run card within 0.3s"


def test_cancel_releases_a_waiting_card_request(harness, monkeypatch):
    monkeypatch.setenv("HERMES_SESSION_PLATFORM", "desktop")
    h = harness(scenario="permission")
    run = _start_background(h)
    _wait_pending(run)
    h.manager.cancel(run.id, "test")
    assert run.settled.wait(20)
    assert run.permissions[0].reason == "run stopped before the request was answered"
    assert run.pending_permissions == {}


def test_background_outside_desktop_still_denies(harness):
    h = harness(scenario="permission")
    run = _start_background(h)
    assert run.settled.wait(20)
    assert all(p.reason == "no one can answer prompts for a background run" for p in run.permissions)


def test_card_approvals_setting_off_denies(harness, monkeypatch):
    monkeypatch.setenv("HERMES_SESSION_PLATFORM", "desktop")
    h = harness({"card_approvals": False}, scenario="permission")
    run = _start_background(h)
    assert run.settled.wait(20)
    assert all(p.decision == "denied" for p in run.permissions)


def test_foreground_request_is_marked_pending_while_the_prompt_is_up(harness):
    h = harness(scenario="permission")
    seen = []
    original = h.gate

    def gate(tool, reason, rule):
        if rule.startswith("devin:perm:"):
            run = h.manager.list()[0]
            with run._lock:
                seen.append([p["via"] for p in run.pending_permissions.values()])
        return original(tool, reason, rule)

    h.plugin.gate = gate
    result = h.call("delegate_to_devin", {"prompt": "x"}, via_hook=False)
    assert result["state"] == "succeeded"
    assert seen and all(v == ["prompt"] for v in seen)
    assert h.manager.get(result["run_id"]).pending_permissions == {}


def test_api_answers_card_requests(harness, monkeypatch):
    monkeypatch.setenv("HERMES_SESSION_PLATFORM", "desktop")
    h = harness(scenario="permission")
    api = _api()
    run = _start_background(h)
    ask = _wait_pending(run)
    assert api.answer_permission(run.id, ask["ask_id"], "yes", h.manager)[0] == 400
    assert api.answer_permission("../x", ask["ask_id"], True, h.manager)[0] == 400
    assert api.answer_permission(run.id, ask["ask_id"], True, None)[0] == 409
    assert api.answer_permission(run.id, "abcdef123456", True, h.manager)[0] == 404
    assert api.answer_permission(run.id, ask["ask_id"], True, h.manager) == (200, {"ok": True, "allowed": True})
    h.manager.cancel(run.id, "done")
    assert run.settled.wait(20)
