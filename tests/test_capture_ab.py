import fcntl
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest import mock

SPEC = importlib.util.spec_from_file_location(
    "capture_ab", Path(__file__).resolve().parents[1] / "scripts/capture_ab.py")
capture_ab = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(capture_ab)


class ReportParsingTests(unittest.TestCase):
    def test_native_fallback_is_detected_from_actual_path_not_warning_wording(self):
        self.assertEqual(capture_ab.actual_capture_modes(
            [{'capture_memory': 'va'}], ['Capture authorized; starting encoder. Memory path: va']), ['va'])
        self.assertEqual(capture_ab.actual_capture_modes(
            [{'capture_memory': 'va'}], ['Capture authorized; starting encoder. Memory path: native (ring)']),
            ['native', 'va'])
        self.assertEqual(capture_ab.actual_capture_modes([], []), [])

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
        self.assertEqual(capture_ab.median([59.5]), 59.5)

    def test_median_rejects_booleans(self):
        # bool is a subclass of int, so a stray flag would be averaged in as
        # 1 fps and silently halve a reported rate.
        self.assertEqual(capture_ab.median([True, 59.5]), 59.5)
        self.assertIsNone(capture_ab.median([True, False]))

    def test_warmup_is_at_least_one_telemetry_window(self):
        # Telemetry is emitted every five seconds; a shorter warm-up would let a
        # cold first window into the measurement.
        self.assertGreaterEqual(capture_ab.WARMUP_SECONDS, 5.0)


class RestartSerialisationTests(unittest.TestCase):
    """The second capture path must not start while the first still holds the lock.

    `systemctl is-active` reports inactive while a unit is still deactivating,
    and host.py exits immediately if the single-instance lock is taken. Polling
    the unit alone would silently reduce the A/B to one measured path.
    """

    def test_a_held_lock_means_the_host_is_not_gone_yet(self):
        with tempfile.TemporaryDirectory() as directory:
            lock = Path(directory) / "host.lock"
            lock.write_text("")
            with mock.patch.object(capture_ab, "LOCK", lock), \
                 mock.patch.object(capture_ab, "unit_active", return_value=False):
                with lock.open("w") as held:
                    fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    self.assertFalse(capture_ab.host_gone())
                self.assertTrue(capture_ab.host_gone())

    def test_an_active_unit_is_never_reported_gone(self):
        with mock.patch.object(capture_ab, "unit_active", return_value=True):
            self.assertFalse(capture_ab.host_gone())

    def test_a_missing_lock_file_means_gone(self):
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.object(capture_ab, "LOCK", Path(directory) / "absent.lock"), \
                 mock.patch.object(capture_ab, "unit_active", return_value=False):
                self.assertTrue(capture_ab.host_gone())


if __name__ == "__main__":
    unittest.main()
