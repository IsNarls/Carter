"""Offline routing, identity, ambiguity, and evidence-consistency tests."""

import base64
import json
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from analysis_client import AnalysisClientError
from raid_integration import GuildAnalysisState, RaidAnalysis, analysis_tool_declaration


class RaidIntegrationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        fights = [
            {"report_id": "ReportABC123", "fight_id": 4, "attempt": 1,
             "name": "Boss", "kill": False, "duration_seconds": 100,
             "start_time_ms": 1000},
            {"report_id": "ReportABC123", "fight_id": 8, "attempt": 2,
             "name": "Boss", "kill": True, "duration_seconds": 120,
             "start_time_ms": 2000},
        ]
        self.client = SimpleNamespace(
            reports=AsyncMock(return_value={"reports": [{"report_id": "ReportABC123"}]}),
            all_fights=AsyncMock(return_value={"fights": fights}),
            fights=AsyncMock(return_value={"report_id": "ReportABC123", "fights": fights}),
            summary=AsyncMock(return_value={
                "name": "Boss", "kill": True, "duration_seconds": 120,
                "raid_dps": 3000, "raid_hps": 900, "deaths": 1, "players": 2,
            }),
            ranking=AsyncMock(return_value={"players": [
                {"guid": "p1", "name": "Alex", "dps": 2000, "damage": 240000,
                 "rank": 1, "hps": 0, "healing": 0, "healing_rank": 2},
                {"guid": "p2", "name": "Blair", "dps": 1000, "damage": 120000,
                 "rank": 2, "hps": 900, "healing": 108000, "healing_rank": 1},
            ]}),
            players=AsyncMock(return_value={"players": [
                {"guid": "p1", "name": "Alex"},
            ]}),
            player=AsyncMock(return_value={"player": {
                "guid": "p1", "name": "Alex", "dps": 2000, "damage": 240000,
                "rank": 1, "hps": 0, "healing": 0, "healing_rank": 2,
            }}),
            deaths=AsyncMock(return_value={"deaths": [
                {"death_id": "fight-8:p1:42000", "player_name": "Alex",
                 "death_number": 1, "time_seconds": 42.0},
            ]}),
            death_answer=AsyncMock(),
            live_attempts=AsyncMock(return_value={"attempts": [
                {"attempt": 3, "name": "Boss", "state": "completed"},
            ]}),
            archived_deaths=AsyncMock(), archived_death_answer=AsyncMock(),
            archived_summary=AsyncMock(), archived_ranking=AsyncMock(),
            archived_players=AsyncMock(), archived_player=AsyncMock(),
            live_deaths=AsyncMock(),
            live_status=AsyncMock(return_value={"configured": True}),
            import_report=AsyncMock(return_value={"job_id": "job-1", "report_id": "ReportABC123"}),
        )
        svg = b'<svg xmlns="http://www.w3.org/2000/svg"/>'
        self.client.death_answer.return_value = {
            "report_id": "ReportABC123", "fight_id": 8,
            "death_id": "fight-8:p1:42000", "answer": {"text": "Alex died to Fire."},
            "analysis": {"player": {"id": "p1", "name": "Alex"},
                         "time_seconds": 42.0,
                         "killing_blow": {"ability": "Fire", "provenance": "reported"},
                         "primary_cause": {"ability": "Fire", "confidence": "high"}},
            "image": {"status": "available", "mime_type": "image/svg+xml",
                      "encoding": "base64", "data": base64.b64encode(svg).decode(),
                      "alt_text": "Alex at death"},
        }
        self.raid = RaidAnalysis(self.client)
        self.state = GuildAnalysisState()

    async def test_latest_report_and_fight_are_selected_once(self):
        answer = await self.raid.summary(self.state, "latest pull summary")
        self.assertIn("Boss", answer.text)
        self.assertEqual((self.state.report_id, self.state.fight_id), ("ReportABC123", 8))
        self.client.summary.assert_awaited_once_with("ReportABC123", 8)

    async def test_latest_fight_uses_absolute_time_across_reports(self):
        self.client.all_fights.return_value = {"fights": [
            {"report_id": "DownloadedLast", "fight_id": 99, "attempt": 20,
             "name": "Old Boss", "kill": True, "start_time_ms": 1000},
            {"report_id": "ChronologicallyNew", "fight_id": 2, "attempt": 1,
             "name": "New Boss", "kill": False, "start_time_ms": 9000},
        ]}
        await self.raid.summary(self.state, "latest fight summary")
        self.client.summary.assert_awaited_once_with("ChronologicallyNew", 2)
        self.assertEqual(
            (self.state.report_id, self.state.fight_id),
            ("ChronologicallyNew", 2),
        )

    async def test_which_bosses_question_uses_local_index_instead_of_plain_ai(self):
        self.client.all_fights.return_value = {"fights": [
            {"report_id": "OlderReport1", "fight_id": 3, "name": "Old Boss",
             "kill": True, "start_time_ms": 1000},
            {"report_id": "NewestReport", "fight_id": 7, "name": "New Boss",
             "kill": False, "start_time_ms": 9000},
            {"report_id": "NewestReport", "fight_id": 8, "name": "New Boss",
             "kill": True, "start_time_ms": 9500},
        ]}

        answer = await self.raid.answer_question(
            self.state, "Which bosses are in the data?", 30,
        )

        self.assertEqual(answer.tool_context["intent"], "bosses")
        self.assertLess(answer.text.index("New Boss"), answer.text.index("Old Boss"))
        self.assertIn("2 pull(s), 1 kill(s)", answer.text)

    async def test_damage_ranking_requests_dps_not_total_damage(self):
        answer = await self.raid.ranking(self.state, "dps", 2)
        self.client.ranking.assert_awaited_once_with("ReportABC123", 8, "dps", 2)
        self.assertIn("2,000 DPS", answer.text)
        self.assertIn("240,000 total", answer.text)

    async def test_misspelled_named_mythic_fight_selects_matching_boss_not_latest(self):
        self.client.all_fights.return_value = {"fights": [
            {"report_id": "OlderReport", "fight_id": 6, "attempt": 1,
             "name": "Kith'ix", "difficulty": 5, "kill": True,
             "start_time_ms": 1000},
            {"report_id": "NewestReport", "fight_id": 26, "attempt": 2,
             "name": "Ula'tek", "difficulty": 4, "kill": True,
             "start_time_ms": 9000},
        ]}

        answer = await self.raid.answer_question(
            self.state, "can you post the top DPS for the Mythic Kitix fight", 30,
        )

        self.client.ranking.assert_awaited_once_with("OlderReport", 6, "dps", 10)
        self.assertIn("Kith'ix (Mythic)", answer.text)
        self.assertNotIn("Ula'tek", answer.text)

    async def test_data_on_short_unique_boss_name_routes_to_matching_difficulty(self):
        self.client.all_fights.return_value = {"fights": [
            {"report_id": "NymReport", "fight_id": 15, "attempt": 1,
             "name": "Nymrissa Wavecaller", "difficulty": 5, "kill": False,
             "start_time_ms": 1000},
            {"report_id": "LatestReport", "fight_id": 26, "attempt": 2,
             "name": "Ula'tek", "difficulty": 4, "kill": True,
             "start_time_ms": 9000},
        ]}
        self.client.summary.return_value = {
            "name": "Nymrissa Wavecaller", "kill": False, "duration_seconds": 240,
            "raid_dps": 3000, "raid_hps": 900, "deaths": 5, "players": 20,
        }

        answer = await self.raid.answer_question(
            self.state, "do you have data on Mythic Nymrissa", 30,
        )

        self.client.summary.assert_awaited_once_with("NymReport", 15)
        self.assertIn("Nymrissa Wavecaller", answer.text)
        self.assertIn("Mythic", answer.text)

    async def test_requested_difficulty_must_exist_for_named_boss(self):
        self.client.all_fights.return_value = {"fights": [
            {"report_id": "HeroicOnly", "fight_id": 6, "name": "Kith'ix",
             "difficulty": 4, "kill": True, "start_time_ms": 1000},
        ]}
        with self.assertRaisesRegex(AnalysisClientError, "fight_not_found"):
            await self.raid.answer_question(
                self.state, "top DPS for the Mythic Kitix fight", 30,
            )
        self.client.ranking.assert_not_awaited()

    async def test_unmatched_explicit_boss_does_not_fall_back_to_latest(self):
        with self.assertRaisesRegex(AnalysisClientError, "fight_not_found"):
            await self.raid.answer_question(
                self.state, "top DPS for the Imaginaryboss fight", 30,
            )
        self.client.ranking.assert_not_awaited()

    async def test_why_did_i_die_uses_identity_and_one_combined_answer_call(self):
        self.state.identities["30"] = "Alex"
        module = SimpleNamespace(svg2png=lambda **kwargs:
                                 kwargs["write_to"].write(b"\x89PNG\r\n\x1a\nfixture"))
        with patch.dict(sys.modules, {"cairosvg": module}):
            answer = await self.raid.answer_question(self.state, "Why did I die?", 30)
        self.client.deaths.assert_awaited_once_with("ReportABC123", 8, player_name="Alex")
        self.client.death_answer.assert_awaited_once_with(
            "ReportABC123", 8, "fight-8:p1:42000",
        )
        self.assertEqual(answer.tool_context["text"], "Alex died to Fire.")
        self.assertIsNotNone(answer.image)
        self.assertNotIn("data", answer.tool_context)

    async def test_multiple_deaths_require_explicit_selection(self):
        self.client.deaths.return_value = {"deaths": [
            {"death_id": "one", "death_number": 1, "time_seconds": 10},
            {"death_id": "two", "death_number": 2, "time_seconds": 20},
        ]}
        with self.assertRaisesRegex(AnalysisClientError, "ambiguous_death") as caught:
            await self.raid.death(self.state, "Alex")
        self.assertEqual(len(caught.exception.details["candidates"]), 2)
        self.client.death_answer.assert_not_awaited()

    async def test_image_adapter_failure_keeps_deterministic_death_text(self):
        with patch("raid_integration.render_answer_image",
                   side_effect=AnalysisClientError("image_render_failed")):
            answer = await self.raid.death(self.state, "Alex")
        self.assertIn("Alex died to Fire.", answer.text)
        self.assertIn("image_render_failed", answer.text)
        self.assertIsNone(answer.image)

    async def test_last_wipe_death_timeline_is_chronological_and_linked(self):
        self.client.deaths.return_value = {"deaths": [
            {"death_id": "late", "player_name": "Blair", "death_number": 1,
             "time_seconds": 30, "killing_blow": {"ability": "Blast"}},
            {"death_id": "early", "player_name": "Alex", "death_number": 1,
             "time_seconds": 10, "killing_blow": {"ability": "Fire"}},
        ]}
        answer = await self.raid.answer_question(
            self.state, "Give me the death timeline for the last wipe", 30,
        )
        self.client.deaths.assert_awaited_once_with("ReportABC123", 4)
        self.assertLess(answer.text.index("10.00s"), answer.text.index("30.00s"))
        self.assertIn(
            "https://www.warcraftlogs.com/reports/ReportABC123#fight=4", answer.text,
        )
        self.assertEqual(answer.tool_context["intent"], "death_timeline")

    async def test_explicit_fight_number_uses_most_recent_report_not_saved_report(self):
        self.state.report_id = "CurrentReport"
        self.state.fight_id = 23
        self.client.all_fights.return_value = {"fights": [
            {"report_id": "DuplicateReport", "fight_id": 22, "name": "Older Copy",
             "kill": False, "start_time_ms": 500},
            {"report_id": "DuplicateReport", "fight_id": 30, "name": "Newest Pull",
             "start_time_ms": 3000},
            {"report_id": "CurrentReport", "fight_id": 22, "name": "Nymrissa Wavecaller",
             "kill": False, "start_time_ms": 1000},
            {"report_id": "CurrentReport", "fight_id": 23, "name": "Nymrissa Wavecaller",
             "start_time_ms": 2000},
        ]}

        answer = await self.raid.answer_question(
            self.state, "List Deaths on Fight 22", 30,
        )

        self.client.deaths.assert_awaited_once_with("DuplicateReport", 22)
        self.assertIn("Wipe 1/1", answer.text)
        self.assertNotIn("fight 22", answer.text)
        self.assertEqual(self.state.report_id, "DuplicateReport")
        self.assertEqual(self.state.fight_id, 22)

    async def test_result_slot_selects_chronological_wipe_for_top_dps(self):
        self.state.report_id = "OldSavedReport"
        self.state.fight_id = 99
        self.client.all_fights.return_value = {"fights": [
            {"report_id": "NewestReport", "fight_id": 10, "name": "Boss A",
             "kill": False, "start_time_ms": 1000},
            {"report_id": "NewestReport", "fight_id": 11, "name": "Boss A",
             "kill": False, "start_time_ms": 2000},
            {"report_id": "NewestReport", "fight_id": 12, "name": "Boss A",
             "kill": False, "start_time_ms": 3000},
            {"report_id": "NewestReport", "fight_id": 13, "name": "Boss A",
             "kill": False, "start_time_ms": 4000},
            {"report_id": "OldSavedReport", "fight_id": 99, "name": "Old Boss",
             "kill": False, "start_time_ms": 500},
        ]}

        answer = await self.raid.answer_question(
            self.state, "review Wipe 2/4 top dps", 30,
        )

        self.client.ranking.assert_awaited_once_with("NewestReport", 11, "dps", 10)
        self.assertIn("Wipe 2/4", answer.text)
        self.assertNotIn("fight 11", answer.text)

    async def test_named_boss_wipe_slots_exclude_other_bosses(self):
        fights = [
            {"report_id": "RaidReport", "fight_id": number,
             "name": "Earlier Boss", "difficulty": 5, "kill": False,
             "start_time_ms": number * 100}
            for number in range(1, 5)
        ] + [
            {"report_id": "RaidReport", "fight_id": 18 + number,
             "name": "Nymrissa Wavecaller", "difficulty": 5, "kill": False,
             "start_time_ms": 1000 + number * 100}
            for number in range(1, 6)
        ]
        self.client.all_fights.return_value = {"fights": fights}

        first = await self.raid.resolve_source(
            self.state, "List deaths from wipe 1 of Nymrissa",
        )
        fifth = await self.raid.resolve_source(
            self.state, "Review the death on wipe 5 of Nymrissa for player: Droppvoker",
        )

        self.assertEqual(first.fight_id, 19)
        self.assertIn("Wipe 1/5", first.label)
        self.assertEqual(fifth.fight_id, 23)
        self.assertIn("Wipe 5/5", fifth.label)
        self.assertNotIn("Wipe 9/9", fifth.label)

    async def test_wipe_slots_span_reports_oldest_to_newest_and_dedupe_mirrors(self):
        fights = []
        for number in range(1, 6):
            fights.append({
                "report_id": "TwoWeeksAgo", "fight_id": number, "name": "Nymrissa",
                "difficulty": 5, "kill": False, "start_time_ms": number * 10000,
                "duration_seconds": 100 + number,
            })
            fights.append({
                "report_id": "MirroredUpload", "fight_id": number + 20,
                "name": "Nymrissa", "difficulty": 5, "kill": False,
                "start_time_ms": number * 10000 + 200, "duration_seconds": 100 + number,
            })
        for number in range(1, 6):
            fights.append({
                "report_id": "ThisWeek", "fight_id": number + 40, "name": "Nymrissa",
                "difficulty": 5, "kill": False, "start_time_ms": 100000 + number * 10000,
                "duration_seconds": 200 + number,
            })
        self.client.all_fights.return_value = {"fights": fights}

        oldest = await self.raid.resolve_source(self.state, "wipe 1/10 of Nymrissa")
        newest = await self.raid.resolve_source(self.state, "wipe 10/10 of Nymrissa")

        self.assertIn("Wipe 1/10", oldest.label)
        self.assertEqual(newest.report_id, "ThisWeek")
        self.assertEqual(newest.fight_id, 45)
        self.assertIn("Wipe 10/10", newest.label)

    async def test_fight_list_uses_result_slots_instead_of_raw_fight_ids(self):
        self.client.all_fights.return_value = {"fights": [
            {"report_id": "OneReport", "fight_id": 10, "name": "Boss",
             "kill": False, "start_time_ms": 1000, "duration_seconds": 30},
            {"report_id": "OneReport", "fight_id": 11, "name": "Boss",
             "kill": False, "start_time_ms": 2000, "duration_seconds": 40},
            {"report_id": "OneReport", "fight_id": 12, "name": "Boss",
             "kill": True, "start_time_ms": 3000, "duration_seconds": 50},
        ]}

        answer = await self.raid.fights(self.state)

        self.assertIn("Wipe 1/2", answer.text)
        self.assertIn("Wipe 2/2", answer.text)
        self.assertIn("Kill 1/1", answer.text)
        self.assertNotIn("fight 10", answer.text)

    async def test_unicode_player_deaths_filter_and_attach_latest_position_map(self):
        self.client.deaths.return_value = {"deaths": [
            {"death_id": "early", "player_name": "Cæylrythë", "death_number": 1,
             "time_seconds": 12, "killing_blow": {"ability": "Venom"}},
            {"death_id": "latest", "player_name": "Cæylrythë", "death_number": 2,
             "time_seconds": 48, "killing_blow": {"ability": "Fangs"}},
        ]}
        module = SimpleNamespace(svg2png=lambda **kwargs:
                                 kwargs["write_to"].write(b"\x89PNG\r\n\x1a\nfixture"))

        with patch.dict(sys.modules, {"cairosvg": module}):
            answer = await self.raid.answer_question(
                self.state, "show deaths for player: Cæylrythë", 30,
            )

        self.client.deaths.assert_awaited_once_with(
            "ReportABC123", 8, player_name="Cæylrythë",
        )
        self.client.death_answer.assert_awaited_once_with(
            "ReportABC123", 8, "latest",
        )
        self.assertIn("Death timeline for Cæylrythë", answer.text)
        self.assertIn("Attached 2D position map", answer.text)
        self.assertIsNotNone(answer.image)
        self.assertEqual(answer.tool_context["player_name"], "Cæylrythë")
        self.assertNotIn('"data"', json.dumps(answer.tool_context, ensure_ascii=False))

    async def test_tool_player_death_timeline_preserves_unicode_name(self):
        module = SimpleNamespace(svg2png=lambda **kwargs:
                                 kwargs["write_to"].write(b"\x89PNG\r\n\x1a\nfixture"))
        with patch.dict(sys.modules, {"cairosvg": module}):
            answer = await self.raid.execute_tool(
                self.state,
                {"intent": "death_timeline", "player_name": "Cæylrythë"},
                30,
            )
        self.client.deaths.assert_awaited_once_with(
            "ReportABC123", 8, player_name="Cæylrythë",
        )
        self.assertEqual(answer.tool_context["player_name"], "Cæylrythë")

    async def test_last_live_pull_uses_archived_attempt_not_current(self):
        self.state.source = "live"
        self.client.archived_summary.return_value = {
            "name": "Boss", "result": "WIPE", "duration_seconds": 80,
            "raid_dps": 1000, "raid_hps": 500, "deaths": 4, "players": 20,
        }
        answer = await self.raid.summary(self.state, "summarize the last pull")
        self.client.archived_summary.assert_awaited_once_with(3)
        self.assertIn("archived live attempt 3", answer.text)

    async def test_last_live_wipe_filters_out_newer_kill(self):
        self.state.source = "live"
        self.client.live_attempts.return_value = {"attempts": [
            {"attempt": 2, "name": "Boss", "state": "completed", "result": "WIPE"},
            {"attempt": 3, "name": "Boss", "state": "completed", "result": "KILL"},
        ]}
        self.client.archived_summary.return_value = {
            "name": "Boss", "result": "WIPE", "duration_seconds": 80,
            "raid_dps": 1000, "raid_hps": 500, "deaths": 4, "players": 20,
        }
        await self.raid.summary(self.state, "summarize the last wipe")
        self.client.archived_summary.assert_awaited_once_with(2)

    async def test_named_boss_and_previous_selection_do_not_silently_change_target(self):
        self.client.all_fights.return_value = {"fights": [
            {"report_id": "ReportABC123", "fight_id": 1, "attempt": 1,
             "name": "The Twin Fangs", "kill": False},
            {"report_id": "ReportABC123", "fight_id": 2, "attempt": 2,
             "name": "Other Boss", "kill": False},
            {"report_id": "ReportABC123", "fight_id": 3, "attempt": 3,
             "name": "The Twin Fangs", "kill": True},
            {"report_id": "ReportABC123", "fight_id": 4, "attempt": 4,
             "name": "Other Boss", "kill": True},
            {"report_id": "ReportABC123", "fight_id": 5, "attempt": 5,
             "name": "Kill Only", "kill": True},
        ]}
        source = await self.raid.resolve_source(
            self.state, "death timeline for the last Twin Fangs wipe",
        )
        self.assertEqual(source.fight_id, 1)
        self.state.fight_id = 4
        source = await self.raid.resolve_source(self.state, "the previous pull")
        self.assertEqual(source.fight_id, 3)
        with self.assertRaisesRegex(AnalysisClientError, "fight_not_found"):
            await self.raid.resolve_source(self.state, "latest Kill Only wipe")

    async def test_tool_schema_is_bounded_and_requires_intent(self):
        declaration = analysis_tool_declaration()
        self.assertEqual(declaration["name"], "query_raid_analysis")
        self.assertEqual(declaration["parameters"]["required"], ["intent"])
        self.assertNotIn("timer", declaration["parameters"]["properties"])
        self.assertIn(
            "death_timeline",
            declaration["parameters"]["properties"]["intent"]["enum"],
        )
        self.assertIn(
            "bosses", declaration["parameters"]["properties"]["intent"]["enum"],
        )

    async def test_failed_tool_selection_does_not_mutate_guild_state(self):
        self.state.source = "stored"
        self.state.report_id = "ReportABC123"
        self.state.fight_id = 8
        with self.assertRaisesRegex(AnalysisClientError, "unsupported_analysis_intent"):
            await self.raid.execute_tool(
                self.state, {"source": "live", "intent": "unsupported"}, 30,
            )
        self.assertEqual(
            (self.state.source, self.state.report_id, self.state.fight_id),
            ("stored", "ReportABC123", 8),
        )
