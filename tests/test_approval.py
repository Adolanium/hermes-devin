"""Human approval before every run."""

from __future__ import annotations

import json

from conftest import sub

approval = sub("approval")
config = sub("config")


def test_hook_ignores_other_tools(harness):
    h = harness()
    assert h.plugin.pre_tool_call(tool_name="terminal", args={"command": "ls"}) is None


def test_hook_returns_approve_directive(harness):
    h = harness()
    directive = h.plugin.pre_tool_call(tool_name="delegate_to_devin", args={"prompt": "Add tests"},
                                       task_id="t1", session_id="s1")
    assert directive["action"] == "approve"
    assert directive["rule_key"].startswith("devin:run:normal:")
    assert "permission mode 'normal'" in directive["message"]
    assert "Add tests" in directive["message"]
    assert str(h.workdir.resolve()) in directive["message"]
    assert len(h.plugin.ledger) == 1


def test_denied_run_never_starts(harness):
    h = harness(gate_answer=False)
    result = h.call("delegate_to_devin", {"prompt": "x"})
    assert result["ok"] is False
    assert h.spawned == []
    assert len(h.plugin.ledger) == 0  # the denial voided the ticket


def test_handler_without_ticket_asks_gate_directly(harness):
    h = harness()
    result = h.call("delegate_to_devin", {"prompt": "x"}, via_hook=False)
    assert result["state"] == "succeeded"
    assert len(h.rec.gate_calls) == 1
    assert h.manager.get(result["run_id"]).approval.via == "direct_gate"


def test_handler_without_ticket_fails_closed(harness):
    h = harness(gate_answer=False)
    result = h.call("delegate_to_devin", {"prompt": "x"}, via_hook=False)
    assert result["error"]["code"] == "not_approved"
    assert h.spawned == []


def test_gate_exception_fails_closed(harness):
    h = harness()

    def broken(*_a):
        raise RuntimeError("no prompt surface")

    h.plugin.gate = broken
    result = json.loads(h.plugin.handlers()["delegate_to_devin"]({"prompt": "x"}, task_id="t", session_id="s"))
    assert result["error"]["code"] == "not_approved"
    assert h.spawned == []


def test_ticket_is_single_use_and_bound_to_arguments(harness):
    h = harness()
    h.plugin.pre_tool_call(tool_name="delegate_to_devin", args={"prompt": "A"}, task_id="t1", session_id="s1")
    # Different arguments do not match the ticket, so the gate is asked.
    h.gate_answer = False
    other = json.loads(h.plugin.delegate({"prompt": "B"}, task_id="t1", session_id="s1"))
    assert other["error"]["code"] == "not_approved"
    # Other session: no match either.
    other_session = json.loads(h.plugin.delegate({"prompt": "A"}, task_id="t9", session_id="s9"))
    assert other_session["error"]["code"] == "not_approved"
    h.gate_answer = True
    first = json.loads(h.plugin.delegate({"prompt": "A"}, task_id="t1", session_id="s1"))
    assert first["state"] == "succeeded"
    assert len(h.plugin.ledger) == 0
    calls = len(h.rec.gate_calls)
    json.loads(h.plugin.delegate({"prompt": "A"}, task_id="t1", session_id="s1"))
    assert len(h.rec.gate_calls) == calls + 1  # ticket was used up, so the gate is asked again


def test_repeated_hook_keeps_one_ticket():
    ledger = approval.ApprovalLedger()
    ledger.issue("s", "fp", "rk", "x")
    ledger.issue("s", "fp", "rk", "x")
    assert len(ledger) == 1


def test_ticket_expires():
    now = [1000.0]
    ledger = approval.ApprovalLedger(clock=lambda: now[0], ttl=60)
    ledger.issue("s", "fp", "rk", "x")
    now[0] += 61
    assert ledger.consume("s", "fp") is None


def test_denial_voids_only_matching_rule():
    ledger = approval.ApprovalLedger()
    ledger.issue("s", "fp1", "rk1", "x")
    ledger.issue("s", "fp2", "rk2", "x")
    ledger.record_decision("rk1", "deny")
    assert ledger.consume("s", "fp1") is None
    ledger.record_decision("rk2", "session")
    assert ledger.consume("s", "fp2").choice == "session"


def test_rule_key_depends_on_mode_and_scope():
    a = config.DevinConfig()
    b = config.DevinConfig(permission_mode="dangerous", allow_dangerous=True)
    ws = config.DevinConfig(approval_scope="workspace")
    assert approval.rule_key_for(a, "p", "/w") != approval.rule_key_for(b, "p", "/w")
    assert approval.rule_key_for(a, "p", "/w") != approval.rule_key_for(a, "q", "/w")
    assert approval.rule_key_for(ws, "p", "/w") == approval.rule_key_for(ws, "q", "/w")
    assert approval.rule_key_for(ws, "p", "/w") != approval.rule_key_for(ws, "p", "/other")
    assert approval.rule_key_for(ws, "p", "/w").startswith("devin:ws:normal:")


def test_dangerous_mode_is_spelled_out_in_the_prompt():
    cfg = config.DevinConfig(permission_mode="dangerous", allow_dangerous=True)
    message = approval.approval_message(cfg, prompt="p", cwd="/w", background=True, timeout=60)
    assert "EVERY action auto-approved" in message
    assert "background" in message


def test_post_approval_response_ignores_other_rules(harness):
    h = harness()
    h.plugin.pre_tool_call(tool_name="delegate_to_devin", args={"prompt": "A"}, task_id="t1", session_id="s1")
    h.plugin.post_approval_response(pattern_key="plugin_rule:other:1", choice="deny")
    h.plugin.post_approval_response(pattern_key="rm -rf", choice="deny")
    assert len(h.plugin.ledger) == 1


def test_hook_blocks_runs_that_cannot_start(harness):
    h = harness({"cli_path": "no-such-devin-cli"}, cli_override=[])
    h.manager.cli_override = None
    directive = h.plugin.pre_tool_call(tool_name="delegate_to_devin", args={"prompt": "x"})
    assert directive["action"] == "block"
    assert json.loads(directive["message"])["error"]["code"] == "cli_not_found"
    assert len(h.plugin.ledger) == 0


def test_long_prompt_is_flagged_in_the_approval_message():
    cfg = config.DevinConfig()
    prompt = "fix the tests. " * 200
    message = approval.approval_message(cfg, prompt=prompt, cwd="/w", background=False, timeout=60)
    assert f"{len(prompt)} characters in total" in message
    short = approval.approval_message(cfg, prompt="fix it", cwd="/w", background=False, timeout=60)
    assert "characters in total" not in short
