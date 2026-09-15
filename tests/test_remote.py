import struct
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import remote as remote_module
from remote import RECORD, RemoteControl, RemoteError, TabletInjector
from shortcuts import format_key, parse_key


class FakeInjector:
    alive = True

    def __init__(self):
        self.calls = []

    def move(self, x, y):
        self.calls.append(("move", round(x, 2), round(y, 2)))

    def button(self, code, press, x, y):
        self.calls.append(("button", code, press, round(x, 2), round(y, 2)))

    def scroll(self, vertical, horizontal):
        self.calls.append(("scroll", round(vertical, 4), round(horizontal, 4)))

    def key(self, code, press):
        self.calls.append(("key", code, press))

    def reset(self):
        self.calls.append(("reset",))

    def start(self):
        self.calls.append(("start",))

    def stop(self):
        self.calls.append(("stop",))


class FakeCapture:
    def __init__(self):
        self.receiver = self._Receiver()
        self.capture = object()
        self.active = False
        self.armed = None
        self.released = 0
        self.released_at = None
        self.activation_position = (0.0, 0.0)
        self.on_activated = None
        self.on_deactivated = None

    def create(self, handler):
        self.handler = handler
        return 7

    class _Receiver:
        devices = {1, 2}

        def dispatch(self):
            pass

    def arm(self, barrier):
        self.armed = barrier

    def disarm(self):
        self.armed = None

    def release(self, position=None):
        self.released += 1
        self.released_at = position
        self.active = False
        if self.on_deactivated:
            self.on_deactivated()

    def close(self):
        self.capture = None
        self.receiver = None
        self.active = False

    def activate(self, x=0.0, y=0.0):
        self.active = True
        self.activation_position = (x, y)
        if self.on_activated:
            self.on_activated(x, y)


def control(**kwargs):
    injector, capture = FakeInjector(), FakeCapture()
    nudges = []
    value = RemoteControl(injector, capture, panel=(2960, 1848), sensitivity=2.0,
                          barrier=lambda side: ((0, 0), (0, 1232)),
                          park=lambda x, y: nudges.append(("park", x, y)),
                          nudge=lambda dx, dy: nudges.append(("nudge", dx, dy)),
                          **kwargs)
    return value, injector, capture, nudges


class RemoteControlTests(unittest.TestCase):
    def test_starting_arms_the_barrier_and_pushes_the_pointer_across(self):
        value, injector, capture, nudges = control(edge="none")

        value.start()

        self.assertEqual(capture.armed, ((0, 0), (0, 1232)))
        self.assertEqual(nudges[0], ("park", 0.0, 616.0))
        self.assertEqual(nudges[1:], [("nudge", -30.0, 0.0)] * RemoteControl.NUDGES)
        self.assertIn(("start",), injector.calls)

    def test_the_barrier_is_left_alone_while_capturing_and_cleared_after(self):
        value, injector, capture, _ = control(edge="none")
        value.start()
        capture.activate(100.0, 200.0)
        # KWin stops delivering events if the barriers change mid-capture.
        self.assertEqual(capture.armed, ((0, 0), (0, 1232)))
        self.assertEqual(value.sessions, 1)
        value.stop()
        self.assertIsNone(capture.armed)

        # An edge the user asked for is put back instead.
        value, injector, capture, _ = control(edge="left")
        value.start()
        capture.activate(100.0, 200.0)
        value.stop()
        self.assertEqual(capture.armed, ((0, 0), (0, 1232)))

    def test_the_pointer_is_put_back_inside_the_screen_when_an_edge_is_armed(self):
        value, injector, capture, _ = control(edge="left")
        value.start()
        capture.activate(0.0, 600.0)

        value.stop()

        self.assertEqual(capture.released_at, (40.0, 600.0))
        # Without an armed edge there is nothing to fall back into.
        value, injector, capture, _ = control(edge="none")
        value.start()
        capture.activate(0.0, 600.0)
        value.stop()
        self.assertIsNone(capture.released_at)

    def test_toggle_takes_the_input_and_gives_it_back(self):
        value, injector, capture, _ = control(edge="none")
        states = []
        value.on_state = states.append

        value.toggle()
        capture.activate()
        self.assertTrue(value.active)
        self.assertEqual(states, ["control"])

        self.assertFalse(value.toggle())
        self.assertEqual(capture.released, 1)
        self.assertEqual(states, ["control", "desktop"])

    def test_motion_accumulates_into_tablet_pixels_and_is_clamped(self):
        value, injector, capture, _ = control()
        value.x, value.y = 100.0, 100.0

        value.handle("motion", 10.0, -20.0)
        value.handle("motion", -1000.0, -1000.0)

        self.assertEqual(injector.calls[0], ("move", 120.0, 60.0))
        self.assertEqual(injector.calls[1], ("move", 0.0, 0.0))

    def test_buttons_keys_and_wheels_are_converted(self):
        value, injector, capture, _ = control()
        value.x, value.y = 10.0, 20.0

        value.handle("button", 0x110, True)
        value.handle("key", 30, True)
        value.handle("discrete", 0, 120)      # one notch down, Wayland sign
        value.handle("scroll", 0.0, 25.0)     # half a notch of touchpad travel
        value.handle("frame", 0, 0)

        self.assertEqual(injector.calls, [
            ("button", 0x110, True, 10.0, 20.0),
            ("key", 30, True),
            ("scroll", -1.0, 0.0),
            ("scroll", -0.5, 0.0)])

    def test_events_stop_and_the_capture_is_released_when_the_tablet_goes_away(self):
        value, injector, capture, _ = control()
        capture.activate()
        injector.alive = False

        value.handle("motion", 5.0, 5.0)

        self.assertEqual(capture.released, 1)
        self.assertEqual(value.events, 0)

    def test_closing_releases_everything(self):
        value, injector, capture, _ = control()
        capture.activate()

        value.close()

        self.assertEqual(capture.released, 1)
        self.assertIn(("reset",), injector.calls)
        self.assertIn(("stop",), injector.calls)

    def test_start_without_an_edge_to_use_is_an_error(self):
        injector, capture = FakeInjector(), FakeCapture()
        value = RemoteControl(injector, capture, barrier=lambda side: None)
        with self.assertRaises(RemoteError):
            value.start()


class InjectorProtocolTests(unittest.TestCase):
    def test_records_are_twelve_bytes_in_the_order_the_tablet_reads(self):
        sent = []
        injector = TabletInjector(lambda *args: None, "adb")
        injector.sock = MagicMock()
        injector.sock.sendall.side_effect = lambda data: sent.append(data)

        injector.move(1400.5, 900.4)
        injector.button(0x111, False, 10, 20)
        injector.scroll(-1.5, 0.25)
        injector.key(30, True)
        injector.reset()

        self.assertTrue(all(len(record) == 12 for record in sent))
        self.assertEqual([RECORD.unpack(record) for record in sent], [
            (remote_module.TYPE_MOVE, 0, 0, 1400, 900),
            (remote_module.TYPE_BUTTON, 0, 0x111, 10, 20),
            (remote_module.TYPE_SCROLL, 0, 0, -1500, 250),
            (remote_module.TYPE_KEY, 1, 30, 0, 0),
            (remote_module.TYPE_RESET, 0, 0, 0, 0)])
        self.assertEqual(injector.sent, 5)

    def test_a_broken_socket_stops_sending_rather_than_raising(self):
        injector = TabletInjector(lambda *args: None, "adb")
        injector.sock = MagicMock()
        injector.sock.sendall.side_effect = OSError("gone")

        injector.move(1, 1)

        self.assertIsNone(injector.sock)
        self.assertFalse(injector.alive)


class ShortcutKeyTests(unittest.TestCase):
    def test_key_names_survive_a_round_trip(self):
        for text in ("Meta+Shift+T", "Ctrl+Alt+Delete", "Meta+F5", "Meta+Shift+Escape"):
            self.assertEqual(format_key(parse_key(text)), text)

    def test_unknown_keys_are_rejected(self):
        for text in ("", "Meta+", "Hyper+T", "Meta+Nonsense"):
            with self.assertRaises(ValueError):
                parse_key(text)

    def test_nothing_bound_prints_as_none(self):
        self.assertEqual(format_key(0), "none")


if __name__ == "__main__":
    unittest.main()
