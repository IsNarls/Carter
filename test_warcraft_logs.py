import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from warcraft_logs import (
    WarcraftLogsClient, WarcraftLogsError, WarcraftLogsSettings, boss_framework,
    compact_fight_breakdown, dps_ranking, latest_boss_fight, message_debug_dump,
    message_report_codes,
    report_codes, report_fingerprint, report_links,
    summarize_report, voice_context,
)


class WarcraftLogsTests(unittest.TestCase):
    def test_extracts_unique_official_report_links_only(self):
        text = ("https://www.warcraftlogs.com/reports/BWftZjM6zxDQ2bdX "
                "https://warcraftlogs.com/reports/BWftZjM6zxDQ2bdX#fight=3 "
                "https://evil.example/reports/AAAAAAAA")
        self.assertEqual(report_codes(text), ("BWftZjM6zxDQ2bdX",))

    def test_report_links_preserve_query_and_fragment_fight_ids(self):
        text = (
            "https://www.warcraftlogs.com/reports/V8dLCwWZfTX91rYN?fight=6 "
            "https://www.warcraftlogs.com/reports/V8dLCwWZfTX91rYN#fight=9 "
            "https://www.warcraftlogs.com/reports/AAAAAAAAAAAAAAAA"
        )
        self.assertEqual(report_links(text), (
            ("V8dLCwWZfTX91rYN", 6),
            ("V8dLCwWZfTX91rYN", 9),
            ("AAAAAAAAAAAAAAAA", None),
        ))

    def test_extracts_bare_subdomain_and_embed_urls_but_not_lookalikes(self):
        message = SimpleNamespace(
            content=("classic.warcraftlogs.com/reports/AAAAAAAAAAAAAAAA "
                     "https://evilwarcraftlogs.com/reports/BBBBBBBBBBBBBBBB"),
            embeds=[SimpleNamespace(to_dict=lambda: {
                "url": "https://www.warcraftlogs.com/reports/CCCCCCCCCCCCCCCC#fight=last",
                "fields": [{"value": "https://sod.warcraftlogs.com/reports/DDDDDDDDDDDDDDDD"}],
            })],
            attachments=[], stickers=[],
        )
        self.assertEqual(message_report_codes(message), (
            "AAAAAAAAAAAAAAAA", "CCCCCCCCCCCCCCCC", "DDDDDDDDDDDDDDDD",
        ))

    def test_message_debug_dump_is_bounded_and_redacts_common_secrets(self):
        message = SimpleNamespace(
            content="token=private-value https://warcraftlogs.com/reports/AAAAAAAAAAAAAAAA",
            embeds=[], attachments=[],
        )
        dumped = message_debug_dump(message)
        self.assertIn("warcraftlogs.com/reports/AAAAAAAAAAAAAAAA", dumped)
        self.assertNotIn("private-value", dumped)
        self.assertLessEqual(len(dumped), 12000)

    def test_extracts_component_and_forwarded_snapshot_urls(self):
        component = SimpleNamespace(to_dict=lambda: {
            "type": 2, "url": "https://www.warcraftlogs.com/reports/EEEEEEEEEEEEEEEE",
        })
        snapshot = SimpleNamespace(
            content="https://classic.warcraftlogs.com/reports/FFFFFFFFFFFFFFFF",
            embeds=[], attachments=[], components=[],
        )
        message = SimpleNamespace(
            content="", embeds=[], attachments=[], stickers=[],
            components=[component], message_snapshots=[snapshot],
        )
        self.assertEqual(message_report_codes(message), (
            "EEEEEEEEEEEEEEEE", "FFFFFFFFFFFFFFFF",
        ))
        dumped = message_debug_dump(message)
        self.assertIn('"components"', dumped)
        self.assertIn('"snapshots"', dumped)

    def test_summary_is_bounded_and_ignores_trash(self):
        report = {
            "title": "Raid night", "startTime": 1000, "endTime": 121000,
            "zone": {"name": "Test Raid"},
            "fights": [
                {"name": "Trash", "encounterID": 0, "kill": False,
                 "startTime": 0, "endTime": 10000},
                {"name": "Boss", "encounterID": 7, "kill": False,
                 "startTime": 10000, "endTime": 40000},
                {"name": "Boss", "encounterID": 7, "kill": True,
                 "startTime": 50000, "endTime": 110000},
            ],
        }
        summary = summarize_report(report)
        self.assertIn("Boss pulls: 2 · Kills: 1", summary)
        self.assertIn("Boss: 2 pulls, killed, 1:30 logged", summary)
        self.assertNotIn("Trash:", summary)
        self.assertLessEqual(len(summary), 1900)

    def test_invalid_report_is_safe_error(self):
        with self.assertRaisesRegex(WarcraftLogsError, "invalid_report"):
            summarize_report(None)

    def test_latest_fight_and_voice_context_default(self):
        report = {
            "code": "AAAAAAAAAAAAAAAA", "title": "Raid", "zone": {"name": "Zone"},
            "fights": [
                {"id": 1, "name": "Boss", "encounterID": 7, "kill": False,
                 "startTime": 0, "endTime": 30000},
                {"id": 2, "name": "Boss", "encounterID": 7, "kill": True,
                 "startTime": 40000, "endTime": 90000},
            ],
        }
        self.assertEqual(latest_boss_fight(report)["id"], 2)
        context = voice_context(report, {"deaths": {"entries": []}})
        self.assertIn("Default fight", context)
        self.assertIn("fight 2 (Boss)", context)
        self.assertIn('"deaths"', context)
        self.assertLessEqual(len(context), 16000)
        selected = report["fights"][0]
        selected_context = voice_context(
            report, {"summary": {"entries": []}}, detail_fight=selected,
        )
        self.assertIn("specifically to fight 1 (Boss)", selected_context)
        before = report_fingerprint(report)
        report["fights"].append({
            "id": 3, "name": "Next", "encounterID": 8, "kill": False,
            "startTime": 100000, "endTime": 120000, "inProgress": True,
        })
        self.assertNotEqual(report_fingerprint(report), before)

    def test_compact_breakdown_retains_each_module_and_bounds_rows(self):
        breakdown = {
            "damageDone": {"data": {"entries": [
                {"name": f"Player {index}", "total": index, "oversized": "x" * 1000}
                for index in range(30)
            ]}},
            "casts": {"entries": [{"name": "Spell", "uses": 4}]},
            "masterData": {"actors": [{"id": 1, "name": "Player"}],
                           "abilities": [{"gameID": 2, "name": "Spell"}]},
        }
        compact = compact_fight_breakdown(breakdown)
        rows = compact["damageDone"]["lists"]["entries"]
        self.assertEqual(rows["rowCount"], 30)
        self.assertEqual(len(rows["topRows"]), 6)
        self.assertEqual(rows["topRows"][0]["name"], "Player 29")
        self.assertIn("casts", compact)
        self.assertIn("masterData", compact)

    def test_compact_breakdown_understands_aura_and_survivability_shapes(self):
        compact = compact_fight_breakdown({
            "buffs": {"data": {"auras": [{"name": "Buff", "total": 20}]}},
            "survivability": {"data": {"players": [{"name": "Tank"}],
                                           "fights": [{"id": 6}]}},
        })
        self.assertEqual(compact["buffs"]["lists"]["auras"]["rowCount"], 1)
        self.assertEqual(compact["survivability"]["lists"]["players"]["rowCount"], 1)

    def test_dps_ranking_uses_fight_time_and_labels_active_dps_separately(self):
        breakdown = {"damageDone": {"data": {
            "totalTime": 30000,
            "entries": [
                {"id": 1, "name": "Burst", "type": "Mage", "total": 1000,
                 "activeTime": 10000},
                {"id": 2, "name": "Total", "type": "Warrior", "total": 1500,
                 "activeTime": 30000},
                {"id": 3, "name": "Invalid", "total": 99999, "activeTime": 0},
                {"id": 4, "name": "Nonfinite", "total": float("inf"),
                 "activeTime": 1000},
                {"id": 5, "name": "Boolean", "total": True, "activeTime": 1000},
                {"id": 6, "name": "Pet", "type": "Pet", "total": 999999,
                 "activeTime": 30000},
            ],
        }}}
        ranking = dps_ranking(breakdown)
        self.assertEqual([item["name"] for item in ranking], ["Invalid", "Total", "Burst"])
        self.assertEqual(ranking[1]["dps"], 50.0)
        self.assertEqual(ranking[2]["dps"], 33.33)
        self.assertEqual(ranking[2]["active_dps"], 100.0)
        self.assertNotIn("active_dps", ranking[0])
        compact = compact_fight_breakdown(breakdown)
        self.assertEqual(compact["damageDone"]["dpsRanking"][0]["name"], "Invalid")

    def test_boss_framework_combines_attempts_across_reports_with_utc_times(self):
        reports = [
            {"code": "A" * 16, "startTime": 1700000000000, "fights": [
                {"id": 1, "name": "Boss", "encounterID": 7, "startTime": 1000,
                 "endTime": 5000, "kill": False},
            ]},
            {"code": "B" * 16, "startTime": 1700100000000, "fights": [
                {"id": 2, "name": "Boss", "encounterID": 7, "startTime": 2000,
                 "endTime": 6000, "kill": True},
            ]},
        ]
        framework = boss_framework(reports)
        self.assertEqual(len(framework["7"]["attempts"]), 2)
        latest = framework["7"]["attempts"][-1]
        self.assertEqual((latest["report_code"], latest["fight_id"]), ("B" * 16, 2))
        self.assertIn("+00:00", latest["start_utc"])


class _FakeResponse:
    def __init__(self, status, payload=None):
        self.status = status
        self.payload = payload or {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    async def json(self):
        return self.payload


class _FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.closed = False
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


class WarcraftGraphQLRetryTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def client_with_responses(responses):
        client = WarcraftLogsClient(WarcraftLogsSettings(True, 0, "client", "secret"))
        client.token = "cached-token"
        client.token_expires = float("inf")
        client.session = _FakeSession(responses)
        return client

    async def test_graphql_retries_rate_limit_then_succeeds(self):
        expected = {"data": {"ok": True}}
        client = self.client_with_responses([
            _FakeResponse(429), _FakeResponse(200, expected),
        ])
        with patch("warcraft_logs.asyncio.sleep", AsyncMock()) as sleep:
            self.assertEqual(await client._graphql("query", {"code": "A"}), expected)
        self.assertEqual(len(client.session.calls), 2)
        sleep.assert_awaited_once_with(0.25)

    async def test_access_token_retries_temporary_failure(self):
        client = WarcraftLogsClient(WarcraftLogsSettings(True, 0, "client", "secret"))
        client.session = _FakeSession([
            _FakeResponse(503),
            _FakeResponse(200, {"access_token": "token", "expires_in": 3600}),
        ])
        with patch("warcraft_logs.asyncio.sleep", AsyncMock()) as sleep:
            self.assertEqual(await client._access_token(), "token")
        self.assertEqual(len(client.session.calls), 2)
        sleep.assert_awaited_once_with(0.25)

    async def test_graphql_retries_multiple_server_errors_then_succeeds(self):
        expected = {"data": {"ok": True}}
        client = self.client_with_responses([
            _FakeResponse(500), _FakeResponse(503), _FakeResponse(200, expected),
        ])
        with patch("warcraft_logs.asyncio.sleep", AsyncMock()) as sleep:
            self.assertEqual(await client._graphql("query", {}), expected)
        self.assertEqual(len(client.session.calls), 3)
        self.assertEqual([call.args[0] for call in sleep.await_args_list], [0.25, 1.0])

    async def test_graphql_retries_timeout_then_succeeds(self):
        expected = {"data": {"ok": True}}
        client = self.client_with_responses([
            asyncio.TimeoutError(), _FakeResponse(200, expected),
        ])
        with patch("warcraft_logs.asyncio.sleep", AsyncMock()) as sleep:
            self.assertEqual(await client._graphql("query", {}), expected)
        self.assertEqual(len(client.session.calls), 2)
        sleep.assert_awaited_once_with(0.25)

    async def test_graphql_transient_exhaustion_preserves_error_category(self):
        for status, expected_error in ((429, "rate_limited"), (503, "request_failed")):
            with self.subTest(status=status):
                client = self.client_with_responses([_FakeResponse(status) for _ in range(3)])
                with patch("warcraft_logs.asyncio.sleep", AsyncMock()) as sleep:
                    with self.assertRaisesRegex(WarcraftLogsError, expected_error):
                        await client._graphql("query", {})
                self.assertEqual(len(client.session.calls), 3)
                self.assertEqual(sleep.await_count, 2)

    async def test_graphql_does_not_retry_non_transient_client_errors(self):
        client = self.client_with_responses([_FakeResponse(400), _FakeResponse(200)])
        with patch("warcraft_logs.asyncio.sleep", AsyncMock()) as sleep:
            with self.assertRaisesRegex(WarcraftLogsError, "request_failed"):
                await client._graphql("query", {})
        self.assertEqual(len(client.session.calls), 1)
        sleep.assert_not_awaited()

    async def test_graphql_preserves_partial_data_but_rejects_error_only_payload(self):
        partial = {"data": {"reportData": {"report": {"damageDone": None}}},
                   "errors": [{"message": "one field unavailable"}]}
        client = self.client_with_responses([_FakeResponse(200, partial)])
        self.assertEqual(await client._graphql("query", {}), partial)
        client = self.client_with_responses([
            _FakeResponse(200, {"errors": [{"message": "query failed"}]}),
        ])
        with self.assertRaisesRegex(WarcraftLogsError, "query_failed"):
            await client._graphql("query", {})


class WarcraftReplayClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_death_events_follows_every_page(self):
        client = WarcraftLogsClient(WarcraftLogsSettings(True, 0, "client", "secret"))
        client._graphql = AsyncMock(side_effect=[
            {"data": {"reportData": {"report": {
                "masterData": {"actors": [{"id": 1, "name": "Player"}]},
                "deaths": {"data": [{"timestamp": 100, "targetID": 1}],
                           "nextPageTimestamp": 200},
            }}}},
            {"data": {"reportData": {"report": {
                "masterData": {"actors": [{"id": 1, "name": "Player"}]},
                "deaths": {"data": [{"timestamp": 250, "targetID": 1}],
                           "nextPageTimestamp": None},
            }}}},
        ])
        result = await client.death_events("V8dLCwWZfTX91rYN", [6])
        self.assertEqual(len(result["deaths"]["data"]), 2)
        self.assertEqual(client._graphql.await_count, 2)
        self.assertIsNone(client._graphql.await_args_list[0].args[1]["startTime"])
        self.assertEqual(client._graphql.await_args_list[1].args[1]["startTime"], 200.0)

    async def test_replay_snapshot_uses_bounded_window_and_actor_positions(self):
        report = {
            "fights": [{
                "id": 6, "name": "Boss", "startTime": 1000, "endTime": 121000,
                "friendlyPlayers": [10], "maps": [{"id": 1}],
                "boundingBox": {"minX": 0, "maxX": 1000, "minY": 0, "maxY": 1000},
            }],
            "masterData": {"actors": [
                {"id": 10, "name": "Player", "type": "Player", "subType": "Mage"},
            ]},
            "events": {"data": [
                {"timestamp": 60000, "resourceActor": 1, "sourceID": 10,
                 "targetID": 20, "x": 100, "y": 200, "mapID": 1},
            ], "nextPageTimestamp": None},
        }
        client = WarcraftLogsClient(WarcraftLogsSettings(True, 0, "client", "secret"))
        client._graphql = AsyncMock(return_value={
            "data": {"reportData": {"report": report}},
        })
        snapshot = await client.replay_snapshot(
            "V8dLCwWZfTX91rYN", 6, 60000, 1000, 121000,
        )
        self.assertEqual(snapshot["friendly_positioned"], 1)
        client._graphql.assert_awaited_once()
        variables = client._graphql.await_args.args[1]
        self.assertEqual(variables["startTime"], 56000)
        self.assertEqual(variables["endTime"], 61000)

    async def test_replay_snapshot_fails_safely_when_positions_are_missing(self):
        report = {
            "fights": [{
                "id": 6, "name": "Boss", "startTime": 1000, "endTime": 121000,
                "friendlyPlayers": [10],
            }],
            "masterData": {"actors": []},
            "events": {"data": [], "nextPageTimestamp": None},
        }
        client = WarcraftLogsClient(WarcraftLogsSettings(True, 0, "client", "secret"))
        client._graphql = AsyncMock(return_value={
            "data": {"reportData": {"report": report}},
        })
        with self.assertRaisesRegex(WarcraftLogsError, "positions_unavailable"):
            await client.replay_snapshot("V8dLCwWZfTX91rYN", 6, 60000, 1000, 121000)
        self.assertEqual(client._graphql.await_count, 2)

    async def test_replay_snapshot_never_renders_a_stalled_partial_page(self):
        report = {
            "fights": [{
                "id": 6, "name": "Boss", "startTime": 1000, "endTime": 121000,
                "friendlyPlayers": [10],
                "boundingBox": {"minX": 0, "maxX": 1000, "minY": 0, "maxY": 1000},
            }],
            "masterData": {"actors": [
                {"id": 10, "name": "Player", "type": "Player", "subType": "Mage"},
            ]},
        }

        async def stalled_page(query, variables):
            return {"data": {"reportData": {"report": {
                **report,
                "events": {"data": [
                    {"timestamp": variables["startTime"], "resourceActor": 1,
                     "sourceID": 10, "targetID": 20, "x": 100, "y": 200, "mapID": 1},
                ], "nextPageTimestamp": variables["startTime"]},
            }}}}

        client = WarcraftLogsClient(WarcraftLogsSettings(True, 0, "client", "secret"))
        client._graphql = AsyncMock(side_effect=stalled_page)
        with self.assertRaisesRegex(WarcraftLogsError, "positions_unavailable"):
            await client.replay_snapshot("V8dLCwWZfTX91rYN", 6, 60000, 1000, 121000)
        self.assertEqual(client._graphql.await_count, 3)

    async def test_replay_snapshot_follows_pages_to_the_requested_time(self):
        metadata = {
            "fights": [{
                "id": 6, "name": "Boss", "startTime": 1000, "endTime": 121000,
                "friendlyPlayers": [10],
                "boundingBox": {"minX": 0, "maxX": 1000, "minY": 0, "maxY": 1000},
            }],
            "masterData": {"actors": [
                {"id": 10, "name": "Player", "type": "Player", "subType": "Mage"},
            ]},
        }

        async def page(query, variables):
            first = variables["startTime"] == 56000
            return {"data": {"reportData": {"report": {
                **metadata,
                "events": {
                    "data": [{
                        "timestamp": 58000 if first else 60500,
                        "resourceActor": 1, "sourceID": 10, "targetID": 20,
                        "x": 100 if first else 900, "y": 200, "mapID": 1,
                    }],
                    "nextPageTimestamp": 59000 if first else None,
                },
            }}}}

        client = WarcraftLogsClient(WarcraftLogsSettings(True, 0, "client", "secret"))
        client._graphql = AsyncMock(side_effect=page)
        snapshot = await client.replay_snapshot(
            "V8dLCwWZfTX91rYN", 6, 60000, 1000, 121000,
        )
        self.assertEqual(snapshot["positions"][0]["x"], 900.0)
        self.assertEqual(client._graphql.await_count, 2)


if __name__ == "__main__":
    unittest.main()
