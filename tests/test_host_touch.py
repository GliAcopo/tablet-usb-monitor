import asyncio
import base64
import os
import pathlib
import tempfile
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
        self.scrolls = []
        self.clicks = []
        self.scroll_capable = True
        self.click_capable = True
        self.pen_down = False
        self.pointer_slot = None

    def handle_message(self, message):
        if self.error is not None:
            raise self.error
        self.messages.append(message)

    def release_all(self):
        self.releases += 1

    def scroll_begin(self, x, y):
        if not self.scroll_capable:
            return False
        self.scrolls.append(("begin", x, y))
        return True

    def scroll(self, dx, dy):
        self.scrolls.append(("move", round(dx, 4), round(dy, 4)))

    def scroll_end(self):
        self.scrolls.append(("end",))

    def click(self, x, y, button=0x111):
        if not self.click_capable or self.pen_down:
            return False
        self.clicks.append((round(x, 4), round(y, 4), button))
        return True


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
    value.scrolls = 0
    value.scroll_sign = -1.0
    value.scroll_gain = 1.0
    value.right_clicks = 0
    value.pen_buttons = 0
    value.pen_button = 'launcher'
    value.shortcut_components = {}
    value.clip = None
    value.clips = 0
    value.pen_gestures = 0
    value.air = None
    value.tablet_mode = 'screen'
    value.remote = None
    value.clipboard = []
    value.set_clipboard = lambda mime, data: value.clipboard.append((mime, data))
    return value


def clip_pieces(transfer_id, mime, payload, chunk=5):
    pieces = []
    for i, offset in enumerate(range(0, len(payload), chunk)):
        part = payload[offset:offset + chunk]
        pieces.append({"type": "clip", "id": transfer_id, "seq": i, "mime": mime, "size": len(payload),
                       "data": base64.b64encode(part).decode(), "last": offset + chunk >= len(payload)})
    return pieces


class FakeShortcuts:
    def __init__(self):
        self.invoked = []

    def invokeShortcut(self, name, reply_handler=None, error_handler=None):
        self.invoked.append(name)


def with_shortcuts(value):
    """Both kglobalaccel components answer to one recorder."""
    value.shortcuts = FakeShortcuts()
    value.shortcut_components = {'kwin': value.shortcuts, 'plasmashell': value.shortcuts}
    return value


def gesture_host():
    """A host whose touch path goes through the gesture filter (timers by hand)."""
    value = bare_host()
    value.pen_actions = dict(host_module.DEFAULT_PEN_ACTIONS)
    value.air = host_module.AirGestures(value.perform_pen_gesture)
    value.control_owner = object()
    value.control_generation = 4
    with_shortcuts(value)
    value.timers = {}

    def schedule(ms, callback):
        handle = len(value.timers) + 1
        value.timers[handle] = callback
        return handle

    value.gestures = host_module.GestureFilter(
        value._deliver_input, value.perform_gesture,
        schedule=schedule, cancel=value.timers.pop, hold_ms=120,
        scroll=value.perform_scroll, tap=value.perform_right_click)
    return value


def two_finger_drag(value, dy, steps=3):
    """Two fingers land, the hold window passes, they move together by dy."""
    for slot in (0, 1):
        value.handle_touch({"type": "touch", "action": 0, "slot": slot, "x": 0.3 + 0.1 * slot, "y": 0.5}, 4)
    (handle, callback), = value.timers.items()
    value.timers.pop(handle)
    value._input_timer(callback)
    for step in range(1, steps + 1):
        for slot in (0, 1):
            value.handle_touch({"type": "touch", "action": 2, "slot": slot,
                                "x": 0.3 + 0.1 * slot, "y": 0.5 + dy * step / steps}, 4)


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

    async def test_a_second_authenticated_controller_replaces_the_first(self):
        """The newcomer is the live app instance; the old socket is a ghost
        (Android recreated the activity and the old connection lived on).
        Rejecting the newcomer left the tablet reconnecting every 2 s."""
        value = bare_host()
        first = FakeWebSocket(value.token)
        first_task = asyncio.create_task(value.control(first))
        await self._settle()
        self.glib.drain()  # begin_control: clean slate for the accepted owner
        self.assertIs(value.control_owner, first)
        self.assertEqual(value.touch.releases, 1)

        second = FakeWebSocket(value.token)
        second_task = asyncio.create_task(value.control(second))
        await self._settle()
        self.glib.drain()

        self.assertIs(value.control_owner, second)
        self.assertIsNotNone(first.closed)
        self.assertEqual(first.closed[1].get("code"), 1000)
        self.assertIsNone(second.closed)
        self.assertEqual(len(second.sent), 1)                  # greeted like any owner
        self.assertEqual(value.touch.releases, 2)              # clean slate for the newcomer

        # The ghost going away must not release the newcomer's ownership.
        first.stop()
        await first_task
        self.glib.drain()
        self.assertIs(value.control_owner, second)
        self.assertEqual(value.touch.releases, 2)

        second.stop()
        await second_task
        self.glib.drain()
        self.assertIsNone(value.control_owner)
        self.assertEqual(value.touch.releases, 3)

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

    async def test_clip_pieces_are_reassembled_onto_the_clipboard_and_acknowledged(self):
        value = bare_host()
        ws = FakeWebSocket(value.token)
        task = asyncio.create_task(value.control(ws))
        await self._settle()
        self.assertIn('clipboard', ws.sent[0]['features'])

        for piece in clip_pieces(7, "image/png", b"\x89PNG-twelve-bytes"):
            ws.push(piece)
        await self._settle()
        ws.stop()
        await task
        self.glib.drain()

        self.assertEqual(value.clipboard, [("image/png", b"\x89PNG-twelve-bytes")])
        self.assertEqual(value.clips, 1)
        self.assertEqual(ws.sent[-1], {"type": "clip_ack", "id": 7, "ok": True, "bytes": 17})
        self.assertEqual(len(ws.sent), 2)   # nothing said about the pieces before the last

    async def test_clip_transfer_rejects_bad_types_order_and_size(self):
        value = bare_host()
        text = clip_pieces(1, "text/plain", b"hello world", chunk=4)

        reply = await value.receive_clip({**text[0], "mime": "application/x-sh"})
        self.assertFalse(reply["ok"])
        self.assertIn("not accepted", reply["error"])

        self.assertIsNone(await value.receive_clip(text[0]))
        reply = await value.receive_clip(text[2])            # piece 1 missing
        self.assertEqual(reply, {"type": "clip_ack", "id": 1, "ok": False, "error": "clip pieces out of order"})
        self.assertIsNone(value.clip)

        big = dict(text[0], size=host_module.CLIP_MAX_BYTES + 1)
        self.assertFalse((await value.receive_clip(big))["ok"])
        short = dict(text[0], last=True)
        self.assertIn("shorter", (await value.receive_clip(short))["error"])
        self.assertFalse((await value.receive_clip({"type": "clip", "id": "x"}))["ok"])
        self.assertFalse((await value.receive_clip(dict(text[0], data="not base64!")))["ok"])

        # A whole transfer after all that still works, and a wl-copy failure is reported.
        for piece in text[:-1]:
            self.assertIsNone(await value.receive_clip(piece))
        self.assertTrue((await value.receive_clip(text[-1]))["ok"])
        self.assertEqual(value.clipboard, [("text/plain", b"hello world")])

        def broken(mime, data):
            raise RuntimeError("wl-copy is missing")
        value.set_clipboard = broken
        for piece in text[:-1]:
            await value.receive_clip(piece)
        reply = await value.receive_clip(text[-1])
        self.assertEqual(reply["error"], "wl-copy is missing")
        self.assertEqual(value.clips, 1)

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

        self.assertEqual(value.shortcuts.invoked, ["Walk Through Windows"])
        self.assertEqual(value.touch.messages, [])
        self.assertEqual(value.gestures_fired, 1)
        self.assertEqual(value.input_rejected, 0)
        self.assertEqual(value.timers, {})

    async def test_four_finger_swipes_switch_desktops_both_ways(self):
        value = gesture_host()

        swipe(value, [0, 1, 2, 3], -0.2)
        swipe(value, [0, 1, 2, 3], 0.2)

        self.assertEqual(value.shortcuts.invoked,
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

        self.assertEqual(value.shortcuts.invoked, ["Overview"])
        self.assertEqual(value.touch.messages, [])

    async def test_unmapped_swipe_does_nothing(self):
        value = gesture_host()

        self.assertFalse(value.perform_gesture(4, "up"))
        self.assertFalse(value.perform_gesture(5, "left"))

        self.assertEqual(value.shortcuts.invoked, [])
        self.assertEqual(value.gestures_fired, 0)

    async def test_single_finger_tap_is_delivered_unchanged(self):
        value = gesture_host()
        down = {"type": "touch", "action": 0, "slot": 0, "x": 0.2, "y": 0.3}
        up = {"type": "touch", "action": 1, "slot": 0, "x": 0.0, "y": 0.0}

        value.handle_touch(down, 4)
        self.assertEqual(value.touch.messages, [])
        value.handle_touch(up, 4)

        self.assertEqual(value.touch.messages, [down, up])
        self.assertEqual(value.shortcuts.invoked, [])

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

    async def test_two_finger_drag_scrolls_naturally_and_never_reaches_the_desktop(self):
        value = gesture_host()

        two_finger_drag(value, 0.3)
        for slot in (0, 1):
            value.handle_touch({"type": "touch", "action": 1, "slot": slot, "x": 0.0, "y": 0.0}, 4)

        self.assertEqual(value.touch.scrolls[0], ("begin", 0.35, 0.5))
        self.assertEqual(value.touch.scrolls[-1], ("end",))
        moves = [s for s in value.touch.scrolls if s[0] == "move"]
        # Fingers moved down 0.3 in total: content follows them, so the axis is negative.
        self.assertAlmostEqual(sum(m[2] for m in moves), -0.3, places=3)
        self.assertTrue(all(m[1] == 0.0 for m in moves))
        self.assertEqual(value.touch.messages, [])
        self.assertEqual(value.scrolls, 1)
        self.assertEqual(value.shortcuts.invoked, [])
        self.assertEqual(value.gestures.state, gestures.IDLE)

    async def test_scroll_direction_and_gain_are_applied(self):
        value = gesture_host()
        value.scroll_sign = 1.0
        value.scroll_gain = 2.0

        two_finger_drag(value, 0.3)

        moves = [s for s in value.touch.scrolls if s[0] == "move"]
        self.assertAlmostEqual(sum(m[2] for m in moves), 0.6, places=3)

    async def test_two_fingers_reach_the_desktop_when_the_session_cannot_scroll(self):
        value = gesture_host()
        value.touch.scroll_capable = False

        two_finger_drag(value, 0.3)

        self.assertEqual(value.touch.scrolls, [])
        self.assertEqual(value.scrolls, 0)
        self.assertEqual([m["action"] for m in value.touch.messages[:2]], [0, 0])
        self.assertEqual(len(value.touch.messages), 8)
        self.assertEqual(value.gestures.state, gestures.PASS)

    async def test_two_finger_tap_is_a_right_click_and_never_reaches_the_desktop(self):
        value = gesture_host()
        for slot in (0, 1):
            value.handle_touch({"type": "touch", "action": 0, "slot": slot, "x": 0.3 + 0.1 * slot, "y": 0.5}, 4)
        for slot in (1, 0):
            value.handle_touch({"type": "touch", "action": 1, "slot": slot, "x": 0.0, "y": 0.0}, 4)

        self.assertEqual(value.touch.clicks, [(0.35, 0.5, 0x111)])
        self.assertEqual(value.touch.messages, [])
        self.assertEqual(value.touch.scrolls, [])
        self.assertEqual(value.right_clicks, 1)
        self.assertEqual(value.gestures.state, gestures.IDLE)
        self.assertEqual(value.timers, {})

    async def test_two_finger_tap_reaches_the_desktop_when_the_session_cannot_click(self):
        value = gesture_host()
        value.touch.click_capable = False
        for slot in (0, 1):
            value.handle_touch({"type": "touch", "action": 0, "slot": slot, "x": 0.3 + 0.1 * slot, "y": 0.5}, 4)
        for slot in (1, 0):
            value.handle_touch({"type": "touch", "action": 1, "slot": slot, "x": 0.0, "y": 0.0}, 4)

        self.assertEqual(value.touch.clicks, [])
        self.assertEqual(value.right_clicks, 0)
        self.assertEqual([(m["action"], m["slot"]) for m in value.touch.messages],
                         [(0, 0), (0, 1), (1, 1), (1, 0)])
        self.assertEqual(value.gestures.state, gestures.IDLE)

    async def test_pen_button_click_while_hovering_opens_the_launcher(self):
        value = gesture_host()
        hover = {"type": "pen", "action": 3, "x": 0.2, "y": 0.3}

        value.handle_touch(hover, 4)
        value.handle_touch({"type": "pen", "action": 5, "x": 0.0, "y": 0.0}, 4)
        value.handle_touch({"type": "pen", "action": 6, "x": 0.0, "y": 0.0}, 4)

        self.assertEqual(value.shortcuts.invoked, ["activate application launcher"])
        self.assertEqual(value.pen_buttons, 1)
        self.assertEqual(value.pen_gestures, 1)
        # The button messages are not input: only the hover reached the pointer.
        self.assertEqual(value.touch.messages, [hover])

    async def test_air_motion_between_button_presses_is_a_gesture(self):
        value = gesture_host()

        value.handle_touch({"type": "pen", "action": 5, "x": 0.0, "y": 0.0}, 4)
        for _ in range(5):
            value.handle_air({"type": "air", "dx": 0.0, "dy": -0.5}, 4)
        value.handle_touch({"type": "pen", "action": 6, "x": 0.0, "y": 0.0}, 4)

        self.assertEqual(value.shortcuts.invoked, ["Overview"])   # 'up' in the defaults
        self.assertEqual(value.pen_gestures, 1)

    async def test_pen_button_with_the_tip_down_is_left_alone(self):
        value = gesture_host()
        value.touch.pen_down = True

        value.handle_touch({"type": "pen", "action": 5, "x": 0.0, "y": 0.0}, 4)
        value.handle_touch({"type": "pen", "action": 6, "x": 0.0, "y": 0.0}, 4)

        self.assertEqual(value.shortcuts.invoked, [])
        self.assertEqual(value.pen_buttons, 0)

    async def test_pen_button_off_ignores_the_press(self):
        value = gesture_host()
        value.air = None

        value.handle_touch({"type": "pen", "action": 5, "x": 0.0, "y": 0.0}, 4)

        self.assertEqual(value.shortcuts.invoked, [])
        self.assertEqual(value.touch.messages, [])

    async def test_a_gesture_bound_to_a_command_runs_it(self):
        value = gesture_host()
        marker = pathlib.Path(tempfile.gettempdir()) / f"tabs9-pen-{os.getpid()}"
        marker.unlink(missing_ok=True)
        value.pen_actions = {"click": f"exec:touch {marker}"}

        value.handle_touch({"type": "pen", "action": 5, "x": 0.0, "y": 0.0}, 4)
        value.handle_touch({"type": "pen", "action": 6, "x": 0.0, "y": 0.0}, 4)

        for _ in range(50):
            if marker.exists():
                break
            await asyncio.sleep(0.02)
        self.assertTrue(marker.exists())
        marker.unlink(missing_ok=True)
        self.assertEqual(value.shortcuts.invoked, [])

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
