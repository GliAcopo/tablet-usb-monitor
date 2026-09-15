import struct
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import remote as remote_module
from remote import RECORD, RemoteControl, RemoteError, TabletInjector, parse_chord
from shortcuts import format_key, parse_key


class FakeInjector:
    alive = True

    def __init__(self):
        self.calls = []

    def move(self, dx, dy):
        self.calls.append(("move", dx, dy))

    def button(self, code, press):
        self.calls.append(("button", code, press))

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
    kwargs.setdefault("sensitivity", 1.0)
    value = RemoteControl(injector, capture, panel=(2960, 1848),
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

    def test_motion_is_scaled_and_keeps_the_fraction_for_the_next_event(self):
        value, injector, capture, _ = control(sensitivity=1.5)

        value.handle("motion", 10.0, -20.0)      # 15, -30
        value.handle("motion", 1.0, 0.0)         # 1.5 -> 1 now, 0.5 kept
        value.handle("motion", 1.0, 0.0)         # 1.5 + 0.5 -> 2
        value.handle("motion", 0.1, 0.0)         # 0.15: nothing to send yet

        self.assertEqual(injector.calls, [
            ("move", 15, -30), ("move", 1, 0), ("move", 2, 0)])

    def test_buttons_keys_and_wheels_are_converted(self):
        value, injector, capture, _ = control()

        value.handle("button", 0x110, True)
        value.handle("key", 30, True)
        value.handle("discrete", 0, 120)      # one notch down, Wayland sign
        value.handle("scroll", 0.0, 25.0)     # half a notch of touchpad travel
        value.handle("frame", 0, 0)

        self.assertEqual(injector.calls, [
            ("button", 0x110, True),
            ("key", 30, True),
            ("scroll", -1.0, 0.0),
            ("scroll", -0.5, 0.0)])

    def test_the_release_chord_is_caught_here_and_never_reaches_the_tablet(self):
        value, injector, capture, _ = control(edge="none", release_chord="Meta+Shift+T")
        capture.activate()
        injector.calls.clear()

        value.handle("key", 125, True)     # Meta down
        value.handle("key", 42, True)      # Shift down
        value.handle("key", 20, True)      # T: the chord

        self.assertEqual(capture.released, 1)
        self.assertNotIn(("key", 20, True), injector.calls)
        # The modifiers themselves go to the tablet: they are ordinary keys
        # until the chord completes.
        self.assertEqual([call for call in injector.calls if call[0] == "key"],
                         [("key", 125, True), ("key", 42, True)])

    def test_the_chord_needs_its_modifiers_and_nothing_else(self):
        value, injector, capture, _ = control(edge="none", release_chord="Meta+Shift+T")
        capture.activate()

        value.handle("key", 20, True)                  # T alone
        value.handle("key", 125, True)                 # Meta
        value.handle("key", 20, True)                  # Meta+T
        value.handle("key", 56, True)                  # Alt as well
        value.handle("key", 42, True)                  # and Shift
        value.handle("key", 20, True)                  # Meta+Alt+Shift+T
        self.assertEqual(capture.released, 0)

        value.handle("key", 56, False)                 # Alt up -> exactly the chord
        value.handle("key", 20, True)
        self.assertEqual(capture.released, 1)

    def test_a_chord_that_names_no_real_key_is_ignored(self):
        value, injector, capture, _ = control(edge="none", release_chord="Meta+Shift+Nonsense")
        capture.activate()
        value.handle("key", 20, True)
        self.assertEqual(capture.released, 0)
        self.assertIn(("key", 20, True), injector.calls)

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

        injector.move(1400, 900)
        injector.button(0x111, False)
        injector.scroll(-1.5, 0.25)
        injector.key(30, True)
        injector.reset()

        self.assertTrue(all(len(record) == 12 for record in sent))
        self.assertEqual([RECORD.unpack(record) for record in sent], [
            (remote_module.TYPE_MOVE, 0, 0, 1400, 900),
            (remote_module.TYPE_BUTTON, 0, 0x111, 0, 0),
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


class ChordTests(unittest.TestCase):
    def test_chords_parse_to_evdev_codes(self):
        self.assertEqual(parse_chord("Meta+Shift+T"), (frozenset({"meta", "shift"}), 20))
        self.assertEqual(parse_chord("Ctrl+Alt+Delete"), (frozenset({"ctrl", "alt"}), 111))
        self.assertEqual(parse_chord("Meta+F5"), (frozenset({"meta"}), 63))

    def test_what_cannot_be_watched_for_is_None(self):
        for text in ("", "Meta+Nonsense", "Hyper+T", "+"):
            self.assertIsNone(parse_chord(text))


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
