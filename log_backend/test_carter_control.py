"""Offline tests for GUI Carter process controls."""

import os
from pathlib import Path
import signal
import tempfile
import unittest
from unittest.mock import MagicMock, patch

if __package__:
    from .carter_control import CarterProcessController, CarterProcessState
else:
    from carter_control import CarterProcessController, CarterProcessState


class FakeProcess:
    def __init__(self, pid=4321):
        self.pid = pid
        self.returncode = None

    def poll(self):
        return self.returncode


class CarterProcessControllerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir='.')
        self.root = Path(self.temp.name)
        (self.root / 'main.py').write_text('pass\n', encoding='utf-8')

    def tearDown(self):
        self.temp.cleanup()

    def test_start_uses_current_python_safe_argv_and_private_log(self):
        calls = []

        def popen(arguments, **kwargs):
            calls.append((arguments, kwargs))
            return FakeProcess()

        controller = CarterProcessController(
            self.root, python_executable='/safe/python', popen_factory=popen)
        with patch.object(
                controller, 'status', return_value=CarterProcessState('stopped')):
            result = controller.start()

        self.assertTrue(result.ok)
        self.assertEqual(calls[0][0], ['/safe/python', str((self.root / 'main.py').resolve())])
        self.assertEqual(calls[0][1]['cwd'], str(self.root.resolve()))
        self.assertTrue(calls[0][1]['start_new_session'])
        self.assertNotIn('shell', calls[0][1])
        self.assertTrue(controller.log_path.is_file())
        if os.name == 'posix':
            self.assertEqual(controller.log_path.stat().st_mode & 0o077, 0)

    def test_start_refuses_to_create_a_second_process(self):
        popen = MagicMock()
        controller = CarterProcessController(self.root, popen_factory=popen)
        with patch.object(
                controller, 'status',
                return_value=CarterProcessState('running', 1234, False)):
            result = controller.start()
        self.assertFalse(result.ok)
        self.assertEqual(result.code, 'already_running')
        popen.assert_not_called()

    def test_stop_refuses_an_unverified_pid(self):
        sender = MagicMock()
        controller = CarterProcessController(self.root, signal_process=sender)
        with (
            patch.object(
                controller, 'status',
                return_value=CarterProcessState('running', 9999, False)),
            patch.object(controller, '_pid_matches_carter', return_value=False),
        ):
            result = controller.stop()
        self.assertFalse(result.ok)
        self.assertEqual(result.code, 'pid_unverified')
        sender.assert_not_called()

    def test_stop_sends_interrupt_only_to_verified_carter(self):
        sent = []
        controller = CarterProcessController(
            self.root, signal_process=lambda pid, kind: sent.append((pid, kind)),
            sleep=lambda _seconds: None,
        )
        with (
            patch.object(
                controller, 'status',
                return_value=CarterProcessState('running', 4321, True)),
            patch.object(controller, '_pid_matches_carter', return_value=True),
            patch.object(controller, '_pid_alive', return_value=False),
        ):
            result = controller.stop()
        self.assertTrue(result.ok)
        self.assertEqual(sent, [(4321, signal.SIGINT)])

    def test_stop_reaps_the_gui_managed_child(self):
        process = FakeProcess()

        def send(pid, kind):
            self.assertEqual((pid, kind), (process.pid, signal.SIGINT))
            process.returncode = 0

        controller = CarterProcessController(
            self.root, signal_process=send, sleep=lambda _seconds: None)
        controller._child = process

        result = controller.stop()

        self.assertTrue(result.ok)
        self.assertEqual(result.code, 'stopped')
        self.assertIsNone(controller._child)


if __name__ == '__main__':
    unittest.main()
