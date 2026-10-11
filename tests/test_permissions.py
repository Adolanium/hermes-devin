"""Devin's in-run permission prompts and the permission-mode policy."""

from __future__ import annotations

import pytest
from conftest import sub

backends = sub("backends")


def _outcomes(result):
    return result["output"]


def test_normal_mode_relays_prompts_to_the_user(harness):
    h = harness(scenario="permission")
    result = h.call("delegate_to_devin", {"prompt": "x"})
    assert result["state"] == "succeeded", result
    out = _outcomes(result)
    assert "permission outcome: selected:yes" in out
    assert "edit outcome: y" in out
    rules = [c[2] for c in h.rec.gate_calls[1:]]
    assert len(rules) == 2 and all(r.startswith("devin:perm:normal:") for r in rules)
    assert "rm -rf build" in h.rec.gate_calls[1][1]
    assert [p["decision"] for p in result["permissions"]] == ["allowed", "allowed"]


def test_user_can_reject_a_prompt(harness):
    h = harness(scenario="permission")
    answers = iter([True, False, True])
    original = h.gate

    def gate(tool, reason, rule):
        h.gate_answer = next(answers)
        return original(tool, reason, rule)

    h.plugin.gate = gate
    result = h.call("delegate_to_devin", {"prompt": "x"}, via_hook=False)
    assert "permission outcome: selected:no" in result["output"]
    assert [p["decision"] for p in result["permissions"]] == ["denied", "allowed"]


def test_accept_edits_auto_allows_edits_only(harness):
    h = harness({"permission_mode": "accept-edits", "permission_requests": "deny"}, scenario="permission")
    result = h.call("delegate_to_devin", {"prompt": "x"})
    assert "permission outcome: selected:no" in result["output"]
    assert "edit outcome: y" in result["output"]
    assert len(h.rec.gate_calls) == 1  # only the run approval


def test_dangerous_allows_everything(harness):
    h = harness({"permission_mode": "dangerous", "allow_dangerous": True}, scenario="permission")
    result = h.call("delegate_to_devin", {"prompt": "x"})
    assert "permission outcome: selected:yes" in result["output"]
    assert len(h.rec.gate_calls) == 1


def test_plan_mode_denies_writes(harness):
    h = harness({"permission_mode": "plan"}, scenario="permission")
    result = h.call("delegate_to_devin", {"prompt": "x"})
    assert "permission outcome: selected:no" in result["output"]
    assert "edit outcome: n" in result["output"]
    assert len(h.rec.gate_calls) == 1


def test_background_runs_deny_prompts(harness):
    h = harness(scenario="permission")
    started = h.call("delegate_to_devin", {"prompt": "x", "background": True})
    run = h.manager.get(started["run_id"])
    assert run.settled.wait(20)
    out = run.output.value()
    assert "permission outcome: selected:no" in out
    assert all(p.decision == "denied" for p in run.permissions)
    assert len(h.rec.gate_calls) == 1


def test_deny_setting(harness):
    h = harness({"permission_requests": "deny"}, scenario="permission")
    result = h.call("delegate_to_devin", {"prompt": "x"})
    assert "permission outcome: selected:no" in result["output"]
    assert len(h.rec.gate_calls) == 1


@pytest.mark.parametrize(
    "mode, kind, expected",
    [
        ("plan", "read", True),
        ("plan", "edit", False),
        ("plan", "execute", False),
        ("normal", "edit", None),
        ("normal", "execute", None),
        ("accept-edits", "edit", True),
        ("accept-edits", "execute", None),
        ("smart", "delete", True),
        ("dangerous", "execute", True),
        ("dangerous", "other", True),
    ],
)
def test_permission_policy(mode, kind, expected):
    assert backends.permission_policy(mode, kind) is expected


def test_env_permission_mode_cannot_be_overridden(harness, monkeypatch):
    monkeypatch.setenv("DEVIN_PERMISSION_MODE", "dangerous")
    h = harness({"extra_env": {"FOO": "1"}})
    result = h.call("delegate_to_devin", {"prompt": "x"})
    assert result["state"] == "succeeded"
    assert {e["env_mode"] for e in h.log_entries()} == {"normal"}
