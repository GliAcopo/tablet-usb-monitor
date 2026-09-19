"""Driving the tablet with the laptop's own mouse and keyboard.

Three pieces meet here:

* **KWin's input capture** hands this process every event of the laptop's
  pointer and keyboard (as libei events, see :mod:`eis_receive`) and stops
  the desktop from seeing them at all.  No window is focused, no pointer is
  warped around the screen: while a capture is active the desktop simply has
  no input, which is what makes "the keyboard now belongs to the tablet"
  true rather than a trick.
* **The tablet's receiver** (``scripts/tabs9-remote/Remote.java``) runs over
  ADB as the shell user and turns those events into Android MotionEvents and
  KeyEvents, so they reach whatever is on the tablet's screen, its own
  desktop-mode windows included.
* **A state machine** connecting the two, driven by a KDE global shortcut
  (see :mod:`shortcuts`).

Capture is entered by crossing a *pointer barrier*: KWin activates a capture
when the pointer is pushed against a screen edge that carries one.  A
shortcut cannot ask for capture directly, so the host arms a barrier on one
edge of the tablet's output, parks the pointer on it and nudges it outwards
with its own libei sender — the same motion the user's hand would make.  The
barrier is disarmed again as soon as the capture is active, so nothing can
be entered by accident afterwards (``--remote-edge`` keeps it armed on
purpose).

Getting out is always possible: the shortcut again, KWin's own
"Disable Active Input Capture" (Meta+Shift+Escape by default, handled inside
KWin even while every other key is captured), unplugging the tablet, or the
host exiting.
"""
from __future__ import annotations

import collections
import contextlib
import logging
import os
import socket
import struct
import subprocess
import time
from typing import Callable

import dbus

from eis_receive import EisReceiver
from eis_touch import EisError

log = logging.getLogger(__name__)

# Record layout of the tablet receiver's socket protocol (see Remote.java).
RECORD = struct.Struct('<BBHii')
TYPE_MOVE, TYPE_BUTTON, TYPE_SCROLL, TYPE_KEY, TYPE_RESET = 1, 2, 3, 4, 5

# The receiver greets every connection with these two bytes and a third
# saying how it delivers events ('u': a real mouse and keyboard through
# /dev/uhid, so the tablet draws a pointer; 'i': injected events, which it
# does not). ADB's forward accepts a local connection whether or not
# anything listens on the device, so the greeting is the only way to tell a
# running receiver from a missing one.
HELLO = b'T9'

# Portal capability bits, as KWin's addInputCapture takes them.
CAP_KEYBOARD, CAP_POINTER, CAP_TOUCH = 1, 2, 4

KWIN_SERVICE = 'org.kde.KWin'
KWIN_CAPTURE_PATH = '/org/kde/KWin/EIS/InputCapture'
KWIN_CAPTURE_MANAGER = 'org.kde.KWin.EIS.InputCaptureManager'
KWIN_CAPTURE_IFACE = 'org.kde.KWin.EIS.InputCapture'

# Wheel: libei sends v120 units (120 per notch) for a mouse wheel and smooth
# deltas in logical pixels for a touchpad. Android wants notches, positive
# away from the user, which is the opposite sign to Wayland's.
SMOOTH_PIXELS_PER_NOTCH = 50.0

# While a capture is active KWin's capture filter runs *before* its global
# shortcuts (InputFilterOrder::EisInput is above GlobalShortcut), so no key
# the user presses can reach KDE -- including the shortcut that started all
# this. The way back therefore has to be recognised here, in the captured
# stream, and the keys that make it up are never passed to the tablet.
EVDEV_MODIFIERS = {29: 'ctrl', 97: 'ctrl', 42: 'shift', 54: 'shift',
                   56: 'alt', 100: 'alt', 125: 'meta', 126: 'meta'}
EVDEV_KEYS = {
    'escape': 1, 'backspace': 14, 'tab': 15, 'return': 28, 'enter': 28, 'space': 57,
    'minus': 12, 'equal': 13, 'insert': 110, 'delete': 111, 'home': 102, 'end': 107,
    'pageup': 104, 'pagedown': 109, 'up': 103, 'down': 108, 'left': 105, 'right': 106,
    'print': 99, 'pause': 119, 'menu': 127,
}
EVDEV_KEYS.update({chr(code): value for value, code in zip(
    [30, 48, 46, 32, 18, 33, 34, 35, 23, 36, 37, 38, 50, 49, 24, 25, 16, 19, 31, 20,
     22, 47, 17, 45, 21, 44], range(ord('a'), ord('z') + 1))})
EVDEV_KEYS.update({str(digit): code for digit, code in
                   zip('1234567890', [2, 3, 4, 5, 6, 7, 8, 9, 10, 11])})
EVDEV_KEYS.update({f'f{n}': code for n, code in
                   zip(range(1, 13), [59, 60, 61, 62, 63, 64, 65, 66, 67, 68, 87, 88])})


def parse_chord(text: str) -> tuple[frozenset, int] | None:
    """'Meta+Shift+T' -> ({'meta', 'shift'}, 20), or None if it has no evdev key."""
    parts = [part.strip().lower() for part in str(text).split('+') if part.strip()]
    if not parts:
        return None
    key = EVDEV_KEYS.get(parts[-1])
    if key is None:
        return None
    modifiers = {part for part in parts[:-1] if part in ('ctrl', 'shift', 'alt', 'meta')}
    if len(modifiers) != len(parts) - 1:
        return None
    return frozenset(modifiers), key


class RemoteError(RuntimeError):
    """The tablet's receiver or KWin's capture could not be set up."""


class TabletInjector:
    """The tablet-side input receiver, over an ADB-forwarded socket."""

    def __init__(self, adb: Callable[..., subprocess.CompletedProcess], adb_path: str, *,
                 port: int = 8892, dex: str = '/data/local/tmp/tabs9-remote.dex',
                 socket_name: str = 'tabs9-remote'):
        self.adb = adb
        # A bare path, or [path, '-s', serial] to address one of several tablets.
        self.adb_command = [adb_path] if isinstance(adb_path, str) else list(adb_path)
        self.port = port
        self.dex = dex
        self.socket_name = socket_name
        self.process: subprocess.Popen | None = None
        self.sock: socket.socket | None = None
        self.sent = 0
        # 'u' while the tablet has a real mouse and keyboard, 'i' when the
        # events are injected (no pointer is drawn then).
        self.mode = '?'

    @property
    def alive(self) -> bool:
        # The socket is what matters: the receiver may be one that was already
        # running on the tablet (ours from a previous session, or one started
        # by hand), in which case the process started here exits immediately
        # because the abstract socket is taken.
        return self.sock is not None

    def start(self) -> None:
        """Connect to the receiver on the tablet, starting it if needed."""
        if self.alive:
            return
        self.close_socket()
        self.adb('forward', f'tcp:{self.port}', f'localabstract:{self.socket_name}')
        if self._connect(0.5):
            return
        # Its output goes to logcat (adb logcat -s UScreenRemote); a pipe here
        # would have nobody reading it and would eventually block the receiver.
        self.process = subprocess.Popen(
            [*self.adb_command, 'shell', f'CLASSPATH={self.dex} app_process / Remote {self.socket_name}'],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True)
        if self._connect(5):
            return
        self.stop()
        raise RemoteError(f'no input receiver answered on port {self.port}; push it with '
                          'scripts/tabs9-remote/build-and-push.sh and check '
                          '`adb logcat -s UScreenRemote`')

    def _connect(self, seconds: float) -> bool:
        deadline = time.monotonic() + seconds
        while True:
            try:
                sock = socket.create_connection(('127.0.0.1', self.port), timeout=2)
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                sock.settimeout(2)
                # recv gives what has arrived, not what was asked for.
                greeting = b''
                while len(greeting) < len(HELLO) + 1:
                    piece = sock.recv(len(HELLO) + 1 - len(greeting))
                    if not piece:
                        break
                    greeting += piece
                if greeting[:len(HELLO)] == HELLO:
                    self.mode = greeting[len(HELLO):].decode(errors='replace') or '?'
                    sock.settimeout(1)
                    self.sock = sock
                    log.info('tablet input receiver: %s', {
                        'u': 'a real mouse and keyboard (the tablet draws a pointer)',
                        'i': 'injected events (the tablet draws no pointer)',
                    }.get(self.mode, 'connected'))
                    return True
                sock.close()
            except OSError:
                pass
            self.sock = None
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.1)

    def _send(self, kind: int, flags: int = 0, code: int = 0, a: int = 0, b: int = 0) -> None:
        if self.sock is None:
            return
        try:
            self.sock.sendall(RECORD.pack(kind, flags, code & 0xFFFF, int(a), int(b)))
            self.sent += 1
        except OSError as error:
            log.warning('tablet receiver went away (%s); releasing the input', error)
            self.close_socket()

    def move(self, dx: int, dy: int) -> None:
        """Relative motion, in tablet pixels (a mouse's own units)."""
        self._send(TYPE_MOVE, a=dx, b=dy)

    def button(self, code: int, press: bool) -> None:
        self._send(TYPE_BUTTON, 1 if press else 0, code)

    def scroll(self, vertical: float, horizontal: float) -> None:
        # Thousandths of a notch, so a smooth touchpad scroll survives the trip.
        self._send(TYPE_SCROLL, a=round(vertical * 1000), b=round(horizontal * 1000))

    def key(self, code: int, press: bool) -> None:
        self._send(TYPE_KEY, 1 if press else 0, code)

    def reset(self) -> None:
        self._send(TYPE_RESET)

    def close_socket(self) -> None:
        if self.sock is not None:
            with contextlib.suppress(OSError):
                self.sock.close()
            self.sock = None

    def stop(self) -> None:
        if self.sock is not None:
            with contextlib.suppress(Exception):
                self.reset()
            self.close_socket()
        if self.process is not None:
            with contextlib.suppress(Exception):
                self.process.terminate()
            with contextlib.suppress(Exception):
                self.process.wait(timeout=2)
            self.process = None
        with contextlib.suppress(Exception):
            self.adb('forward', '--remove', f'tcp:{self.port}')


class InputCapture:
    """One KWin input capture, with its libei receiver.

    The capture is asked of KWin directly: the same interface
    xdg-desktop-portal-kde drives on the other side of
    org.freedesktop.portal.InputCapture, reachable only by this session's
    own processes and without a dialog. The portal is the portable path and
    would ask the user each time a session is created; it is not implemented
    here (see docs/pc-to-tablet-control.md).
    """

    def __init__(self, bus, *, source: str = 'kwin',
                 capabilities: int = CAP_KEYBOARD | CAP_POINTER,
                 on_activated: Callable[[float, float], None] | None = None,
                 on_deactivated: Callable[[], None] | None = None):
        self.bus = bus
        self.source = source
        self.capabilities = capabilities
        self.on_activated = on_activated
        self.on_deactivated = on_deactivated
        self.capture = None          # the org.kde.KWin.EIS.InputCapture proxy
        self.receiver: EisReceiver | None = None
        self.active = False
        self.armed = False
        self.activation_position = (0.0, 0.0)
        self._signals = []

    def create(self, handler: Callable[[str, float, float], None]) -> int:
        """Create the capture and its receiver; returns the libei fd to watch."""
        if self.source != 'kwin':
            raise RemoteError(f'unsupported input-capture source {self.source!r}')
        manager = dbus.Interface(self.bus.get_object(KWIN_SERVICE, KWIN_CAPTURE_PATH),
                                 KWIN_CAPTURE_MANAGER)
        path = manager.addInputCapture(dbus.UInt32(self.capabilities))
        self.capture = dbus.Interface(self.bus.get_object(KWIN_SERVICE, path), KWIN_CAPTURE_IFACE)
        self._signals = [
            self.bus.add_signal_receiver(self._activated, signal_name='activated',
                dbus_interface=KWIN_CAPTURE_IFACE, path=str(path)),
            self.bus.add_signal_receiver(self._deactivated, signal_name='deactivated',
                dbus_interface=KWIN_CAPTURE_IFACE, path=str(path)),
        ]
        fd = self.capture.connectToEIS().take()
        try:
            self.receiver = EisReceiver(fd, handler)
        except EisError:
            os.close(fd)
            raise
        return self.receiver.fd

    def _activated(self, activation_id, position):
        self.active = True
        self.activation_position = (float(position[0]), float(position[1]))
        if self.on_activated is not None:
            self.on_activated(*self.activation_position)

    def _deactivated(self, activation_id):
        self.active = False
        if self.on_deactivated is not None:
            self.on_deactivated()

    # Every call into KWin here is made without waiting for the reply: they
    # happen inside D-Bus signal handlers, where blocking would stall this
    # process (and with it the libei socket) until KWin answered.
    @staticmethod
    def _async(call, *args) -> None:
        call(*args, reply_handler=lambda *_: None,
             error_handler=lambda error: log.warning('input capture call failed: %s', error))

    def arm(self, barrier) -> None:
        """Watch one barrier: ((x1, y1), (x2, y2)), a screen-edge segment."""
        if self.capture is None:
            return
        barriers = dbus.Array([dbus.Struct(
            (dbus.Struct((dbus.Int32(barrier[0][0]), dbus.Int32(barrier[0][1])), signature='ii'),
             dbus.Struct((dbus.Int32(barrier[1][0]), dbus.Int32(barrier[1][1])), signature='ii')),
            signature='(ii)(ii)')], signature='((ii)(ii))')
        self._async(self.capture.enable, barriers)
        self.armed = True

    def disarm(self) -> None:
        """Forget every barrier; an active capture keeps running."""
        if self.capture is None:
            return
        self._async(self.capture.enable, dbus.Array([], signature='((ii)(ii))'))
        self.armed = False

    def release(self, position: tuple[float, float] | None = None) -> None:
        """Give the input back to the desktop, optionally restoring the pointer."""
        if self.capture is None or not self.active:
            return
        where = position or self.activation_position
        if position is not None:
            log.info('input capture: giving the pointer back at %s', position)
        self._async(self.capture.release,
                    dbus.Struct((dbus.Double(where[0]), dbus.Double(where[1])), signature='dd'),
                    dbus.Boolean(position is not None))

    def close(self) -> None:
        if self.capture is not None:
            with contextlib.suppress(Exception):
                self.capture.disable()
            self.capture = None
        for match in self._signals:
            with contextlib.suppress(Exception):
                match.remove()
        self._signals = []
        if self.receiver is not None:
            self.receiver.close()
            self.receiver = None
        self.active = False
        self.armed = False


class RemoteControl:
    """Hands the laptop's pointer and keyboard to the tablet, and back.

    The host owns the GLib loop, so it supplies ``watch(fd, pump)`` to poll
    the libei socket, ``nudge(dx, dy)`` to push the pointer across the
    barrier with its own sender, and ``notify`` for what the user should see.
    """

    def __init__(self, injector: TabletInjector, capture: InputCapture, *,
                 panel: tuple[int, int] = (2960, 1848), sensitivity: float = 1.0,
                 edge: str = 'left', barrier: Callable[[str], tuple] | None = None,
                 watch: Callable[[int, Callable[[], bool]], None] | None = None,
                 unwatch: Callable[[], None] | None = None,
                 park: Callable[[float, float], None] | None = None,
                 nudge: Callable[[float, float], None] | None = None,
                 notify: Callable[[str, str], None] | None = None,
                 on_state: Callable[[str], None] | None = None,
                 home: Callable[[], tuple[float, float]] | None = None,
                 release_chord: str = 'Meta+Shift+T'):
        self.injector = injector
        self.capture = capture
        self.panel = panel
        self.sensitivity = sensitivity
        self.edge = edge
        self.barrier = barrier
        self.watch = watch
        self.unwatch = unwatch
        self.park = park
        self.nudge = nudge
        self.notify = notify or (lambda summary, body: None)
        self.on_state = on_state or (lambda state: None)
        self.home = home
        # The key combination that gives the input back, watched for here
        # because KDE cannot see it while the capture is on.
        self.release_chord = parse_chord(release_chord)
        self.release_chord_text = release_chord if self.release_chord else ''
        self.held_modifiers: set[str] = set()
        # Leftover fraction of a tablet pixel, so slow movement is not lost to
        # rounding: the tablet's own pointer keeps the position now.
        self.x = 0.0
        self.y = 0.0
        self.sessions = 0
        self.events = 0
        # How many of each kind, so a live session can be told apart from a
        # capture that is on but receiving nothing.
        self.kinds: collections.Counter = collections.Counter()
        self.keep_armed = edge != 'none'
        capture.on_activated = self._activated
        capture.on_deactivated = self._deactivated

    # -- state ---------------------------------------------------------------
    @property
    def active(self) -> bool:
        return self.capture.active

    def ensure_capture(self) -> None:
        """Create the capture and its receiver (no input is taken yet).

        Done as soon as the host is up rather than on the first shortcut: the
        compositor needs a moment to create the capture's devices, and a
        capture activated before they exist swallows the input instead of
        forwarding it.
        """
        if self.capture.receiver is None:
            fd = self.capture.create(self.handle)
            if self.watch is not None:
                self.watch(fd, self.pump)
        self.wait_for_devices()
        self.rearm()

    def ensure_ready(self) -> None:
        """Everything needed to take the input: the capture and the tablet."""
        self.ensure_capture()
        self.injector.start()

    def wait_for_devices(self, seconds: float = 2.0) -> bool:
        """Let the compositor finish creating the capture's devices.

        KWin creates them when this side binds the seat, one round trip after
        the capture is made. A capture that activates before they exist has
        nowhere to send the input: the desktop stops seeing it and the tablet
        never gets it.
        """
        receiver = self.capture.receiver
        if receiver is None:
            return False
        deadline = time.monotonic() + seconds
        while not receiver.devices and time.monotonic() < deadline:
            receiver.dispatch()
            time.sleep(0.02)
        return bool(receiver.devices)

    def rearm(self) -> None:
        """Put the barrier back on the edge, wherever the screen is now.

        An edge the user asked for stays armed, so pushing the pointer against
        it hands the input over without the shortcut; the screens can be
        rearranged under it, so the segment is recomputed each time. Never
        while a capture is active: KWin stops delivering events if its
        barriers change under it.
        """
        if (not self.keep_armed or self.capture.capture is None
                or self.capture.active or self.barrier is None):
            return
        edge = self.barrier(self.edge)
        if edge is not None:
            self.capture.arm(edge)

    def toggle(self) -> bool:
        """Shortcut pressed: take the input, or give it back. True when capturing."""
        if self.active:
            self.stop()
            return False
        self.start()
        return self.active

    # How many outward pushes the shortcut makes. KWin wants two motions in a
    # row on the barrier (the first only sets its "previous position"), and
    # the pointer may be moving when the shortcut arrives, so a few more cost
    # nothing: once the capture is on they are captured like any other motion
    # and the tablet's pointer is put back where it was anyway.
    NUDGES = 6

    def start(self) -> None:
        """Hand the input over: arm the edge, then push the pointer across it."""
        self.ensure_ready()
        if not self.wait_for_devices():
            raise RemoteError('KWin created no devices for the input capture')
        edge = self.barrier(self.edge if self.edge != 'none' else 'left') if self.barrier else None
        if edge is None:
            raise RemoteError('no screen edge to hand the pointer over at')
        self.capture.arm(edge)
        self.cross(edge)

    def cross(self, edge=None) -> bool:
        """Push the pointer against the barrier, the way a hand would.

        KWin activates a capture when the pointer is already on a barrier and
        a motion carries it further out, which at a screen edge means the
        position does not change and the delta does.
        """
        edge = edge or (self.barrier(self.side) if self.barrier else None)
        if edge is None or self.park is None or self.nudge is None:
            return False
        (x1, y1), (x2, y2) = edge
        outward = {'left': (-30.0, 0.0), 'right': (30.0, 0.0),
                   'top': (0.0, -30.0), 'bottom': (0.0, 30.0)}[self.side]
        self.park((x1 + x2) / 2, (y1 + y2) / 2)
        for _ in range(self.NUDGES):
            self.nudge(*outward)
        return True

    @property
    def side(self) -> str:
        return self.edge if self.edge != 'none' else 'left'

    # How far inside the screen the pointer is put back: on the barrier it
    # would cross it again with the first movement and be captured anew.
    RELEASE_MARGIN = 40

    def stop(self) -> None:
        if self.capture.active:
            self.capture.release(self.release_position())
        self.injector.reset()

    def release_position(self) -> tuple[float, float] | None:
        """Where to leave the pointer when the computer gets it back.

        Not where it was captured: that is on the barrier at the edge of the
        tablet's screen, where the next movement would hand it straight back
        (and where it is of no use anyway). ``home`` is the host's idea of a
        sensible place on the computer's own screen.
        """
        if self.home is not None:
            with contextlib.suppress(Exception):
                return self.home()
        if not self.keep_armed:
            return None
        x, y = self.capture.activation_position
        inward = {'left': (self.RELEASE_MARGIN, 0), 'right': (-self.RELEASE_MARGIN, 0),
                  'top': (0, self.RELEASE_MARGIN), 'bottom': (0, -self.RELEASE_MARGIN)}
        dx, dy = inward.get(self.edge, (0, 0))
        return (x + dx, y + dy)

    def close(self) -> None:
        self.stop()
        if self.unwatch is not None:
            self.unwatch()
        self.capture.close()
        self.injector.stop()

    # -- capture lifecycle ---------------------------------------------------
    def _activated(self, x, y):
        self.sessions += 1
        self.held_modifiers.clear()
        self.on_state('control')
        # The barriers are left alone while the capture is active: KWin's
        # capture is only meant to be enabled and disabled in its inactive
        # state (the portal enforces that), and changing them under it stops
        # the events from being delivered at all. They are cleared on the way
        # out instead, which is when an unwanted barrier would matter.
        self.x = self.y = 0.0
        self.injector.reset()
        self.notify('Tablet has your mouse and keyboard',
                    'Press the shortcut again (or Meta+Shift+Escape) to get them back.')
        log.info('remote control on; the desktop sees no input until it is released')

    def _deactivated(self):
        self.on_state('desktop')
        self.injector.reset()
        with contextlib.suppress(Exception):
            if self.keep_armed:
                self.rearm()
            else:
                # Nothing can be entered by accident until the shortcut arms
                # the edge again.
                self.capture.disarm()
        self.notify('Your mouse and keyboard are back on the computer', '')
        log.info('remote control off')

    # -- event pump ----------------------------------------------------------
    def watch_for_release(self, code: int, press: bool) -> bool:
        """Track modifiers and catch the chord that hands the input back.

        Returns True when the key is the host's own and must not be sent to
        the tablet.
        """
        modifier = EVDEV_MODIFIERS.get(code)
        if modifier is not None:
            if press:
                self.held_modifiers.add(modifier)
            else:
                self.held_modifiers.discard(modifier)
            return False
        if not self.release_chord or not press:
            return False
        modifiers, key = self.release_chord
        if code != key or self.held_modifiers != modifiers:
            return False
        log.info('%s pressed on the captured keyboard: giving the input back',
                 self.release_chord_text)
        self.stop()
        return True

    def pump(self) -> bool:
        if self.capture.receiver is None:
            return False
        self.capture.receiver.dispatch()
        return True

    def handle(self, kind: str, a, b) -> None:
        """One captured event, on its way to the tablet."""
        if not self.injector.alive:
            if self.capture.active:
                log.warning('tablet receiver is gone; releasing the capture')
                self.stop()
            return
        self.events += 1
        self.kinds[kind] += 1
        if kind == 'motion':
            # Whole tablet pixels go now, the fraction waits for the next
            # event; a mouse moved slowly would otherwise not move at all.
            self.x += a * self.sensitivity
            self.y += b * self.sensitivity
            dx, dy = int(self.x), int(self.y)
            self.x -= dx
            self.y -= dy
            if dx or dy:
                self.injector.move(dx, dy)
        elif kind == 'button':
            self.injector.button(int(a), bool(b))
        elif kind == 'discrete':
            # v120: 120 units per wheel notch, positive down/right on Wayland.
            self.injector.scroll(-b / 120.0, a / 120.0)
        elif kind == 'scroll':
            self.injector.scroll(-b / SMOOTH_PIXELS_PER_NOTCH, a / SMOOTH_PIXELS_PER_NOTCH)
        elif kind == 'key':
            if self.watch_for_release(int(a), bool(b)):
                return
            self.injector.key(int(a), bool(b))
        # 'frame' and 'scroll_stop' need no tablet event: Android has no frame
        # concept and a stopped wheel is simply the absence of more scrolls.
