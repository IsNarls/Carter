"""Credential-free conversation lifecycle and fallback regression tests."""
import asyncio
import struct
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from audio import PCMPlayback
from providers import ProviderError, ProviderEvent
from session import SessionLimits, VoiceSession


REAL_SLEEP = asyncio.sleep
MIC_FRAME = struct.pack("<hh", 2000, 2000) * 960


async def eventually(predicate, timeout=0.8):
    async with asyncio.timeout(timeout):
        while not predicate():
            await REAL_SLEEP(0.005)


class FakeProvider:
    input_rate = 16000

    def __init__(self, connect_error=None):
        self.connect_error = connect_error
        self.connects = self.closes = self.starts = self.ends = 0
        self.sent = []
        self.tool_results = []
        self.events = asyncio.Queue()

    async def connect(self):
        self.connects += 1
        if self.connect_error:
            raise self.connect_error

    async def send_audio(self, pcm):
        self.sent.append(pcm)

    async def start_audio(self):
        self.starts += 1

    async def end_audio(self):
        self.ends += 1

    async def send_tool_results(self, results):
        self.tool_results.append(results)

    async def receive_audio(self):
        while True:
            event = await self.events.get()
            if isinstance(event, Exception):
                raise event
            yield event

    async def interrupt(self):
        pass

    async def close(self):
        self.closes += 1


class FakeFactory:
    def __init__(self, *providers):
        self.providers = list(providers)
        self.names = []

    def __call__(self, name, settings):
        self.names.append(name)
        if not self.providers:
            raise AssertionError("Unexpected extra provider connection")
        return self.providers.pop(0)


class FakeVoice:
    def __init__(self, listen_error=None):
        self.source = self.sink = None
        self.listening = self.playing = False
        self.connected = True
        self.listen_error = listen_error
        self.cleanup_waits = 0

    def play(self, source):
        self.source, self.playing = source, True

    def listen(self, sink):
        self.sink = sink
        if self.listen_error:
            raise self.listen_error
        self.listening = True

    def is_listening(self):
        return self.listening

    def is_playing(self):
        return self.playing

    def is_connected(self):
        return self.connected

    def stop_listening(self):
        self.listening = False
        if self.sink:
            self.sink.cleanup()

    def stop(self):
        self.playing = False

    async def wait_receive_cleanup(self):
        self.cleanup_waits += 1


class VoiceSessionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.sessions = []
        self.notices = []

    async def asyncTearDown(self):
        for session in self.sessions:
            await session.stop()

    async def notice(self, message):
        self.notices.append(message)

    def make(self, *providers, limits=None):
        factory = FakeFactory(*(providers or (FakeProvider(),)))
        session = VoiceSession(SimpleNamespace(connect_timeout=0.1),
                               limits or SessionLimits(), factory)
        self.sessions.append(session)
        return session, factory

    async def test_inactive_audio_does_not_connect_or_queue(self):
        session, factory = self.make()
        session.capture(42, MIC_FRAME)
        self.assertFalse(session.active)
        self.assertFalse(session.accept_audio)
        self.assertTrue(session.input.empty())
        self.assertEqual(factory.names, [])

    async def test_activation_filters_owner_and_resamples_input(self):
        provider, voice = FakeProvider(), FakeVoice()
        session, factory = self.make(provider)
        await session.start(voice, 42, self.notice)
        self.assertEqual(factory.names, ["gemini"])
        self.assertEqual(voice.sink.allowed_user_ids, {42})
        session.capture(43, MIC_FRAME)
        session.capture(42, b"wrong frame size")
        session.capture(42, MIC_FRAME)
        await eventually(lambda: len(provider.sent) == 1)
        self.assertEqual(provider.starts, 1)
        self.assertEqual(len(provider.sent[0]), 640)
        self.assertAlmostEqual(session.input_seconds, 0.02)
        self.assertEqual(session.owner_id, 42)
        self.assertEqual(session.rejected_speaker_frames, 1)
        with self.assertRaises(RuntimeError):
            await session.start(voice, 43)
        self.assertEqual(factory.names, ["gemini"])
        await eventually(lambda: provider.ends == 1, timeout=1.2)

    async def test_seed_audio_requires_explicit_matching_owner(self):
        provider, voice = FakeProvider(), FakeVoice()
        session, _ = self.make(provider)
        voice.listening = True  # The shared wake receiver owns capture in this mode.
        await session.start(voice, 42, self.notice, external_capture=True)
        now = time.monotonic()
        session.seed_audio([(now, MIC_FRAME)])
        session.seed_audio([(now, MIC_FRAME)], source_user_id=43)
        await REAL_SLEEP(0.03)
        self.assertEqual(provider.sent, [])
        session.seed_audio([(time.monotonic(), MIC_FRAME)], source_user_id=42)
        await eventually(lambda: len(provider.sent) == 1)

    async def test_generated_audio_turn_and_usage_become_playable(self):
        provider = FakeProvider()
        session, _ = self.make(provider)
        await session.start(FakeVoice(), 42)
        provider.events.put_nowait(ProviderEvent("audio", struct.pack("<h", 1200) * 480))
        provider.events.put_nowait(ProviderEvent("turn_complete"))
        provider.events.put_nowait(ProviderEvent("usage", usage={"total_token_count": 7}))
        await eventually(lambda: session.usage == {"total_token_count": 7})
        frame = session.playback.read()
        self.assertEqual(len(frame), PCMPlayback.FRAME)
        self.assertTrue(any(frame))
        self.assertAlmostEqual(session.output_seconds, 0.02)

    async def test_tool_call_is_bounded_and_returned_to_same_provider(self):
        provider = FakeProvider()
        calls = []

        async def handle(call):
            calls.append(call)
            return {"text": "Prepared evidence", "image_status": "available"}

        factory = FakeFactory(provider)
        session = VoiceSession(
            SimpleNamespace(connect_timeout=0.1), SessionLimits(), factory,
            tool_handler=handle,
        )
        self.sessions.append(session)
        await session.start(FakeVoice(), 42)
        provider.events.put_nowait(ProviderEvent("tool_call", tool_calls=({
            "id": "call-1", "name": "query_raid_analysis",
            "arguments": {"intent": "death"},
        },)))
        await eventually(lambda: bool(provider.tool_results))
        self.assertEqual(calls[0]["arguments"], {"intent": "death"})
        self.assertEqual(provider.tool_results[0][0]["id"], "call-1")
        self.assertEqual(
            provider.tool_results[0][0]["response"]["text"], "Prepared evidence",
        )
        self.assertEqual(session.tool_calls, 1)

    async def test_extra_tool_calls_receive_bounded_errors(self):
        provider = FakeProvider()
        calls = []

        async def handle(call):
            calls.append(call)
            return {"text": "ok"}

        factory = FakeFactory(provider)
        session = VoiceSession(
            SimpleNamespace(connect_timeout=0.1), SessionLimits(), factory,
            tool_handler=handle,
        )
        self.sessions.append(session)
        await session.start(FakeVoice(), 42)
        provider.events.put_nowait(ProviderEvent("tool_call", tool_calls=tuple(
            {"id": f"call-{index}", "name": "query_raid_analysis", "arguments": {}}
            for index in range(5)
        )))
        await eventually(lambda: bool(provider.tool_results))
        self.assertEqual(len(calls), 4)
        self.assertEqual(len(provider.tool_results[0]), 5)
        self.assertEqual(
            provider.tool_results[0][-1]["response"], {"error": "tool_call_limit"},
        )

    async def test_interruption_clears_pending_and_queued_output(self):
        for event_kind in ("interrupted", "speech_started"):
            with self.subTest(event_kind=event_kind):
                provider = FakeProvider()
                session, _ = self.make(provider)
                await session.start(FakeVoice(), 42)
                provider.events.put_nowait(ProviderEvent("audio", struct.pack("<h", 1300) * 900))
                await eventually(lambda: bool(session.playback.frames))
                provider.events.put_nowait(ProviderEvent(event_kind))
                await eventually(lambda: not session.playback.frames and not session.playback.pending)
                self.assertEqual(session.playback.read(), bytes(PCMPlayback.FRAME))
                await session.stop()

    async def test_immediate_stop_closes_provider_and_voice_pipeline(self):
        provider, voice = FakeProvider(), FakeVoice()
        session, _ = self.make(provider)
        await session.start(voice, 42)
        self.assertTrue(session.owns_receiver)
        await session.stop()
        self.assertFalse(session.active)
        self.assertFalse(session.accept_audio)
        self.assertFalse(voice.listening)
        self.assertFalse(voice.playing)
        self.assertTrue(session.playback.closed)
        self.assertIsNone(session.provider)
        self.assertIsNone(session.owner_id)
        self.assertGreaterEqual(provider.closes, 1)
        self.assertGreaterEqual(voice.cleanup_waits, 1)
        await session.stop()

    async def test_external_capture_stop_preserves_shared_wake_receiver(self):
        provider, voice = FakeProvider(), FakeVoice()
        shared_sink = SimpleNamespace(cleanup=Mock())
        voice.sink, voice.listening = shared_sink, True
        session, _ = self.make(provider)
        with patch.object(voice, "listen", wraps=voice.listen) as listen:
            await session.start(voice, 42, external_capture=True)
            self.assertFalse(session.owns_receiver)
            listen.assert_not_called()
            session.capture(42, MIC_FRAME)
            await eventually(lambda: bool(provider.sent))
            await session.stop()
        self.assertTrue(voice.listening)
        self.assertIs(voice.sink, shared_sink)
        shared_sink.cleanup.assert_not_called()
        self.assertEqual(voice.cleanup_waits, 0)
        self.assertFalse(voice.playing)
        self.assertFalse(session.active)
        self.assertTrue(session.playback.closed)
        self.assertGreaterEqual(provider.closes, 1)

    async def test_external_capture_connect_failure_preserves_shared_receiver(self):
        provider = FakeProvider(ProviderError("configuration_rejected"))
        voice = FakeVoice()
        shared_sink = SimpleNamespace(cleanup=Mock())
        voice.sink, voice.listening = shared_sink, True
        session, _ = self.make(provider)
        with self.assertRaises(ProviderError):
            await session.start(voice, 42, external_capture=True)
        self.assertFalse(session.active)
        self.assertTrue(voice.listening)
        shared_sink.cleanup.assert_not_called()
        self.assertEqual(voice.cleanup_waits, 0)
        self.assertFalse(voice.playing)
        self.assertGreaterEqual(provider.closes, 1)

    async def test_external_capture_expiry_returns_to_shared_wake_receiver(self):
        provider, voice = FakeProvider(), FakeVoice()
        shared_sink = SimpleNamespace(cleanup=Mock())
        voice.sink, voice.listening = shared_sink, True
        session, _ = self.make(provider)
        await session.start(voice, 42, external_capture=True)
        session.last_activity = time.monotonic() - 46
        await eventually(lambda: not session.active)
        await session.task
        self.assertEqual(session.reason, "inactivity")
        self.assertTrue(voice.connected)
        self.assertTrue(voice.listening)
        shared_sink.cleanup.assert_not_called()
        self.assertEqual(voice.cleanup_waits, 0)
        self.assertFalse(voice.playing)
        self.assertGreaterEqual(provider.closes, 1)

    async def test_reactivation_waits_for_previous_task_final_notice(self):
        first, second, voice = FakeProvider(), FakeProvider(), FakeVoice()
        session, factory = self.make(first, second)
        notice_started, release_notice = asyncio.Event(), asyncio.Event()

        async def held_notice(message):
            notice_started.set()
            await release_notice.wait()

        await session.start(voice, 42, held_notice)
        session.last_activity = time.monotonic() - 46
        await asyncio.wait_for(notice_started.wait(), timeout=0.8)
        try:
            self.assertFalse(session.active)
            self.assertFalse(session.task.done())
            with self.assertRaises(RuntimeError):
                await session.start(voice, 43)
            self.assertEqual(factory.names, ["gemini"])
        finally:
            release_notice.set()
            await session.task
        await session.start(voice, 43)
        self.assertEqual(factory.names, ["gemini", "gemini"])
        self.assertEqual(session.owner_id, 43)

    async def test_reactivate_after_stop_uses_fresh_provider_and_owner(self):
        first, second, voice = FakeProvider(), FakeProvider(), FakeVoice()
        session, factory = self.make(first, second)
        await session.start(voice, 42)
        first_playback = session.playback
        session.capture(42, MIC_FRAME)
        await eventually(lambda: bool(first.sent))
        await session.stop()
        session.capture(42, MIC_FRAME)
        self.assertTrue(session.input.empty())
        await session.start(voice, 43)
        self.assertEqual(factory.names, ["gemini", "gemini"])
        self.assertEqual(session.owner_id, 43)
        self.assertTrue(first_playback.closed)
        self.assertIsNot(first_playback, session.playback)
        self.assertEqual(session.input_seconds, 0)
        self.assertEqual(session.usage, {})
        session.capture(42, MIC_FRAME)
        session.capture(43, MIC_FRAME)
        await eventually(lambda: len(second.sent) == 1)
        self.assertEqual(len(first.sent), 1)

    async def test_listen_failure_releases_connected_provider_and_playback(self):
        provider, voice = FakeProvider(), FakeVoice(RuntimeError("receive failed"))
        session, _ = self.make(provider)
        with self.assertRaises(RuntimeError):
            await session.start(voice, 42)
        self.assertFalse(session.active)
        self.assertFalse(session.accept_audio)
        self.assertFalse(voice.playing)
        self.assertTrue(session.playback.closed)
        self.assertGreaterEqual(provider.closes, 1)

    async def test_inactivity_and_absolute_limit_stop_without_disconnecting_voice(self):
        for reason in ("inactivity", "session_limit"):
            with self.subTest(reason=reason):
                provider, voice = FakeProvider(), FakeVoice()
                session, _ = self.make(provider)
                await session.start(voice, 42, self.notice)
                if reason == "inactivity":
                    session.last_activity = time.monotonic() - 46
                else:
                    session.started = time.monotonic() - 181
                    session.last_activity = time.monotonic()
                await eventually(lambda: not session.active)
                await session.task
                self.assertEqual(session.reason, reason)
                self.assertTrue(voice.connected)
                self.assertFalse(voice.playing)
                self.assertGreaterEqual(provider.closes, 1)
                self.assertTrue(any(reason in message for message in self.notices))

    async def test_disconnected_voice_and_receive_fault_close_session(self):
        for reason in ("voice_disconnected", "receive_fault"):
            with self.subTest(reason=reason):
                provider, voice = FakeProvider(), FakeVoice()
                session, _ = self.make(provider)
                await session.start(voice, 42)
                if reason == "voice_disconnected":
                    voice.connected = False
                else:
                    session._receive_error("dave_decryption_failed")
                await eventually(lambda: not session.active)
                self.assertEqual(session.reason, "voice_disconnected" if not voice.connected else "error")
                self.assertGreaterEqual(provider.closes, 1)

    async def test_input_overflow_is_bounded_and_surfaces_failure(self):
        provider = FakeProvider()
        session, _ = self.make(provider)
        await session.start(FakeVoice(), 42)
        for _ in range(51):
            session.capture(42, MIC_FRAME)
        self.assertEqual(session.input.qsize(), 50)
        self.assertEqual(session.receive_fault, "input_overflow")
        await eventually(lambda: not session.active)
        self.assertEqual(session.reason, "error")
        self.assertTrue(session.input.empty())

    async def test_quota_with_fallback_disabled_never_opens_openai(self):
        provider = FakeProvider(ProviderError("quota_exhausted", quota=True))
        session, factory = self.make(provider)
        with self.assertRaises(ProviderError):
            await session.start(FakeVoice(), 42, self.notice)
        self.assertEqual(factory.names, ["gemini"])
        self.assertFalse(session.active)
        self.assertGreaterEqual(provider.closes, 1)
        self.assertEqual(self.notices, [])

    async def test_enabled_quota_fallback_opens_openai_once_and_notifies(self):
        gemini = FakeProvider(ProviderError("quota_exhausted", quota=True))
        openai = FakeProvider()
        session, factory = self.make(gemini, openai, limits=SessionLimits(allow_fallback=True))
        await session.start(FakeVoice(), 42, self.notice)
        self.assertEqual(factory.names, ["gemini", "openai"])
        self.assertEqual(session.provider_name, "openai")
        self.assertTrue(session.active)
        self.assertGreaterEqual(gemini.closes, 1)
        self.assertEqual(len(self.notices), 1)
        self.assertIn("paid OpenAI", self.notices[0])
        self.assertIn("context resets", self.notices[0])
        await session.stop()
        self.assertGreaterEqual(openai.closes, 1)

    async def test_nonquota_errors_never_trigger_enabled_fallback(self):
        for code in ("authentication_failed", "configuration_rejected", "provider_failed"):
            with self.subTest(code=code):
                provider = FakeProvider(ProviderError(code))
                session, factory = self.make(provider, limits=SessionLimits(allow_fallback=True))
                with self.assertRaises(ProviderError):
                    await session.start(FakeVoice(), 42, self.notice)
                self.assertEqual(factory.names, ["gemini"])
                self.assertFalse(session.active)
                self.assertGreaterEqual(provider.closes, 1)
                self.assertEqual(session.last_error, code)
                self.assertEqual(session.reason, "error")

    async def test_hard_deadline_interrupts_recovery_before_reconnecting(self):
        provider, unused_retry, voice = FakeProvider(), FakeProvider(), FakeVoice()
        session, factory = self.make(provider, unused_retry,
                                     limits=SessionLimits(max_seconds=0.05))
        await session.start(voice, 42, self.notice)
        provider.events.put_nowait(ProviderError("connection_lost", transient=True))
        await eventually(lambda: session.state == "reconnecting", timeout=0.1)
        async with asyncio.timeout(0.2):
            await session.task
        self.assertEqual(session.reason, "session_limit")
        self.assertFalse(session.active)
        self.assertFalse(session.accept_audio)
        self.assertEqual(factory.names, ["gemini"])
        self.assertEqual(unused_retry.connects, 0)
        self.assertGreaterEqual(provider.closes, 1)
        self.assertFalse(voice.listening)
        self.assertFalse(voice.playing)
        self.assertTrue(session.playback.closed)

    async def test_runtime_quota_fallback_discards_old_output_and_restarts_input(self):
        gemini, openai, voice = FakeProvider(), FakeProvider(), FakeVoice()
        session, factory = self.make(gemini, openai, limits=SessionLimits(allow_fallback=True))
        await session.start(voice, 42, self.notice)
        session.playback.push(b"\x01" * (PCMPlayback.FRAME + 2))
        gemini.events.put_nowait(ProviderError("quota_exhausted", quota=True))
        await eventually(lambda: session.provider_name == "openai" and session.state == "listening")
        self.assertEqual(factory.names, ["gemini", "openai"])
        self.assertEqual(session.playback.read(), bytes(PCMPlayback.FRAME))
        session.capture(42, MIC_FRAME)
        await eventually(lambda: len(openai.sent) == 1)
        self.assertGreaterEqual(gemini.closes, 1)

    async def test_transient_recovery_retries_only_once(self):
        first, retry = FakeProvider(), FakeProvider()
        session, factory = self.make(first, retry)

        async def skip_retry_delay(delay):
            await REAL_SLEEP(0 if delay == 1 else delay)

        with patch("session.asyncio.sleep", new=skip_retry_delay):
            await session.start(FakeVoice(), 42, self.notice)
            first.events.put_nowait(ProviderError("connection_lost", transient=True))
            await eventually(lambda: session.retry_count == 1 and session.state == "listening")
            retry.events.put_nowait(ProviderError("connection_lost", transient=True))
            await eventually(lambda: not session.active)
        self.assertEqual(factory.names, ["gemini", "gemini"])
        self.assertEqual(session.last_error, "connection_lost")
        self.assertEqual(session.reason, "error")
        self.assertGreaterEqual(first.closes, 1)
        self.assertGreaterEqual(retry.closes, 1)
        self.assertEqual(sum("Retrying once" in message for message in self.notices), 1)


if __name__ == "__main__":
    unittest.main()
