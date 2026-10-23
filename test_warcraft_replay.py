import struct
import unittest
import zlib

from warcraft_replay import (
    ReplayDataError, build_replay_snapshot, format_fight_timestamp,
    parse_fight_timestamp, render_replay_png, replay_caption, replay_url,
)


def sample_report():
    return {
        "fights": [{
            "id": 6,
            "name": "Test Boss",
            "startTime": 1000,
            "endTime": 121000,
            "friendlyPlayers": [10, 11],
            "maps": [{"id": 42}],
            "boundingBox": {"minX": 0, "maxX": 1000, "minY": 0, "maxY": 500},
        }],
        "masterData": {"actors": [
            {"id": 10, "name": "Mage One", "type": "Player", "subType": "Mage"},
            {"id": 11, "name": "Tank Two", "type": "Player", "subType": "Warrior"},
            {"id": 20, "name": "Test Boss", "type": "NPC", "subType": "Boss"},
        ]},
    }


class ReplayTimestampTests(unittest.TestCase):
    def test_parses_contextual_relative_timestamps(self):
        self.assertEqual(parse_fight_timestamp("What happened at 4:42?"), 282000)
        self.assertEqual(parse_fight_timestamp("Show 1:02.5 into the fight"), 62500)
        self.assertEqual(parse_fight_timestamp("At exactly 1:02:03.004"), 3723004)
        self.assertEqual(parse_fight_timestamp("timestamp: 0:45"), 45000)
        self.assertEqual(format_fight_timestamp(62500), "1:02.500")

    def test_rejects_wall_clocks_context_free_and_ambiguous_times(self):
        self.assertIsNone(parse_fight_timestamp("Logged 2026-10-20T15:36:59 UTC"))
        self.assertIsNone(parse_fight_timestamp("The score says 4:42"))
        self.assertIsNone(parse_fight_timestamp("Compare at 1:10 and at 2:20"))
        self.assertIsNone(parse_fight_timestamp("Look at 12:99"))

    def test_accepts_official_replay_position_parameter(self):
        prompt = (
            "https://www.warcraftlogs.com/reports/V8dLCwWZfTX91rYN"
            "#fight=6&view=replay&position=60000"
        )
        self.assertEqual(parse_fight_timestamp(prompt), 60000)
        self.assertEqual(
            replay_url("V8dLCwWZfTX91rYN", 6, 60000),
            prompt,
        )
        self.assertIsNone(parse_fight_timestamp(
            "https://evilwarcraftlogs.com/reports/V8dLCwWZfTX91rYN"
            "#fight=6&view=replay&position=60000",
        ))


class ReplaySnapshotTests(unittest.TestCase):
    def test_maps_resource_owner_and_keeps_latest_position(self):
        events = [
            {"timestamp": 58000, "resourceActor": 1, "sourceID": 10, "targetID": 20,
             "x": 100, "y": 100, "mapID": 42, "hitPoints": 90, "maxHitPoints": 100},
            {"timestamp": 60000, "resourceActor": 1, "sourceID": 10, "targetID": 20,
             "x": 200, "y": 150, "mapID": 42, "hitPoints": 80, "maxHitPoints": 100},
            {"timestamp": 60500, "resourceActor": 2, "sourceID": 20, "targetID": 11,
             "x": 800, "y": 350, "mapID": 42, "hitPoints": 100, "maxHitPoints": 100},
            {"timestamp": 61000, "resourceActor": 2, "sourceID": 10, "targetID": 20,
             "x": 500, "y": 250, "mapID": 42, "hitPoints": 1000, "maxHitPoints": 1000},
        ]
        snapshot = build_replay_snapshot(sample_report(), 6, 60000, events)
        actors = {item["actor_id"]: item for item in snapshot["positions"]}
        self.assertEqual((actors[10]["x"], actors[10]["y"]), (200.0, 150.0))
        self.assertEqual((actors[11]["x"], actors[11]["y"]), (800.0, 350.0))
        self.assertTrue(actors[20]["is_boss"])
        self.assertEqual(snapshot["friendly_positioned"], 2)
        self.assertEqual(snapshot["friendly_total"], 2)

    def test_out_of_range_timestamp_is_rejected(self):
        with self.assertRaisesRegex(ReplayDataError, "timestamp_out_of_range"):
            build_replay_snapshot(sample_report(), 6, 120001, [])

    def test_png_is_valid_fixed_rgb_and_contains_no_actor_names(self):
        snapshot = build_replay_snapshot(sample_report(), 6, 60000, [
            {"timestamp": 60000, "resourceActor": 1, "sourceID": 10, "targetID": 20,
             "x": 200, "y": 150, "mapID": 42, "hitPoints": 80, "maxHitPoints": 100},
            {"timestamp": 60000, "resourceActor": 2, "sourceID": 20, "targetID": 11,
             "x": 800, "y": 350, "mapID": 42, "hitPoints": 100, "maxHitPoints": 100},
        ])
        png = render_replay_png(snapshot)
        self.assertTrue(png.startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertEqual(struct.unpack(">II", png[16:24]), (960, 640))
        self.assertNotIn(b"Mage One", png)
        position, compressed = 8, b""
        while position < len(png):
            length = struct.unpack(">I", png[position:position + 4])[0]
            kind = png[position + 4:position + 8]
            data = png[position + 8:position + 8 + length]
            if kind == b"IDAT":
                compressed += data
            position += 12 + length
        raw = zlib.decompress(compressed)
        self.assertEqual(len(raw), 640 * (1 + 960 * 3))

    def test_caption_is_bounded_and_links_exact_replay(self):
        snapshot = build_replay_snapshot(sample_report(), 6, 60000, [
            {"timestamp": 60000, "resourceActor": 1, "sourceID": 10, "targetID": 20,
             "x": 200, "y": 150, "mapID": 42},
        ])
        url = replay_url("V8dLCwWZfTX91rYN", 6, 60000)
        caption = replay_caption(snapshot, url)
        self.assertIn("Carter positional snapshot", caption)
        self.assertIn(url, caption)
        self.assertLessEqual(len(caption), 1900)


if __name__ == "__main__":
    unittest.main()
