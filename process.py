"""Start the Devin CLI in its own process tree and tear that tree down reliably.

Devin spawns shells, language servers, test runners and MCP servers. Killing
only the ``devin`` process would orphan all of them, so every run gets a
container for its whole tree:

* **Windows**: the child starts in a new process group and is assigned to a
  Job Object created with ``JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE``. Ending the run
  terminates the job, which takes every descendant with it, and if Hermes
  itself dies the handle closes and Windows kills the tree anyway. When a job
  cannot be created or assigned (old or locked-down systems), the fallback is
  ``taskkill /PID <pid> /T /F``.
* **POSIX**: the child starts in a new session (its own process group), and
  shutdown is ``SIGTERM`` to the group, a grace period, then ``SIGKILL``.

A graceful stop is attempted first (the caller passes a ``soft_stop`` such as
an ACP ``session/cancel``; on POSIX the group also gets ``SIGTERM``), then the
hard kill. Windows gets no console control event: the child has no console,
and a misdirected ``CTRL_BREAK_EVENT`` could reach Hermes itself.

Limitation: the child is assigned to its job right after it starts, so a
process it spawned in that first instant would not be in the job. Devin
starts helpers well after startup, so in practice the job holds the tree.
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

IS_WINDOWS = sys.platform.startswith("win")


# --- Windows Job Object --------------------------------------------------------


class _WinJob:
    """Minimal ctypes wrapper for a kill-on-close Job Object."""

    JobObjectExtendedLimitInformation = 9
    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
    PROCESS_SET_QUOTA = 0x0100
    PROCESS_TERMINATE = 0x0001

    def __init__(self) -> None:
        import ctypes
        from ctypes import wintypes

        self._ctypes = ctypes
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._k = kernel32

        class IO_COUNTERS(ctypes.Structure):
            _fields_ = [(name, ctypes.c_ulonglong) for name in (
                "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

        class BASIC(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_longlong),
                ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class EXTENDED(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", BASIC),
                ("IoInfo", IO_COUNTERS),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        kernel32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        kernel32.SetInformationJobObject.restype = wintypes.BOOL
        kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        kernel32.TerminateJobObject.restype = wintypes.BOOL
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL

        handle = kernel32.CreateJobObjectW(None, None)
        if not handle:
            raise OSError(ctypes.get_last_error(), "CreateJobObjectW failed")
        info = EXTENDED()
        info.BasicLimitInformation.LimitFlags = self.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not kernel32.SetInformationJobObject(handle, self.JobObjectExtendedLimitInformation,
                                                ctypes.byref(info), ctypes.sizeof(info)):
            err = ctypes.get_last_error()
            kernel32.CloseHandle(handle)
            raise OSError(err, "SetInformationJobObject failed")
        self.handle = handle

    def assign(self, pid: int) -> None:
        k = self._k
        proc = k.OpenProcess(self.PROCESS_SET_QUOTA | self.PROCESS_TERMINATE, False, pid)
        if not proc:
            raise OSError(self._ctypes.get_last_error(), "OpenProcess failed")
        try:
            if not k.AssignProcessToJobObject(self.handle, proc):
                raise OSError(self._ctypes.get_last_error(), "AssignProcessToJobObject failed")
        finally:
            k.CloseHandle(proc)

    def terminate(self, exit_code: int = 1) -> bool:
        return bool(self.handle and self._k.TerminateJobObject(self.handle, exit_code))

    def close(self) -> None:
        if self.handle:
            self._k.CloseHandle(self.handle)
            self.handle = None


def _create_job() -> Any:
    """Indirection so tests can force the no-job fallback."""
    return _WinJob()


def _run_taskkill(pid: int) -> int:
    """``taskkill /PID <pid> /T /F``; returns its exit code (-1 when it could not run)."""
    try:
        done = subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return done.returncode
    except Exception:
        logger.debug("taskkill failed for pid %s", pid, exc_info=True)
        return -1


def pid_alive(pid: int) -> bool:
    """Best-effort liveness probe that works for processes we did not spawn."""
    if pid <= 0:
        return False
    if IS_WINDOWS:
        import ctypes
        from ctypes import wintypes

        k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        k.OpenProcess.restype = wintypes.HANDLE
        k.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        k.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = k.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            code = wintypes.DWORD()
            if not k.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == 259  # STILL_ACTIVE
        finally:
            k.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    # A zombie still answers kill(0); treat it as dead.
    try:
        with open(f"/proc/{pid}/stat", "rb") as fh:
            return fh.read().split(b")")[-1].split()[0] != b"Z"
    except OSError:
        return True


# --- process tree -----------------------------------------------------------------


@dataclass
class KillReport:
    method: str = ""
    graceful: bool = False
    exit_code: int | None = None
    steps: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"method": self.method, "graceful": self.graceful, "exit_code": self.exit_code,
                "steps": list(self.steps)}


class ProcessTree:
    """A spawned CLI process plus whatever is needed to kill its descendants."""

    def __init__(self, argv: Sequence[str], *, cwd: str, env: dict[str, str],
                 stdin: Any = subprocess.DEVNULL, popen: Callable[..., subprocess.Popen] = subprocess.Popen):
        self.argv = list(argv)
        self.job: Any = None
        self.job_error: str = ""
        kwargs: dict[str, Any] = dict(
            cwd=cwd, env=env, stdin=stdin, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            bufsize=-1,
        )
        if IS_WINDOWS:
            kwargs["creationflags"] = (subprocess.CREATE_NEW_PROCESS_GROUP
                                       | getattr(subprocess, "CREATE_NO_WINDOW", 0))
        else:
            kwargs["start_new_session"] = True
        self.proc = popen(self.argv, **kwargs)
        self.pid = self.proc.pid
        if IS_WINDOWS:
            try:
                job = _create_job()
                job.assign(self.pid)
                self.job = job
            except Exception as exc:  # fall back to taskkill /T
                self.job_error = str(exc)
                logger.warning("devin: Job Object unavailable for pid %s (%s); using taskkill fallback",
                               self.pid, exc)

    # -- queries ----------------------------------------------------------

    def poll(self) -> int | None:
        return self.proc.poll()

    def wait(self, timeout: float | None = None) -> int | None:
        try:
            return self.proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            return None

    # -- shutdown ---------------------------------------------------------

    def _signal_group(self, sig: int) -> bool:
        try:
            os.killpg(self.pid, sig)
            return True
        except (ProcessLookupError, PermissionError, OSError):
            return False

    def stop(self, *, grace: float, soft_stop: Callable[[], None] | None = None) -> KillReport:
        """Stop the whole tree. Always ends with nothing of the tree left running."""
        report = KillReport()
        if soft_stop is not None and self.poll() is None:
            try:
                soft_stop()
                report.steps.append("soft_stop")
            except Exception as exc:
                report.steps.append(f"soft_stop_failed:{exc}")
            if self.wait(max(0.0, grace / 2)) is not None:
                report.graceful = True
        if not report.graceful and self.poll() is None:
            # Windows has no SIGTERM for a windowless child: console control events
            # only reach processes attached to the sender's console, and a
            # misdirected CTRL_BREAK can land on Hermes itself. There the graceful
            # step is the protocol-level cancel above; the Job Object does the rest.
            if not IS_WINDOWS and self._signal_group(signal.SIGTERM):
                report.steps.append("sigterm_group")
                if self.wait(max(0.0, grace / 2)) is not None:
                    report.graceful = True
        # Hard kill of the tree, even when the main process already exited:
        # its children may still be alive.
        report.method = self._kill_tree(report)
        self.wait(10)
        report.exit_code = self.poll()
        self.close()
        return report

    def _kill_tree(self, report: KillReport) -> str:
        if IS_WINDOWS:
            if self.job is not None:
                try:
                    if self.job.terminate(1):
                        report.steps.append("terminate_job")
                        return "job_object"
                except Exception as exc:
                    report.steps.append(f"terminate_job_failed:{exc}")
            code = _run_taskkill(self.pid)
            report.steps.append(f"taskkill_rc:{code}")
            if self.poll() is None:
                try:
                    self.proc.kill()
                    report.steps.append("proc_kill")
                except Exception:
                    pass
            return "taskkill"
        if self._signal_group(signal.SIGKILL):
            report.steps.append("sigkill_group")
        if self.poll() is None:
            try:
                self.proc.kill()
                report.steps.append("proc_kill")
            except Exception:
                pass
        return "process_group"

    def close(self) -> None:
        """Release the job handle. Pipes are left to the readers, which see EOF once the tree is gone."""
        job, self.job = self.job, None
        if job is not None:
            try:
                job.close()
            except Exception:
                pass

    def close_streams(self) -> None:
        for stream in (self.proc.stdin, self.proc.stdout, self.proc.stderr):
            try:
                if stream is not None:
                    stream.close()
            except Exception:
                pass

    def reap_descendants(self) -> None:
        """After a normal exit, make sure nothing the CLI left behind keeps running."""
        if IS_WINDOWS:
            if self.job is not None:
                try:
                    self.job.terminate(0)
                except Exception:
                    pass
        else:
            self._signal_group(signal.SIGKILL)
        self.close()
