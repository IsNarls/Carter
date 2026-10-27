"""Focused, offline tests for opt-in live encounter voice announcements."""

import asyncio
from datetime import datetime, timezone
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

from analysis_client import AnalysisClientSettings
from discord_receive import CarterVoiceClient
from main import Carter
from providers import ProviderError
from wake_listener import WakeSettings
from log_backend.companion_events import CompanionEvents


def lifecycle(kind, attempt, *, age=0.1, result=None, boss="Test Boss"):
    event = {
        "cursor": attempt,
        "type": kind,
        "session_id": "live-session",
        "attempt_id": f"live-session-{attempt:04d}",
        "attempt": attempt,
        "encounter_id": 123,
        "encounter_name": boss,
        "event_age_seconds": age,
    }
    if result is not None:
        event["result"] = result
    return event


def guide_callout(attempt, mechanic, occurrence, *, age=0.1, text="Move now.",
                  max_lateness=5.0):
    return {
        "cursor": 100 + occurrence,
        "type": "MECHANIC_CALLOUT",
        "session_id": "live-session",
        "attempt_id": f"live-session-{attempt:04d}",
        "attempt": attempt,
        "encounter_id": 123,
        "encounter_name": "Known Boss",
        "profile_id": "known-boss-v1",
        "mechanic_id": mechanic,
        "occurrence": occurrence,
        "message": text,
        "event_age_seconds": age,
        "max_lateness_seconds": max_lateness,
    }


class LiveAnnouncementTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = TemporaryDirectory()
        self.root_patch = patch("main.DATA_ROOT", Path(self.directory.name))
        self.root_patch.start()
        self.bot = Carter(
            0, {30}, wake_settings=WakeSettings(enabled=False, auto_join=False),
            analysis_settings=AnalysisClientSettings(False),
        )
        self.state = self.bot.runtime(10)
        self.state.channel_id = 20
        self.state.want_voice = True
        self.state.live_logging_enabled = True
        self.state.live_session_id = "live-session"
        self.state.live_feed_id = "live-feed"
        self.state.live_announcer = SimpleNamespace(speak=AsyncMock())
        self.channel = SimpleNamespace(id=20)
        self.voice = MagicMock(spec=CarterVoiceClient)
        self.voice.channel = self.channel
        self.voice.is_connected.return_value = True
        self.voice.is_playing.return_value = False
        self.guild = SimpleNamespace(id=10, voice_client=self.voice)
        self.guild_patch = patch.object(self.bot, "get_guild", return_value=self.guild)
        self.guild_patch.start()

    async def asyncTearDown(self):
        self.state.live_logging_enabled = False
        await self.bot.close()
        self.guild_patch.stop()
        self.root_patch.stop()
        self.directory.cleanup()

    async def drain(self):
        tasks = list(self.state.live_announcement_tasks)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def test_fresh_start_kill_and_wipe_use_fixed_text(self):
        self.bot._queue_live_lifecycle(
            self.state, lifecycle("ENCOUNTER_STARTED", 1, boss="Arcanotron Custos"),
        )
        self.bot._queue_live_lifecycle(
            self.state, lifecycle("ENCOUNTER_ENDED", 1, result="KILL"),
        )
        self.bot._queue_live_lifecycle(
            self.state, lifecycle("ENCOUNTER_ENDED", 2, result="WIPE"),
        )
        await self.drain()

        self.assertEqual(
            [call.args[1] for call in self.state.live_announcer.speak.await_args_list],
            ["Good luck on Arcanotron Custos!", "Good job!", "That sucks!"],
        )

    async def test_companion_receiver_feed_speaks_trusted_mechanic_and_end(self):
        receiver = CompanionEvents(Path(self.directory.name) / 'receiver')
        receiver.set_source('companion')
        session, attempt = str(uuid4()), str(uuid4())
        stamp = datetime.now(timezone.utc).isoformat()
        base = dict(session_id=session, attempt_id=attempt, encounter_id=3470,
                    difficulty_id=17, log_timestamp=stamp, detected_at=stamp)

        async def deliver(kind, seq, **extra):
            receiver.receive({'schema_version': 1, 'events': [dict(
                base, event_id=str(uuid4()), sequence=seq, type=kind, **extra)]})
            updates, _ = receiver.updates(self.state.live_cursor)
            for event in updates:
                self.state.live_cursor = event['cursor']
                self.bot._queue_live_lifecycle(self.state, event)
            await self.drain()

        await deliver('ENCOUNTER_STARTED', 1)
        await deliver('MECHANIC_OBSERVED', 2, profile_id='nekzali-observed-v1',
                      profile_revision=1, mechanic_id='hungering_pyre', occurrence=1)
        await deliver('ENCOUNTER_ENDED', 3, result='KILL')
        self.assertEqual([c.args[1] for c in self.state.live_announcer.speak.await_args_list],
                         ["Good luck on Nek'zali the Soulcoiler!", 'Group soak.', 'Good job!'])

    async def test_expired_end_cancels_queued_guidance_and_blocks_late_mechanics(self):
        async with self.state.voice_lock:
            self.bot._queue_live_lifecycle(self.state, guide_callout(1, 'soak', 1))
            self.bot._queue_live_lifecycle(self.state, lifecycle('ENCOUNTER_ENDED', 1,
                                                               age=60, result='KILL'))
            self.bot._queue_live_lifecycle(self.state, guide_callout(1, 'soak', 2))
        await self.drain()
        self.state.live_announcer.speak.assert_not_awaited()

    async def test_duplicate_is_suppressed_but_second_pull_is_new(self):
        events = [
            lifecycle("ENCOUNTER_STARTED", 1),
            lifecycle("ENCOUNTER_ENDED", 1, result="WIPE"),
        ]
        for event in events:
            self.bot._queue_live_lifecycle(self.state, event)
            self.bot._queue_live_lifecycle(self.state, dict(event, cursor=99))
        self.bot._queue_live_lifecycle(self.state, lifecycle("ENCOUNTER_STARTED", 2))
        self.bot._queue_live_lifecycle(
            self.state, lifecycle("ENCOUNTER_ENDED", 2, result="KILL"),
        )
        await self.drain()

        self.assertEqual(self.state.live_announcer.speak.await_count, 4)

    async def test_distinct_guide_callouts_are_spoken_and_duplicates_are_suppressed(self):
        first = guide_callout(1, "move", 1, text="Move out.")
        second = guide_callout(1, "soak", 1, text="Group soak.")
        self.bot._queue_live_lifecycle(self.state, first)
        self.bot._queue_live_lifecycle(self.state, dict(first, cursor=999))
        self.bot._queue_live_lifecycle(self.state, second)
        await self.drain()

        self.assertEqual(
            [call.args[1] for call in self.state.live_announcer.speak.await_args_list],
            ["Move out.", "Group soak."],
        )

    async def test_guide_lateness_is_stricter_than_lifecycle_freshness(self):
        self.bot._queue_live_lifecycle(
            self.state,
            guide_callout(1, "move", 1, age=5.1, max_lateness=5.0),
        )
        self.bot._queue_live_lifecycle(
            self.state,
            lifecycle("ENCOUNTER_ENDED", 1, age=5.1, result="KILL"),
        )
        await self.drain()

        self.state.live_announcer.speak.assert_awaited_once_with(self.voice, "Good job!")

    async def test_guide_callout_can_expire_while_waiting_for_speech_lock(self):
        await self.state.live_announcement_lock.acquire()
        try:
            self.bot._queue_live_lifecycle(
                self.state,
                guide_callout(1, "move", 1, age=0.0, max_lateness=0.01),
            )
            await asyncio.sleep(0.02)
        finally:
            self.state.live_announcement_lock.release()
        await self.drain()

        self.state.live_announcer.speak.assert_not_awaited()

    async def test_retained_callout_uses_current_log_timestamp_for_freshness(self):
        event = guide_callout(1, "move", 1, age=0.1, max_lateness=5.0)
        event["effective_log_timestamp"] = time.time() - 10
        self.bot._queue_live_lifecycle(self.state, event)
        await self.drain()

        self.state.live_announcer.speak.assert_not_awaited()

    async def test_retained_end_uses_raw_log_boundary_not_processing_timestamp(self):
        event = lifecycle("ENCOUNTER_ENDED", 1, age=0.1, result="KILL")
        event["effective_log_timestamp"] = time.time()
        event["log_end_timestamp"] = time.time() - 20
        self.bot._queue_live_lifecycle(self.state, event)
        await self.drain()

        self.state.live_announcer.speak.assert_not_awaited()

    async def test_encounter_end_cancels_queued_mechanics_for_same_attempt(self):
        await self.state.live_announcement_lock.acquire()
        try:
            self.bot._queue_live_lifecycle(
                self.state, guide_callout(1, "move", 1, text="Move now."),
            )
            await asyncio.sleep(0)
            self.bot._queue_live_lifecycle(
                self.state, lifecycle("ENCOUNTER_ENDED", 1, result="KILL"),
            )
        finally:
            self.state.live_announcement_lock.release()
        await self.drain()

        self.state.live_announcer.speak.assert_awaited_once_with(self.voice, "Good job!")

    async def test_authenticated_test_event_uses_fixed_text_and_live_opt_in(self):
        event = {
            "cursor": 50,
            "type": "ANNOUNCEMENT_TEST_REQUESTED",
            "session_id": "live-session",
            "attempt_id": f"test-{'a' * 32}",
            "request_id": "a" * 32,
            "event_age_seconds": 0.0,
        }
        self.bot._queue_live_lifecycle(self.state, event)
        self.bot._queue_live_lifecycle(self.state, dict(event, cursor=51))
        await self.drain()

        self.state.live_announcer.speak.assert_awaited_once_with(
            self.voice, "I have received the test prompt",
        )

        self.state.live_logging_enabled = False
        self.bot._queue_live_lifecycle(
            self.state, dict(event, cursor=52, attempt_id=f"test-{'b' * 32}",
                             request_id="b" * 32),
        )
        await self.drain()
        self.assertEqual(self.state.live_announcer.speak.await_count, 1)

    async def test_stale_start_does_not_block_fresh_end(self):
        self.bot._queue_live_lifecycle(
            self.state,
            lifecycle(
                "ENCOUNTER_STARTED", 1,
                age=self.bot.live_announcement_max_age + 1,
            ),
        )
        self.bot._queue_live_lifecycle(
            self.state, lifecycle("ENCOUNTER_ENDED", 1, result="WIPE"),
        )
        await self.drain()

        self.state.live_announcer.speak.assert_awaited_once_with(
            self.voice, "That sucks!",
        )

    async def test_voice_unavailable_or_speech_failure_does_not_escape_task(self):
        self.guild.voice_client = None
        self.bot._queue_live_lifecycle(
            self.state, lifecycle("ENCOUNTER_STARTED", 1),
        )
        await self.drain()
        self.state.live_announcer.speak.assert_not_awaited()

        self.guild.voice_client = self.voice
        self.state.live_announcer.speak.side_effect = ProviderError("provider_unavailable")
        self.bot._queue_live_lifecycle(
            self.state, lifecycle("ENCOUNTER_ENDED", 1, result="KILL"),
        )
        await self.drain()
        self.state.live_announcer.speak.assert_awaited_once()

    async def test_live_slash_command_is_required_and_never_connects_voice(self):
        self.state.live_logging_enabled = False
        self.state.want_voice = False
        self.guild.voice_client = None
        self.bot.analysis_settings = AnalysisClientSettings(True)
        self.state.raid.client.live_announcement_status = AsyncMock(return_value={
            "configured": True, "available": True, "cursor": 50,
            "session_id": "live-session",
        })
        interaction = SimpleNamespace(
            guild_id=10, guild=self.guild,
            user=SimpleNamespace(id=30), command=None,
            response=SimpleNamespace(
                defer=AsyncMock(), send_message=AsyncMock(),
                is_done=MagicMock(return_value=True),
            ),
            followup=SimpleNamespace(send=AsyncMock()),
        )
        monitor_gate = asyncio.Event()

        async def monitor(_state):
            await monitor_gate.wait()

        self.bot._monitor_live_encounters = monitor
        await self.bot.logs_live(interaction, "on")

        self.assertTrue(self.state.live_logging_enabled)
        self.assertEqual(self.state.live_cursor, 50)
        self.assertIsNotNone(self.state.live_monitor_task)
        self.state.raid.client.live_announcement_status.assert_awaited_once()
        await self.bot.logs_live(interaction, "off")
        self.assertFalse(self.state.live_logging_enabled)

    async def test_monitor_resets_cursor_when_backend_feed_generation_changes(self):
        self.state.live_cursor = 50
        self.state.live_feed_id = "old-feed"
        calls = []

        async def updates(since, limit):
            calls.append((since, limit))
            if len(calls) == 1:
                return {
                    "session_id": "live-session", "feed_id": "new-feed",
                    "cursor": 60, "updates": [lifecycle("ENCOUNTER_STARTED", 60)],
                }
            event = lifecycle("ENCOUNTER_STARTED", 1)
            event["received_at"] = datetime.now(timezone.utc).isoformat()
            return {
                "session_id": "live-session", "feed_id": "new-feed",
                "cursor": 1, "updates": [event],
            }

        self.state.raid.client.live_announcement_updates = updates
        async def stop_after_delivery(_delay):
            raise asyncio.CancelledError

        with patch("main.asyncio.sleep", new=AsyncMock(side_effect=stop_after_delivery)):
            with self.assertRaises(asyncio.CancelledError):
                await self.bot._monitor_live_encounters(self.state)
        await self.drain()

        self.assertEqual(calls, [(50, 100), (0, 100)])
        self.assertEqual(self.state.live_feed_id, "new-feed")
        self.state.live_announcer.speak.assert_awaited_once_with(
            self.voice, "Good luck on Test Boss!",
        )
