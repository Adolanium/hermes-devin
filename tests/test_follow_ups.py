"""Follow-ups typed on a Desktop run card continue the same Devin session."""

from __future__ import annotations

import importlib.util

from conftest import PLUGIN_DIR


def _api():
    spec = importlib.util.spec_from_file_location("devin_api_follow", PLUGIN_DIR / "dashboard" / "plugin_api.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _finished_run(h):
    result = h.call("delegate_to_devin", {"prompt": "first task"})
    assert result["state"] == "succeeded", result
    return h.manager.get(result["run_id"])


def test_follow_up_resumes_the_devin_session(harness):
    h = harness()
    parent = _finished_run(h)
    gates = len(h.rec.gate_calls)
    started = h.plugin.follow_up(parent.id, "  also add tests  ")
    assert started["ok"] is True, started
    child = h.manager.get(started["run"]["id"])
    assert child.settled.wait(20)
    assert child.state.value == "succeeded"
    assert child.parent_run_id == parent.id and child.desktop and child.background
    assert child.title == "Follow-up: also add tests" and child.prompt == "also add tests"
    assert child.approval.via == "desktop_card" and child.approval.decided_by == "user"
    assert len(h.rec.gate_calls) == gates  # sending it was the approval
    out = child.output.value()
    assert "Task was: also add tests" in out and "REPLAYED" not in out
    assert all("REPLAYED" not in e.text for e in child.events)
    loads = [e["msg"] for e in h.log_entries() if e["msg"].get("method") == "session/load"]
    assert loads and loads[-1]["params"]["sessionId"] == parent.devin_session_id
    assert child.devin_session_id == parent.devin_session_id
    assert child.summary()["parent_run_id"] == parent.id


def test_follow_up_refusals(harness):
    h = harness()
    parent = _finished_run(h)
    assert h.plugin.follow_up(parent.id, "   ")["error"]["code"] == "invalid_arguments"
    assert h.plugin.follow_up("dv-0000000000", "x")["error"]["code"] == "not_found"
    printed = harness({"backend": "print"})
    p = printed.call("delegate_to_devin", {"prompt": "x"})
    assert printed.plugin.follow_up(p["run_id"], "more")["error"]["code"] == "not_resumable"


def test_follow_up_waits_for_the_run_to_finish(harness):
    h = harness(scenario="slow")
    started = h.call("delegate_to_devin", {"prompt": "x", "background": True})
    assert h.plugin.follow_up(started["run_id"], "more")["error"]["code"] == "busy"
    h.manager.cancel(started["run_id"], "done")
    assert h.manager.get(started["run_id"]).settled.wait(20)


def test_follow_up_reports_a_session_devin_lost(harness):
    h = harness()
    parent = _finished_run(h)
    parent.devin_session_id = "missing-session"
    started = h.plugin.follow_up(parent.id, "more")
    child = h.manager.get(started["run"]["id"])
    assert child.settled.wait(20)
    assert child.state.value == "failed" and "could not resume session missing-session" in child.error.message


def test_follow_up_api(harness):
    h = harness()
    api = _api()
    parent = _finished_run(h)
    assert api.follow_up("../x", "more", h.plugin)[0] == 400
    assert api.follow_up(parent.id, None, h.plugin)[0] == 400
    assert api.follow_up(parent.id, "more", None)[0] == 409
    assert api.follow_up("dv-0000000000", "more", h.plugin)[0] == 404
    status, body = api.follow_up(parent.id, "more", h.plugin)
    assert status == 200 and body["run"]["parent_run_id"] == parent.id
    assert h.manager.get(body["run"]["id"]).settled.wait(20)
