"""Background runs: completion notice, concurrency limit, companions, history."""

from __future__ import annotations

import json
import time

from conftest import sub

models = sub("models")


def _wait_for(predicate, timeout=20.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def test_background_returns_immediately_and_posts_result(harness):
    h = harness()
    started = h.call("delegate_to_devin", {"prompt": "Refactor utils", "background": True, "title": "refactor"})
    assert started["ok"] is True and started["background"] is True
    assert started["run_id"].startswith("dv-")
    run = h.manager.get(started["run_id"])
    assert run.settled.wait(20)
    assert _wait_for(lambda: h.rec.injected)
    text, key = h.rec.injected[0]
    assert key == "sess-key-1"
    assert started["run_id"] in text and "succeeded" in text
    assert "<<<DEVIN_OUTPUT" in text and "DEVIN_OUTPUT>>>" in text
    assert "Fixed the bug" in text
    assert "devin_run_result" in text
    assert run.notified
    # Exactly one notice even if finish logic ran again.
    h.manager._notify(run)
    assert len(h.rec.injected) == 1


def test_background_failure_is_reported(harness):
    h = harness({"backend": "print"}, scenario="fail")
    started = h.call("delegate_to_devin", {"prompt": "x", "background": True})
    assert h.manager.wait(started["run_id"], 20)
    assert _wait_for(lambda: h.rec.injected)
    assert "failed" in h.rec.injected[0][0]
    assert "exit_nonzero" in h.rec.injected[0][0]


def test_background_uses_background_timeout(harness):
    h = harness({"timeout_seconds": 100, "background_timeout_seconds": 200})
    started = h.call("delegate_to_devin", {"prompt": "x", "background": True})
    assert h.manager.get(started["run_id"]).timeout_seconds == 200


def test_default_background_setting(harness):
    h = harness({"default_background": True})
    started = h.call("delegate_to_devin", {"prompt": "x"})
    assert started["background"] is True


def test_concurrency_limit(harness):
    h = harness({"max_concurrent_runs": 2}, scenario="slow")
    a = h.call("delegate_to_devin", {"prompt": "a", "background": True})
    b = h.call("delegate_to_devin", {"prompt": "b", "background": True})
    assert a["ok"] and b["ok"]
    gate_calls = len(h.rec.gate_calls)
    c = h.call("delegate_to_devin", {"prompt": "c", "background": True})
    assert c["ok"] is False and c["error"]["code"] == "concurrency_limit"
    assert "max_concurrent_runs=2" in c["error"]["message"]
    assert len(h.rec.gate_calls) == gate_calls  # blocked before asking anyone
    # The manager enforces the limit itself too (two hooks can race).
    cfg = h.config.load()
    try:
        h.manager.start(cfg, prompt="d", cwd=str(h.workdir), background=True, timeout_seconds=60, title="d",
                        approval=models.ApprovalRecord("test", "k", "s", 0, 0))
        raise AssertionError("expected RunRefused")
    except sub("runner").RunRefused as exc:
        assert exc.code == "concurrency_limit"
    h.call("devin_cancel_run", {"run_id": a["run_id"]})
    d = h.call("delegate_to_devin", {"prompt": "d", "background": True})
    assert d["ok"] is True


def test_list_status_cancel(harness):
    h = harness(scenario="slow")
    started = h.call("delegate_to_devin", {"prompt": "long job", "background": True, "title": "long job"})
    run_id = started["run_id"]
    assert _wait_for(lambda: h.manager.get(run_id).state.value == "running")
    listing = h.call("devin_list_runs", {"state": "active"})
    assert [r["id"] for r in listing["runs"]] == [run_id]
    status = h.call("devin_run_status", {"run_id": run_id, "events": 5})
    assert status["ok"] and status["state"] == "running"
    assert status["title"] == "long job"
    assert "prompt" not in status
    assert len(status["events"]) <= 5
    partial = h.call("devin_run_result", {"run_id": run_id})
    assert "still active" in partial["note"]
    cancelled = h.call("devin_cancel_run", {"run_id": run_id})
    assert cancelled["ok"] and cancelled["state"] == "cancelled"
    again = h.call("devin_cancel_run", {"run_id": run_id})
    assert again["note"] == "Run already finished."
    assert h.call("devin_list_runs", {"state": "active"})["runs"] == []
    assert h.call("devin_list_runs", {"state": "finished"})["runs"][0]["id"] == run_id
    # A cancelled background run still reports back.
    assert _wait_for(lambda: h.rec.injected)
    assert "cancelled" in h.rec.injected[0][0]


def test_unknown_run_ids(harness):
    h = harness()
    for tool in ("devin_run_status", "devin_run_result", "devin_cancel_run"):
        assert h.call(tool, {"run_id": "dv-0000000000"})["error"]["code"] == "not_found"
        assert h.call(tool, {"run_id": "../../etc/passwd"})["error"]["code"] == "not_found"


def test_history_survives_restart(harness, tmp_path):
    h = harness()
    done = h.call("delegate_to_devin", {"prompt": "x"})
    stored = h.store.load(done["run_id"])
    assert stored["state"] == "succeeded"
    assert stored["owner_pid"]
    # A new manager over the same store (a Hermes restart) still finds it.
    h2 = harness()
    h2.manager._runs.clear()
    result = h2.call("devin_run_result", {"run_id": done["run_id"]})
    assert "Fixed the bug" in result["output"]
    assert result["note"] == "Loaded from run history."
    listing = h2.call("devin_list_runs", {})
    assert done["run_id"] in [r["id"] for r in listing["runs"]]


def test_shutdown_stops_background_runs(harness):
    h = harness(scenario="hang")
    started = h.call("delegate_to_devin", {"prompt": "x", "background": True})
    run = h.manager.get(started["run_id"])
    assert _wait_for(lambda: h.spawned and h.spawned[0].pid)
    h.plugin.on_unload()
    assert run.done.is_set()
    assert run.state.value == "cancelled"
    assert h.spawned[0].poll() is not None
    refused = h.call("delegate_to_devin", {"prompt": "y", "background": True})
    assert refused["ok"] is False


def test_persisted_snapshot_is_json_for_desktop(harness):
    h = harness()
    done = h.call("delegate_to_devin", {"prompt": "x"})
    raw = (h.tmp / "data" / "runs" / f"{done['run_id']}.json").read_text(encoding="utf-8")
    data = json.loads(raw)
    assert data["schema_version"] == models.SCHEMA_VERSION
    assert data["events"] and data["approval"]["via"] == "pre_tool_call"


def test_foreground_run_moves_to_background_on_yield(harness):
    h = harness(scenario="slow")
    h.yield_after(1.0)
    result = h.call("delegate_to_devin", {"prompt": "long task"})
    assert result["ok"] is True and result["background"] is True
    assert "moved to the background" in result["message"]
    run = h.manager.get(result["run_id"])
    assert run.active and run.background and run.session_key == "sess-key-1"
    h.call("devin_cancel_run", {"run_id": run.id})
    assert run.settled.wait(20)
    assert _wait_for(lambda: h.rec.injected)
    assert run.id in h.rec.injected[0][0]


def test_completion_notice_can_be_turned_off(harness):
    h = harness({"notify_on_complete": False})
    started = h.call("delegate_to_devin", {"prompt": "x", "background": True})
    assert h.manager.wait(started["run_id"], 20)
    assert h.rec.injected == []
    assert h.call("devin_run_result", {"run_id": started["run_id"]})["state"] == "succeeded"
