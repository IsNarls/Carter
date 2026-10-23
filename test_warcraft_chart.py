"""Dependency-free historical DPS chart tests."""
import struct
import unittest

from warcraft_chart import ChartDataError, TrendPoint, render_dps_trend_png


def png_dimensions(payload):
    if payload[:8] != b"\x89PNG\r\n\x1a\n" or payload[12:16] != b"IHDR":
        raise AssertionError("not a PNG")
    return struct.unpack(">II", payload[16:24])


class WarcraftChartTests(unittest.TestCase):
    def test_renders_deterministic_png_with_requested_dimensions(self):
        points = [
            ("2026-10-01", 1_820_000),
            ("2026-10-08", 1_910_000),
            ("2026-10-15", 2_040_000),
        ]
        first = render_dps_trend_png(points, title="Shorted DPS", width=800, height=400)
        second = render_dps_trend_png(points, title="Shorted DPS", width=800, height=400)
        self.assertEqual(first[:8], b"\x89PNG\r\n\x1a\n")
        self.assertEqual(png_dimensions(first), (800, 400))
        self.assertEqual(first, second)

    def test_rejects_empty_excessive_nonfinite_and_oversized_inputs(self):
        invalid = (
            [],
            [(str(index), index) for index in range(26)],
            [("2026-10-01", float("nan"))],
            [("2026-10-01", -1)],
            [("x" * 33, 1)],
        )
        for points in invalid:
            with self.subTest(points=len(points)):
                with self.assertRaises(ChartDataError):
                    render_dps_trend_png(points)
        with self.assertRaises(ChartDataError):
            render_dps_trend_png([("2026-10-01", 1)], width=200, height=200)
        with self.assertRaises(ChartDataError):
            render_dps_trend_png([("2026-10-01", 1)], title="x" * 121)

    def test_single_point_trend_renders(self):
        payload = render_dps_trend_png(
            [TrendPoint("2026-10-20", 1_250_000)], title="One raid night",
        )
        self.assertEqual(png_dimensions(payload), (900, 450))
        self.assertGreater(len(payload), 1000)

    def test_uniform_trend_renders_without_zero_range(self):
        payload = render_dps_trend_png([
            {"date": "2026-10-01", "dps": 500_000},
            {"date": "2026-10-08", "dps": 500_000},
            {"date": "2026-10-15", "dps": 500_000},
        ])
        self.assertEqual(png_dimensions(payload), (900, 450))
        self.assertGreater(len(payload), 1000)


if __name__ == "__main__":
    unittest.main()
