import json
import os
import sqlite3
import stat
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from warcraft_history import (
    AmbiguousCharacterError,
    AmbiguousEncounterError,
    HistoryStore,
    WarcraftHistoryError,
)


def timestamp(text):
    return int(datetime.fromisoformat(text).replace(tzinfo=timezone.utc).timestamp() * 1000)


def player(actor_id=1, name="Shorted", realm="Realm One", region="US", role="dps",
           class_name="Mage", spec="Frost"):
    return {
        "id": actor_id, "guid": 100000 + actor_id, "name": name,
        "server": realm, "region": region, "type": class_name,
        "icon": f"{class_name}-{spec}", "specs": [{"spec": spec, "count": 1}],
        "_test_role": role,
    }


def breakdown(players, duration, damage=None, healing=None, death_ids=()):
    grouped = {"dps": [], "healers": [], "tanks": []}
    for item in players:
        copy = dict(item)
        role = copy.pop("_test_role", "dps")
        grouped[{"dps": "dps", "healer": "healers", "tank": "tanks"}.get(role, role)].append(copy)
    damage = damage or {}
    healing = healing or {}
    by_id = {item["id"]: item for item in players}
    damage_rows = [{
        "id": actor_id, "guid": by_id[actor_id]["guid"], "name": by_id[actor_id]["name"],
        "type": by_id[actor_id]["type"], "icon": by_id[actor_id]["icon"],
        "total": total, "activeTime": duration / 2,
    } for actor_id, total in damage.items()]
    healing_rows = [{
        "id": actor_id, "guid": by_id[actor_id]["guid"], "name": by_id[actor_id]["name"],
        "type": by_id[actor_id]["type"], "icon": by_id[actor_id]["icon"],
        "total": total, "overheal": total / 4, "activeTime": duration,
    } for actor_id, total in healing.items()]
    return {
        "playerDetails": {"data": {"playerDetails": grouped}},
        "damageDone": {"data": {"totalTime": duration, "entries": damage_rows}},
        "healing": {"data": {"totalTime": duration, "entries": healing_rows}},
        "deaths": {"data": {"entries": [
            {"id": actor_id, "name": by_id[actor_id]["name"]} for actor_id in death_ids
        ]}},
    }


def fight(fight_id, encounter_id, name, start, duration, *, kill=True, difficulty=4,
          in_progress=False):
    return {
        "id": fight_id, "encounterID": encounter_id, "name": name,
        "startTime": start, "endTime": start + duration, "kill": kill,
        "inProgress": in_progress, "difficulty": difficulty, "size": 20,
        "averageItemLevel": 300.5,
    }


def report(code, start, fights, title="Raid"):
    return {
        "code": code, "title": title, "zone": {"name": "Test Raid"},
        "startTime": start,
        "endTime": start + max((item["endTime"] for item in fights), default=0),
        "fights": fights,
    }


class HistoryStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "history.sqlite3"
        self.store = HistoryStore(self.path, timezone_name="UTC", cutoff_hour=6)

    def tearDown(self):
        self.temp.cleanup()

    def test_schema_and_database_are_owner_only(self):
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        connection = sqlite3.connect(self.path)
        try:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 2)
        finally:
            connection.close()
        with self.assertRaisesRegex(WarcraftHistoryError, "raid_night_policy_changed"):
            HistoryStore(self.path, timezone_name="UTC", cutoff_hour=5)

    def test_schema_v1_aliases_migrate_without_losing_history(self):
        member = player()
        raid = report("MIGRATION0000001", timestamp("2026-01-01T20:00:00"), [
            fight(1, 100, "Boss", 0, 10000),
        ])
        self.store.upsert_report_bundle(1, raid, {
            1: breakdown([member], 10000, {1: 1000}),
        })
        connection = sqlite3.connect(self.path)
        try:
            connection.executescript(
                """
                BEGIN IMMEDIATE;
                ALTER TABLE character_aliases RENAME TO character_aliases_v2;
                DROP INDEX aliases_character_idx;
                DROP INDEX aliases_lookup_idx;
                CREATE TABLE character_aliases (
                    guild_id INTEGER NOT NULL,
                    character_id INTEGER NOT NULL,
                    name_key TEXT NOT NULL,
                    realm_key TEXT NOT NULL,
                    region_key TEXT NOT NULL,
                    display_name TEXT NOT NULL,
                    display_realm TEXT,
                    display_region TEXT,
                    first_seen_ms INTEGER NOT NULL,
                    last_seen_ms INTEGER NOT NULL,
                    PRIMARY KEY (guild_id,name_key,realm_key,region_key),
                    FOREIGN KEY (guild_id,character_id)
                        REFERENCES characters(guild_id,character_id) ON DELETE CASCADE
                );
                INSERT INTO character_aliases SELECT * FROM character_aliases_v2;
                DROP TABLE character_aliases_v2;
                CREATE INDEX aliases_character_idx
                    ON character_aliases(guild_id,character_id);
                PRAGMA user_version=1;
                COMMIT;
                """
            )
        finally:
            connection.close()

        migrated = HistoryStore(self.path, timezone_name="UTC", cutoff_hour=6)
        self.assertEqual(migrated.resolve_character(1, "Shorted")["current_name"], "Shorted")
        self.assertEqual(migrated.player_nights(1, "Shorted")["nights"][0]["dps"], 100.0)
        connection = sqlite3.connect(self.path)
        try:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 2)
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM character_aliases").fetchone()[0], 1,
            )
        finally:
            connection.close()

    def test_atomic_idempotent_upsert_and_mutable_fight_replacement(self):
        code = "A" * 16
        member = player()
        pull = fight(1, 101, "First Boss", 0, 10000, kill=False, in_progress=True)
        raid = report(code, timestamp("2026-01-01T20:00:00"), [pull])
        first = breakdown([member], 10000, {1: 1000})
        first["damageDone"]["data"]["entries"].append({
            "id": 99, "name": "Mage Pet", "type": "Pet", "total": 999999,
            "activeTime": 10000,
        })
        self.store.upsert_report_bundle(1, raid, {1: first})
        self.store.upsert_report_bundle(1, raid, {(code, 1): first})
        self.assertEqual(self.store.counts(1), {
            "reports": 1, "encounters": 1, "performances": 1,
            "characters": 1, "raid_nights": 1,
        })

        completed = dict(pull, kill=True, inProgress=False)
        updated = report(code, raid["startTime"], [completed])
        self.store.upsert_report_bundle(
            1, updated, {1: breakdown([member], 10000, {1: 2500})},
        )
        result = self.store.player_nights(1, "Shorted", nights=5)
        self.assertEqual(result["nights"][0]["dps"], 250.0)
        self.assertEqual(self.store.counts(1)["performances"], 1)
        # A later partial API response must not erase the last complete metrics.
        self.store.upsert_report_bundle(1, updated, {
            1: {"damageDone": None, "healing": None, "playerDetails": None},
        })
        preserved = self.store.player_nights(1, "Shorted", nights=5)
        self.assertEqual(preserved["nights"][0]["dps"], 250.0)
        self.assertEqual(self.store.counts(1)["performances"], 1)

    def test_raid_night_cutoff_and_duration_weighted_dps(self):
        member = player()
        first_start = timestamp("2026-01-02T23:00:00")
        first_report = report("B" * 16, first_start, [
            fight(1, 201, "Alpha", 0, 10000),
            fight(2, 202, "Beta", 2 * 60 * 60 * 1000, 30000),
        ])
        self.store.upsert_report_bundle(1, first_report, {
            1: breakdown([member], 10000, {1: 1000}),
            2: breakdown([member], 30000, {1: 6000}),
        })
        # The 01:00 UTC pull stays on the prior raid date with a 06:00 cutoff.
        result = self.store.player_nights(1, "shorted", nights=5)
        self.assertEqual(result["nights_returned"], 1)
        self.assertEqual(result["nights"][0]["raid_night"], "2026-01-02")
        self.assertEqual(result["nights"][0]["eligible_encounters"], 2)
        self.assertEqual(result["nights"][0]["dps"], 175.0)
        self.assertNotEqual(result["nights"][0]["dps"], (100 + 200) / 2)

        second_report = report("C" * 16, timestamp("2026-01-03T20:00:00"), [
            fight(1, 201, "Alpha", 0, 10000),
        ])
        # Per-report actor IDs can change; the WCL GUID is the stable identity.
        changed_id = dict(member, id=44)
        self.store.upsert_report_bundle(1, second_report, {
            1: breakdown([changed_id], 10000, {44: 3000}),
        })
        result = self.store.player_nights(1, "Shorted", nights=5)
        self.assertEqual([item["dps"] for item in result["nights"]], [175.0, 300.0])
        self.assertEqual(result["trend"]["direction"], "increased")
        self.assertAlmostEqual(result["trend"]["percent_change"], 71.428571, places=4)

    def test_compact_summary_fallback_preserves_historical_dps(self):
        member = player()
        raid = report("K" * 16, timestamp("2026-01-10T20:00:00"), [
            fight(1, 250, "Cached Boss", 0, 10000),
        ])
        compact = {"summary": {"data": {
            "totalTime": 10000,
            "playerDetails": {"dps": [member], "healers": [], "tanks": []},
            "damageDone": [{"id": 1, "guid": member["guid"], "name": "Shorted",
                            "type": "Mage", "total": 1250}],
            "healingDone": [], "deathEvents": [],
        }}}
        self.store.upsert_report_bundle(1, raid, {1: compact})
        result = self.store.player_nights(1, "Shorted")
        self.assertEqual(result["nights"][0]["dps"], 125.0)

    def test_same_name_different_realms_requires_disambiguation(self):
        players = [
            player(1, realm="Realm One"),
            player(2, realm="Realm Two"),
        ]
        raid = report("D" * 16, timestamp("2026-02-01T20:00:00"), [
            fight(1, 301, "Boss", 0, 20000),
        ])
        self.store.upsert_report_bundle(1, raid, {
            1: breakdown(players, 20000, {1: 2000, 2: 4000}),
        })
        with self.assertRaises(AmbiguousCharacterError) as raised:
            self.store.player_nights(1, "Shorted")
        self.assertEqual(len(raised.exception.choices), 2)
        result = self.store.player_nights(1, "Shorted", realm="realm-two", region="us")
        self.assertEqual(result["nights"][0]["dps"], 200.0)

    def test_canonical_guid_preserves_history_across_rename_and_transfer(self):
        original = player(1, name="Before", realm="Realm One")
        renamed = dict(original, name="After", server="Realm Two")
        for code, started, member, total in (
            ("GUIDHISTORY00001", "2026-02-01T20:00:00", original, 1000),
            ("GUIDHISTORY00002", "2026-02-08T20:00:00", renamed, 2000),
        ):
            raid = report(code, timestamp(started), [
                fight(1, 302, "Boss", 0, 10000),
            ])
            self.store.upsert_report_bundle(1, raid, {
                1: breakdown([member], 10000, {member["id"]: total}),
            })
        before = self.store.resolve_character(1, "Before", realm="Realm One", region="US")
        after = self.store.resolve_character(1, "After", realm="Realm Two", region="US")
        self.assertEqual(before["character_id"], after["character_id"])
        self.assertEqual(
            [night["dps"] for night in self.store.player_nights(1, "After")["nights"]],
            [100.0, 200.0],
        )
        self.assertEqual(self.store.counts(1)["characters"], 1)

    def test_distinct_guids_sharing_an_alias_are_never_merged(self):
        first = player(1, name="Reused", realm="Same Realm")
        second = dict(first, id=2, guid=222222)
        for code, member, total in (
            ("SHAREDALIAS00001", first, 1000),
            ("SHAREDALIAS00002", second, 3000),
        ):
            raid = report(code, timestamp("2026-02-10T20:00:00"), [
                fight(1, 303, "Boss", 0, 10000),
            ])
            self.store.upsert_report_bundle(1, raid, {
                1: breakdown([member], 10000, {member["id"]: total}),
            })
        self.assertEqual(self.store.counts(1)["characters"], 2)
        with self.assertRaises(AmbiguousCharacterError):
            self.store.resolve_character(
                1, "Reused", realm="Same Realm", region="US",
            )
        connection = sqlite3.connect(self.path)
        try:
            guids = {
                row[0] for row in connection.execute(
                    "SELECT wcl_canonical_id FROM characters WHERE guild_id=1",
                )
            }
            aliases = connection.execute(
                """SELECT COUNT(*) FROM character_aliases
                   WHERE guild_id=1 AND name_key='reused'""",
            ).fetchone()[0]
        finally:
            connection.close()
        self.assertEqual(guids, {first["guid"], second["guid"]})
        self.assertEqual(aliases, 2)

        # Re-scans remain idempotent and do not let the newer GUID take ownership
        # of the older character's history.
        self.store.upsert_report_bundle(1, raid, {
            1: breakdown([second], 10000, {second["id"]: 3000}),
        })
        self.assertEqual(self.store.counts(1)["characters"], 2)

    def test_missing_fight_detail_is_unavailable_not_player_absence(self):
        member = player()
        raid = report("MISSINGDETAIL001", timestamp("2026-02-11T20:00:00"), [
            fight(1, 304, "Available", 0, 10000),
            fight(2, 305, "Unavailable", 20000, 10000),
        ])
        stored = self.store.upsert_report_bundle(1, raid, {
            1: breakdown([member], 10000, {1: 1000}),
        })
        self.assertEqual(stored["sync_status"], "partial")
        result = self.store.player_nights(1, "Shorted")["nights"][0]
        self.assertEqual(result["pulls"], 2)
        self.assertEqual(result["eligible_encounters"], 1)
        self.assertEqual(result["unavailable_encounters"], 1)
        self.assertEqual(result["absent_encounters"], 0)
        self.assertEqual(result["data_unavailable_encounters"], 1)

    def test_transient_missing_death_payload_preserves_known_count(self):
        member = player()
        raid = report("DEATHRETRY000001", timestamp("2026-02-12T20:00:00"), [
            fight(1, 306, "Boss", 0, 10000),
        ])
        detail = breakdown([member], 10000, {1: 1000})
        self.store.upsert_report_bundle(1, raid, {1: detail}, {
            "deaths": {"data": [{"type": "death", "fight": 1, "targetID": 1}]},
        })
        without_deaths = dict(detail)
        without_deaths.pop("deaths")
        self.store.upsert_report_bundle(
            1, raid, {1: without_deaths}, None, sync_complete=False,
        )
        result = self.store.player_nights(1, "Shorted")["nights"][0]
        self.assertEqual(result["deaths"], 1)
        self.assertEqual(self.store.report_state(1, raid["code"])["sync_status"], "partial")

    def test_guild_isolation_even_for_same_report_code(self):
        code = "E" * 16
        raid = report(code, timestamp("2026-03-01T20:00:00"), [
            fight(1, 401, "Boss", 0, 10000),
        ])
        member = player()
        self.store.upsert_report_bundle(1, raid, {1: breakdown([member], 10000, {1: 1000})})
        self.store.upsert_report_bundle(2, raid, {1: breakdown([member], 10000, {1: 9000})})
        self.assertEqual(self.store.player_nights(1, "Shorted")["nights"][0]["dps"], 100.0)
        self.assertEqual(self.store.player_nights(2, "Shorted")["nights"][0]["dps"], 900.0)
        self.assertEqual(self.store.counts(1)["reports"], 1)
        self.assertEqual(self.store.counts(2)["reports"], 1)

    def test_latest_night_groups_repeated_pulls_under_distinct_bosses(self):
        member = player()
        raid = report("F" * 16, timestamp("2026-04-01T20:00:00"), [
            fight(1, 501, "Progress Boss", 0, 10000, kill=False),
            fight(2, 501, "Progress Boss", 20000, 20000, kill=True),
            fight(3, 502, "Second Boss", 50000, 10000, kill=True),
        ])
        self.store.upsert_report_bundle(1, raid, {
            1: breakdown([member], 10000, {1: 1000}),
            2: breakdown([member], 20000, {1: 6000}),
            3: breakdown([member], 10000, {1: 2000}),
        })
        result = self.store.player_raid_night(1, "Shorted", bosses=5, result_filter="all")
        self.assertEqual(len(result["bosses"]), 2)
        self.assertEqual(len(result["bosses"][0]["pull_details"]), 2)
        self.assertEqual(result["bosses"][0]["dps"], 7000 * 1000 / 30000)
        self.assertEqual(result["strongest"]["boss_name"], "Progress Boss")
        kills_only = self.store.player_raid_night(1, "Shorted", result_filter="kills")
        self.assertEqual(len(kills_only["bosses"][0]["pull_details"]), 1)

    def test_boss_history_matches_encounter_and_difficulty_not_name(self):
        member = player()
        reports = [
            ("G" * 16, "2026-05-01T20:00:00", 4, 1000),
            ("H" * 16, "2026-05-08T20:00:00", 4, 2000),
            ("I" * 16, "2026-05-15T20:00:00", 5, 9000),
        ]
        for code, started, difficulty, total in reports:
            raid = report(code, timestamp(started), [
                fight(1, 601, "Same Name", 0, 10000, difficulty=difficulty),
            ])
            self.store.upsert_report_bundle(1, raid, {
                1: breakdown([member], 10000, {1: total}),
            })
        result = self.store.player_boss_history(
            1, "Shorted", 601, difficulty=4, nights=5,
        )
        self.assertEqual([item["dps"] for item in result["nights"]], [100.0, 200.0])
        self.assertEqual(result["difficulty"], 4)
        with self.assertRaises(AmbiguousEncounterError):
            self.store.player_boss_history(1, "Shorted", 601, difficulty=None)
        resolved = self.store.resolve_encounter(1, "same name", difficulty=4)
        self.assertEqual((resolved["encounter_id"], resolved["difficulty"]), (601, 4))
        with self.assertRaises(AmbiguousEncounterError):
            self.store.resolve_encounter(1, "Same")

    def test_deaths_recent_reports_counts_and_question_context(self):
        member = player()
        code = "J" * 16
        raid = report(code, timestamp("2026-06-01T20:00:00"), [
            fight(7, 701, "Final Boss", 0, 10000),
        ], title="June Raid")
        deaths = {
            "masterData": {"actors": [{"id": 1, "name": "Shorted", "type": "Player"}]},
            "deaths": {"data": [
                {"type": "death", "fight": 7, "targetID": 1, "timestamp": 5000},
                {"type": "death", "fight": 7, "targetID": 1, "timestamp": 8000},
            ]},
        }
        self.store.upsert_report_bundle(
            1, raid, {7: breakdown([member], 10000, {1: 1500}, {1: 500})}, deaths,
        )
        comparison = self.store.player_raid_night(1, "Shorted")
        pull = comparison["bosses"][0]["pull_details"][0]
        self.assertEqual(pull["deaths"], 2)
        self.assertEqual(pull["hps"], 50.0)
        recent = self.store.recent_reports(1)
        self.assertEqual(recent[0]["report_code"], code)
        self.assertEqual((recent[0]["encounters"], recent[0]["players"]), (1, 1))
        self.assertEqual(self.store.report_state(1, code)["sync_status"], "complete")
        self.assertIsNone(self.store.report_state(1, "Z" * 16))
        context = self.store.question_context(1, "Show me Shorted's DPS trend over the last 5 raids")
        self.assertIn("WARCRAFT LOGS HISTORICAL DATA", context)
        self.assertIn('"mode":"player_nights"', context)
        prose = self.store.question_context(
            1, "How has Shorted's DPS changed over our last five raids?",
        )
        self.assertIn('"mode":"player_nights"', prose)
        healing_context = self.store.question_context(
            1, "Show me Shorted's HPS trend over the last 5 raids",
        )
        self.assertIn('"requested_metric":"hps"', healing_context)
        self.assertIn('"metric":"hps"', healing_context)
        self.assertIsNone(self.store.question_context(1, "Who died on the last pull?"))
        bounded = self.store.compact_context(
            {"mode": "player_nights", "nights": [{"notes": "x" * 1000}]},
            max_chars=500,
        )
        self.assertLessEqual(len(bounded), 500)
        json.loads(bounded.split("\n", 1)[1])

    def test_five_night_query_and_three_of_five_player_coverage(self):
        shorted = player()
        other = player(2, name="Someoneelse", realm="Realm Three")
        for index in range(5):
            code = f"NIGHT{index:011d}"
            started = timestamp(f"2026-07-{index + 1:02d}T20:00:00")
            raid = report(code, started, [
                fight(1, 801, "Coverage Boss", 0, 10000),
            ])
            self.store.upsert_report_bundle(3, raid, {
                1: breakdown([shorted], 10000, {1: 1000 + index * 100}),
            })
            participant = shorted if index in {0, 2, 4} else other
            self.store.upsert_report_bundle(4, raid, {
                1: breakdown([participant], 10000, {participant["id"]: 1000}),
            })
        complete = self.store.player_nights(3, "Shorted", nights=5)
        self.assertEqual(complete["nights_returned"], 5)
        self.assertEqual(len(complete["nights"]), 5)
        partial = self.store.player_nights(4, "Shorted", nights=5)
        self.assertEqual(partial["nights_requested"], 5)
        self.assertEqual(partial["nights_returned"], 3)
        self.assertEqual(len(partial["nights"]), 3)

    def test_coverage_counts_pulls_where_player_is_absent(self):
        shorted = player()
        other = player(2, name="Someoneelse", realm="Realm Three")
        raid = report("COVERAGE00000001", timestamp("2026-08-01T20:00:00"), [
            fight(1, 901, "First", 0, 10000),
            fight(2, 902, "Absent Boss", 20000, 20000),
            fight(3, 903, "Third", 50000, 10000),
        ])
        self.store.upsert_report_bundle(5, raid, {
            1: breakdown([shorted], 10000, {1: 1000}),
            2: breakdown([other], 20000, {2: 4000}),
            3: breakdown([shorted], 10000, {1: 3000}),
        })
        history = self.store.player_nights(5, "Shorted")
        night = history["nights"][0]
        self.assertEqual(night["pulls"], 3)
        self.assertEqual(night["eligible_encounters"], 2)
        self.assertEqual(night["unavailable_encounters"], 1)
        self.assertEqual(night["absent_encounters"], 1)
        self.assertEqual(night["dps"], 200.0)
        comparison = self.store.player_raid_night(5, "Shorted", bosses=5)
        absent = next(item for item in comparison["bosses"] if item["boss_name"] == "Absent Boss")
        self.assertEqual(absent["eligible_encounters"], 0)
        self.assertEqual(absent["absent_encounters"], 1)
        self.assertIsNone(absent["pull_details"][0]["dps"])
        boss_history = self.store.player_boss_history(
            5, "Shorted", 902, difficulty=4, nights=5,
        )
        self.assertEqual(boss_history["nights_returned"], 1)
        self.assertIsNone(boss_history["nights"][0]["dps"])
        self.assertEqual(boss_history["nights"][0]["absent_encounters"], 1)


if __name__ == "__main__":
    unittest.main()
