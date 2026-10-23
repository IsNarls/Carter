"""Safe, testable lifecycle controls for Carter's single bot process."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

try:
    import fcntl
except ImportError:  # The analysis GUI remains usable where Carter's POSIX lock is unavailable.
    fcntl = None


@dataclass(frozen=True)
class CarterProcessState:
    state: str
    pid: int | None = None
    managed: bool = False
    detail: str = ""


@dataclass(frozen=True)
class CarterControlResult:
    ok: bool
    code: str
    message: str


class CarterProcessController:
    """Start exactly one Carter process and stop only a verified Carter PID."""

    def __init__(self, project_root=None, *, python_executable=None, popen_factory=None,
                 signal_process=None, sleep=None):
        self.project_root = Path(project_root or Path(__file__).resolve().parent.parent).resolve()
        self.entrypoint = self.project_root / "main.py"
        self.lock_path = self.project_root / ".carter.lock"
        self.runtime_directory = self.project_root / ".carter_data"
        self.log_path = self.runtime_directory / "gui-carter.log"
        project_python = self.project_root / ".venv" / "bin" / "python"
        self.python_executable = str(
            python_executable or (project_python if project_python.is_file() else sys.executable)
        )
        self._popen = popen_factory or subprocess.Popen
        self._signal_process = signal_process or os.kill
        self._sleep = sleep or time.sleep
        self._child = None

    def _lock_held(self):
        if fcntl is None:
            raise OSError("Carter process control requires POSIX file locking")
        if not self.lock_path.exists():
            return False
        with self.lock_path.open("a+") as stream:
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            finally:
                try:
                    fcntl.flock(stream, fcntl.LOCK_UN)
                except OSError:
                    pass
        return False

    def _recorded_pid(self):
        try:
            value = self.lock_path.read_text(encoding="ascii").strip()
        except (OSError, UnicodeError):
            return None
        return int(value) if value.isdecimal() and int(value) > 1 else None

    @staticmethod
    def _pid_alive(pid):
        try:
            os.kill(int(pid), 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except (OSError, TypeError, ValueError):
            return False
        return True

    def _process_stopped(self, pid):
        child = self._child
        if child is not None and child.pid == pid:
            return child.poll() is not None
        return not self._pid_alive(pid)

    def _pid_matches_carter(self, pid):
        if self._child is not None and self._child.pid == pid and self._child.poll() is None:
            return True
        proc = Path("/proc") / str(pid)
        try:
            arguments = [
                item.decode("utf-8", "replace")
                for item in (proc / "cmdline").read_bytes().split(b"\0") if item
            ]
            cwd = Path(os.readlink(proc / "cwd"))
        except OSError:
            return False
        for argument in arguments[1:]:
            candidate = Path(argument)
            if not candidate.is_absolute():
                candidate = cwd / candidate
            try:
                if candidate.resolve() == self.entrypoint:
                    return True
            except OSError:
                continue
        return False

    def status(self):
        child = self._child
        if child is not None and child.poll() is None:
            return CarterProcessState("running", child.pid, True, "started by this GUI")
        if child is not None:
            self._child = None
        try:
            held = self._lock_held()
        except OSError:
            return CarterProcessState("error", detail="cannot inspect the Carter lock")
        if not held:
            return CarterProcessState("stopped")
        pid = self._recorded_pid()
        if pid and self._pid_alive(pid):
            return CarterProcessState("running", pid, False, "started outside this GUI")
        return CarterProcessState("running", detail="single-instance lock is held")

    def start(self):
        state = self.status()
        if state.state == "running":
            return CarterControlResult(False, "already_running", "Carter is already running.")
        if state.state == "error":
            return CarterControlResult(False, "status_unavailable", state.detail)
        if not self.entrypoint.is_file():
            return CarterControlResult(False, "entrypoint_missing", "Carter main.py was not found.")
        try:
            self.runtime_directory.mkdir(parents=True, exist_ok=True)
            descriptor = os.open(
                self.log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600,
            )
            with os.fdopen(descriptor, "ab", buffering=0) as output:
                if hasattr(os, "fchmod"):
                    os.fchmod(output.fileno(), 0o600)
                self._child = self._popen(
                    [self.python_executable, str(self.entrypoint)],
                    cwd=str(self.project_root), stdin=subprocess.DEVNULL,
                    stdout=output, stderr=subprocess.STDOUT,
                    start_new_session=True, close_fds=True,
                )
        except OSError:
            self._child = None
            return CarterControlResult(False, "start_failed", "Carter could not be started.")
        return CarterControlResult(True, "started", "Carter launch requested.")

    def stop(self, *, timeout=15.0):
        state = self.status()
        if state.state == "stopped":
            return CarterControlResult(True, "already_stopped", "Carter is already stopped.")
        if state.state != "running" or not state.pid:
            return CarterControlResult(
                False, "pid_unavailable",
                "Carter is running, but its PID could not be verified safely.",
            )
        if not self._pid_matches_carter(state.pid):
            return CarterControlResult(
                False, "pid_unverified", "Refusing to stop an unverified process.",
            )
        try:
            self._signal_process(state.pid, signal.SIGINT)
        except ProcessLookupError:
            self._child = None
            return CarterControlResult(True, "stopped", "Carter stopped.")
        except OSError:
            return CarterControlResult(False, "stop_failed", "Carter could not be stopped.")

        deadline = time.monotonic() + max(0.1, float(timeout))
        while time.monotonic() < deadline:
            if self._process_stopped(state.pid):
                self._child = None
                return CarterControlResult(True, "stopped", "Carter stopped.")
            self._sleep(0.1)
        try:
            self._signal_process(state.pid, signal.SIGTERM)
        except ProcessLookupError:
            self._child = None
            return CarterControlResult(True, "stopped", "Carter stopped.")
        except OSError:
            return CarterControlResult(
                False, "stop_timeout", "Carter did not stop before the cleanup timeout.",
            )
        forced_deadline = time.monotonic() + 3.0
        while time.monotonic() < forced_deadline:
            if self._process_stopped(state.pid):
                self._child = None
                return CarterControlResult(
                    True, "forced_stop", "Carter stopped after its cleanup timeout.",
                )
            self._sleep(0.1)
        return CarterControlResult(
            False, "stop_timeout", "Carter did not stop before the cleanup timeout.",
        )

    def restart(self):
        stopped = self.stop()
        if not stopped.ok:
            return stopped
        return self.start()
