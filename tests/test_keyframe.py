"""A client that asks for a keyframe gets one even on a quiet desktop."""
from pathlib import Path
import sys
import unittest
from unittest.mock import MagicMock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

import host as host_module  # noqa: E402


class FakeGLib:
    def __init__(self):
        self.timeouts = []

    def timeout_add(self, ms, func, *args):
        self.timeouts.append((ms, func, args))
        return len(self.timeouts)

    def idle_add(self, func, *args):
        self.timeouts.append((0, func, args))
        return len(self.timeouts)

    def run_due(self):
        due, self.timeouts = self.timeouts, []
        for _, func, args in due:
            func(*args)


def bare_host():
    value = host_module.Host.__new__(host_module.Host)
    value.closing = False
    value.pipeline = MagicMock()
    # No real encoder here (GStreamer is not initialised in the tests): the
    # force-key-unit event is skipped and only the replay logic is exercised.
    value.pipeline.get_by_name.side_effect = lambda name: None if name == 'encoder' else MagicMock()
    value.native = MagicMock()
    value.native_last_encoded = 2
    value.native_last_seq = 40
    value.capture_wall = None
    value.nudge_pending = True          # keep idle_nudge out of the picture
    value.clients = {object()}
    value.keyframes = 0
    value.replayed = 0
    value.replay_last_frame = lambda: setattr(value, 'replayed', value.replayed + 1)
    return value


class EnsureKeyframeTests(unittest.TestCase):
    def setUp(self):
        self.glib = FakeGLib()
        self.old = host_module.GLib
        host_module.GLib = self.glib

    def tearDown(self):
        host_module.GLib = self.old

    def test_a_request_on_an_idle_desktop_replays_at_once_and_checks_later(self):
        value = bare_host()
        value.request_keyframe()
        self.assertEqual(value.replayed, 1)
        self.assertEqual([ms for ms, _, _ in self.glib.timeouts], [value.KEYFRAME_CHECK_MS])

    def test_a_frame_just_captured_does_not_excuse_a_missing_keyframe(self):
        """The frame in flight comes out as a P-frame; the check feeds the
        last picture again until an IDR has actually gone out."""
        value = bare_host()
        value.capture_wall = host_module.time.monotonic()      # a frame looked imminent
        value.request_keyframe()
        self.assertEqual(value.replayed, 0)
        self.glib.run_due()                                   # 250 ms later: still no IDR
        self.assertEqual(value.replayed, 1)
        self.glib.run_due()
        self.assertEqual(value.replayed, 2)
        value.keyframes = 1                                   # the replay came out as an IDR
        self.glib.run_due()
        self.assertEqual(value.replayed, 2)
        self.assertEqual(self.glib.timeouts, [])

    def test_the_check_gives_up_after_a_few_tries(self):
        value = bare_host()
        value.capture_wall = host_module.time.monotonic()
        value.request_keyframe()
        for _ in range(10):
            self.glib.run_due()
        self.assertEqual(value.replayed, value.KEYFRAME_CHECKS_MAX)

    def test_no_client_or_no_native_path_means_nothing_to_do(self):
        value = bare_host()
        value.clients = set()
        value.ensure_keyframe(0, 0)
        self.assertEqual(value.replayed, 0)
        value = bare_host()
        value.native = None
        value.ensure_keyframe(0, 0)
        self.assertEqual(value.replayed, 0)
        self.assertEqual(self.glib.timeouts, [])


if __name__ == '__main__':
    unittest.main()
