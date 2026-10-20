"""Persistent local wake listening and shared receiver lifecycle, without APIs."""
import asyncio
import threading
import time
import unittest

from wake_listener import WakeListener, WakeSettings


WAKE = b"w" * 3840
BEFORE = b"a" * 3840
AFTER = b"b" * 3840
OTHER = b"c" * 3840


async def eventually(predicate, timeout=1):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.005)


class FakeDetector:
    def __init__(self):
        self.calls = []
        self.resets = 0
        self.closed = False
        self.processing = threading.Event()
        self.finished = threading.Event()
        self.release = None
        self.result = lambda pcm: pcm == WAKE
        self.error = None
        self.overlap = False
        self.worker_threads = set()

    def process(self, user_id, pcm):
        self.worker_threads.add(threading.get_ident())
        self.processing.set()
        try:
            if self.release:
                self.release.wait(timeout=2)
            self.calls.append((user_id, pcm))
            if self.error:
                raise self.error
            return self.result(pcm)
        finally:
            self.processing.clear()
            self.finished.set()

    def reset(self):
        self.worker_threads.add(threading.get_ident())
        self.resets += 1

    def close(self):
        self.worker_threads.add(threading.get_ident())
        self.overlap = self.processing.is_set()
        self.closed = True


class FakeVoice:
    def __init__(self):
        self.connected = True
        self.listening = False
        self.sink = None
        self.cleanup_waits = 0

    def listen(self, sink):
        if self.listening:
            raise AssertionError("A second receiver was started")
        self.sink, self.listening = sink, True

    def is_connected(self):
        return self.connected

    def is_listening(self):
        return self.listening

    def stop_listening(self):
        self.listening = False
        if self.sink:
            self.sink.cleanup()

    async def wait_receive_cleanup(self):
        self.cleanup_waits += 1


class FakeSession:
    def __init__(self):
        self.active = False
        self.task = None
        self.owner_id = None
        self.captured = []
        self.seeded = []

    def capture(self, user_id, pcm):
        if user_id == self.owner_id:
            self.captured.append((user_id, pcm))

    def seed_audio(self, frames):
        self.seeded.extend(frames)


class WakeListenerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.session = FakeSession()
        self.voice = FakeVoice()
        self.detector = FakeDetector()
        self.listeners = []
        self.wakes = []

    async def asyncTearDown(self):
        # A failed assertion must not leave a native worker blocked.
        if self.detector.release:
            self.detector.release.set()
        for listener in self.listeners:
            await asyncio.wait_for(listener.stop(), timeout=2)

    async def wake(self, user_id, voice, generation):
        self.wakes.append((user_id, voice, generation))
        self.session.active, self.session.owner_id = True, user_id
        return True

    async def make(self, callback=None):
        def factory(path, word):
            self.assertEqual(word, "Carter")
            return self.detector

        listener = WakeListener(self.session, {42, 43}, WakeSettings(),
                                callback or self.wake, factory=factory)
        self.listeners.append(listener)
        await listener.start(self.voice)
        await eventually(lambda: self.detector.resets == 1)
        return listener

    async def test_idle_speech_is_local_and_does_not_open_conversation(self):
        listener = await self.make()
        for _ in range(10):
            listener.capture(42, BEFORE)
        await eventually(lambda: len(self.detector.calls) == 10)
        self.assertEqual(self.wakes, [])
        self.assertEqual(self.session.captured, [])
        self.assertEqual(self.session.seeded, [])
        self.assertEqual(listener.state, "armed")
        self.assertTrue(self.voice.connected)
        self.assertTrue(self.voice.listening)

    async def test_only_authorized_wake_frames_are_processed(self):
        listener = await self.make()
        listener.capture(999, WAKE)
        listener.capture(42, b"invalid PCM")
        listener.capture(42, BEFORE)
        await eventually(lambda: len(self.detector.calls) == 1)
        self.assertEqual(self.detector.calls, [(42, BEFORE)])
        self.assertEqual(self.wakes, [])
        listener.capture(42, WAKE)
        await eventually(lambda: bool(self.wakes))
        self.assertEqual(self.wakes[0][:2], (42, self.voice))
        self.assertEqual(listener.state, "conversation")

    async def test_connecting_buffers_only_waking_speaker_and_replays_in_order(self):
        connecting, connected = asyncio.Event(), asyncio.Event()

        async def delayed_wake(user_id, voice, generation):
            connecting.set()
            await connected.wait()
            return await self.wake(user_id, voice, generation)

        listener = await self.make(delayed_wake)
        listener.capture(42, BEFORE)
        listener.capture(42, WAKE)
        await asyncio.wait_for(connecting.wait(), timeout=1)
        self.assertEqual(listener.state, "connecting")
        listener.capture(42, AFTER)
        listener.capture(43, OTHER)
        listener.capture(42, BEFORE)
        self.assertEqual(self.session.captured, [])
        connected.set()
        await eventually(lambda: listener.pending_owner is None)
        self.assertEqual([pcm for _, pcm in self.session.seeded],
                         [BEFORE, WAKE, AFTER, BEFORE])
        self.assertNotIn(OTHER, [pcm for _, pcm in self.session.seeded])
        listener.capture(42, AFTER)
        listener.capture(43, OTHER)
        self.assertEqual(self.session.captured, [(42, AFTER)])

    async def test_reset_invalidates_detection_in_native_worker(self):
        self.detector.release = threading.Event()
        listener = await self.make()
        listener.capture(42, WAKE)
        await eventually(self.detector.processing.is_set)
        generation = listener.generation
        listener.reset(cooldown=0)
        self.detector.release.set()
        await eventually(lambda: self.detector.resets == 2)
        self.assertGreater(listener.generation, generation)
        self.assertEqual(self.wakes, [])
        self.assertEqual(listener.state, "armed")
        self.assertIsNone(listener.pending_owner)

    async def test_active_session_routes_audio_and_expiry_rearms_listener(self):
        listener = await self.make()
        listener.capture(42, WAKE)
        await eventually(lambda: bool(self.session.seeded))
        processed = len(self.detector.calls)
        generation = listener.generation
        listener.capture(42, AFTER)
        self.assertEqual(self.session.captured, [(42, AFTER)])
        self.assertEqual(len(self.detector.calls), processed)
        self.session.active, self.session.owner_id = False, None
        await eventually(lambda: listener.generation > generation)
        self.assertEqual(listener.state, "armed")
        self.assertTrue(self.voice.connected)
        self.assertTrue(self.voice.listening)
        self.assertGreater(listener.cooldown_until, time.monotonic())
        listener.capture(42, WAKE)
        self.assertTrue(listener.input.empty())
        self.assertEqual(len(self.wakes), 1)
        listener.cooldown_until = 0
        listener.capture(43, WAKE)
        await eventually(lambda: len(self.wakes) == 2)
        self.assertEqual(self.session.owner_id, 43)

    async def test_stop_cancels_pending_activation_and_drops_future_audio(self):
        connecting, resume, cancelled = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def delayed_wake(user_id, voice, generation):
            connecting.set()
            try:
                await resume.wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise
            return await self.wake(user_id, voice, generation)

        listener = await self.make(delayed_wake)
        listener.capture(42, WAKE)
        await asyncio.wait_for(connecting.wait(), timeout=1)
        listener.capture(42, AFTER)
        await listener.stop()
        resume.set()
        listener.capture(42, WAKE)
        self.assertTrue(cancelled.is_set())
        self.assertFalse(self.session.active)
        self.assertEqual(self.wakes, [])
        self.assertFalse(listener.running)
        self.assertEqual(listener.state, "off")
        self.assertIsNone(listener.task)
        self.assertIsNone(listener.pending_owner)
        self.assertFalse(listener.pending)
        self.assertTrue(listener.input.empty())
        self.assertFalse(self.voice.listening)
        self.assertTrue(self.detector.closed)

    async def test_failed_activation_discards_old_audio_and_applies_cooldown(self):
        attempts = []

        async def failed_wake(user_id, voice, generation):
            attempts.append(user_id)
            return False

        listener = await self.make(failed_wake)
        listener.capture(42, WAKE)
        listener.capture(42, WAKE)
        await eventually(lambda: bool(attempts) and listener.pending_owner is None)
        self.assertEqual(attempts, [42])
        self.assertFalse(listener.pending)
        self.assertTrue(listener.input.empty())
        self.assertGreater(listener.cooldown_until, time.monotonic() + 20)
        listener.capture(42, WAKE)
        self.assertTrue(listener.input.empty())
        self.assertFalse(self.session.active)

    async def test_callback_success_after_session_ended_does_not_stick_connecting(self):
        attempts = []

        async def ended_wake(user_id, voice, generation):
            attempts.append(user_id)
            return True

        listener = await self.make(ended_wake)
        listener.capture(42, WAKE)
        await eventually(lambda: bool(attempts) and listener.pending_owner is None)
        self.assertEqual(listener.state, "armed")
        self.assertFalse(listener.pending)
        self.assertFalse(self.session.active)
        self.assertGreater(listener.cooldown_until, time.monotonic() + 20)

    async def test_rtp_gap_supplies_one_local_silence_tail_per_utterance(self):
        listener = await self.make()
        listener.capture(42, BEFORE)
        await eventually(lambda: len(self.detector.calls) == 2)
        self.assertEqual(self.detector.calls[0], (42, BEFORE))
        silence = self.detector.calls[1][1]
        self.assertEqual(silence, bytes(48000 * 4))
        await asyncio.sleep(0.35)
        self.assertEqual(len(self.detector.calls), 2)
        self.assertEqual(self.wakes, [])

    async def test_wake_completed_by_local_silence_replays_only_real_microphone_frames(self):
        self.detector.result = lambda pcm: len(pcm) > 3840
        listener = await self.make()
        listener.capture(42, BEFORE)
        await eventually(lambda: bool(self.wakes))
        self.assertEqual(len(self.wakes), 1)
        self.assertEqual(self.detector.calls[1][1], bytes(48000 * 4))
        self.assertEqual([pcm for _, pcm in self.session.seeded], [BEFORE])
        self.assertEqual(listener.state, "conversation")

    async def test_factory_error_releases_executor_without_opening_receiver(self):
        def failed_factory(path, word):
            raise RuntimeError("private diagnostic detail")

        listener = WakeListener(self.session, {42}, WakeSettings(), self.wake,
                                factory=failed_factory)
        self.listeners.append(listener)
        with self.assertRaises(RuntimeError):
            await asyncio.wait_for(listener.start(self.voice), timeout=1)
        self.assertEqual(listener.last_error, "RuntimeError")
        self.assertEqual(listener.state, "unavailable")
        self.assertIsNone(listener.detector)
        self.assertIsNone(listener.executor)
        self.assertIsNone(listener.voice)
        self.assertFalse(self.voice.listening)

    async def test_native_process_error_surfaces_safe_type_and_can_close_promptly(self):
        self.detector.error = RuntimeError("private diagnostic detail")
        listener = await self.make()
        listener.capture(42, BEFORE)
        await eventually(lambda: not listener.running)
        self.assertEqual(listener.last_error, "RuntimeError")
        self.assertEqual(listener.state, "unavailable")
        self.assertFalse(self.voice.listening)
        await asyncio.wait_for(listener.stop(), timeout=1)
        self.assertTrue(self.detector.closed)

    async def test_native_fast_results_and_errors_complete_and_allow_shutdown(self):
        listener = await self.make()
        self.assertEqual(await asyncio.wait_for(listener._native(lambda: 7), timeout=1), 7)

        def failed_job():
            raise ValueError("safe test error")

        with self.assertRaises(ValueError):
            await asyncio.wait_for(listener._native(failed_job), timeout=1)
        await asyncio.wait_for(listener.stop(), timeout=1)
        self.assertTrue(self.detector.closed)
        self.assertIsNone(listener.executor)

    async def test_persistent_receiver_fault_is_visible_while_asleep(self):
        listener = await self.make()
        listener._fault("dave_decryption_failed")
        await eventually(lambda: not listener.running)
        self.assertEqual(listener.state, "unavailable")
        self.assertEqual(listener.last_error, "dave_decryption_failed")
        self.assertFalse(self.voice.listening)
        self.assertFalse(self.session.active)
        self.assertEqual(self.wakes, [])

    async def test_native_close_waits_for_inflight_process_on_same_worker(self):
        self.detector.release = threading.Event()
        listener = await self.make()
        listener.capture(42, BEFORE)
        await eventually(self.detector.processing.is_set)
        stopping = asyncio.create_task(listener.stop())
        await asyncio.sleep(0.02)
        self.assertFalse(self.detector.closed)
        self.assertFalse(stopping.done())
        self.detector.release.set()
        await asyncio.wait_for(stopping, timeout=1)
        self.assertTrue(self.detector.finished.is_set())
        self.assertTrue(self.detector.closed)
        self.assertFalse(self.detector.overlap)
        self.assertEqual(len(self.detector.worker_threads), 1)

    async def test_real_session_shares_receiver_and_returns_to_wake_mode(self):
        from types import SimpleNamespace
        from session import VoiceSession
        from test_session import FakeProvider, FakeVoice as SessionVoice

        provider, voice = FakeProvider(), SessionVoice()
        session = VoiceSession(SimpleNamespace(connect_timeout=0.1),
                               factory=lambda name, settings: provider)

        async def activate(user_id, current_voice, generation):
            await session.start(current_voice, user_id, external_capture=True)
            return True

        listener = WakeListener(session, {42}, WakeSettings(), activate,
                                factory=lambda path, word: self.detector)
        self.listeners.append(listener)
        try:
            await listener.start(voice)
            receiver = voice.sink
            listener.capture(42, WAKE)
            await eventually(lambda: bool(provider.sent))
            self.assertTrue(session.active)
            self.assertIs(voice.sink, receiver)
            self.assertFalse(session.owns_receiver)
            listener.capture(42, AFTER)
            await eventually(lambda: len(provider.sent) > 1)
            session.last_activity = time.monotonic() - 46
            await eventually(lambda: not session.active)
            await eventually(lambda: listener.state == "armed")
            self.assertTrue(voice.connected)
            self.assertTrue(voice.listening)
            self.assertFalse(voice.playing)
            self.assertGreaterEqual(provider.closes, 1)
            self.assertIs(voice.sink, receiver)
        finally:
            await listener.stop()
            await session.stop()


if __name__ == "__main__":
    unittest.main()
