"""End-to-end runs against the fake Devin CLI (foreground)."""

from __future__ import annotations

import os
import sys

import pytest
from conftest import sub

models = sub("models")


def test_acp_foreground_success(harness):
    h = harness()
    result = h.call("delegate_to_devin", {"prompt": "Fix the failing test in app.py"})
    assert result["ok"] is True, result
    assert result["state"] == "succeeded"
    assert "Fixed the bug in app.py" in result["output"]
    assert result["stop_reason"] == "end_turn"
    assert result["files_changed"] == ["app.py"]
    assert result["tool_calls"] == 1
    assert result["usage"]["cost"] == {"amount": 0.42, "currency": "USD"}
    assert result["usage"]["tokens"]["totalTokens"] == 1200
    assert result["permission_mode"] == "normal"
    # Approval went through the pre_tool_call hook, not the fallback.
    run = h.manager.get(result["run_id"])
    assert run.approval.via == "pre_tool_call"
    assert len(h.rec.gate_calls) == 1
    methods = h.acp_methods()
    assert methods[:3] == ["initialize", "session/new", "session/prompt"]
    assert "session/set_mode" not in methods  # already in normal mode
    entries = h.log_entries()
    assert {e["env_mode"] for e in entries} == {"normal"}
    new = next(e["msg"] for e in entries if e["msg"].get("method") == "session/new")
    assert new["params"]["cwd"] == str(h.workdir.resolve())
    types = h.rec.types()
    assert types[0] == "run.created" and types[-1] == "run.finished"
    kinds = {e["event"]["kind"] for e in h.rec.events if "event" in e}
    assert {"plan", "tool_call", "message", "usage", "status"} <= kinds
    assert run.plan[0]["content"] == "read code"


@pytest.mark.parametrize("mode, expected", [("accept-edits", "accept-edits"), ("plan", "plan")])
def test_acp_selects_matching_session_mode(harness, mode, expected):
    h = harness({"permission_mode": mode})
    result = h.call("delegate_to_devin", {"prompt": "do it"})
    assert result["state"] == "succeeded", result
    set_mode = [e["msg"] for e in h.log_entries() if e["msg"].get("method") == "session/set_mode"]
    assert set_mode and set_mode[0]["params"]["modeId"] == expected


def test_dangerous_mode_maps_to_bypass(harness):
    h = harness({"permission_mode": "dangerous", "allow_dangerous": True})
    result = h.call("delegate_to_devin", {"prompt": "do it"})
    assert result["state"] == "succeeded"
    set_mode = [e["msg"] for e in h.log_entries() if e["msg"].get("method") == "session/set_mode"]
    assert set_mode[0]["params"]["modeId"] == "bypass"
    assert {e["env_mode"] for e in h.log_entries()} == {"dangerous"}


def test_acp_refuses_mode_devin_does_not_offer(harness):
    # Devin advertises no "normal" mode and starts in accept-edits; running
    # anyway would let it edit without asking. "auto" must not match "autonomous-x".
    h = harness(scenario="devin_modes")
    result = h.call("delegate_to_devin", {"prompt": "do it"})
    assert result["state"] == "failed" and result["error"]["code"] == "refused", result
    assert "'normal'" in result["error"]["message"] and "print" in result["error"]["message"]
    methods = h.acp_methods()
    assert "session/prompt" not in methods and "session/set_mode" not in methods


@pytest.mark.parametrize("mode, expected", [("accept-edits", None), ("smart", "smart"), ("plan", "plan")])
def test_acp_selects_devin_modes(harness, mode, expected):
    h = harness({"permission_mode": mode}, scenario="devin_modes")
    result = h.call("delegate_to_devin", {"prompt": "do it"})
    assert result["state"] == "succeeded", result
    set_mode = [e["msg"]["params"]["modeId"] for e in h.log_entries() if e["msg"].get("method") == "session/set_mode"]
    assert set_mode == ([expected] if expected else [])


def test_files_changed_includes_diff_paths(harness):
    h = harness(scenario="diff_edit")
    result = h.call("delegate_to_devin", {"prompt": "do it"})
    assert result["files_changed"] == ["app.py", "util.py", "README.md"]


def test_print_backend_success(harness, tmp_path):
    h = harness({"backend": "print", "permission_mode": "accept-edits", "model": "opus"})
    result = h.call("delegate_to_devin", {"prompt": "List the TODOs"})
    assert result["state"] == "succeeded", result
    assert "Done: List the TODOs" in result["output"]
    entry = next(e for e in h.log_entries() if e["mode"] == "print")
    argv = entry["argv"]
    assert argv[argv.index("--permission-mode") + 1] == "accept-edits"
    assert argv[argv.index("--model") + 1] == "opus"
    assert "--respect-workspace-trust" not in argv
    assert entry["prompt"] == "List the TODOs"
    prompt_file = argv[argv.index("--prompt-file") + 1]
    assert not os.path.exists(prompt_file), "prompt file must be removed after the run"


def test_print_rejections_without_answer_fail(harness):
    h = harness({"backend": "print"}, scenario="print_rejected")
    result = h.call("delegate_to_devin", {"prompt": "x"})
    assert result["state"] == "failed" and result["error"]["code"] == "permission_denied", result
    assert "2 action(s)" in result["error"]["message"] and "'normal'" in result["error"]["message"]
    assert [p["decision"] for p in result["permissions"]] == ["denied", "denied"]


def test_print_rejections_with_answer_are_reported(harness):
    h = harness({"backend": "print"}, scenario="print_rejected_partial")
    result = h.call("delegate_to_devin", {"prompt": "x"})
    assert result["state"] == "succeeded", result
    assert "could not run the tests" in result["output"]
    assert [p["decision"] for p in result["permissions"]] == ["denied", "denied"]


def test_print_backend_trust_flag_only_when_configured(harness):
    h = harness({"backend": "print", "trust_workspace": True})
    h.call("delegate_to_devin", {"prompt": "x"})
    argv = next(e for e in h.log_entries() if e["mode"] == "print")["argv"]
    assert argv[argv.index("--respect-workspace-trust") + 1] == "false"


def test_cli_missing(harness):
    h = harness({"cli_path": "definitely-not-devin-xyz"}, cli_override=[])
    h.manager.cli_override = None
    result = h.call("delegate_to_devin", {"prompt": "x"})
    assert result["ok"] is False
    assert result["error"]["code"] == "cli_not_found"
    assert "Install it" in result["error"]["message"]
    assert h.rec.gate_calls == []  # never asked to approve something that cannot run
    assert h.manager.list() == []


def test_cli_missing_explicit_path(harness, tmp_path):
    h = harness({"cli_path": str(tmp_path / "nope" / "devin.exe")}, cli_override=[])
    h.manager.cli_override = None
    result = h.plugin.handlers()["delegate_to_devin"]({"prompt": "x"})
    assert '"cli_not_found"' in result


def test_not_logged_in_preflight(harness):
    h = harness(auth="no")
    result = h.call("delegate_to_devin", {"prompt": "x"})
    assert result["error"]["code"] == "not_logged_in"
    assert "devin auth login" in result["error"]["message"]
    assert h.spawned == []


def test_unknown_auth_output_does_not_block(harness):
    h = harness(auth="weird")
    result = h.call("delegate_to_devin", {"prompt": "x"})
    assert result["state"] == "succeeded"


def test_not_logged_in_reported_by_acp(harness):
    h = harness({"preflight_auth_check": False}, scenario="auth")
    result = h.call("delegate_to_devin", {"prompt": "x"})
    assert result["state"] == "failed"
    assert result["error"]["code"] == "not_logged_in"


def test_not_logged_in_reported_by_print(harness):
    h = harness({"backend": "print", "preflight_auth_check": False}, scenario="auth")
    result = h.call("delegate_to_devin", {"prompt": "x"})
    assert result["error"]["code"] == "not_logged_in"


def test_untrusted_workspace_print(harness):
    h = harness({"backend": "print"}, scenario="trust")
    result = h.call("delegate_to_devin", {"prompt": "x"})
    assert result["error"]["code"] == "workspace_untrusted"


def test_nonzero_exit_print(harness):
    h = harness({"backend": "print"}, scenario="fail")
    result = h.call("delegate_to_devin", {"prompt": "x"})
    assert result["state"] == "failed"
    assert result["error"]["code"] == "exit_nonzero"
    assert result["error"]["exit_code"] == 2
    assert "boom" in result["error"]["stderr_tail"]
    assert "partial work" in result["output"]


def test_acp_crash_is_nonzero_exit(harness):
    h = harness(scenario="crash")
    result = h.call("delegate_to_devin", {"prompt": "x"})
    assert result["state"] == "failed"
    assert result["error"]["code"] == "exit_nonzero"
    assert result["error"]["exit_code"] == 3


def test_refusal(harness):
    h = harness(scenario="refusal")
    result = h.call("delegate_to_devin", {"prompt": "x"})
    assert result["state"] == "failed" and result["error"]["code"] == "refused"


@pytest.mark.parametrize("backend", ["acp", "print"])
def test_timeout_stops_run(harness, backend):
    h = harness({"backend": backend}, scenario="hang")
    cfg = h.config.load()
    run = h.manager.start(cfg, prompt="x", cwd=str(h.workdir), background=False, timeout_seconds=1, title="t",
                          approval=models.ApprovalRecord("test", "k", "s", 0, 0))
    assert run.settled.wait(30)
    assert run.state == models.RunState.TIMED_OUT
    assert run.error.code == "timeout"
    assert run.kill_report["method"]
    assert h.spawned[0].poll() is not None


def test_timeout_argument_is_capped(harness):
    h = harness({"max_timeout_seconds": 100, "timeout_seconds": 50, "background_timeout_seconds": 60})
    result = h.call("delegate_to_devin", {"prompt": "x", "timeout_seconds": 99999})
    assert h.manager.get(result["run_id"]).timeout_seconds == 100


@pytest.mark.parametrize("backend", ["acp", "print"])
def test_output_is_capped(harness, backend, monkeypatch):
    monkeypatch.setenv("FAKE_DEVIN_BIG", "60000")
    h = harness({"backend": backend, "max_output_chars": 5000, "max_result_chars": 1000}, scenario="big")
    result = h.call("delegate_to_devin", {"prompt": "x"})
    assert result["state"] == "succeeded", result
    assert result["output_truncated"] is True
    assert result["output_chars"] >= 60000
    assert len(result["output"]) < 1200
    assert "THE END" in result["output"]  # the tail is kept
    full = h.call("devin_run_result", {"run_id": result["run_id"], "max_chars": 4000})
    assert len(full["output"]) <= 4200 and "THE END" in full["output"]


def test_permission_mode_cannot_come_from_arguments(harness):
    h = harness()
    for key in ("permission_mode", "mode", "allow_dangerous", "model"):
        result = h.call("delegate_to_devin", {"prompt": "x", key: "dangerous"})
        assert result["ok"] is False
        assert result["error"]["code"] == "invalid_arguments"
        assert "plugin settings" in result["error"]["message"]
    assert h.spawned == []
    assert h.rec.gate_calls == []


def test_invalid_arguments(harness):
    h = harness()
    assert h.call("delegate_to_devin", {"prompt": "  "})["error"]["code"] == "invalid_arguments"
    assert h.call("delegate_to_devin", {"prompt": "x", "background": "yes"})["error"]["code"] == "invalid_arguments"
    assert h.call("delegate_to_devin", {"prompt": "x", "extra": 1})["error"]["code"] == "invalid_arguments"
    big = harness({"max_prompt_chars": 100})
    assert big.call("delegate_to_devin", {"prompt": "x" * 101})["error"]["code"] == "invalid_arguments"


def test_cwd_validation(harness, tmp_path):
    h = harness({"allowed_roots": [str(tmp_path / "work")]})
    missing = h.call("delegate_to_devin", {"prompt": "x", "cwd": str(tmp_path / "missing")})
    assert missing["error"]["code"] == "cwd_invalid"
    outside = tmp_path / "outside"
    outside.mkdir()
    blocked = h.call("delegate_to_devin", {"prompt": "x", "cwd": str(outside)})
    assert blocked["error"]["code"] == "cwd_not_allowed"
    inner = tmp_path / "work" / "pkg"
    inner.mkdir()
    ok = h.call("delegate_to_devin", {"prompt": "x", "cwd": "pkg"})
    assert ok["cwd"] == str(inner.resolve())


def test_invalid_settings_block_before_approval(harness):
    h = harness({"permission_mode": "dangerous"})
    result = h.call("delegate_to_devin", {"prompt": "x"})
    assert result["error"]["code"] == "config_invalid"
    assert "allow_dangerous" in result["error"]["message"]
    assert h.rec.gate_calls == []


def test_python_is_used_for_fake(harness):
    # Sanity: the fake runs under the same interpreter as the tests.
    h = harness()
    assert h.manager.cli_override[0] == sys.executable
