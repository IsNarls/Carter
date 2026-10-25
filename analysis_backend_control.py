"""Lifecycle control for Carter's private local analysis backend."""

from __future__ import annotations

from dataclasses import dataclass
import http.client
import ipaddress
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from urllib.parse import urlsplit


@dataclass(frozen=True)
class AnalysisBackendResult:
    ok: bool
    code: str
    message: str
    pid: int | None = None
    owned: bool = False


class AnalysisBackendController:
    """Start, monitor, and stop only the analysis process Carter owns."""

    def __init__(self, base_url, project_root=None, *, startup_timeout=15.0,
                 python_executable=None, popen_factory=None, sleep=None,
                 monotonic=None, health_check=None):
        self.project_root = Path(
            project_root or Path(__file__).resolve().parent,
        ).resolve()
        self.entrypoint = self.project_root / "log_backend" / "analysis_api.py"
        self.storage = self.project_root / "log_backend" / "data"
        self.runtime_directory = self.project_root / ".carter_data"
        self.log_path = self.runtime_directory / "analysis-backend.log"
        project_python = self.project_root / ".venv" / "bin" / "python"
        self.python_executable = str(
            python_executable or (project_python if project_python.is_file() else sys.executable)
        )
        self.startup_timeout = max(0.1, float(startup_timeout))
        self._popen = popen_factory or subprocess.Popen
        self._sleep = sleep or time.sleep
        self._monotonic = monotonic or time.monotonic
        self._health_check = health_check or self._probe_health
        self._child = None

        parsed = urlsplit(str(base_url).strip())
        try:
            loopback = ipaddress.ip_address(parsed.hostname or "").is_loopback
        except ValueError:
            loopback = (parsed.hostname or "").casefold() == "localhost"
        if (parsed.scheme != "http" or not loopback or parsed.username or parsed.password
                or parsed.query or parsed.fragment or parsed.path.rstrip("/") != "/api/v1"):
            raise ValueError("managed_analysis_backend_requires_loopback_http")
        self.host = parsed.hostname or "127.0.0.1"
        self.port = parsed.port or 80
        self.health_path = parsed.path.rstrip("/") + "/health"

    @property
    def owned(self):
        return self._child is not None and self._child.poll() is None

    def _probe_health(self):
        connection = http.client.HTTPConnection(self.host, self.port, timeout=1.0)
        try:
            connection.request("GET", self.health_path, headers={"Accept": "application/json"})
            response = connection.getresponse()
            body = response.read(65537)
            if response.status != 200 or len(body) > 65536:
                return False
            payload = json.loads(body.decode("utf-8"))
            return (isinstance(payload, dict) and payload.get("status") == "ok"
                    and isinstance(payload.get("schema_version"), (int, str)))
        except (OSError, UnicodeError, json.JSONDecodeError, http.client.HTTPException):
            return False
        finally:
            connection.close()

    def healthy(self):
        try:
            return bool(self._health_check())
        except Exception:
            return False

    def _wait_until_ready(self):
        deadline = self._monotonic() + self.startup_timeout
        while self._monotonic() < deadline:
            if self.healthy():
                return True
            if self._child is not None and self._child.poll() is not None:
                return False
            self._sleep(0.1)
        return self.healthy()

    def ensure_running(self):
        if self.healthy():
            return AnalysisBackendResult(
                True, "ready", "Analysis backend is ready.",
                getattr(self._child, "pid", None), self.owned,
            )
        if self._child is not None and self._child.poll() is not None:
            self._child = None
        if self._child is not None:
            if self._wait_until_ready():
                return AnalysisBackendResult(
                    True, "ready", "Analysis backend is ready.",
                    self._child.pid, True,
                )
            self.stop(timeout=1.0)
            return AnalysisBackendResult(
                False, "startup_timeout", "Analysis backend did not become ready.",
            )
        if not self.entrypoint.is_file():
            return AnalysisBackendResult(
                False, "entrypoint_missing", "Analysis backend entry point was not found.",
            )
        try:
            self.runtime_directory.mkdir(parents=True, exist_ok=True)
            self.storage.mkdir(parents=True, exist_ok=True)
            descriptor = os.open(
                self.log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600,
            )
            with os.fdopen(descriptor, "ab", buffering=0) as output:
                if hasattr(os, "fchmod"):
                    os.fchmod(output.fileno(), 0o600)
                self._child = self._popen(
                    [
                        self.python_executable, str(self.entrypoint),
                        "--storage", str(self.storage),
                        "--host", self.host,
                        "--port", str(self.port),
                    ],
                    cwd=str(self.project_root),
                    # Root .env is Carter's single configuration file. The
                    # backend importer reads only its WARCRAFT_LOGS_* entries;
                    # never forward Carter's Discord or AI provider values.
                    env={key: value for key, value in os.environ.items()
                         if not key.startswith("CARTER_")},
                    stdin=subprocess.DEVNULL,
                    stdout=output,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                    close_fds=True,
                )
        except OSError:
            self._child = None
            return AnalysisBackendResult(
                False, "start_failed", "Analysis backend could not be started.",
            )
        if self._wait_until_ready():
            return AnalysisBackendResult(
                True, "started", "Analysis backend started.", self._child.pid, True,
            )
        self.stop(timeout=1.0)
        return AnalysisBackendResult(
            False, "startup_timeout", "Analysis backend did not become ready.",
        )

    def stop(self, *, timeout=10.0):
        child = self._child
        if child is None:
            return AnalysisBackendResult(
                True, "not_owned", "Analysis backend is not owned by Carter.",
            )
        if child.poll() is not None:
            self._child = None
            return AnalysisBackendResult(True, "stopped", "Analysis backend stopped.")
        try:
            child.send_signal(signal.SIGINT)
            child.wait(timeout=max(0.1, float(timeout)))
        except subprocess.TimeoutExpired:
            try:
                child.terminate()
                child.wait(timeout=3.0)
            except (OSError, subprocess.TimeoutExpired):
                return AnalysisBackendResult(
                    False, "stop_timeout", "Analysis backend did not stop safely.",
                    child.pid, True,
                )
        except OSError:
            return AnalysisBackendResult(
                False, "stop_failed", "Analysis backend could not be stopped.",
                child.pid, True,
            )
        self._child = None
        return AnalysisBackendResult(True, "stopped", "Analysis backend stopped.")
