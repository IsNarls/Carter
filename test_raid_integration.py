"""Offline routing, identity, ambiguity, and evidence-consistency tests."""

import base64
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from analysis_client import AnalysisClientError
from raid_integration import GuildAnalysisState, RaidAnalysis, analysis_tool_declaration


class RaidIntegrationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.client = SimpleNamespace(
            reports=AsyncMock(return_value={"reports": [{"report_id": "ReportABC123"}]}),
            fights=AsyncMock(return_value={"report_id": "ReportABC123", "fights": [
                {"fight_id": 4, "attempt": 1, "name": "Boss", "kill": False,
                 "duration_seconds": 100},
                {"fight_id": 8, "attempt": 2, "name": "Boss", "kill": True,
                 "duration_seconds": 120},
            ]}),
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

    async def test_damage_ranking_requests_dps_not_total_damage(self):
        answer = await self.raid.ranking(self.state, "dps", 2)
        self.client.ranking.assert_awaited_once_with("ReportABC123", 8, "dps", 2)
        self.assertIn("2,000 DPS", answer.text)
        self.assertIn("240,000 total", answer.text)

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
        self.client.fights.return_value = {"report_id": "ReportABC123", "fights": [
            {"fight_id": 1, "attempt": 1, "name": "The Twin Fangs", "kill": False},
            {"fight_id": 2, "attempt": 2, "name": "Other Boss", "kill": False},
            {"fight_id": 3, "attempt": 3, "name": "The Twin Fangs", "kill": True},
            {"fight_id": 4, "attempt": 4, "name": "Other Boss", "kill": True},
            {"fight_id": 5, "attempt": 5, "name": "Kill Only", "kill": True},
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
