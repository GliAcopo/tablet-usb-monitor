import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import host as host_module  # noqa: E402
import gestures  # noqa: E402


STOP = object()


class QueuedGLib:
    def __init__(self):
        self.pending = []

    def idle_add(self, callback, *args):
        self.pending.append((callback, args))
        return len(self.pending)

    def drain(self):
        while self.pending:
            callback, args = self.pending.pop(0)
            callback(*args)


class FakeTouch:
    def __init__(self):
        self.messages = []
        self.releases = 0
        self.error = None

    def handle_message(self, message):
        if self.error is not None:
            raise self.error
        self.messages.append(message)

    def release_all(self):
        self.releases += 1


class FakeWebSocket:
    def __init__(self, token):
        self.auth = json.dumps({"type": "auth", "token": token})
        self.incoming = asyncio.Queue()
        self.sent = []
        self.closed = None

    async def recv(self):
        return self.auth

    async def send(self, message):
        self.sent.append(json.loads(message))

    async def close(self, *args, **kwargs):
        self.closed = (args, kwargs)

    def __aiter__(self):
        return self

    async def __anext__(self):
        item = await self.incoming.get()
        if item is STOP:
            raise StopAsyncIteration
        if isinstance(item, BaseException):
            raise item
        return item

    def push(self, message):
        self.incoming.put_nowait(json.dumps(message))

    def stop(self):
        self.incoming.put_nowait(STOP)

    def fail(self, error):
        self.incoming.put_nowait(error)


def bare_host():
    value = host_module.Host.__new__(host_module.Host)
    value.token = "a" * 64
    value.controls = set()
    value.control_owner = None
    value.control_generation = 0
    value.touch = FakeTouch()
    value.args = SimpleNamespace(width=2960, height=1848, fps=120, bitrate=60000)
    value.sent = {}
    value.stats = []
    value.rendered = 0
    value.tablet_stats = {}
    value.input_messages = 0
    value.input_rejected = 0
    value.input_followon_rejected = 0
    value.tablet_panel = None
    value.panel_mismatch_reported = False
    value.client_features = set()
    value.gestures = None
    value.gestures_fired = 0
    value.kglobalaccel = None
    return value


class FakeShortcuts:
    def __init__(self):
        self.invoked = []

    def invokeShortcut(self, name, reply_handler=None, error_handler=None):
        self.invoked.append(name)


def gesture_host():
    """A host whose touch path goes through the gesture filter (timers by hand)."""
    value = bare_host()
    value.control_owner = object()
    value.control_generation = 4
    value.kglobalaccel = FakeShortcuts()
    value.timers = {}

    def schedule(ms, callback):
        handle = len(value.timers) + 1
        value.timers[handle] = callback
        return handle

    value.gestures = host_module.GestureFilter(
        value._deliver_input, value.perform_gesture,
        schedule=schedule, cancel=value.timers.pop, hold_ms=120)
    return value


def swipe(value, slots, dx):
    for i, slot in enumerate(slots):
        value.handle_touch({"type": "touch", "action": 0, "slot": slot, "x": 0.3 + 0.05 * i, "y": 0.5}, 4)
    for step in (1, 2, 3):
        for i, slot in enumerate(slots):
            value.handle_touch({"type": "touch", "action": 2, "slot": slot,
                                "x": 0.3 + 0.05 * i + dx * step / 3, "y": 0.5}, 4)
    for slot in slots:
        value.handle_touch({"type": "touch", "action": 1, "slot": slot, "x": 0.0, "y": 0.0}, 4)


class HostTouchIntegrationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.old_glib = host_module.GLib
        self.glib = QueuedGLib()
        host_module.GLib = self.glib

    def tearDown(self):
        host_module.GLib = self.old_glib

    async def _settle(self):
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    async def test_unauthenticated_socket_cannot_release_active_input(self):
        value = bare_host()
        ws = FakeWebSocket("wrong")

        await value.control(ws)
        self.glib.drain()

        self.assertIsNotNone(ws.closed)
        self.assertEqual(value.touch.releases, 0)
        self.assertIsNone(value.control_owner)

    async def test_second_authenticated_controller_is_rejected_without_cleanup(self):
        value = bare_host()
        first = FakeWebSocket(value.token)
        first_task = asyncio.create_task(value.control(first))
        await self._settle()
        self.glib.drain()  # begin_control: clean slate for the accepted owner
        self.assertIs(value.control_owner, first)
        self.assertEqual(value.touch.releases, 1)

        second = FakeWebSocket(value.token)
        second.stop()
        await value.control(second)
        self.glib.drain()

        self.assertIs(value.control_owner, first)
        self.assertIsNotNone(second.closed)
        self.assertEqual(second.closed[1].get("code"), 1008)
        self.assertEqual(value.touch.releases, 1)

        first.stop()
        await first_task
        self.glib.drain()
        self.assertEqual(value.touch.releases, 2)

    async def test_disconnected_generation_cannot_inject_queued_touch(self):
        value = bare_host()
        old = FakeWebSocket(value.token)
        old_task = asyncio.create_task(value.control(old))
        await self._settle()

        event = {"type": "touch", "action": 0, "slot": 0, "x": 0.4, "y": 0.5}
        old.push(event)
        await self._settle()  # queues touch, but deliberately do not run GLib
        old.stop()
        await old_task       # queues old-generation cleanup

        new = FakeWebSocket(value.token)
        new_task = asyncio.create_task(value.control(new))
        await self._settle()  # invalidates both old callbacks, queues begin_control
        self.glib.drain()

        self.assertEqual(value.touch.messages, [])
        self.assertEqual(value.touch.releases, 1)

        new.push(event)
        await self._settle()
        self.glib.drain()
        self.assertEqual(value.touch.messages, [event])

        new.stop()
        await new_task
        self.glib.drain()

    async def test_transport_failure_releases_owner_contacts(self):
        value = bare_host()
        ws = FakeWebSocket(value.token)
        task = asyncio.create_task(value.control(ws))
        await self._settle()
        self.glib.drain()
        self.assertIs(value.control_owner, ws)

        ws.fail(ConnectionError("USB transport disappeared"))
        await task
        self.glib.drain()

        self.assertIsNone(value.control_owner)
        self.assertNotIn(ws, value.controls)
        self.assertEqual(value.touch.releases, 2)  # begin + disconnect

    async def test_mode_request_is_answered_with_the_mode_actually_in_force(self):
        # The tablet's pen-only toggle is not implemented host-side. Without an
        # answer its UI would keep showing a mode the host never entered.
        value = bare_host()
        ws = FakeWebSocket(value.token)
        task = asyncio.create_task(value.control(ws))
        await self._settle()
        self.glib.drain()
        greeting = len(ws.sent)

        ws.push({"type": "mode", "pen_only": True})
        await self._settle()
        ws.stop()
        await task
        self.glib.drain()

        self.assertEqual(len(ws.sent), greeting + 1)
        self.assertIs(ws.sent[-1]["pen_only"], False)

    async def test_unhandled_message_types_are_counted_as_no_input(self):
        value = bare_host()
        ws = FakeWebSocket(value.token)
        task = asyncio.create_task(value.control(ws))
        await self._settle()
        self.glib.drain()

        ws.push({"type": "resolution", "width": 2960, "height": 1848})
        ws.push({"type": "touch", "action": 0, "slot": 0, "x": 0.1, "y": 0.1})
        await self._settle()
        ws.stop()
        await task
        self.glib.drain()

        self.assertEqual(value.input_messages, 1)
        self.assertEqual(value.tablet_panel, (2960, 1848))
        self.assertFalse(value.panel_mismatch_reported)

    async def test_config_features_are_recorded_and_advertised_back(self):
        """The greeting names the host's features; the client's config names
        the ones it implements (only well-formed strings are kept), so the
        video socket sends heartbeats only to clients that can parse them."""
        value = bare_host()
        ws = FakeWebSocket(value.token)
        task = asyncio.create_task(value.control(ws))
        await self._settle()
        self.glib.drain()
        self.assertIn('video_heartbeat', ws.sent[0]['features'])

        ws.push({"type": "config", "protocol": 2, "fps": 120, "bitrate": 60000,
                 "features": ["video_heartbeat", 7, "x" * 40]})
        await self._settle()
        self.assertEqual(value.client_features, {'video_heartbeat'})
        ws.push({"type": "config", "protocol": 2, "fps": 120, "bitrate": 60000})
        await self._settle()
        self.assertEqual(value.client_features, {'video_heartbeat'})  # absent list: unchanged
        ws.stop()
        await task
        self.glib.drain()

    async def test_mismatched_tablet_panel_is_reported_once(self):
        value = bare_host()

        self.assertTrue(value.note_tablet_panel({"width": 2560, "height": 1600}))
        self.assertTrue(value.panel_mismatch_reported)
        self.assertEqual(value.tablet_panel, (2560, 1600))

        # A second announcement must not repeat the warning.
        value.panel_mismatch_reported = False
        value.note_tablet_panel({"width": 2960, "height": 1848})
        self.assertFalse(value.panel_mismatch_reported)

    async def test_implausible_panel_announcement_is_ignored(self):
        value = bare_host()
        for bad in ({"width": 0, "height": 1848}, {"width": "2960", "height": 1848},
                    {"width": True, "height": 1848}, {"height": 1848},
                    {"width": 99999, "height": 1848}):
            self.assertFalse(value.note_tablet_panel(bad))
        self.assertIsNone(value.tablet_panel)

    async def test_rejected_touch_event_releases_all_contacts(self):
        value = bare_host()
        value.control_owner = object()
        value.control_generation = 4
        value.touch.error = host_module.TouchInputError("invalid lifecycle")

        result = value.handle_touch(
            {"type": "touch", "action": 2, "slot": 8, "x": 0.2, "y": 0.3}, 4)

        self.assertFalse(result)
        self.assertEqual(value.touch.releases, 1)
        self.assertEqual(value.input_rejected, 1)
        self.assertEqual(value.input_followon_rejected, 0)

    async def test_inactive_slot_is_counted_separately_from_root_rejection(self):
        value = bare_host()
        value.control_owner = object()
        value.control_generation = 4
        value.touch.error = host_module.TouchInputError("touch slot is not active")

        value.handle_touch(
            {"type": "touch", "action": 2, "slot": 8, "x": 0.2, "y": 0.3}, 4)

        self.assertEqual(value.input_rejected, 0)
        self.assertEqual(value.input_followon_rejected, 1)

    async def test_three_finger_swipe_walks_windows_and_never_reaches_the_desktop(self):
        value = gesture_host()

        swipe(value, [0, 1, 2], -0.2)

        self.assertEqual(value.kglobalaccel.invoked, ["Walk Through Windows"])
        self.assertEqual(value.touch.messages, [])
        self.assertEqual(value.gestures_fired, 1)
        self.assertEqual(value.input_rejected, 0)
        self.assertEqual(value.timers, {})

    async def test_four_finger_swipes_switch_desktops_both_ways(self):
        value = gesture_host()

        swipe(value, [0, 1, 2, 3], -0.2)
        swipe(value, [0, 1, 2, 3], 0.2)

        self.assertEqual(value.kglobalaccel.invoked,
                         ["Switch One Desktop to the Right", "Switch One Desktop to the Left"])
        self.assertEqual(value.touch.messages, [])

    async def test_three_finger_swipe_up_opens_the_overview(self):
        value = gesture_host()
        for slot in (0, 1, 2):
            value.handle_touch({"type": "touch", "action": 0, "slot": slot, "x": 0.3 + 0.05 * slot, "y": 0.6}, 4)
        for step in (1, 2, 3):
            for slot in (0, 1, 2):
                value.handle_touch({"type": "touch", "action": 2, "slot": slot,
                                    "x": 0.3 + 0.05 * slot, "y": 0.6 - 0.05 * step}, 4)
        for slot in (0, 1, 2):
            value.handle_touch({"type": "touch", "action": 1, "slot": slot, "x": 0.0, "y": 0.0}, 4)

        self.assertEqual(value.kglobalaccel.invoked, ["Overview"])
        self.assertEqual(value.touch.messages, [])

    async def test_unmapped_swipe_does_nothing(self):
        value = gesture_host()

        self.assertFalse(value.perform_gesture(4, "up"))
        self.assertFalse(value.perform_gesture(5, "left"))

        self.assertEqual(value.kglobalaccel.invoked, [])
        self.assertEqual(value.gestures_fired, 0)

    async def test_single_finger_tap_is_delivered_unchanged(self):
        value = gesture_host()
        down = {"type": "touch", "action": 0, "slot": 0, "x": 0.2, "y": 0.3}
        up = {"type": "touch", "action": 1, "slot": 0, "x": 0.0, "y": 0.0}

        value.handle_touch(down, 4)
        self.assertEqual(value.touch.messages, [])
        value.handle_touch(up, 4)

        self.assertEqual(value.touch.messages, [down, up])
        self.assertEqual(value.kglobalaccel.invoked, [])

    async def test_held_drag_is_delivered_when_the_timer_fires(self):
        value = gesture_host()
        down = {"type": "touch", "action": 0, "slot": 0, "x": 0.2, "y": 0.3}
        value.handle_touch(down, 4)
        (handle, callback), = value.timers.items()

        self.assertFalse(value._input_timer(callback))   # GLib: one-shot
        value.timers.pop(handle)

        self.assertEqual(value.touch.messages, [down])

    async def test_pen_bypasses_the_gesture_filter(self):
        value = gesture_host()
        pen = {"type": "pen", "action": 3, "x": 0.2, "y": 0.3}

        value.handle_touch(pen, 4)

        self.assertEqual(value.touch.messages, [pen])

    async def test_rejected_replay_releases_input_and_resets_the_filter(self):
        value = gesture_host()
        value.touch.error = host_module.TouchInputError("invalid lifecycle")
        down = {"type": "touch", "action": 0, "slot": 0, "x": 0.2, "y": 0.3}
        value.handle_touch(down, 4)
        (handle, callback), = value.timers.items()
        value.timers.pop(handle)   # GLib drops a one-shot as it fires

        value._input_timer(callback)

        self.assertEqual(value.touch.releases, 1)
        self.assertEqual(value.input_rejected, 1)
        self.assertEqual(value.gestures.state, gestures.IDLE)
        self.assertEqual(value.timers, {})

    async def test_release_touch_drops_a_gesture_in_progress(self):
        value = gesture_host()
        for slot in (0, 1, 2):
            value.handle_touch({"type": "touch", "action": 0, "slot": slot, "x": 0.3, "y": 0.5}, 4)
        self.assertEqual(value.gestures.state, gestures.GESTURE)

        value.release_touch()

        self.assertEqual(value.gestures.state, gestures.IDLE)
        self.assertEqual(value.touch.releases, 1)
        self.assertEqual(value.touch.messages, [])


if __name__ == "__main__":
    unittest.main()
