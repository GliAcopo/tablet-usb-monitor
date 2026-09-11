import math
import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from touch_input import (  # noqa: E402
    BTN_LEFT,
    BUTTON_PRESSED,
    BUTTON_RELEASED,
    FixedTarget,
    LiveKScreenTarget,
    OutputGeometry,
    POINTER,
    PortalTouchInput,
    TOUCHSCREEN,
    TouchInputError,
)


class FakePortal:
    def __init__(self, fail_on=None):
        self.calls = []
        self.fail_on = fail_on

    def __getattr__(self, name):
        def call(*args):
            self.calls.append((name, args))
            if name == self.fail_on:
                raise RuntimeError("injection failed")
        return call


def geometry(scale=1.5):
    return OutputGeometry("Virtual-1", 2960, 1848, scale, -1973, 0)


def controller(devices=TOUCHSCREEN, *, target=None):
    portal = FakePortal()
    value = PortalTouchInput(portal, "/session/1", 77,
                             target or FixedTarget(geometry()), devices)
    return value, portal


class PortalTouchInputTests(unittest.TestCase):
    def test_maps_normalized_coordinates_to_live_logical_stream_size(self):
        target = FixedTarget(geometry())
        touch, portal = controller(target=target)
        self.assertTrue(touch.handle_message(
            {"type": "touch", "action": 0, "slot": 3, "x": 0.5, "y": 0.25}))
        self.assertEqual(portal.calls[-1][0], "NotifyTouchDown")
        _, options, stream, slot, x, y = portal.calls[-1][1]
        self.assertEqual((options, stream, slot), ({}, 77, 3))
        self.assertEqual((x, y), (986.5, 308.0))

        # A live scale change alters logical coordinates; desktop position does
        # not enter the mapping because portal coordinates are stream-relative.
        target.current = OutputGeometry("Virtual-1", 2960, 1848, 2.0, 4000, 300)
        touch.handle_message(
            {"type": "touch", "action": 2, "slot": 3, "x": 0.5, "y": 0.25})
        self.assertEqual(portal.calls[-1][1][-2:], (740.0, 231.0))

    def test_multitouch_lifecycle_and_disconnect_release(self):
        touch, portal = controller()
        for slot in (4, 1):
            touch.handle_message(
                {"type": "touch", "action": 0, "slot": slot, "x": 0.1, "y": 0.2})
        touch.handle_message(
            {"type": "touch", "action": 1, "slot": 4, "x": 0.1, "y": 0.2})
        touch.release_all()
        self.assertEqual([call[0] for call in portal.calls], [
            "NotifyTouchDown", "NotifyTouchDown", "NotifyTouchUp", "NotifyTouchUp"])
        self.assertEqual(portal.calls[-1][1][-1], 1)
        self.assertEqual(touch.active_slots, set())

    def test_failed_down_is_not_recorded_as_active(self):
        portal = FakePortal(fail_on="NotifyTouchDown")
        touch = PortalTouchInput(
            portal, "/session/1", 77, FixedTarget(geometry()), TOUCHSCREEN)
        with self.assertRaises(RuntimeError):
            touch.handle_message(
                {"type": "touch", "action": 0, "slot": 3, "x": 0.5, "y": 0.25})
        self.assertEqual(touch.active_slots, set())

    def test_inclusive_android_endpoint_stays_inside_portal_bounds(self):
        touch, portal = controller()
        touch.handle_message(
            {"type": "touch", "action": 0, "slot": 0, "x": 1.0, "y": 1.0})
        x, y = portal.calls[-1][1][-2:]
        self.assertLess(x, 1973.0)
        self.assertLess(y, 1232.0)
        self.assertEqual(x, math.nextafter(1973.0, 0.0))
        self.assertEqual(y, math.nextafter(1232.0, 0.0))

    def test_rejects_bad_bounds_slots_and_lifecycle(self):
        bad_messages = [
            {"type": "touch", "action": 0, "slot": 0, "x": -0.01, "y": 0.2},
            {"type": "touch", "action": 0, "slot": 0, "x": math.nan, "y": 0.2},
            {"type": "touch", "action": 0, "slot": 16, "x": 0.1, "y": 0.2},
            {"type": "touch", "action": 9, "slot": 0, "x": 0.1, "y": 0.2},
            {"type": "touch", "action": 2, "slot": 0, "x": 0.1, "y": 0.2},
        ]
        for message in bad_messages:
            touch, portal = controller()
            with self.assertRaises(TouchInputError):
                touch.handle_message(message)
            self.assertEqual(portal.calls, [])

    def test_pointer_fallback_is_single_contact_and_releases(self):
        touch, portal = controller(POINTER)
        self.assertEqual(touch.mode, "pointer-fallback")
        self.assertTrue(touch.handle_message(
            {"type": "touch", "action": 0, "slot": 2, "x": 1.0, "y": 1.0}))
        self.assertFalse(touch.handle_message(
            {"type": "touch", "action": 0, "slot": 5, "x": 0.2, "y": 0.2}))
        touch.handle_message(
            {"type": "touch", "action": 2, "slot": 2, "x": 0.5, "y": 0.5})
        touch.release_all()
        self.assertEqual(portal.calls[1], (
            "NotifyPointerButton", ("/session/1", {}, BTN_LEFT, BUTTON_PRESSED)))
        self.assertEqual(portal.calls[-1], (
            "NotifyPointerButton", ("/session/1", {}, BTN_LEFT, BUTTON_RELEASED)))
        self.assertIsNone(touch.pointer_slot)

    def test_bind_accepts_only_matching_existing_monitor(self):
        portal = FakePortal()
        target = FixedTarget(geometry())
        touch = PortalTouchInput.bind(
            portal, "/session/1",
            (77, {"source_type": 1, "size": (2960, 1848),
                  "logical_size": (1973, 1232), "position": (-1973, 0)}),
            target, TOUCHSCREEN)
        self.assertEqual(touch.stream_id, 77)

        for props in (
            {"source_type": 2, "size": (2960, 1848)},
            {"source_type": 1, "size": (2560, 1600)},
            {"source_type": 1, "size": (2960, 1848), "position": (0, 0)},
            {"source_type": 1, "size": (2960, 1848), "logical_size": (1460, 900)},
        ):
            with self.assertRaises(TouchInputError):
                PortalTouchInput.bind(
                    portal, "/session/1", (77, props), target, TOUCHSCREEN)

    def test_touch_grant_is_preferred_over_pointer(self):
        touch, _ = controller(TOUCHSCREEN | POINTER)
        self.assertEqual(touch.mode, "touchscreen")

    def test_pen_hover_and_tip_use_granted_absolute_pointer(self):
        touch, portal = controller(TOUCHSCREEN | POINTER)
        hover = {"type": "pen", "action": 3, "x": 0.25, "y": 0.5}
        down = {"type": "pen", "action": 0, "x": 0.25, "y": 0.5}
        move = {"type": "pen", "action": 2, "x": 0.5, "y": 0.75}
        up = {"type": "pen", "action": 1, "x": 0.5, "y": 0.75}
        for event in (hover, down, move, up):
            self.assertTrue(touch.handle_message(event))
        self.assertEqual([call[0] for call in portal.calls], [
            "NotifyPointerMotionAbsolute", "NotifyPointerMotionAbsolute",
            "NotifyPointerButton", "NotifyPointerMotionAbsolute", "NotifyPointerButton"])
        self.assertFalse(touch.pen_down)

    def test_pen_requires_pointer_and_disconnect_releases_tip(self):
        touch, portal = controller(TOUCHSCREEN)
        self.assertFalse(touch.handle_message(
            {"type": "pen", "action": 0, "x": 0.2, "y": 0.3}))
        self.assertEqual(portal.calls, [])

        touch, portal = controller(TOUCHSCREEN | POINTER)
        touch.handle_message({"type": "pen", "action": 0, "x": 0.2, "y": 0.3})
        touch.release_all()
        self.assertEqual(portal.calls[-1], (
            "NotifyPointerButton", ("/session/1", {}, BTN_LEFT, BUTTON_RELEASED)))
        self.assertFalse(touch.pen_down)

    def test_pen_does_not_steal_pointer_fallback_drag(self):
        touch, portal = controller(POINTER)
        touch.handle_message(
            {"type": "touch", "action": 0, "slot": 2, "x": 0.1, "y": 0.1})
        before = list(portal.calls)
        self.assertFalse(touch.handle_message(
            {"type": "pen", "action": 0, "x": 0.8, "y": 0.8}))
        self.assertEqual(portal.calls, before)

    def test_non_touch_messages_are_left_for_host(self):
        touch, portal = controller()
        self.assertFalse(touch.handle_message({"type": "rendered", "seq": 9}))
        self.assertEqual(portal.calls, [])


class LiveKScreenTargetTests(unittest.TestCase):
    def test_reads_current_mode_and_fractional_scale(self):
        outputs = [{
            "name": "Virtual-1", "enabled": True, "currentModeId": "4",
            "modes": [{"id": "4", "size": {"width": 2960, "height": 1848}}],
            "scale": 1.5, "pos": {"x": -1973, "y": 0},
        }]
        target = LiveKScreenTarget(
            "Virtual-1", cache_seconds=0, read_outputs=lambda: outputs)
        self.assertEqual(target.geometry().logical_size, (1973, 1232))

    def test_refuses_missing_or_disabled_named_output(self):
        for outputs in ([], [{"name": "Virtual-1", "enabled": False}]):
            target = LiveKScreenTarget(
                "Virtual-1", cache_seconds=0, read_outputs=lambda: outputs)
            with self.assertRaises(TouchInputError):
                target.geometry()

    def test_normalizes_layout_probe_failures_for_safe_release(self):
        def failed_probe():
            raise TimeoutError("kscreen did not answer")

        target = LiveKScreenTarget(
            "Virtual-1", cache_seconds=0, read_outputs=failed_probe)
        with self.assertRaisesRegex(TouchInputError, "could not refresh"):
            target.geometry()


if __name__ == "__main__":
    unittest.main()
