"""Discord command authorization and lifecycle tests with no credentials/network."""
import asyncio
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from main import (
    Carter, CarterVoiceClient, ProviderError, ai_configuration, wake_configuration,
    warcraft_logs_configuration, warcraft_question_route,
)
from wake_listener import WakeSettings
from warcraft_history import HistoryStore, WarcraftHistoryError
from warcraft_logs import WarcraftLogsError, WarcraftLogsSettings


def complete_fight_detail(duration=10000, *, name="Player"):
    participant = {
        "id": 1, "guid": 100001, "name": name, "server": "Realm",
        "region": "US", "type": "Mage",
    }
    return {
        "playerDetails": {"data": {"playerDetails": {
            "dps": [participant], "healers": [], "tanks": [],
        }}},
        "damageDone": {"data": {"totalTime": duration, "entries": [
            {**participant, "total": duration, "activeTime": duration},
        ]}},
        "healing": {"data": {"totalTime": duration, "entries": []}},
    }


class ConfigurationTests(unittest.TestCase):
    def test_defaults_gemini_and_no_fallback(self):
        with patch.dict("os.environ", {}, clear=True):
            settings, limits = ai_configuration()
        self.assertEqual(settings.gemini_model, "gemini-3.8-live")
        self.assertEqual(settings.gemini_text_model, "gemini-3.8-flash")
        self.assertFalse(limits.allow_fallback)
        self.assertEqual(limits.inactivity, 45)
        self.assertIn("Respond only in English", settings.instructions)

    def test_invalid_provider_fallback_and_limits_rejected(self):
        for setting in ({"CARTER_AI_PROVIDER": "openai"},
                        {"CARTER_ALLOW_OPENAI_FALLBACK": "yes"},
                        {"CARTER_SESSION_TIMEOUT": "0"},
                        {"CARTER_MAX_SESSION_SECONDS": "999999"}):
            with patch.dict("os.environ", setting, clear=True):
                with self.assertRaises(ValueError):
                    ai_configuration()

    def test_wake_defaults_and_disable(self):
        with patch.dict("os.environ", {}, clear=True):
            settings = wake_configuration()
        self.assertTrue(settings.enabled)
        self.assertTrue(settings.auto_join)
        self.assertEqual(settings.word, "Carter")
        with patch.dict("os.environ", {"CARTER_WAKE_WORD_ENABLED": "false",
                                      "CARTER_AUTO_JOIN": "false"}, clear=True):
            settings = wake_configuration()
        self.assertFalse(settings.enabled)
        self.assertFalse(settings.auto_join)

    def test_warcraft_logs_requires_private_configuration_when_enabled(self):
        with patch.dict("os.environ", {"CARTER_WARCRAFT_LOGS_ENABLED": "false"}, clear=True):
            self.assertFalse(warcraft_logs_configuration().enabled)
        configured = {
            "CARTER_WARCRAFT_LOGS_ENABLED": "true",
            "CARTER_WARCRAFT_LOGS_CLIENT_ID": "client",
            "CARTER_WARCRAFT_LOGS_CLIENT_SECRET": "secret",
        }
        with patch.dict("os.environ", configured, clear=True):
            self.assertTrue(warcraft_logs_configuration().enabled)
            self.assertEqual(warcraft_logs_configuration().scan_messages, 5)
            self.assertEqual(warcraft_logs_configuration().backfill_messages, 100)
            self.assertEqual(warcraft_logs_configuration().timezone_name, "UTC")
        configured["CARTER_WARCRAFT_LOGS_CLIENT_SECRET"] = ""
        with patch.dict("os.environ", configured, clear=True):
            with self.assertRaises(ValueError):
                warcraft_logs_configuration()

    def test_warcraft_history_configuration_is_bounded_and_validated(self):
        configured = {
            "CARTER_WARCRAFT_LOGS_ENABLED": "true",
            "CARTER_WARCRAFT_LOGS_CLIENT_ID": "client",
            "CARTER_WARCRAFT_LOGS_CLIENT_SECRET": "secret",
            "CARTER_WARCRAFT_LOGS_SCAN_MESSAGES": "12",
            "CARTER_WARCRAFT_LOGS_BACKFILL_MESSAGES": "250",
            "CARTER_WARCRAFT_LOGS_TIMEZONE": "America/Los_Angeles",
            "CARTER_WARCRAFT_LOGS_RAID_DAY_CUTOFF_HOUR": "5",
        }
        with patch.dict("os.environ", configured, clear=True):
            settings = warcraft_logs_configuration()
        self.assertEqual(settings.scan_messages, 12)
        self.assertEqual(settings.backfill_messages, 250)
        self.assertEqual(settings.timezone_name, "America/Los_Angeles")
        self.assertEqual(settings.raid_day_cutoff_hour, 5)
        configured["CARTER_WARCRAFT_LOGS_TIMEZONE"] = "Not/A_Timezone"
        with patch.dict("os.environ", configured, clear=True):
            with self.assertRaises(ValueError):
                warcraft_logs_configuration()
        configured["CARTER_WARCRAFT_LOGS_TIMEZONE"] = "UTC"
        configured["CARTER_WARCRAFT_LOGS_SCAN_MESSAGES"] = "20"
        configured["CARTER_WARCRAFT_LOGS_BACKFILL_MESSAGES"] = "10"
        with patch.dict("os.environ", configured, clear=True):
            with self.assertRaises(ValueError):
                warcraft_logs_configuration()

    def test_warcraft_question_router_selects_evidence_modules(self):
        intent, modules = warcraft_question_route("Why did we wipe and who died?")
        self.assertEqual(intent, "death_analysis")
        self.assertIn("deaths", modules)
        self.assertIn("damageTaken", modules)
        intent, modules = warcraft_question_route("Why is my DPS low?")
        self.assertEqual(intent, "dps_analysis")
        self.assertIn("damageDone", modules)
        self.assertNotIn("dispels", modules)

class CommandTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.cache_directory = TemporaryDirectory()
        self.cache_patch = patch("main.CACHE_ROOT", Path(self.cache_directory.name))
        self.cache_patch.start()
        self.bot = Carter(10, 20, {30}, wake_settings=WakeSettings(enabled=False, auto_join=False))
        self.state = self.bot.runtime(10)
        self.state.channel_id = 20
        self.fake_session = SimpleNamespace(active=False, owner_id=30,
            start=AsyncMock(), stop=AsyncMock(), provider_name="gemini",
            settings=self.state.session.settings)
        self.state.session = self.fake_session
        self.channel = MagicMock()
        self.channel.id = 20
        self.channel.permissions_for.return_value = SimpleNamespace(
            view_channel=True, connect=True, speak=True,
        )
        self.guild = SimpleNamespace(voice_client=None, me=object(),
            get_channel=MagicMock(return_value=self.channel))
        self.interaction = SimpleNamespace(guild_id=10, guild=self.guild,
            user=SimpleNamespace(id=30, voice=SimpleNamespace(channel=self.channel)),
            response=SimpleNamespace(send_message=AsyncMock(), defer=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()))

    async def asyncTearDown(self):
        await self.bot.close()
        self.cache_patch.stop()
        self.cache_directory.cleanup()

    async def test_command_registration_preserves_existing_names(self):
        group = self.bot.tree.get_command("carter")
        self.assertEqual({command.name for command in group.commands},
                         {"join", "leave", "status", "activate", "stop", "ask",
                          "logsdebug", "setvoice", "setlogs", "voicechannel"})
        logs = self.bot.tree.get_command("logs")
        self.assertEqual(
            {command.name for command in logs.commands},
            {"scan", "sync", "reports", "history", "compare", "boss"},
        )

    async def test_ask_returns_public_text_without_starting_voice(self):
        with patch("main.generate_text", AsyncMock(return_value="Chat answer")) as generate:
            await self.bot.ask(self.interaction, "Question")
        self.interaction.response.defer.assert_awaited_once_with(
            ephemeral=False, thinking=True,
        )
        generate.assert_awaited_once_with(self.fake_session.settings, "Question")
        self.interaction.followup.send.assert_awaited_once()
        args, kwargs = self.interaction.followup.send.await_args
        self.assertEqual(args[0], "Chat answer")
        self.assertIn("allowed_mentions", kwargs)
        self.fake_session.start.assert_not_awaited()

    async def test_ask_injects_authoritative_historical_context(self):
        context = (
            'WARCRAFT LOGS HISTORICAL DATA (authoritative calculations):\n'
            '{"mode":"player_nights","nights":[{"dps":123.0}]}'
        )
        history = SimpleNamespace(question_context=MagicMock(return_value=context))
        self.bot.warcraft_history = history
        with patch("main.generate_text", AsyncMock(return_value="Historical answer")) as generate:
            await self.bot.ask(
                self.interaction, "Show me Shorted's DPS over the last five raids",
            )
        history.question_context.assert_called_once_with(
            10, "Show me Shorted's DPS over the last five raids",
        )
        settings = generate.await_args.args[0]
        self.assertIn("authoritative calculations", settings.instructions)
        self.assertIn("Do not recalculate by averaging per-fight DPS", settings.instructions)

    async def test_logs_history_posts_weighted_result_and_chart(self):
        result = {
            "character": {"current_name": "Shorted"},
            "nights_requested": 5,
            "nights_returned": 2,
            "nights": [
                {"raid_night": "2026-10-01", "dps": 1000.0,
                 "eligible_encounters": 2, "pulls": 2,
                 "source_urls": ["https://www.warcraftlogs.com/reports/AAAAAAAAAAAAAAAA"]},
                {"raid_night": "2026-10-08", "dps": 1200.0,
                 "eligible_encounters": 3, "pulls": 3,
                 "source_urls": ["https://www.warcraftlogs.com/reports/BBBBBBBBBBBBBBBB"]},
            ],
            "trend": {"direction": "increased", "percent_change": 20.0},
            "warning": "Boss composition differs.",
        }
        history = SimpleNamespace(player_nights=MagicMock(return_value=result))
        self.bot.warcraft_history = history
        await self.bot.logs_history(self.interaction, "Shorted", 5, "", False, False)
        history.player_nights.assert_called_once_with(
            10, "Shorted", nights=5, realm=None, result_filter="kills",
        )
        arguments = self.interaction.followup.send.await_args
        self.assertIn("Coverage: **2/5**", arguments.args[0])
        self.assertIn("+20.0%", arguments.args[0])
        self.assertEqual(arguments.kwargs["file"].filename, "dps-trend.png")

    async def test_logs_reports_compare_and_boss_use_stored_history(self):
        report_url = "https://www.warcraftlogs.com/reports/AAAAAAAAAAAAAAAA"
        history = SimpleNamespace(
            recent_reports=MagicMock(return_value=[{
                "title": "Raid", "canonical_url": report_url,
                "start_ms": 1_700_000_000_000, "encounters": 2, "players": 20,
                "report_code": "AAAAAAAAAAAAAAAA",
            }]),
            player_raid_night=MagicMock(return_value={
                "character": {"current_name": "Shorted"}, "raid_night": "2026-10-01",
                "bosses": [{
                    "boss_name": "Boss", "difficulty": 4, "dps": 1000.0,
                    "eligible_encounters": 1, "pulls": 2, "absent_encounters": 1,
                    "kills": 1, "pull_details": [{
                        "fight_id": 2, "kill": True, "duration_ms": 10000,
                        "dps": 1000.0, "metric_status": "damage_only",
                        "url": f"{report_url}?fight=2",
                    }],
                }],
                "strongest": {"boss_name": "Boss", "dps": 1000.0},
                "weakest": {"boss_name": "Boss", "dps": 1000.0},
            }),
            resolve_encounter=MagicMock(return_value={
                "encounter_id": 7, "boss_name": "Boss", "difficulty": 4,
            }),
            player_boss_history=MagicMock(return_value={
                "character": {"current_name": "Shorted"},
                "nights_requested": 5, "nights_returned": 1,
                "nights": [{
                    "raid_night": "2026-10-01", "dps": 1000.0,
                    "eligible_encounters": 1, "pulls": 1,
                    "source_urls": [report_url],
                }],
                "trend": {"direction": "insufficient_data", "percent_change": None},
            }),
        )
        self.bot.warcraft_history = history
        await self.bot.logs_reports(self.interaction, 10)
        self.assertIn("Stored Warcraft Logs reports", self.interaction.followup.send.await_args.args[0])
        self.interaction.followup.send.reset_mock()
        await self.bot.logs_compare(self.interaction, "Shorted", 5, "", False)
        comparison = self.interaction.followup.send.await_args.args[0]
        self.assertIn("fight 2", comparison)
        self.assertIn("1 absent", comparison)
        self.interaction.followup.send.reset_mock()
        await self.bot.logs_boss(self.interaction, "Shorted", "Boss", 5, "", None, False)
        self.assertIn("Matched encounter ID 7", self.interaction.followup.send.await_args.args[0])
        history.player_boss_history.assert_called_once_with(
            10, "Shorted", 7, difficulty=4, nights=5, realm=None,
            result_filter="kills",
        )

    async def test_logs_scan_reports_safe_counts(self):
        self.bot.warcraft_logs = SimpleNamespace(close=AsyncMock())
        self.bot.warcraft_history = SimpleNamespace(
            counts=MagicMock(return_value={"reports": 5, "encounters": 12,
                                           "performances": 100}),
        )
        self.state.logs_channel_id = 40
        scan_result = {
            "messages_scanned": 5, "codes_discovered": 5,
            "reports": [{}, {}, {}, {}], "failures": [("A", "private")],
            "skipped": [],
        }
        with patch.object(
            self.bot, "_scan_warcraft_channel", AsyncMock(return_value=scan_result),
        ) as scan:
            await self.bot.logs_scan(self.interaction, 5, False)
        scan.assert_awaited_once_with(self.state, limit=5, force=False)
        response = self.interaction.followup.send.await_args.args[0]
        self.assertIn("Scanned 5 messages", response)
        self.assertIn("failed 1", response)
        self.assertNotIn("private", response)

    async def test_large_backfill_is_reserved_and_completes_via_bot_channel(self):
        self.bot.warcraft_settings = WarcraftLogsSettings(
            True, 40, "client", "secret", scan_messages=5, backfill_messages=10,
        )
        self.bot.warcraft_logs = SimpleNamespace(close=AsyncMock())
        self.bot.warcraft_history = SimpleNamespace(
            counts=MagicMock(return_value={
                "reports": 6, "encounters": 20, "performances": 100,
            }),
        )
        self.state.logs_channel_id = 40
        self.interaction.channel_id = 90
        notification = SimpleNamespace(id=90, send=AsyncMock())
        self.bot.get_channel = MagicMock(return_value=notification)
        started = asyncio.Event()
        release = asyncio.Event()

        async def slow_scan(state, *, limit, force):
            self.assertIs(state, self.state)
            self.assertEqual(limit, 6)
            self.assertFalse(force)
            started.set()
            await release.wait()
            return {
                "messages_scanned": 6, "codes_discovered": 6,
                "reports": [{}] * 6, "failures": [], "skipped": [],
            }

        with patch.object(self.bot, "_scan_warcraft_channel", side_effect=slow_scan) as scan:
            await self.bot.logs_scan(self.interaction, 6, False)
            task = self.state.warcraft_backfill_task
            self.assertIsNotNone(task)
            await asyncio.wait_for(started.wait(), timeout=1)
            await self.bot._run_logs_scan(self.interaction, 6, force=False)
            self.assertIn(
                "already running", self.interaction.followup.send.await_args.args[0],
            )
            release.set()
            await asyncio.wait_for(task, timeout=1)
        self.assertEqual(scan.call_count, 1)
        self.assertIsNone(self.state.warcraft_backfill_task)
        notification.send.assert_awaited_once()
        summary = notification.send.await_args.args[0]
        self.assertIn("Scanned 6 messages", summary)
        self.assertIn("failed 0", summary)

    async def test_log_output_chunking_does_not_drop_later_coverage(self):
        lines = [f"{index}:" + "x" * 1800 for index in range(7)]
        lines.append("Coverage: final required summary")
        await self.bot._send_log_lines(self.interaction, lines)
        self.assertGreater(self.interaction.followup.send.await_count, 5)
        rendered = "\n".join(
            call.args[0] for call in self.interaction.followup.send.await_args_list
        )
        self.assertIn("Coverage: final required summary", rendered)

    async def test_ask_with_fight_timestamp_posts_positional_snapshot(self):
        code = "V8dLCwWZfTX91rYN"
        report = {
            "code": code, "title": "Raid", "zone": {"name": "Zone"},
            "startTime": 1700000000000,
            "fights": [{
                "id": 6, "name": "Test Boss", "encounterID": 1,
                "startTime": 1000, "endTime": 31000, "kill": False,
            }],
        }
        self.state.warcraft_reports = [report]
        self.state.latest_warcraft_report = report
        self.state.latest_warcraft_code = code
        from warcraft_logs import boss_framework
        self.state.boss_framework = boss_framework([report])
        self.state.warcraft_breakdowns[(code, 6)] = {}
        self.bot.warcraft_logs = SimpleNamespace(close=AsyncMock())
        attachment = object()
        replay_response = AsyncMock(return_value=("Replay caption", attachment))
        with (
            patch("main.generate_text", AsyncMock(return_value="Chat answer")),
            patch.object(self.bot, "_warcraft_replay_response", replay_response),
        ):
            await self.bot.ask(self.interaction, "What happened at 0:10?")
        replay_response.assert_awaited_once()
        fight, offset = replay_response.await_args.args[1:]
        self.assertEqual(fight["id"], 6)
        self.assertEqual(offset, 10000)
        self.assertEqual(self.interaction.followup.send.await_count, 2)
        replay_call = self.interaction.followup.send.await_args_list[1]
        self.assertEqual(replay_call.args[0], "Replay caption")
        self.assertIs(replay_call.kwargs["file"], attachment)
        self.assertTrue(replay_call.kwargs["suppress_embeds"])

    async def test_ask_reports_safe_provider_error(self):
        with patch("main.generate_text", AsyncMock(side_effect=ProviderError("provider_timeout"))):
            await self.bot.ask(self.interaction, "Question")
        self.interaction.followup.send.assert_awaited_once_with(
            "Carter could not answer that prompt (provider_timeout). Try again later.",
            ephemeral=True,
        )

    async def test_ask_loads_named_fight_detail_from_current_report(self):
        self.bot.warcraft_logs = SimpleNamespace(
            fight_breakdown=AsyncMock(return_value={"summary": {"entries": []}}),
            close=AsyncMock(),
        )
        self.state.latest_warcraft_code = "V8dLCwWZfTX91rYN"
        self.state.latest_warcraft_report = {
            "code": "V8dLCwWZfTX91rYN", "title": "Raid", "zone": {"name": "Zone"},
            "fights": [
                {"id": 6, "name": "Kith'ix", "encounterID": 1,
                 "startTime": 0, "endTime": 1000, "kill": True},
                {"id": 15, "name": "Nymrissa Wavecaller", "encounterID": 2,
                 "startTime": 2000, "endTime": 3000, "kill": False},
            ],
        }
        with patch("main.generate_text", AsyncMock(return_value="Answer")) as generate:
            await self.bot.ask(self.interaction, "Who had top DPS on Kith'ix?")
        self.bot.warcraft_logs.fight_breakdown.assert_awaited_once_with(
            "V8dLCwWZfTX91rYN", 6,
        )
        settings = generate.await_args.args[0]
        self.assertIn("Detailed API tables apply specifically to fight 6 (Kith'ix)",
                      settings.instructions)

    async def test_prompt_url_selects_exact_report_and_fight(self):
        self.bot.warcraft_logs = SimpleNamespace(
            fight_breakdown=AsyncMock(return_value={"damageDone": {"entries": []}}),
            close=AsyncMock(),
        )
        code = "V8dLCwWZfTX91rYN"
        self.state.warcraft_reports = [{
            "code": code, "startTime": 1700000000000,
            "fights": [
                {"id": 6, "name": "Kith'ix", "encounterID": 1,
                 "startTime": 0, "endTime": 1000, "kill": True},
                {"id": 9, "name": "Other", "encounterID": 2,
                 "startTime": 2000, "endTime": 3000, "kill": True},
            ],
        }]
        from warcraft_logs import boss_framework
        self.state.boss_framework = boss_framework(self.state.warcraft_reports)
        fight, detail = await self.bot._warcraft_prompt_detail(
            self.state,
            "Analyze https://www.warcraftlogs.com/reports/V8dLCwWZfTX91rYN?fight=6",
        )
        self.assertEqual(fight["id"], 6)
        self.assertEqual(fight["_carter_report_code"], code)
        self.assertIn("damageDone", detail)
        self.bot.warcraft_logs.fight_breakdown.assert_awaited_once_with(code, 6)

    async def test_unknown_timestamped_report_url_never_falls_back_to_current_fight(self):
        code = "V8dLCwWZfTX91rYN"
        report = {
            "code": code, "startTime": 1700000000000,
            "fights": [{"id": 6, "name": "Boss", "encounterID": 1,
                        "startTime": 0, "endTime": 20000, "kill": False}],
        }
        self.state.warcraft_reports = [report]
        from warcraft_logs import boss_framework
        self.state.boss_framework = boss_framework([report])
        self.bot.warcraft_logs = SimpleNamespace(close=AsyncMock())
        fight, detail = await self.bot._warcraft_prompt_detail(
            self.state,
            "Show https://www.warcraftlogs.com/reports/AAAAAAAAAAAAAAAA"
            "#fight=9&view=replay&position=5000",
        )
        self.assertIsNone(fight)
        self.assertIsNone(detail)

    async def test_bare_report_url_and_textual_fight_number_select_exact_fight(self):
        code = "V8dLCwWZfTX91rYN"
        report = {
            "code": code, "startTime": 1700000000000,
            "fights": [
                {"id": 6, "name": "First Boss", "encounterID": 1,
                 "startTime": 0, "endTime": 20000, "kill": False},
                {"id": 9, "name": "Newest Boss", "encounterID": 2,
                 "startTime": 30000, "endTime": 60000, "kill": False},
            ],
        }
        self.state.warcraft_reports = [report]
        from warcraft_logs import boss_framework
        self.state.boss_framework = boss_framework([report])
        self.state.warcraft_breakdowns[(code, 6)] = {"summary": {}}
        self.bot.warcraft_logs = SimpleNamespace(close=AsyncMock())
        fight, _ = await self.bot._warcraft_prompt_detail(
            self.state,
            f"Analyze https://www.warcraftlogs.com/reports/{code} fight 6 at 0:10",
        )
        self.assertEqual(fight["id"], 6)

    async def test_timestamped_latest_wipe_selects_wipe_instead_of_newer_kill(self):
        code = "V8dLCwWZfTX91rYN"
        report = {
            "code": code, "startTime": 1700000000000,
            "fights": [
                {"id": 6, "name": "Test Boss", "encounterID": 1,
                 "startTime": 1000, "endTime": 21000, "kill": False},
                {"id": 7, "name": "Test Boss", "encounterID": 1,
                 "startTime": 30000, "endTime": 50000, "kill": True},
                {"id": 8, "name": "Test Boss", "encounterID": 1,
                 "startTime": 60000, "endTime": 70000, "kill": False,
                 "inProgress": True},
            ],
        }
        self.state.warcraft_reports = [report]
        self.state.latest_warcraft_report = report
        from warcraft_logs import boss_framework
        self.state.boss_framework = boss_framework([report])
        self.state.warcraft_breakdowns[(code, 6)] = {"summary": {}}
        self.bot.warcraft_logs = SimpleNamespace(close=AsyncMock())
        fight, _ = await self.bot._warcraft_prompt_detail(
            self.state, "What happened at 0:05 in the latest wipe on Test Boss?",
        )
        self.assertEqual(fight["id"], 6)

    async def test_warcraft_context_uses_dps_rate_not_damage_order(self):
        code = "A" * 16
        fight = {"id": 6, "name": "Boss", "encounterID": 1,
                 "startTime": 0, "endTime": 30000, "kill": True,
                 "_carter_report_code": code}
        self.state.latest_warcraft_report = {
            "code": code, "title": "Raid", "zone": {"name": "Zone"},
            "fights": [fight],
        }
        self.state.warcraft_reports = [self.state.latest_warcraft_report]
        detail = {"damageDone": {"data": {"totalTime": 30000, "entries": [
            {"id": 1, "name": "Burst", "type": "Mage", "total": 1000,
             "activeTime": 10000},
            {"id": 2, "name": "Total", "type": "Warrior", "total": 1500,
             "activeTime": 30000},
        ]}}}
        self.bot._apply_warcraft_context(self.state, fight, detail, "Who had top DPS?")
        instructions = self.state.session.settings.instructions
        self.assertIn("1. Total 50.00 DPS", instructions)
        self.assertIn("Do not answer by sorting or comparing raw total damage", instructions)

    async def test_bare_top_dps_uses_newest_fight_breakdown(self):
        code = "B" * 16
        self.state.latest_warcraft_report = {
            "code": code, "title": "Raid", "zone": {"name": "Zone"},
            "fights": [{"id": 9, "name": "Latest Boss", "encounterID": 2,
                        "startTime": 0, "endTime": 20000, "kill": False}],
        }
        self.state.warcraft_reports = [self.state.latest_warcraft_report]
        self.state.latest_warcraft_breakdown = {
            "damageDone": {"data": {"totalTime": 20000, "entries": [
                {"id": 1, "name": "Top Player", "type": "Mage",
                 "total": 2000, "activeTime": 19000},
            ]}},
        }
        self.bot._apply_warcraft_context(self.state, prompt="Who had top DPS?")
        self.assertIn("1. Top Player 100.00 DPS", self.state.session.settings.instructions)

    async def test_latest_death_maps_player_event_to_boss_attempt_and_utc(self):
        code = "A" * 16
        report = {"code": code, "startTime": 1700000000000, "fights": [
            {"id": 4, "name": "Test Boss", "encounterID": 9,
             "startTime": 1000, "endTime": 10000, "kill": False},
        ]}
        self.state.warcraft_reports = [report]
        from warcraft_logs import boss_framework
        self.state.boss_framework = boss_framework([report])
        self.bot.warcraft_logs = SimpleNamespace(
            death_events=AsyncMock(return_value={
                "masterData": {"actors": [{"id": 12, "name": "Exampleplayer"}]},
                "deaths": {"data": [{"type": "death", "targetID": 12, "timestamp": 5000}]},
            }),
            close=AsyncMock(),
        )
        context = await self.bot._latest_death_context(
            self.state, "What was the latest death of Exampleplayer?",
        )
        self.assertIn("Exampleplayer most recently died", context)
        self.assertIn("Test Boss", context)
        self.assertIn(f"report {code} fight 4", context)
        self.assertIn("UTC", context)

    async def test_warcraft_disk_cache_round_trip_is_server_and_channel_scoped(self):
        report = {"code": "A" * 16, "startTime": 1700000000000, "fights": [
            {"id": 4, "name": "Boss", "encounterID": 9,
             "startTime": 1000, "endTime": 10000, "kill": True},
        ]}
        self.state.logs_channel_id = 40
        self.state.warcraft_reports = [report]
        from warcraft_logs import boss_framework
        self.state.boss_framework = boss_framework([report])
        self.state.warcraft_breakdowns[("A" * 16, 4)] = {"summary": {}}
        self.state.warcraft_death_events["A" * 16] = {"deaths": {"data": []}}
        with TemporaryDirectory() as directory, patch("main.CACHE_ROOT", Path(directory)):
            self.bot._save_warcraft_cache(self.state)
            cache = Path(directory) / "guild-10.json"
            self.assertTrue(cache.exists())
            self.assertEqual(cache.stat().st_mode & 0o777, 0o600)
            self.state.warcraft_reports.clear()
            self.state.boss_framework.clear()
            self.state.warcraft_breakdowns.clear()
            self.state.warcraft_death_events.clear()
            self.assertTrue(self.bot._load_warcraft_cache(self.state))
        self.assertEqual(self.state.warcraft_reports[0]["code"], "A" * 16)
        self.assertIn(("A" * 16, 4), self.state.warcraft_breakdowns)
        self.assertIn("9", self.state.boss_framework)

    async def test_logs_channel_setting_round_trip_is_persistent(self):
        self.state.logs_channel_id = 40
        with TemporaryDirectory() as directory, patch("main.CACHE_ROOT", Path(directory)):
            self.bot._save_logs_channel_setting(self.state)
            settings_file = Path(directory) / "guild-settings.json"
            self.assertEqual(settings_file.stat().st_mode & 0o777, 0o600)
            self.assertEqual(
                self.bot._load_guild_settings()["10"]["logs_channel_id"], 40,
            )

    async def test_logs_debug_renders_safe_parsed_data_tiles(self):
        code = "A" * 16
        report = {
            "code": code, "title": "Debug Raid", "startTime": 1700000000000,
            "zone": {"name": "Test Zone"},
            "fights": [{"id": 4, "name": "Test Boss", "encounterID": 9,
                        "startTime": 1000, "endTime": 10000, "kill": True}],
        }
        from warcraft_logs import boss_framework
        self.bot.warcraft_logs = SimpleNamespace(close=AsyncMock())
        self.state.logs_channel_id = 40
        self.state.latest_warcraft_code = code
        self.state.latest_warcraft_report = report
        self.state.warcraft_reports = [report]
        self.state.boss_framework = boss_framework([report])
        self.state.latest_warcraft_breakdown = {
            "damageDone": {"data": {"totalTime": 10000, "entries": [
                {"name": "Nested Player", "total": 123, "activeTime": 456},
            ]}},
            "interrupts": {"entries": []},
        }
        self.state.warcraft_breakdowns[(code, 4)] = self.state.latest_warcraft_breakdown
        self.state.warcraft_death_events[code] = {
            "masterData": {"actors": [{"id": 1, "name": "Player"}]},
            "deaths": {"data": [{"targetID": 1, "timestamp": 5000}]},
        }
        await self.bot.logs_debug(self.interaction)
        kwargs = self.interaction.followup.send.await_args.kwargs
        self.assertTrue(kwargs["ephemeral"])
        self.assertGreaterEqual(len(kwargs["embeds"]), 3)
        rendered = "\n".join(
            value
            for embed in kwargs["embeds"]
            for value in ([embed.title or "", embed.description or ""]
                          + [item for field in embed.fields
                             for item in (field.name, field.value)])
        )
        self.assertIn("Debug Raid", rendered)
        self.assertIn("Test Boss", rendered)
        self.assertIn("Nested Player", rendered)
        self.assertIn("total=123", rendered)
        self.assertIn("entries=1", rendered)
        self.assertIn("Death event index", rendered)
        self.assertNotIn("client_secret", rendered.lower())

    async def test_ready_restores_saved_logs_channel_without_joining_voice(self):
        bot = Carter(0, 0, {30}, wake_settings=WakeSettings(enabled=False, auto_join=False),
                     warcraft_settings=WarcraftLogsSettings(True, 0, "client", "secret"))
        bot.warcraft_logs = SimpleNamespace(close=AsyncMock())
        bot._connection.user = SimpleNamespace(id=123)
        channel = SimpleNamespace(id=40, history=MagicMock())
        guild = SimpleNamespace(id=10, get_channel=MagicMock(return_value=channel), voice_client=None)
        bot._connection._guilds[10] = guild
        bot._restore_latest_warcraft_report = AsyncMock()
        bot._connect_voice = AsyncMock()
        try:
            with TemporaryDirectory() as directory, patch("main.CACHE_ROOT", Path(directory)):
                state = bot.runtime(10)
                state.logs_channel_id = 40
                bot._save_logs_channel_setting(state)
                state.logs_channel_id = 0
                await bot.on_ready()
                await state.warcraft_restore_task
                self.assertEqual(state.logs_channel_id, 40)
                bot._restore_latest_warcraft_report.assert_awaited_once_with(state)
                bot._connect_voice.assert_not_awaited()
        finally:
            await bot.close()

    async def test_guild_runtimes_are_isolated(self):
        other = self.bot.runtime(999)
        self.state.channel_id = 20
        self.state.logs_channel_id = 40
        self.state.channels_selected = True
        other.channel_id = 21
        other.logs_channel_id = 41
        other.channels_selected = True
        self.assertIsNot(self.state.session, other.session)
        self.assertIsNot(self.state.wake, other.wake)
        self.assertIsNot(self.state.voice_lock, other.voice_lock)
        self.assertEqual((self.state.channel_id, self.state.logs_channel_id), (20, 40))
        self.assertEqual((other.channel_id, other.logs_channel_id), (21, 41))

    async def test_authorization_denies_wrong_user_but_allows_another_guild(self):
        self.interaction.user.id = 999
        await self.bot.activate(self.interaction)
        self.fake_session.start.assert_not_awaited()
        self.interaction.response.send_message.assert_awaited_once()
        self.interaction.user.id = 30
        self.interaction.guild_id = 999
        await self.bot.leave(self.interaction)
        self.assertIn(999, self.bot.guilds_runtime)

    async def test_expired_interaction_stops_command_before_side_effects(self):
        class ExpiredInteraction(Exception):
            code = 10062

        self.interaction.command = SimpleNamespace(qualified_name="carter setlogs")
        self.interaction.response.defer.side_effect = ExpiredInteraction()
        with patch("main.discord.NotFound", ExpiredInteraction):
            await self.bot.set_logs_channel(self.interaction, MagicMock(id=40))
        self.assertEqual(self.state.logs_channel_id, 0)
        self.interaction.followup.send.assert_not_awaited()

    async def test_error_handler_does_not_reply_twice_to_expired_interaction(self):
        class ExpiredInteraction(Exception):
            code = 10062

        self.interaction.command = SimpleNamespace(qualified_name="carter setlogs")
        self.interaction.response.is_done = MagicMock(return_value=False)
        self.interaction.response.send_message.side_effect = ExpiredInteraction()
        with patch("main.discord.NotFound", ExpiredInteraction):
            await self.bot.command_error(self.interaction, RuntimeError("private detail"))
        self.interaction.response.send_message.assert_awaited_once()
        self.interaction.followup.send.assert_not_awaited()

    async def test_join_uses_receive_client_and_stays_inactive(self):
        self.state.channels_selected = True
        self.channel.connect = AsyncMock()
        with patch("main.discord.VoiceChannel", type(self.channel)):
            await self.bot.join(self.interaction)
        self.channel.connect.assert_awaited_once_with(
            cls=CarterVoiceClient, timeout=20, reconnect=True, self_deaf=False,
        )
        self.fake_session.start.assert_not_awaited()

    async def test_join_requires_runtime_channel_selection(self):
        await self.bot.join(self.interaction)
        self.interaction.followup.send.assert_awaited_once_with(
            "Use /carter setvoice and /carter setlogs to configure this server first.",
            ephemeral=True,
        )

    async def test_setvoice_does_not_require_member_voice_or_connect(self):
        self.interaction.user.voice = None
        current = SimpleNamespace(disconnect=AsyncMock())
        self.guild.voice_client = current
        await self.bot.set_voice_channel(self.interaction, self.channel)
        self.assertEqual(self.state.channel_id, 20)
        self.assertFalse(self.state.channels_selected)
        current.disconnect.assert_awaited_once_with(force=True)
        self.fake_session.stop.assert_awaited_once()
        self.channel.connect.assert_not_called()

    async def test_setlogs_does_not_require_voice_and_restores_history(self):
        bot = Carter(0, 0, {30}, wake_settings=WakeSettings(enabled=False, auto_join=False),
                     warcraft_settings=WarcraftLogsSettings(True, 0, "client", "secret"))
        bot.warcraft_logs = SimpleNamespace(close=AsyncMock())
        state = bot.runtime(10)
        state.channel_id = 20
        channel = MagicMock(id=40)
        channel.permissions_for.return_value = SimpleNamespace(
            view_channel=True, read_message_history=True, send_messages=True,
        )
        interaction = SimpleNamespace(
            guild_id=10, guild=SimpleNamespace(me=object(), voice_client=None),
            user=SimpleNamespace(id=30, voice=None),
            response=SimpleNamespace(send_message=AsyncMock(), defer=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
        )
        bot._restore_latest_warcraft_report = AsyncMock()
        try:
            await bot.set_logs_channel(interaction, channel)
            await state.warcraft_restore_task
            self.assertEqual(state.logs_channel_id, 40)
            self.assertTrue(state.channels_selected)
            bot._restore_latest_warcraft_report.assert_awaited_once_with(state)
        finally:
            await bot.close()

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

    async def test_activate_accepts_owner_then_rejects_duplicate(self):
        voice = MagicMock(spec=CarterVoiceClient)
        voice.is_connected.return_value = True
        voice.channel = self.channel
        self.guild.voice_client = voice
        await self.bot.activate(self.interaction)
        self.fake_session.start.assert_awaited_once()
        self.assertEqual(self.fake_session.start.await_args.args[:2], (voice, 30))
        self.fake_session.active = True
        await self.bot.activate(self.interaction)
        self.fake_session.start.assert_awaited_once()

    async def test_leave_closes_ai_before_disconnecting(self):
        calls = []
        self.fake_session.stop.side_effect = lambda: calls.append("stop")
        voice = SimpleNamespace(disconnect=AsyncMock(side_effect=lambda **kw: calls.append("disconnect")))
        self.guild.voice_client = voice
        await self.bot.leave(self.interaction)
        self.assertEqual(calls, ["stop", "disconnect"])

    async def test_owner_leaving_closes_ai(self):
        member = SimpleNamespace(id=30, guild=SimpleNamespace(id=10))
        await self.bot.on_voice_state_update(member, None, SimpleNamespace(channel=None))
        self.fake_session.stop.assert_awaited_once()

    async def test_stop_ai_keeps_voice_connected(self):
        voice = SimpleNamespace(disconnect=AsyncMock())
        self.guild.voice_client = voice
        await self.bot.stop_ai(self.interaction)
        self.fake_session.stop.assert_awaited_once()
        voice.disconnect.assert_not_awaited()

    async def test_wake_activates_authorized_member_with_shared_receiver(self):
        voice = SimpleNamespace(channel=self.channel, is_connected=lambda: True,
            guild=SimpleNamespace(get_member=lambda user_id: self.interaction.user))
        self.state.wake.running = True
        self.state.wake.voice = voice
        generation = self.state.wake.generation
        self.assertTrue(await self.bot._wake_activate(self.state, 30, voice, generation))
        self.fake_session.start.assert_awaited_once()
        self.assertTrue(self.fake_session.start.await_args.kwargs["external_capture"])
        self.fake_session.start.reset_mock()
        self.state.wake.reset()
        self.assertFalse(await self.bot._wake_activate(self.state, 30, voice, generation))
        self.fake_session.start.assert_not_awaited()
        self.state.wake.voice = None
        self.state.wake.running = False

    async def test_wake_rechecks_speaker_membership_before_opening_ai(self):
        voice = SimpleNamespace(channel=self.channel, is_connected=lambda: True,
            guild=SimpleNamespace(get_member=lambda user_id: None))
        self.state.wake.running = True
        self.state.wake.voice = voice
        self.assertFalse(await self.bot._wake_activate(
            self.state, 30, voice, self.state.wake.generation,
        ))
        self.fake_session.start.assert_not_awaited()
        self.state.wake.voice = None
        self.state.wake.running = False

    async def test_stop_rearms_local_listener_and_leave_disarms_it(self):
        calls = []
        wake = SimpleNamespace(running=True, reset=MagicMock(),
            stop=AsyncMock(side_effect=lambda: calls.append("wake_stop")),
            settings=WakeSettings(), last_error=None)
        self.state.wake = wake
        self.fake_session.stop.side_effect = lambda: calls.append("ai_stop")
        voice = SimpleNamespace(disconnect=AsyncMock(side_effect=lambda **kw: calls.append("disconnect")))
        self.guild.voice_client = voice
        await self.bot.stop_ai(self.interaction)
        wake.reset.assert_called_once()
        wake.stop.assert_not_awaited()
        voice.disconnect.assert_not_awaited()
        calls.clear()
        await self.bot.leave(self.interaction)
        self.assertEqual(calls, ["wake_stop", "ai_stop", "disconnect"])
        self.assertFalse(self.state.want_voice)

    async def test_ready_never_autojoins_or_restores_logs(self):
        bot_user = SimpleNamespace(id=123)
        self.bot._connection.user = bot_user
        self.state.want_voice = True
        self.bot.get_guild = MagicMock(return_value=self.guild)
        self.bot._connect_voice = AsyncMock()
        await self.bot.on_ready()
        self.bot._connect_voice.assert_not_awaited()
        self.assertIsNone(self.state.warcraft_restore_task)

    async def test_warcraft_link_monitor_is_channel_and_user_scoped(self):
        bot = Carter(10, 20, {30}, wake_settings=WakeSettings(enabled=False, auto_join=False),
                     warcraft_settings=WarcraftLogsSettings(True, 40, "client", "secret"))
        report = {"title": "Raid", "startTime": 0, "endTime": 1000, "fights": []}
        bot.warcraft_logs = SimpleNamespace(
            report=AsyncMock(return_value=report),
            fight_breakdown=AsyncMock(return_value={}), close=AsyncMock(),
        )
        state = bot.runtime(10)
        state.channels_selected = True
        state.logs_channel_id = 40
        message = SimpleNamespace(
            guild=SimpleNamespace(id=10), channel=SimpleNamespace(id=40),
            author=SimpleNamespace(id=30, bot=False),
            content="https://www.warcraftlogs.com/reports/BWftZjM6zxDQ2bdX",
            reply=AsyncMock(),
        )
        try:
            await bot.on_message(message)
            bot.warcraft_logs.report.assert_awaited_once_with("BWftZjM6zxDQ2bdX")
            message.reply.assert_awaited_once()
            message.channel.id = 999
            message.content = "https://www.warcraftlogs.com/reports/AAAAAAAAAAAAAAAA"
            await bot.on_message(message)
            bot.warcraft_logs.report.assert_awaited_once()
        finally:
            await bot.close()

    async def test_history_scan_ingests_five_messages_and_isolates_first_failure(self):
        settings = WarcraftLogsSettings(
            True, 40, "client", "secret", scan_messages=5,
        )
        bot = Carter(10, 20, {30}, wake_settings=WakeSettings(enabled=False, auto_join=False),
                     warcraft_settings=settings)
        codes = [f"REPORT{index:010d}" for index in range(5)]

        async def report(code):
            if code == codes[0]:
                raise main.WarcraftLogsError("report_unavailable")
            return {
                "code": code, "title": code, "startTime": codes.index(code) * 1000,
                "endTime": codes.index(code) * 1000 + 500, "fights": [],
            }

        # Import through the module so the exception class matches Carter's handler.
        import main
        bot.warcraft_logs = SimpleNamespace(
            report=AsyncMock(side_effect=report), close=AsyncMock(),
        )
        state = bot.runtime(10)
        state.logs_channel_id = 40
        messages = [SimpleNamespace(
            id=index, guild=SimpleNamespace(id=10), channel=SimpleNamespace(id=40),
            content=f"https://www.warcraftlogs.com/reports/{code}",
        ) for index, code in enumerate(codes)]

        async def history(*, limit):
            self.assertEqual(limit, 5)
            for message in messages:
                yield message

        channel = SimpleNamespace(history=history)
        bot.get_channel = MagicMock(return_value=channel)
        try:
            result = await bot._scan_warcraft_channel(state)
            self.assertEqual(result["messages_scanned"], 5)
            self.assertEqual(result["codes_discovered"], 5)
            self.assertEqual(len(result["reports"]), 4)
            self.assertEqual(result["failures"][0][0], codes[0])
            self.assertEqual(bot.warcraft_logs.report.await_count, 5)
            self.assertEqual({item["code"] for item in state.warcraft_reports}, set(codes[1:]))
            self.assertEqual(bot.warcraft_history.counts(10)["reports"], 4)
        finally:
            await bot.close()

    async def test_five_messages_persist_five_reports_without_discord_afterward(self):
        settings = WarcraftLogsSettings(True, 40, "client", "secret", scan_messages=5)
        bot = Carter(10, 20, {30}, wake_settings=WakeSettings(enabled=False, auto_join=False),
                     warcraft_settings=settings)
        codes = [f"FIVETEST{index:08d}" for index in range(5)]

        async def report(code):
            position = codes.index(code)
            return {"code": code, "title": f"Night {position + 1}",
                    "startTime": 1000 + position * 1000, "endTime": 1500 + position * 1000,
                    "fights": []}

        bot.warcraft_logs = SimpleNamespace(report=AsyncMock(side_effect=report), close=AsyncMock())
        state = bot.runtime(10)
        state.logs_channel_id = 40
        messages = [SimpleNamespace(
            id=index, guild=SimpleNamespace(id=10), channel=SimpleNamespace(id=40),
            content=f"https://www.warcraftlogs.com/reports/{code}",
        ) for index, code in enumerate(codes)]

        async def history(*, limit):
            self.assertEqual(limit, 5)
            for message in messages:
                yield message

        bot.get_channel = MagicMock(return_value=SimpleNamespace(history=history))
        try:
            result = await bot._scan_warcraft_channel(state)
            self.assertEqual(len(result["reports"]), 5)
            self.assertEqual(bot.warcraft_history.counts(10)["reports"], 5)
            stored = bot.warcraft_history.recent_reports(10, limit=10)
            self.assertEqual({item["report_code"] for item in stored}, set(codes))
            # The query above is database-only; Discord history is no longer involved.
            bot.get_channel = MagicMock(side_effect=AssertionError("Discord must not be fetched"))
            reopened = HistoryStore(
                bot.warcraft_history.path,
                timezone_name=settings.timezone_name,
                cutoff_hour=settings.raid_day_cutoff_hour,
            )
            self.assertEqual(len(reopened.recent_reports(10, limit=10)), 5)
        finally:
            await bot.close()

    async def test_live_message_ingests_more_than_three_links_and_edit_additions(self):
        settings = WarcraftLogsSettings(True, 40, "client", "secret")
        bot = Carter(10, 20, {30}, wake_settings=WakeSettings(enabled=False, auto_join=False),
                     warcraft_settings=settings)
        codes = [f"MULTILINK{index:07d}" for index in range(4)]

        async def report(code):
            return {"code": code, "title": code, "startTime": codes.index(code) * 1000,
                    "endTime": codes.index(code) * 1000 + 500, "fights": []}

        bot.warcraft_logs = SimpleNamespace(report=AsyncMock(side_effect=report), close=AsyncMock())
        state = bot.runtime(10)
        state.logs_channel_id = 40
        message = SimpleNamespace(
            id=1, guild=SimpleNamespace(id=10), channel=SimpleNamespace(id=40),
            content=" ".join(
                f"https://www.warcraftlogs.com/reports/{code}" for code in codes
            ),
            reply=AsyncMock(),
        )
        try:
            result = await bot._process_warcraft_message(message)
            self.assertEqual(len(result["reports"]), 4)
            self.assertEqual(bot.warcraft_logs.report.await_count, 4)
            message.content += f" https://www.warcraftlogs.com/reports/{codes[0]}?fight=2"
            await bot.on_message_edit(None, message)
            self.assertEqual(bot.warcraft_logs.report.await_count, 4)
            new_code = "MULTILINK9999999"
            codes.append(new_code)
            message.content += f" https://www.warcraftlogs.com/reports/{new_code}"
            await bot.on_message_edit(None, message)
            self.assertEqual(bot.warcraft_logs.report.await_count, 5)
            state.warcraft_seen = {item: -1_000_000_000 for item in codes}
            await bot.on_message_edit(None, message)
            self.assertEqual(bot.warcraft_logs.report.await_count, 10)
            message.reply.assert_awaited_once()
        finally:
            await bot.close()

    async def test_live_report_monitor_refreshes_when_fights_change(self):
        bot = Carter(0, 0, {30}, wake_settings=WakeSettings(enabled=False, auto_join=False),
                     warcraft_settings=WarcraftLogsSettings(True, 40, "client", "secret", 0.01))
        first = {"code": "AAAAAAAAAAAAAAAA", "title": "Live",
                 "startTime": 1700000000000, "endTime": 1700000004000, "fights": [
            {"id": 1, "name": "Boss", "encounterID": 7, "startTime": 0,
             "endTime": 1000, "kill": False, "inProgress": False},
        ]}
        updated = {"code": "AAAAAAAAAAAAAAAA", "title": "Live",
                   "startTime": first["startTime"], "endTime": first["endTime"],
                   "fights": first["fights"] + [
            {"id": 2, "name": "Boss", "encounterID": 7, "startTime": 2000,
             "endTime": 4000, "kill": True, "inProgress": False},
        ]}
        bot.warcraft_logs = SimpleNamespace(
            report=AsyncMock(return_value=first),
            fight_breakdown=AsyncMock(return_value={}), close=AsyncMock(),
            death_events=AsyncMock(return_value={"masterData": {"actors": []},
                                                  "deaths": {"data": []}}),
        )
        state = bot.runtime(10)
        state.channels_selected = True
        state.logs_channel_id = 40
        try:
            await bot._load_warcraft_report(state, "AAAAAAAAAAAAAAAA")
            bot.warcraft_logs.report.return_value = updated
            await bot._start_warcraft_monitor(state, "AAAAAAAAAAAAAAAA")
            await asyncio.sleep(0.04)
            self.assertEqual(len(state.latest_warcraft_report["fights"]), 2)
        finally:
            await bot.close()

    async def test_completed_live_fight_replaces_provisional_detail(self):
        bot = Carter(0, 0, {30}, wake_settings=WakeSettings(enabled=False, auto_join=False),
                     warcraft_settings=WarcraftLogsSettings(True, 40, "client", "secret"))
        code = "LIVEFINAL0000001"
        live = {"code": code, "title": "Live", "startTime": 1_700_000_000_000,
                "fights": [{"id": 1, "name": "Boss", "encounterID": 7,
                            "startTime": 0, "endTime": 5_000, "kill": False,
                            "inProgress": True}]}
        completed = {**live, "fights": [{**live["fights"][0], "endTime": 10_000,
                                         "kill": True, "inProgress": False}]}
        provisional = complete_fight_detail(5_000)
        final = complete_fight_detail(10_000)
        bot.warcraft_logs = SimpleNamespace(
            report=AsyncMock(return_value=live),
            fight_breakdown=AsyncMock(side_effect=[provisional, final]),
            death_events=AsyncMock(return_value={"deaths": {"data": []}}),
            close=AsyncMock(),
        )
        state = bot.runtime(10)
        state.logs_channel_id = 40
        try:
            await bot._load_warcraft_report(state, code)
            await bot._load_warcraft_report(state, code, completed)
            self.assertEqual(bot.warcraft_logs.fight_breakdown.await_count, 2)
            self.assertIs(state.warcraft_breakdowns[(code, 1)], final)
        finally:
            await bot.close()

    async def test_completed_live_fight_detail_failure_keeps_provisional_state(self):
        bot = Carter(0, 0, {30}, wake_settings=WakeSettings(enabled=False, auto_join=False),
                     warcraft_settings=WarcraftLogsSettings(True, 40, "client", "secret"))
        code = "LIVEFINALRETRY01"
        live = {"code": code, "title": "Live", "startTime": 1_700_000_000_000,
                "fights": [{"id": 1, "name": "Boss", "encounterID": 7,
                            "startTime": 0, "endTime": 5_000, "kill": False,
                            "inProgress": True}]}
        completed = {**live, "fights": [{**live["fights"][0], "endTime": 10_000,
                                         "kill": True, "inProgress": False}]}
        provisional = complete_fight_detail(5_000)
        final = complete_fight_detail(10_000)
        bot.warcraft_logs = SimpleNamespace(
            report=AsyncMock(return_value=live),
            fight_breakdown=AsyncMock(side_effect=[
                provisional, WarcraftLogsError("temporary_failure"), final,
            ]),
            death_events=AsyncMock(return_value={"deaths": {"data": []}}),
            close=AsyncMock(),
        )
        state = bot.runtime(10)
        state.logs_channel_id = 40
        try:
            await bot._load_warcraft_report(state, code)
            original_fingerprint = state.latest_warcraft_fingerprint
            with self.assertRaisesRegex(WarcraftLogsError, "fight_detail_incomplete"):
                await bot._load_warcraft_report(state, code, completed)
            self.assertIs(state.warcraft_breakdowns[(code, 1)], provisional)
            self.assertTrue(state.latest_warcraft_report["fights"][0]["inProgress"])
            self.assertEqual(state.latest_warcraft_fingerprint, original_fingerprint)
            await bot._load_warcraft_report(state, code, completed)
            self.assertIs(state.warcraft_breakdowns[(code, 1)], final)
            self.assertEqual(bot.warcraft_history.report_state(10, code)["sync_status"], "complete")
        finally:
            await bot.close()

    async def test_death_event_failure_marks_partial_and_retries_without_refetching_detail(self):
        bot = Carter(0, 0, {30}, wake_settings=WakeSettings(enabled=False, auto_join=False),
                     warcraft_settings=WarcraftLogsSettings(True, 40, "client", "secret"))
        code = "DEATHPENDING0001"
        raid = {"code": code, "title": "Raid", "startTime": 1_700_000_000_000,
                "fights": [{"id": 1, "name": "Boss", "encounterID": 7,
                            "startTime": 0, "endTime": 10_000, "kill": True}]}
        bot.warcraft_logs = SimpleNamespace(
            report=AsyncMock(return_value=raid),
            fight_breakdown=AsyncMock(return_value=complete_fight_detail()),
            death_events=AsyncMock(side_effect=[
                WarcraftLogsError("temporary_failure"), {"deaths": {"data": []}},
            ]),
            close=AsyncMock(),
        )
        state = bot.runtime(10)
        state.logs_channel_id = 40
        try:
            await bot._load_warcraft_report(state, code)
            self.assertIn(code, state.warcraft_pending_deaths)
            self.assertEqual(bot.warcraft_history.report_state(10, code)["sync_status"], "partial")
            await bot._load_warcraft_report(state, code, raid)
            self.assertNotIn(code, state.warcraft_pending_deaths)
            self.assertEqual(bot.warcraft_logs.fight_breakdown.await_count, 1)
            self.assertEqual(bot.warcraft_logs.death_events.await_count, 2)
            self.assertEqual(bot.warcraft_history.report_state(10, code)["sync_status"], "complete")
        finally:
            await bot.close()

    async def test_partial_fight_payload_remains_pending_until_complete_retry(self):
        bot = Carter(0, 0, {30}, wake_settings=WakeSettings(enabled=False, auto_join=False),
                     warcraft_settings=WarcraftLogsSettings(True, 40, "client", "secret"))
        code = "DETAILPENDING001"
        raid = {"code": code, "title": "Raid", "startTime": 1_700_000_000_000,
                "fights": [{"id": 1, "name": "Boss", "encounterID": 7,
                            "startTime": 0, "endTime": 10_000, "kill": True}]}
        bot.warcraft_logs = SimpleNamespace(
            report=AsyncMock(return_value=raid),
            fight_breakdown=AsyncMock(side_effect=[{}, complete_fight_detail()]),
            death_events=AsyncMock(return_value={"deaths": {"data": []}}),
            close=AsyncMock(),
        )
        state = bot.runtime(10)
        state.logs_channel_id = 40
        try:
            await bot._load_warcraft_report(state, code)
            self.assertIn((code, 1), state.warcraft_pending_details)
            self.assertEqual(bot.warcraft_history.report_state(10, code)["sync_status"], "partial")
            # Simulate a restart: runtime pending markers are gone, while the
            # durable report state and cached partial payload remain.
            state.warcraft_pending_details.clear()
            state.warcraft_seen[code] = -1_000_000_000
            result = await bot._ingest_warcraft_codes(state, [code], monitor=False)
            self.assertEqual(len(result["reports"]), 1)
            self.assertNotIn((code, 1), state.warcraft_pending_details)
            self.assertEqual(bot.warcraft_logs.fight_breakdown.await_count, 2)
            self.assertEqual(bot.warcraft_history.report_state(10, code)["sync_status"], "complete")
        finally:
            await bot.close()

    async def test_partial_refresh_keeps_last_complete_runtime_detail(self):
        bot = Carter(0, 0, {30}, wake_settings=WakeSettings(enabled=False, auto_join=False),
                     warcraft_settings=WarcraftLogsSettings(True, 40, "client", "secret"))
        code = "KEEPCOMPLETE0001"
        raid = {"code": code, "title": "Raid", "startTime": 1_700_000_000_000,
                "fights": [{"id": 1, "name": "Boss", "encounterID": 7,
                            "startTime": 0, "endTime": 10_000, "kill": True}]}
        complete = complete_fight_detail()
        refreshed = complete_fight_detail(name="Player Renamed")
        bot.warcraft_logs = SimpleNamespace(
            report=AsyncMock(return_value=raid),
            fight_breakdown=AsyncMock(side_effect=[complete, {}, refreshed]),
            death_events=AsyncMock(return_value={"deaths": {"data": []}}),
            close=AsyncMock(),
        )
        state = bot.runtime(10)
        state.logs_channel_id = 40
        try:
            await bot._load_warcraft_report(state, code)
            await bot._load_warcraft_report(state, code, raid, refresh_details=True)
            self.assertIs(state.warcraft_breakdowns[(code, 1)], complete)
            self.assertIn((code, 1), state.warcraft_pending_details)
            self.assertEqual(bot.warcraft_history.report_state(10, code)["sync_status"], "partial")
            await bot._load_warcraft_report(state, code, raid)
            self.assertIs(state.warcraft_breakdowns[(code, 1)], refreshed)
            self.assertNotIn((code, 1), state.warcraft_pending_details)
            self.assertEqual(bot.warcraft_history.report_state(10, code)["sync_status"], "complete")
        finally:
            await bot.close()

    async def test_report_loader_caches_every_boss_fight_bundle(self):
        bot = Carter(0, 0, {30}, wake_settings=WakeSettings(enabled=False, auto_join=False),
                     warcraft_settings=WarcraftLogsSettings(True, 40, "client", "secret"))
        code = "A" * 16
        report = {"code": code, "title": "Raid", "startTime": 1700000000000,
                  "fights": [
                      {"id": 1, "name": "Boss", "encounterID": 7,
                       "startTime": 0, "endTime": 1000, "kill": False},
                      {"id": 2, "name": "Boss", "encounterID": 7,
                       "startTime": 2000, "endTime": 4000, "kill": True},
                      {"id": 3, "name": "Trash", "encounterID": 0,
                       "startTime": 5000, "endTime": 6000, "kill": False},
                  ]}

        async def detail(report_code, fight_id):
            return complete_fight_detail(1000, name=f"P{fight_id}")

        bot.warcraft_logs = SimpleNamespace(
            report=AsyncMock(return_value=report),
            fight_breakdown=AsyncMock(side_effect=detail),
            death_events=AsyncMock(return_value={"deaths": {"data": []}}),
            close=AsyncMock(),
        )
        state = bot.runtime(10)
        state.logs_channel_id = 40
        try:
            await bot._load_warcraft_report(state, code)
            self.assertEqual(set(state.warcraft_breakdowns), {(code, 1), (code, 2)})
            self.assertEqual(bot.warcraft_logs.fight_breakdown.await_count, 2)
            await bot._load_warcraft_report(state, code, report)
            self.assertEqual(bot.warcraft_logs.fight_breakdown.await_count, 2)
        finally:
            await bot.close()

    async def test_backfill_keeps_full_history_but_only_five_rich_reports(self):
        bot = Carter(0, 0, {30}, wake_settings=WakeSettings(enabled=False, auto_join=False),
                     warcraft_settings=WarcraftLogsSettings(True, 40, "client", "secret"))
        codes = [f"HOTCACHE{index:08d}" for index in range(6)]

        async def report(code):
            position = codes.index(code)
            return {
                "code": code, "title": code, "startTime": 1_700_000_000_000 + position * 10_000,
                "endTime": 1_700_000_005_000 + position * 10_000,
                "fights": [{
                    "id": 1, "name": "Boss", "encounterID": 7,
                    "startTime": 0, "endTime": 5_000, "kill": True,
                }],
            }

        bot.warcraft_logs = SimpleNamespace(
            report=AsyncMock(side_effect=report),
            fight_breakdown=AsyncMock(return_value={}),
            death_events=AsyncMock(return_value={"deaths": {"data": []}}),
            close=AsyncMock(),
        )
        state = bot.runtime(10)
        state.logs_channel_id = 40
        try:
            result = await bot._ingest_warcraft_codes(state, codes, monitor=False)
            self.assertEqual(len(result["reports"]), 6)
            self.assertEqual(bot.warcraft_history.counts(10)["reports"], 6)
            self.assertEqual(len(state.warcraft_reports), 5)
            hot_codes = {item["code"] for item in state.warcraft_reports}
            self.assertEqual({key[0] for key in state.warcraft_breakdowns}, hot_codes)
            self.assertEqual(set(state.warcraft_death_events), hot_codes)
            self.assertNotIn(codes[0], hot_codes)
        finally:
            await bot.close()

    async def test_failed_history_write_does_not_leak_rich_cache_data(self):
        bot = Carter(0, 0, {30}, wake_settings=WakeSettings(enabled=False, auto_join=False),
                     warcraft_settings=WarcraftLogsSettings(True, 40, "client", "secret"))
        code = "FAILEDWRITE00001"
        raid = {"code": code, "title": "Raid", "startTime": 1_700_000_000_000,
                "fights": [{"id": 1, "name": "Boss", "encounterID": 7,
                            "startTime": 0, "endTime": 5_000, "kill": True}]}
        bot.warcraft_logs = SimpleNamespace(
            report=AsyncMock(return_value=raid),
            fight_breakdown=AsyncMock(return_value={"damageDone": {"data": {}}}),
            death_events=AsyncMock(return_value={"deaths": {"data": []}}),
            close=AsyncMock(),
        )
        bot.warcraft_history = SimpleNamespace(
            upsert_report_bundle=MagicMock(side_effect=WarcraftHistoryError("write_failed")),
        )
        state = bot.runtime(10)
        state.logs_channel_id = 40
        try:
            result = await bot._ingest_warcraft_codes(state, [code], monitor=False)
            self.assertEqual(result["failures"], [(code, "history_storage_failed")])
            self.assertFalse(state.warcraft_reports)
            self.assertNotIn((code, 1), state.warcraft_breakdowns)
            self.assertNotIn(code, state.warcraft_death_events)
        finally:
            await bot.close()

    async def test_voicechannel_updates_both_channels_without_starting_ai(self):
        bot = Carter(10, 20, {30}, wake_settings=WakeSettings(enabled=False, auto_join=False),
                     warcraft_settings=WarcraftLogsSettings(True, 40, "client", "secret"))
        bot.warcraft_logs = SimpleNamespace(close=AsyncMock())
        state = bot.runtime(10)
        state.wake.stop = AsyncMock()
        state.wake.reset = MagicMock()
        state.session.stop = AsyncMock()
        bot._connect_voice = AsyncMock()
        current = SimpleNamespace(disconnect=AsyncMock())
        voice_channel = MagicMock(id=21)
        voice_channel.permissions_for.return_value = SimpleNamespace(
            view_channel=True, connect=True, speak=True,
        )
        logs_channel = MagicMock(id=41)
        logs_channel.permissions_for.return_value = SimpleNamespace(
            view_channel=True, read_message_history=True, send_messages=True,
        )
        interaction = SimpleNamespace(
            guild_id=10,
            guild=SimpleNamespace(me=object(), voice_client=current),
            user=SimpleNamespace(id=30, voice=SimpleNamespace(channel=voice_channel)),
            response=SimpleNamespace(send_message=AsyncMock(), defer=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
        )
        try:
            await bot.set_channels(interaction, voice_channel, logs_channel)
            self.assertEqual(state.channel_id, 21)
            self.assertEqual(state.logs_channel_id, 41)
            self.assertTrue(state.channels_selected)
            bot._connect_voice.assert_awaited_once_with(state, interaction.guild, voice_channel)
            state.session.stop.assert_awaited_once()
            interaction.followup.send.assert_awaited_once()
        finally:
            await bot.close()


if __name__ == "__main__":
    unittest.main()
