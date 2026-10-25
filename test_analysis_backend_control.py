"""Offline tests for Carter-owned analysis backend lifecycle control."""

from pathlib import Path
import os
import tempfile
import unittest
from unittest.mock import MagicMock

from analysis_backend_control import AnalysisBackendController


class FakeProcess:
    def __init__(self, pid=2468):
        self.pid = pid
        self.returncode = None
        self.signals = []

    def poll(self):
        return self.returncode

    def send_signal(self, kind):
        self.signals.append(kind)
        self.returncode = 0

    def wait(self, timeout=None):
        return self.returncode

    def terminate(self):
        self.returncode = 0


class AnalysisBackendControllerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=".")
        self.root = Path(self.temp.name).resolve()
        backend = self.root / "log_backend"
        backend.mkdir()
        (backend / "analysis_api.py").write_text("pass\n", encoding="utf-8")

    def tearDown(self):
        self.temp.cleanup()

    def controller(self, **kwargs):
        return AnalysisBackendController(
            "http://127.0.0.1:8765/api/v1", self.root,
            python_executable="/safe/python", sleep=lambda _seconds: None,
            **kwargs,
        )

    def test_reuses_healthy_existing_backend_without_owning_it(self):
        popen = MagicMock()
        controller = self.controller(health_check=lambda: True, popen_factory=popen)

        result = controller.ensure_running()

        self.assertTrue(result.ok)
        self.assertEqual(result.code, "ready")
        self.assertFalse(result.owned)
        popen.assert_not_called()
        self.assertEqual(controller.stop().code, "not_owned")

    def test_starts_backend_with_private_storage_and_log_then_stops_owned_child(self):
        calls = []
        health = iter((False, True))
        process = FakeProcess()

        def popen(arguments, **kwargs):
            calls.append((arguments, kwargs))
            return process

        controller = self.controller(
            health_check=lambda: next(health), popen_factory=popen,
        )
        previous = os.environ.get("CARTER_DISCORD_TOKEN")
        os.environ["CARTER_DISCORD_TOKEN"] = "must-not-reach-backend"
        try:
            result = controller.ensure_running()
        finally:
            if previous is None:
                os.environ.pop("CARTER_DISCORD_TOKEN", None)
            else:
                os.environ["CARTER_DISCORD_TOKEN"] = previous

        self.assertTrue(result.ok)
        self.assertEqual(result.code, "started")
        self.assertTrue(result.owned)
        self.assertEqual(calls[0][0], [
            "/safe/python", str(self.root / "log_backend" / "analysis_api.py"),
            "--storage", str(self.root / "log_backend" / "data"),
            "--host", "127.0.0.1", "--port", "8765",
        ])
        self.assertEqual(calls[0][1]["cwd"], str(self.root))
        self.assertTrue(calls[0][1]["start_new_session"])
        self.assertNotIn("shell", calls[0][1])
        self.assertNotIn("CARTER_DISCORD_TOKEN", calls[0][1]["env"])
        self.assertTrue(controller.log_path.is_file())
        if os.name == "posix":
            self.assertEqual(controller.log_path.stat().st_mode & 0o077, 0)

        stopped = controller.stop()
        self.assertTrue(stopped.ok)
        self.assertEqual(stopped.code, "stopped")
        self.assertEqual(len(process.signals), 1)

    def test_rejects_remote_or_authenticated_management_urls(self):
        for url in (
            "http://analysis.example/api/v1",
            "https://127.0.0.1:8765/api/v1",
            "http://user:password@127.0.0.1:8765/api/v1",
        ):
            with self.subTest(url=url), self.assertRaises(ValueError):
                AnalysisBackendController(url, self.root)


if __name__ == "__main__":
    unittest.main()
