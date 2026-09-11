import importlib.util
from pathlib import Path
import unittest

SPEC = importlib.util.spec_from_file_location(
    "capture_ab", Path(__file__).resolve().parents[1] / "scripts/capture_ab.py")
capture_ab = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(capture_ab)


class ReportParsingTests(unittest.TestCase):
    def test_only_telemetry_lines_are_parsed(self):
        lines = [
            "Started tab-s9-usb-display.service",
            "Capture format: video/x-raw(memory:DMABuf), width=(int)2960",
            '{"encoded_frames": 10, "capture_fps": 118.4, "encoded_fps": 118.2}',
            "{not json but has capture_fps in it}",
            '{"status": "something else without the counter"}',
            '{"encoded_frames": 20, "capture_fps": 59.9, "encoded_fps": 59.8}',
        ]
        parsed = capture_ab.reports(lines)
        self.assertEqual([r["capture_fps"] for r in parsed], [118.4, 59.9])

    def test_median_ignores_missing_and_non_numeric_values(self):
        self.assertEqual(capture_ab.median([60.0, None, 120.0, "n/a"]), 90.0)
        self.assertIsNone(capture_ab.median([None, None]))
        self.assertIsNone(capture_ab.median([]))
        # A booleans-are-ints accident would silently skew a frame rate.
        self.assertEqual(capture_ab.median([59.5]), 59.5)

    def test_warmup_is_at_least_one_telemetry_window(self):
        # Telemetry is emitted every five seconds; a shorter warm-up would let a
        # cold first window into the measurement.
        self.assertGreaterEqual(capture_ab.WARMUP_SECONDS, 5.0)


if __name__ == "__main__":
    unittest.main()
