"""Interrupts, cancellation and process-tree teardown."""

from __future__ import annotations

import subprocess
import time

import pytest
from conftest import sub

process = sub("process")
models = sub("models")


def _wait_dead(pid: int, timeout: float = 15.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not process.pid_alive(pid):
            return True
        time.sleep(0.1)
    return False


def test_interrupt_cancels_foreground_run_gracefully(harness):
    h = harness(scenario="slow")
    h.interrupt_after(1.5)
    result = h.call("delegate_to_devin", {"prompt": "long task"})
    assert result["state"] == "cancelled", result
    assert result["error"]["code"] == "interrupted"
    assert result["cancel_reason"] == "interrupted by the user"
    assert "session/cancel" in h.acp_methods()
    assert result["kill_report"]["graceful"] is True
    # The agent ended on the protocol-level cancel alone, before any signal.
    assert result["kill_report"]["steps"][0] == "soft_stop"
    assert "sigterm_group" not in result["kill_report"]["steps"]
    assert "stopped" in result["note"]
    assert h.spawned[0].poll() is not None


def test_interrupt_hard_kills_agent_that_ignores_cancel(harness):
    h = harness(scenario="hang")
    h.interrupt_after(1.0)
    result = h.call("delegate_to_devin", {"prompt": "long task"})
    assert result["state"] == "cancelled"
    steps = result["kill_report"]["steps"]
    # The protocol-level cancel was ignored, so the stop escalated past it.
    assert steps[0] == "soft_stop"
    assert any(s in steps for s in ("sigterm_group", "terminate_job")) or \
        result["kill_report"]["method"] == "taskkill"
    assert result["kill_report"]["method"] in ("job_object", "taskkill", "process_group")
    assert h.spawned[0].poll() is not None


@pytest.mark.parametrize("backend", ["acp", "print"])
def test_cancel_kills_grandchildren(harness, tmp_path, monkeypatch, backend):
    pidfile = tmp_path / "grandchild.pid"
    monkeypatch.setenv("FAKE_DEVIN_PIDFILE", str(pidfile))
    h = harness({"backend": backend}, scenario="spawn_child")
    started = h.call("delegate_to_devin", {"prompt": "spawn", "background": True})
    assert started["ok"], started
    deadline = time.monotonic() + 20
    while not pidfile.exists() or not pidfile.read_text().strip():
        assert time.monotonic() < deadline, "grandchild never started"
        time.sleep(0.05)
    grandchild = int(pidfile.read_text())
    assert process.pid_alive(grandchild)
    result = h.call("devin_cancel_run", {"run_id": started["run_id"]})
    assert result["ok"] is True, result
    assert result["state"] == "cancelled"
    assert _wait_dead(grandchild), "grandchild survived the cancel"
    if process.IS_WINDOWS:
        assert result["kill_report"]["method"] == "job_object"


def test_interrupt_answer_reports_what_happened(harness):
    h = harness(scenario="hang")
    h.interrupt_after(0.5)
    result = h.call("delegate_to_devin", {"prompt": "x"})
    assert result["ok"] is False
    assert result["kill_report"]["steps"]


# --- Windows kill path, mocked so it runs everywhere ------------------------------------------


class _FakeProc:
    def __init__(self, pid=4242):
        self.pid = pid
        self.returncode = None
        self.signals = []
        self.killed = False
        self.stdin = self.stdout = self.stderr = None

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        if self.returncode is None:
            raise subprocess.TimeoutExpired("x", timeout)
        return self.returncode

    def send_signal(self, sig):
        self.signals.append(sig)

    def kill(self):
        self.killed = True
        self.returncode = 1


class _FakeJob:
    def __init__(self, ok=True, boom=False):
        self.ok, self.boom = ok, boom
        self.terminated = False
        self.closed = False
        self.assigned = None

    def assign(self, pid):
        if self.boom:
            raise OSError(5, "access denied")
        self.assigned = pid

    def terminate(self, code=1):
        self.terminated = True
        return self.ok

    def close(self):
        self.closed = True


def _windows_tree(monkeypatch, job):
    monkeypatch.setattr(process, "IS_WINDOWS", True)
    monkeypatch.setattr(process.subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200, raising=False)
    monkeypatch.setattr(process, "_create_job", lambda: job)
    seen = {}

    def popen(argv, **kwargs):
        seen.update(kwargs)
        return _FakeProc()

    tree = process.ProcessTree(["devin", "acp"], cwd=".", env={}, popen=popen)
    return tree, seen


def test_windows_uses_new_process_group_and_job(monkeypatch):
    job = _FakeJob()
    tree, seen = _windows_tree(monkeypatch, job)
    assert seen["creationflags"] & 0x200
    assert "start_new_session" not in seen
    assert job.assigned == 4242
    calls = []
    monkeypatch.setattr(process, "_run_taskkill", lambda pid: calls.append(pid) or 0)
    report = tree.stop(grace=0.0)
    assert report.method == "job_object"
    assert job.terminated and job.closed
    assert calls == []
    assert tree.proc.signals == []  # no console control events on Windows


def test_windows_falls_back_to_taskkill_without_job(monkeypatch):
    job = _FakeJob(boom=True)
    tree, _ = _windows_tree(monkeypatch, job)
    assert tree.job is None and "access denied" in tree.job_error
    calls = []
    monkeypatch.setattr(process, "_run_taskkill", lambda pid: calls.append(pid) or 0)
    report = tree.stop(grace=0.0)
    assert report.method == "taskkill"
    assert calls == [4242]
    assert "taskkill_rc:0" in report.steps
    assert tree.proc.killed  # still alive after taskkill -> direct kill


def test_windows_falls_back_when_job_terminate_fails(monkeypatch):
    job = _FakeJob(ok=False)
    tree, _ = _windows_tree(monkeypatch, job)
    calls = []
    monkeypatch.setattr(process, "_run_taskkill", lambda pid: calls.append(pid) or 128)
    report = tree.stop(grace=0.0)
    assert report.method == "taskkill" and calls == [4242]


def test_taskkill_command_line(monkeypatch):
    seen = {}

    def fake_run(argv, **kwargs):
        seen["argv"] = argv
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(process.subprocess, "run", fake_run)
    assert process._run_taskkill(99) == 0
    assert seen["argv"] == ["taskkill", "/PID", "99", "/T", "/F"]


@pytest.mark.skipif(not process.IS_WINDOWS, reason="real Job Object only on Windows")
def test_real_job_object_is_created_on_windows(harness):
    h = harness(scenario="hang")
    cfg = h.config.load()
    run = h.manager.start(cfg, prompt="x", cwd=str(h.workdir), background=True, timeout_seconds=60, title="t",
                          approval=models.ApprovalRecord("test", "k", "s", 0, 0))
    deadline = time.monotonic() + 10
    while not h.spawned and time.monotonic() < deadline:
        time.sleep(0.05)
    assert h.spawned[0].job is not None, h.spawned[0].job_error
    h.manager.cancel(run.id)
    assert run.settled.wait(30)


@pytest.mark.skipif(process.IS_WINDOWS, reason="POSIX process groups")
def test_posix_child_gets_its_own_session(monkeypatch):
    seen = {}

    def popen(argv, **kwargs):
        seen.update(kwargs)
        return _FakeProc()

    process.ProcessTree(["devin"], cwd=".", env={}, popen=popen)
    assert seen["start_new_session"] is True
