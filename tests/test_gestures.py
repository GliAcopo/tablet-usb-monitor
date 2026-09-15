from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from gestures import GestureFilter, GESTURE, HOLD, IDLE, PASS, SCROLL, TWO  # noqa: E402


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
                       {"min_fingers": 5, "max_fingers": 4}, {"scroll_threshold": 0.0},
                       {"scroll": lambda *a: True, "min_fingers": 2}):
            with self.assertRaises(ValueError):
                GestureFilter(self.delivered.append, lambda *a: None,
                              schedule=self.loop.schedule, cancel=self.loop.cancel, **kwargs)


class TwoFingerScrollTests(GestureFilterTests):
    """The same filter with two-finger scrolling on: every swipe test still holds."""

    def setUp(self):
        super().setUp()
        self.scrolled = []
        self.can_scroll = True

        def scroll(phase, x, y):
            self.scrolled.append((phase, round(x, 4), round(y, 4)))
            return self.can_scroll
        self.filter.scroll = scroll
        self.filter.scroll_threshold = 0.01

    def two(self):
        self.land([0, 1])
        self.loop.advance(120)
        self.assertEqual(self.filter.state, TWO)
        self.assertEqual(self.delivered, [])
        self.assertEqual(self.loop.timers, {})

    def test_two_finger_pinch_is_not_a_gesture(self):
        # Overrides the base test: the fingers are held until they move.
        self.two()
        self.send(touch(MOTION, 0, 0.28, 0.5), touch(MOTION, 1, 0.37, 0.5))   # apart
        self.assertEqual(self.filter.state, PASS)
        self.assertEqual(len(self.delivered), 4)
        self.assertEqual(self.scrolled, [])
        self.lift([0, 1])
        self.assertEqual(self.filter.state, IDLE)

    def test_fingers_moving_together_scroll_and_never_reach_the_desktop(self):
        self.two()
        self.send(touch(MOTION, 0, 0.3, 0.505))          # under the threshold
        self.assertEqual(self.filter.state, TWO)
        self.send(touch(MOTION, 1, 0.35, 0.52))          # mean moved 0.0125 down
        self.assertEqual(self.filter.state, SCROLL)
        self.assertEqual(self.scrolled, [("begin", 0.325, 0.5), ("move", 0.0, 0.0125)])
        self.send(touch(MOTION, 0, 0.3, 0.6), touch(MOTION, 1, 0.35, 0.6))
        self.assertEqual(self.scrolled[2:], [("move", 0.0, 0.0475), ("move", 0.0, 0.04)])
        self.send(touch(UP, 0))
        self.assertEqual(self.scrolled[-1], ("end", 0.0, 0.0))
        self.send(touch(MOTION, 1, 0.35, 0.7))           # the other finger: nothing
        self.send(touch(UP, 1))
        self.assertEqual(len(self.scrolled), 5)
        self.assertEqual(self.delivered, [])
        self.assertEqual(self.filter.state, IDLE)
        self.assertEqual(self.filter.scrolled, 1)

    def test_scroll_that_cannot_begin_replays_the_contacts(self):
        self.can_scroll = False
        self.two()
        self.send(touch(MOTION, 0, 0.3, 0.55), touch(MOTION, 1, 0.35, 0.55))
        self.assertEqual(self.scrolled, [("begin", 0.325, 0.5)])
        self.assertEqual(self.filter.state, PASS)
        self.assertEqual(len(self.delivered), 4)
        self.assertEqual(self.filter.scrolled, 0)

    def test_resting_fingers_keep_one_motion_per_slot(self):
        self.two()
        for i in range(50):
            self.send(touch(MOTION, 0, 0.3 + 0.0001 * (i % 3), 0.5), touch(MOTION, 1, 0.35, 0.5))
        self.assertEqual(len(self.filter.buffer), 4)
        self.send(touch(MOTION, 0, 0.28, 0.5), touch(MOTION, 1, 0.37, 0.5))   # apart
        self.assertEqual(self.filter.state, PASS)
        # The replay holds the latest position per slot; the second finger's
        # move arrived after the classification and went straight through.
        self.assertEqual([m["action"] for m in self.delivered], [DOWN, DOWN, MOTION, MOTION, MOTION])
        self.assertEqual([m["x"] for m in self.delivered[2:]], [0.35, 0.28, 0.37])

    def test_two_finger_tap_reaches_the_desktop(self):
        self.two()
        self.send(touch(UP, 1))
        self.assertEqual(len(self.delivered), 3)
        self.assertEqual(self.filter.state, PASS)
        self.send(touch(UP, 0))
        self.assertEqual(self.filter.state, IDLE)
        self.assertEqual(self.scrolled, [])

    def test_third_finger_after_the_window_is_ordinary_input(self):
        # Overrides the base test: two fingers are still held when it lands.
        self.land([0, 1], spacing_ms=70)
        self.assertEqual(self.filter.state, TWO)   # 140 ms: the window closed
        self.send(touch(DOWN, 2, 0.4, 0.5))
        self.assertEqual(len(self.delivered), 3)
        self.swipe([0, 1, 2], 0.3)
        self.assertEqual(self.acted, [])

    def test_late_third_finger_makes_it_ordinary_input(self):
        self.two()
        self.send(touch(DOWN, 2, 0.4, 0.5))
        self.assertEqual(self.filter.state, PASS)
        self.assertEqual(len(self.delivered), 3)
        self.swipe([0, 1, 2], -0.3)
        self.assertEqual(self.acted, [])
        self.assertEqual(self.scrolled, [])

    def test_third_finger_inside_the_window_is_still_a_swipe(self):
        self.land([0, 1, 2])
        self.assertEqual(self.filter.state, GESTURE)
        self.swipe([0, 1, 2], -0.2)
        self.assertEqual(self.acted, [(3, "left")])
        self.assertEqual(self.scrolled, [])

    def test_fingers_landing_during_a_scroll_are_dropped(self):
        self.two()
        self.send(touch(MOTION, 0, 0.3, 0.55), touch(MOTION, 1, 0.35, 0.55))
        self.send(touch(DOWN, 2, 0.6, 0.5), touch(MOTION, 2, 0.6, 0.7))
        self.lift([0, 1])
        self.assertEqual(self.filter.state, SCROLL)   # the late finger is still down
        self.send(touch(UP, 2))
        self.assertEqual(self.filter.state, IDLE)
        self.assertEqual(self.delivered, [])
        self.assertEqual(self.scrolled[-1], ("end", 0.0, 0.0))

    def test_late_timer_still_reaches_the_two_finger_state(self):
        self.land([0, 1])
        self.loop.time += 0.5     # the loop stalled; the timer has not run
        self.send(touch(MOTION, 0, 0.3, 0.505))
        self.assertEqual(self.filter.state, TWO)
        self.assertEqual(self.delivered, [])
        self.assertEqual(self.loop.timers, {})

    def test_reset_during_a_scroll_forgets_it(self):
        self.two()
        self.send(touch(MOTION, 0, 0.3, 0.55), touch(MOTION, 1, 0.35, 0.55))
        self.filter.reset()
        self.assertEqual(self.filter.state, IDLE)
        self.send(touch(DOWN, 0, 0.1, 0.1), touch(UP, 0))
        self.assertEqual(len(self.delivered), 2)


class TwoFingerTapTests(TwoFingerScrollTests):
    """Scrolling and taps on together: every scroll and swipe test still holds."""

    def setUp(self):
        super().setUp()
        self.tapped = []
        self.can_tap = True

        def tap(x, y):
            self.tapped.append((round(x, 4), round(y, 4)))
            return self.can_tap
        self.filter.tap = tap

    def test_two_finger_tap_reaches_the_desktop(self):
        # Overrides the base test: the tap is the host's now.
        self.two()
        self.send(touch(UP, 1))
        self.assertEqual(self.tapped, [(0.325, 0.5)])
        self.assertEqual(self.delivered, [])
        self.assertEqual(self.filter.state, GESTURE)
        self.send(touch(UP, 0))
        self.assertEqual(self.filter.state, IDLE)
        self.assertEqual(self.scrolled, [])
        self.assertEqual(self.filter.tapped, 1)

    def test_lift_inside_the_window_with_a_finger_left_passes_through(self):
        # Overrides the base test: lifting without moving is the tap now, so
        # the finger has to have moved for the sequence to pass through.
        self.land([0, 1])
        self.send(touch(MOTION, 1, 0.35, 0.52))
        self.send(touch(UP, 1))
        self.assertEqual(len(self.delivered), 4)
        self.assertEqual(self.filter.state, PASS)
        self.send(touch(UP, 0))
        self.assertEqual(self.filter.state, IDLE)
        self.assertEqual(self.tapped, [])

    def test_quick_two_finger_tap_inside_the_window_is_a_tap_too(self):
        self.land([0, 1])
        self.send(touch(UP, 0))
        self.assertEqual(self.tapped, [(0.325, 0.5)])
        self.assertEqual(self.delivered, [])
        self.assertEqual(self.loop.timers, {})
        self.send(touch(UP, 1))
        self.assertEqual(self.filter.state, IDLE)
        # The next single tap is ordinary again.
        self.send(touch(DOWN, 0, 0.1, 0.1), touch(UP, 0))
        self.assertEqual(len(self.delivered), 2)

    def test_two_fingers_that_moved_are_not_a_tap(self):
        self.two()
        self.send(touch(MOTION, 0, 0.3, 0.505), touch(MOTION, 1, 0.35, 0.512))   # under the scroll threshold
        self.assertEqual(self.filter.state, TWO)
        self.send(touch(UP, 1))
        self.assertEqual(self.tapped, [])
        self.assertEqual(len(self.delivered), 5)    # 2 down, 2 motion, 1 up
        self.assertEqual(self.filter.state, PASS)

    def test_a_late_finger_during_a_tap_is_dropped(self):
        self.two()
        self.send(touch(UP, 1), touch(DOWN, 2, 0.6, 0.6), touch(UP, 2), touch(UP, 0))
        self.assertEqual(self.delivered, [])
        self.assertEqual(self.filter.state, IDLE)

    def test_tap_the_host_cannot_perform_reaches_the_desktop(self):
        self.can_tap = False
        self.two()
        self.send(touch(UP, 1))
        self.assertEqual(self.tapped, [(0.325, 0.5)])
        self.assertEqual([m["action"] for m in self.delivered], [DOWN, DOWN, UP])
        self.assertEqual(self.filter.state, PASS)

    def test_taps_need_no_scrolling(self):
        self.filter.scroll = None
        self.two()
        self.send(touch(UP, 1), touch(UP, 0))
        self.assertEqual(self.tapped, [(0.325, 0.5)])
        self.assertEqual(self.delivered, [])
        # Two fingers moving together are the desktop's without ``scroll``.
        self.two()
        self.send(touch(MOTION, 0, 0.3, 0.55), touch(MOTION, 1, 0.35, 0.55))
        self.assertEqual(self.filter.state, PASS)
        self.assertEqual(len(self.delivered), 4)


if __name__ == "__main__":
    unittest.main()
