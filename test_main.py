"""Offline Discord integration tests; no credentials, network, or paid calls."""

import asyncio
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from analysis_client import AnalysisClientSettings, AnalysisImage
from main import (
    Carter, CarterVoiceClient, ai_configuration, analysis_configuration,
    wake_configuration, _format_job, _message_report_urls,
)
from raid_integration import RaidAnswer
from wake_listener import WakeSettings


class ConfigurationTests(unittest.TestCase):
    def test_ai_defaults_are_english_gemini_without_fallback(self):
        with patch.dict("os.environ", {}, clear=True):
            settings, limits = ai_configuration()
        self.assertEqual(settings.gemini_model, "gemini-3.8-live")
        self.assertEqual(settings.gemini_text_model, "gemini-3.8-flash")
        self.assertFalse(settings.tool_declarations)
        self.assertFalse(limits.allow_fallback)
        self.assertIn("Respond only in English", settings.instructions)

    def test_analysis_tools_are_opt_in_and_remote_http_is_rejected(self):
        settings, _ = ai_configuration(analysis_tools=True)
        self.assertEqual(settings.tool_declarations[0]["name"], "query_raid_analysis")
        with patch.dict("os.environ", {
            "CARTER_ANALYSIS_ENABLED": "true",
            "CARTER_ANALYSIS_API_URL": "http://analysis.example/api/v1",
        }, clear=True):
            with self.assertRaises(ValueError):
                analysis_configuration()
        with patch.dict("os.environ", {
            "CARTER_ANALYSIS_ENABLED": "true",
            "CARTER_ANALYSIS_API_URL": "http://127.0.0.1:8765/api/v1",
        }, clear=True):
            configured, monitor = analysis_configuration()
        self.assertTrue(configured.enabled)
        self.assertTrue(monitor)

    def test_invalid_provider_fallback_and_limits_are_rejected(self):
        for setting in ({"CARTER_AI_PROVIDER": "openai"},
                        {"CARTER_ALLOW_OPENAI_FALLBACK": "yes"},
                        {"CARTER_SESSION_TIMEOUT": "0"},
                        {"CARTER_MAX_SESSION_SECONDS": "999999"}):
            with patch.dict("os.environ", setting, clear=True):
                with self.assertRaises(ValueError):
                    ai_configuration()

    def test_wake_never_autojoin_on_boot(self):
        with patch.dict("os.environ", {"CARTER_AUTO_JOIN": "true"}, clear=True):
            settings = wake_configuration()
        self.assertTrue(settings.enabled)
        self.assertFalse(settings.auto_join)
        self.assertEqual(settings.word, "Carter")

    def test_forwarded_embed_report_url_is_parsed_without_logging_contents(self):
        message = SimpleNamespace(
            content="", embeds=[], attachments=[], stickers=[], components=[],
            message_snapshots=[SimpleNamespace(to_dict=lambda: {
                "embeds": [{"fields": [{"value": (
                    "[Logs](https://www.warcraftlogs.com/reports/AbCdEf1234567890#fight=4)"
                )}]}],
            })],
        )
        self.assertEqual(
            _message_report_urls(message),
            ["https://www.warcraftlogs.com/reports/AbCdEf1234567890#fight=4"],
        )

    def test_import_job_format_uses_backend_progress_contract(self):
        text = _format_job({
            "job_id": "a" * 32,
            "report_id": "ReportABC123",
            "status": "partial",
            "progress": {"completed_fights": 4, "total_fights": 5},
            "errors": [{"code": "ANALYSIS_FAILED", "message": "private detail"}],
        })
        self.assertIn("partial; 4/5 fights", text)
        self.assertIn("ANALYSIS_FAILED", text)
        self.assertNotIn("private detail", text)


class CommandTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = TemporaryDirectory()
        self.root_patch = patch("main.DATA_ROOT", Path(self.directory.name))
        self.root_patch.start()
        analysis = AnalysisClientSettings(False)
        self.bot = Carter(
            10, {30}, wake_settings=WakeSettings(enabled=False, auto_join=False),
            analysis_settings=analysis,
        )
        self.state = self.bot.runtime(10)
        self.state.channel_id = 20
        self.fake_session = SimpleNamespace(
            active=False, owner_id=30, start=AsyncMock(), stop=AsyncMock(),
            provider_name="gemini", settings=self.state.session.settings,
            state="inactive", usage={}, limits=self.state.session.limits,
            input_seconds=0, output_seconds=0, input_frames=0, speech_turns=0,
            provider_events=0, tool_calls=0, reason="not_activated", last_error=None,
        )
        self.state.session = self.fake_session
        self.channel = MagicMock()
        self.channel.id = 20
        self.channel.permissions_for.return_value = SimpleNamespace(
            view_channel=True, connect=True, speak=True,
            read_message_history=True, send_messages=True, attach_files=True,
        )
        self.guild = SimpleNamespace(
            id=10, voice_client=None, me=object(),
            get_channel=MagicMock(return_value=self.channel),
        )
        self.interaction = SimpleNamespace(
            guild_id=10, guild=self.guild, channel=self.channel,
            user=SimpleNamespace(id=30, voice=SimpleNamespace(channel=self.channel)),
            response=SimpleNamespace(
                send_message=AsyncMock(), defer=AsyncMock(), is_done=MagicMock(return_value=True),
            ),
            followup=SimpleNamespace(send=AsyncMock()), command=None,
        )

    async def asyncTearDown(self):
        await self.bot.close()
        self.root_patch.stop()
        self.directory.cleanup()

    async def test_global_command_registration(self):
        carter = self.bot.tree.get_command("carter")
        self.assertEqual(
            {command.name for command in carter.commands},
            {"join", "leave", "status", "activate", "stop", "ask",
             "setvoice", "setlogs", "voicechannel"},
        )
        logs = self.bot.tree.get_command("logs")
        self.assertEqual(
            {command.name for command in logs.commands},
            {"health", "reports", "fights", "select", "source", "summary", "ranking",
             "player", "death", "deaths", "identity", "import", "job"},
        )

    async def test_generic_ask_uses_text_model_without_voice(self):
        with patch("main.generate_text", AsyncMock(return_value="Chat answer")) as generate:
            await self.bot.ask(self.interaction, "Hello")
        self.interaction.response.defer.assert_awaited_once_with(
            ephemeral=False, thinking=True,
        )
        generate.assert_awaited_once_with(self.fake_session.settings, "Hello")
        self.assertEqual(self.interaction.followup.send.await_args.args[0], "Chat answer")
        self.fake_session.start.assert_not_awaited()

    async def test_raid_ask_uses_deterministic_backend_result(self):
        self.bot.analysis_settings = AnalysisClientSettings(True)
        self.bot.raid.answer_question = AsyncMock(return_value=RaidAnswer("Top DPS result"))
        with patch("main.generate_text", AsyncMock()) as generate:
            await self.bot.ask(self.interaction, "Who had top DPS?")
        self.assertEqual(self.interaction.followup.send.await_args.args[0], "Top DPS result")
        generate.assert_not_awaited()

    async def test_logs_import_returns_job_id_and_accepts_backend_success_state(self):
        queued = RaidAnswer(
            "Queued report `ReportABC123` for backend retrieval and analysis "
            "(job `job-1`).",
            tool_context={"job_id": "job-1", "report_id": "ReportABC123"},
        )
        self.bot.raid.submit_report = AsyncMock(return_value=queued)
        self.bot.analysis_client.import_job = AsyncMock(return_value={
            "job_id": "job-1", "report_id": "ReportABC123", "status": "succeeded",
            "progress": {"completed_fights": 2, "total_fights": 2},
            "result": {"report_id": "ReportABC123", "fight_count": 2,
                       "analyzed_fights": 2, "failed_fights": 0},
            "errors": [],
        })
        await self.bot.logs_import(
            self.interaction, "https://www.warcraftlogs.com/reports/ReportABC123",
        )
        tasks = list(self.state.import_tasks)
        if tasks:
            await asyncio.gather(*tasks)
        self.assertIn("job `job-1`", self.interaction.followup.send.await_args_list[0].args[0])
        self.assertEqual(self.state.analysis_state.report_id, "ReportABC123")

    async def test_authorization_denies_wrong_user_but_supports_other_guild(self):
        self.interaction.user.id = 999
        await self.bot.activate(self.interaction)
        self.fake_session.start.assert_not_awaited()
        self.interaction.response.send_message.assert_awaited_once()
        self.interaction.user.id = 30
        self.interaction.guild_id = 999
        self.interaction.guild.id = 999
        await self.bot.leave(self.interaction)
        self.assertIn(999, self.bot.guilds_runtime)

    async def test_guild_runtime_state_is_isolated_and_persisted(self):
        other = self.bot.runtime(999)
        self.state.logs_channel_id = 40
        self.state.analysis_state.report_id = "Report123"
        other.channel_id = 21
        self.bot.save_runtime(self.state)
        self.assertIsNot(self.state.session, other.session)
        payload = json.loads((Path(self.directory.name) / "guild-settings.json").read_text())
        self.assertEqual(payload["guilds"]["10"]["voice_channel_id"], 20)
        self.assertEqual(payload["guilds"]["10"]["logs_channel_id"], 40)
        self.assertEqual(payload["guilds"]["10"]["analysis"]["report_id"], "Report123")

    async def test_setvoice_does_not_join_and_saves_channel(self):
        self.interaction.user.voice = None
        current = SimpleNamespace(disconnect=AsyncMock())
        self.guild.voice_client = current
        await self.bot.set_voice_channel(self.interaction, self.channel)
        self.assertEqual(self.state.channel_id, 20)
        current.disconnect.assert_awaited_once_with(force=True)
        self.fake_session.stop.assert_awaited_once()
        self.channel.connect.assert_not_called()

    async def test_join_uses_receive_client_and_does_not_activate_ai(self):
        self.channel.connect = AsyncMock(return_value=SimpleNamespace(channel=self.channel))
        with patch("main.discord.VoiceChannel", type(self.channel)):
            await self.bot.join(self.interaction)
        self.channel.connect.assert_awaited_once_with(
            cls=CarterVoiceClient, timeout=20, reconnect=True, self_deaf=False,
        )
        self.fake_session.start.assert_not_awaited()

    async def test_activate_requires_join_and_same_channel(self):
        await self.bot.activate(self.interaction)
        self.fake_session.start.assert_not_awaited()
        voice = MagicMock(spec=CarterVoiceClient)
        voice.is_connected.return_value = True
        voice.channel = self.channel
        self.guild.voice_client = voice
        self.interaction.user.voice = None
        await self.bot.activate(self.interaction)
        self.fake_session.start.assert_not_awaited()

    async def test_activate_uses_exclusive_owner_session(self):
        voice = MagicMock(spec=CarterVoiceClient)
        voice.is_connected.return_value = True
        voice.channel = self.channel
        self.guild.voice_client = voice
        await self.bot.activate(self.interaction)
        self.fake_session.start.assert_awaited_once()
        self.assertEqual(self.fake_session.start.await_args.args[:2], (voice, 30))

    async def test_stop_keeps_voice_and_leave_disconnects(self):
        calls = []
        self.fake_session.stop.side_effect = lambda: calls.append("stop")
        voice = SimpleNamespace(
            disconnect=AsyncMock(side_effect=lambda **_kwargs: calls.append("disconnect")),
        )
        self.guild.voice_client = voice
        await self.bot.stop_ai(self.interaction)
        self.assertEqual(calls, ["stop"])
        await self.bot.leave(self.interaction)
        self.assertEqual(calls, ["stop", "stop", "disconnect"])

    async def test_wake_rechecks_owner_and_voice_membership(self):
        voice = SimpleNamespace(
            channel=self.channel, is_connected=lambda: True,
            guild=SimpleNamespace(get_member=lambda _user_id: self.interaction.user),
        )
        self.state.wake.running = True
        self.state.wake.voice = voice
        generation = self.state.wake.generation
        self.assertTrue(await self.bot._wake_activate(self.state, 30, voice, generation))
        self.fake_session.start.assert_awaited_once()
        self.fake_session.start.reset_mock()
        self.assertFalse(await self.bot._wake_activate(self.state, 999, voice, generation))
        self.fake_session.start.assert_not_awaited()
        self.state.wake.running = False
        self.state.wake.voice = None

    async def test_voice_tool_posts_matching_image_and_returns_no_bytes(self):
        self.bot.analysis_settings = AnalysisClientSettings(True)
        self.fake_session.owner_id = 30
        image = AnalysisImage(b"png", "death.png", "death position")
        answer = RaidAnswer("Death answer", image, {"text": "Death answer", "image_status": "available"})
        self.bot.raid.execute_tool = AsyncMock(return_value=answer)
        self.bot._post_voice_analysis = AsyncMock()
        result = await self.bot._voice_analysis_tool(self.state, {
            "name": "query_raid_analysis", "arguments": {"intent": "death"},
        })
        self.assertEqual(result["text"], "Death answer")
        self.assertNotIn("data", result)
        self.bot._post_voice_analysis.assert_awaited_once_with(self.state, answer)

    async def test_ready_never_autojoins(self):
        self.bot._connection.user = SimpleNamespace(id=123)
        self.bot._connection._guilds[10] = self.guild
        self.bot._connect_voice = AsyncMock()
        await self.bot.on_ready()
        self.bot._connect_voice.assert_not_awaited()

    async def test_error_handler_does_not_expose_exception_message(self):
        await self.bot.command_error(self.interaction, RuntimeError("private detail"))
        sent = self.interaction.followup.send.await_args.args[0]
        self.assertNotIn("private detail", sent)
