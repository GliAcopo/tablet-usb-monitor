from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from gestures import GestureFilter, GESTURE, HOLD, IDLE, PASS  # noqa: E402


class FakeLoop:
    """A clock and one-shot timers the test advances by hand."""

    def __init__(self):
        self.time = 10.0
        self.timers = {}
        self.next_id = 1
        self.cancelled = []

    def now(self):
        return self.time

    def schedule(self, ms, callback):
        handle = self.next_id
        self.next_id += 1
        self.timers[handle] = (self.time + ms / 1000.0, callback)
        return handle

    def cancel(self, handle):
        self.cancelled.append(handle)
        if handle not in self.timers:
            raise AssertionError(f"timer {handle} cancelled twice or after firing")
        del self.timers[handle]

    def advance(self, ms):
        self.time += ms / 1000.0
        for handle, (due, callback) in sorted(self.timers.items(), key=lambda item: item[1][0]):
            if due <= self.time:
                del self.timers[handle]
                callback()


def touch(action, slot, x=0.5, y=0.5):
    return {"type": "touch", "action": action, "slot": slot, "x": x, "y": y, "pressure": 0.5}


DOWN, UP, MOTION = 0, 1, 2


class GestureFilterTests(unittest.TestCase):
    def setUp(self):
        self.loop = FakeLoop()
        self.delivered = []
        self.acted = []
        self.filter = GestureFilter(self.delivered.append,
                                    lambda fingers, direction: self.acted.append((fingers, direction)),
                                    schedule=self.loop.schedule, cancel=self.loop.cancel,
                                    now=self.loop.now, hold_ms=120, threshold=0.07)

    def send(self, *messages):
        for message in messages:
            self.filter.handle(message)

    def swipe(self, slots, dx, dy=0.0, steps=4, spacing_ms=8):
        for step in range(1, steps + 1):
            for i, slot in enumerate(slots):
                self.send(touch(MOTION, slot, 0.3 + 0.05 * i + dx * step / steps,
                                0.5 + dy * step / steps))
            self.loop.advance(spacing_ms)

    def land(self, slots, spacing_ms=10):
        for i, slot in enumerate(slots):
            self.send(touch(DOWN, slot, 0.3 + 0.05 * i, 0.5))
            self.loop.advance(spacing_ms)

    def lift(self, slots):
        for slot in slots:
            self.send(touch(UP, slot))

    # -- ordinary touches -------------------------------------------------
    def test_tap_is_delivered_immediately(self):
        down, up = touch(DOWN, 0, 0.2, 0.2), touch(UP, 0)
        self.send(down)
        self.assertEqual(self.delivered, [])
        self.assertEqual(self.filter.state, HOLD)
        self.send(up)
        self.assertEqual(self.delivered, [down, up])
        self.assertEqual(self.filter.state, IDLE)
        self.assertEqual(self.loop.timers, {})

    def test_drag_is_replayed_after_the_hold_then_passes_through(self):
        events = [touch(DOWN, 0, 0.2, 0.2), touch(MOTION, 0, 0.21, 0.2), touch(MOTION, 0, 0.22, 0.2)]
        self.send(*events)
        self.loop.advance(60)
        self.assertEqual(self.delivered, [])
        self.loop.advance(60)
        self.assertEqual(self.delivered, events)
        self.assertEqual(self.filter.state, PASS)
        later = touch(MOTION, 0, 0.4, 0.2)
        self.send(later)
        self.assertEqual(self.delivered[-1], later)
        self.send(touch(UP, 0))
        self.assertEqual(self.filter.state, IDLE)
        self.assertEqual(self.acted, [])

    def test_two_finger_pinch_is_not_a_gesture(self):
        self.land([0, 1])
        self.loop.advance(120)
        self.assertEqual(len(self.delivered), 2)
        self.swipe([0, 1], 0.3)
        self.lift([0, 1])
        self.assertEqual(self.acted, [])
        self.assertEqual(self.filter.state, IDLE)

    def test_third_finger_after_the_window_is_ordinary_input(self):
        self.land([0, 1], spacing_ms=70)
        self.assertEqual(self.filter.state, PASS)   # 140 ms: the window closed
        self.send(touch(DOWN, 2, 0.4, 0.5))
        self.assertEqual(len(self.delivered), 3)
        self.swipe([0, 1, 2], 0.3)
        self.assertEqual(self.acted, [])

    def test_lift_inside_the_window_with_a_finger_left_passes_through(self):
        self.land([0, 1])
        self.send(touch(UP, 1))
        self.assertEqual(len(self.delivered), 3)
        self.assertEqual(self.filter.state, PASS)
        self.send(touch(UP, 0))
        self.assertEqual(self.filter.state, IDLE)

    def test_late_timer_is_covered_by_the_clock(self):
        self.send(touch(DOWN, 0, 0.2, 0.2))
        self.loop.time += 0.5     # the loop stalled; the timer has not run
        self.send(touch(DOWN, 1, 0.3, 0.2))
        self.send(touch(DOWN, 2, 0.4, 0.2))
        self.assertEqual(len(self.delivered), 3)
        self.assertEqual(self.filter.state, PASS)
        self.assertEqual(self.loop.timers, {})

    # -- gestures --------------------------------------------------------
    def test_three_finger_swipe_left_fires_once_and_delivers_nothing(self):
        self.land([0, 1, 2])
        self.assertEqual(self.filter.state, GESTURE)
        self.assertEqual(self.loop.timers, {})
        self.swipe([0, 1, 2], -0.2)
        self.assertEqual(self.acted, [(3, "left")])
        self.swipe([0, 1, 2], -0.4)
        self.assertEqual(self.acted, [(3, "left")])
        self.lift([0, 1, 2])
        self.assertEqual(self.delivered, [])
        self.assertEqual(self.filter.state, IDLE)
        self.assertEqual(self.filter.recognised, 1)

    def test_four_finger_swipe_right(self):
        self.land([0, 1, 2, 3])
        self.swipe([0, 1, 2, 3], 0.15)
        self.assertEqual(self.acted, [(4, "right")])
        self.lift([3, 2, 1, 0])
        self.assertEqual(self.delivered, [])

    def test_vertical_swipe_reports_up_and_down(self):
        self.land([0, 1, 2])
        self.swipe([0, 1, 2], 0.02, -0.2)
        self.lift([0, 1, 2])
        self.land([0, 1, 2])
        self.swipe([0, 1, 2], 0.0, 0.2)
        self.lift([0, 1, 2])
        self.assertEqual(self.acted, [(3, "up"), (3, "down")])

    def test_short_movement_fires_nothing_and_swallows_the_contacts(self):
        self.land([0, 1, 2])
        self.swipe([0, 1, 2], 0.03)
        self.lift([0, 1, 2])
        self.assertEqual(self.acted, [])
        self.assertEqual(self.delivered, [])
        self.assertEqual(self.filter.state, IDLE)

    def test_fourth_finger_joins_only_while_it_is_still_landing(self):
        self.land([0, 1, 2])
        self.loop.advance(200)
        self.send(touch(DOWN, 3, 0.6, 0.5))     # late: dropped, not a fourth finger
        self.swipe([0, 1, 2], -0.2)
        self.assertEqual(self.acted, [(3, "left")])
        self.lift([0, 1, 2])
        self.assertEqual(self.filter.state, GESTURE)   # the late finger is still down
        self.send(touch(UP, 3))
        self.assertEqual(self.filter.state, IDLE)
        self.assertEqual(self.delivered, [])

    def test_fingers_landing_after_the_swipe_fired_are_dropped(self):
        self.land([0, 1, 2])
        self.swipe([0, 1, 2], -0.2)
        self.send(touch(DOWN, 3, 0.6, 0.5), touch(MOTION, 3, 0.4, 0.5), touch(UP, 3))
        self.lift([0, 1, 2])
        self.assertEqual(self.acted, [(3, "left")])
        self.assertEqual(self.delivered, [])
        self.assertEqual(self.filter.state, IDLE)

    def test_first_lift_ends_the_swipe(self):
        self.land([0, 1, 2])
        self.send(touch(UP, 2))
        self.swipe([0, 1], -0.3)
        self.assertEqual(self.acted, [])
        self.lift([0, 1])
        self.assertEqual(self.filter.state, IDLE)

    def test_a_new_tap_after_a_gesture_is_ordinary(self):
        self.land([0, 1, 2])
        self.swipe([0, 1, 2], -0.2)
        self.lift([0, 1, 2])
        self.send(touch(DOWN, 0, 0.1, 0.1), touch(UP, 0))
        self.assertEqual(len(self.delivered), 2)

    # -- failure paths ---------------------------------------------------
    def test_reset_drops_the_buffer_and_the_timer(self):
        self.send(touch(DOWN, 0, 0.2, 0.2))
        self.filter.reset()
        self.assertEqual(self.loop.timers, {})
        self.loop.advance(200)
        self.assertEqual(self.delivered, [])
        self.assertEqual(self.filter.state, IDLE)

    def test_delivery_error_leaves_the_failed_message_out_of_the_replay(self):
        calls = []

        def forward(message):
            calls.append(message)
            if message["action"] == DOWN:
                raise RuntimeError("portal said no")

        self.filter.forward = forward
        self.send(touch(DOWN, 0, 0.2, 0.2), touch(MOTION, 0, 0.3, 0.2))
        with self.assertRaises(RuntimeError):
            self.loop.advance(120)
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.filter.buffer, [touch(MOTION, 0, 0.3, 0.2)])
        self.filter.reset()   # what the host does after a rejection
        self.assertEqual(self.filter.buffer, [])

    def test_malformed_message_flushes_then_reaches_the_validator(self):
        first = touch(DOWN, 0, 0.2, 0.2)
        bad = {"type": "touch", "action": 7, "slot": 0}
        self.send(first, bad)
        self.assertEqual(self.delivered, [first, bad])
        self.assertEqual(self.loop.timers, {})

    def test_parameters_are_checked(self):
        for kwargs in ({"hold_ms": 0}, {"threshold": 0.0}, {"threshold": 1.5},
                       {"min_fingers": 5, "max_fingers": 4}):
            with self.assertRaises(ValueError):
                GestureFilter(self.delivered.append, lambda *a: None,
                              schedule=self.loop.schedule, cancel=self.loop.cancel, **kwargs)


if __name__ == "__main__":
    unittest.main()
