"""Receiving the laptop's own mouse and keyboard through libei (input capture).

The mirror image of :mod:`eis_touch`: there the host *sends* events to KWin,
here KWin sends the host every event of a captured pointer and keyboard so
they can be forwarded to the tablet.  KWin's input-capture context hands out
a libei receiver socket (through the InputCapture portal, or directly over
``org.kde.KWin.EIS.InputCaptureManager``); while a capture is active the
desktop itself sees no input at all, which is what makes "the PC's keyboard
now belongs to the tablet" honest rather than a window stealing focus.

Only the receiver-side subset of libei is bound, through ctypes, and
everything runs on the caller's thread (libei is not thread-safe).
"""
from __future__ import annotations

import ctypes
import ctypes.util
import logging
from typing import Callable

from eis_touch import (EisError, EI_DEVICE_CAP_BUTTON, EI_DEVICE_CAP_KEYBOARD,
                       EI_DEVICE_CAP_POINTER, EI_DEVICE_CAP_POINTER_ABSOLUTE,
                       EI_DEVICE_CAP_SCROLL, EI_DEVICE_CAP_TOUCH,
                       EI_EVENT_CONNECT, EI_EVENT_DISCONNECT, EI_EVENT_SEAT_ADDED)

log = logging.getLogger(__name__)

EI_EVENT_DEVICE_ADDED = 5
EI_EVENT_DEVICE_REMOVED = 6
EI_EVENT_DEVICE_PAUSED = 7
EI_EVENT_DEVICE_RESUMED = 8
EI_EVENT_FRAME = 100
EI_EVENT_DEVICE_START_EMULATING = 200
EI_EVENT_DEVICE_STOP_EMULATING = 201
EI_EVENT_POINTER_MOTION = 300
EI_EVENT_POINTER_MOTION_ABSOLUTE = 400
EI_EVENT_BUTTON_BUTTON = 500
EI_EVENT_SCROLL_DELTA = 600
EI_EVENT_SCROLL_STOP = 601
EI_EVENT_SCROLL_CANCEL = 602
EI_EVENT_SCROLL_DISCRETE = 603
EI_EVENT_KEYBOARD_KEY = 700

# What a capture can carry: everything KWin puts on its capture devices.
RECEIVE_CAPABILITIES = (EI_DEVICE_CAP_POINTER, EI_DEVICE_CAP_POINTER_ABSOLUTE,
                        EI_DEVICE_CAP_BUTTON, EI_DEVICE_CAP_SCROLL,
                        EI_DEVICE_CAP_KEYBOARD, EI_DEVICE_CAP_TOUCH)


def _load():
    name = ctypes.util.find_library('ei') or 'libei.so.1'
    try:
        lib = ctypes.CDLL(name)
    except OSError as error:
        raise EisError(f'libei not loadable: {error}') from error
    P = ctypes.c_void_p
    sigs = {
        'ei_new_receiver': (P, [P]),
        'ei_unref': (P, [P]),
        'ei_configure_name': (None, [P, ctypes.c_char_p]),
        'ei_setup_backend_fd': (ctypes.c_int, [P, ctypes.c_int]),
        'ei_get_fd': (ctypes.c_int, [P]),
        'ei_dispatch': (None, [P]),
        'ei_get_event': (P, [P]),
        'ei_event_unref': (P, [P]),
        'ei_event_get_type': (ctypes.c_int, [P]),
        'ei_event_get_seat': (P, [P]),
        'ei_event_get_device': (P, [P]),
        'ei_seat_has_capability': (ctypes.c_bool, [P, ctypes.c_int]),
        'ei_event_pointer_get_dx': (ctypes.c_double, [P]),
        'ei_event_pointer_get_dy': (ctypes.c_double, [P]),
        'ei_event_pointer_get_absolute_x': (ctypes.c_double, [P]),
        'ei_event_pointer_get_absolute_y': (ctypes.c_double, [P]),
        'ei_event_button_get_button': (ctypes.c_uint32, [P]),
        'ei_event_button_get_is_press': (ctypes.c_bool, [P]),
        'ei_event_scroll_get_dx': (ctypes.c_double, [P]),
        'ei_event_scroll_get_dy': (ctypes.c_double, [P]),
        'ei_event_scroll_get_discrete_dx': (ctypes.c_int32, [P]),
        'ei_event_scroll_get_discrete_dy': (ctypes.c_int32, [P]),
        'ei_event_scroll_get_stop_x': (ctypes.c_bool, [P]),
        'ei_event_scroll_get_stop_y': (ctypes.c_bool, [P]),
        'ei_event_keyboard_get_key': (ctypes.c_uint32, [P]),
        'ei_event_keyboard_get_key_is_press': (ctypes.c_bool, [P]),
    }
    for fname, (restype, argtypes) in sigs.items():
        fn = getattr(lib, fname)
        fn.restype = restype
        fn.argtypes = argtypes
    lib.ei_seat_bind_capabilities.restype = None
    lib.ei_seat_bind_capabilities.argtypes = [P]
    return lib


class EisReceiver:
    """One libei receiver on an input-capture socket.

    ``handler(kind, a, b)`` is called for each event, with:

    ``('motion', dx, dy)``        relative pointer motion, in logical pixels
    ``('absolute', x, y)``        absolute pointer motion (KWin does not send
                                  these for captures, handled for completeness)
    ``('button', code, press)``   evdev button code, press is a bool
    ``('scroll', dx, dy)``        smooth scroll, in logical pixels
    ``('discrete', dx, dy)``      wheel clicks, in v120 units
    ``('scroll_stop', x, y)``     which axes stopped (bools)
    ``('key', code, press)``      evdev key code, press is a bool
    ``('frame', 0, 0)``           end of one event group
    """

    def __init__(self, fd: int, handler: Callable[[str, float, float], None], *,
                 name: str = 'tabs9-remote'):
        self.lib = _load()
        self.handler = handler
        self.ei = self.lib.ei_new_receiver(None)
        if not self.ei:
            raise EisError('ei_new_receiver failed')
        self.lib.ei_configure_name(self.ei, name.encode())
        if self.lib.ei_setup_backend_fd(self.ei, fd) != 0:
            self.lib.ei_unref(self.ei)
            self.ei = None
            raise EisError('ei_setup_backend_fd failed (receiver)')
        self.fd = self.lib.ei_get_fd(self.ei)
        self.connected = False
        self.events = 0
        # The devices the compositor created for this capture. Until at least
        # one exists there is nothing for it to send captured events on: a
        # capture activated before then swallows the input instead of
        # forwarding it, so the host waits for this.
        self.devices: set[int] = set()
        self._closed = False

    def dispatch(self) -> None:
        if self._closed:
            return
        self.lib.ei_dispatch(self.ei)
        while True:
            event = self.lib.ei_get_event(self.ei)
            if not event:
                break
            try:
                self._handle(event)
            finally:
                self.lib.ei_event_unref(event)

    def _handle(self, event) -> None:
        kind = self.lib.ei_event_get_type(event)
        if kind == EI_EVENT_CONNECT:
            self.connected = True
            return
        if kind == EI_EVENT_DISCONNECT:
            self.connected = False
            return
        if kind == EI_EVENT_SEAT_ADDED:
            seat = self.lib.ei_event_get_seat(event)
            caps = [cap for cap in RECEIVE_CAPABILITIES
                    if self.lib.ei_seat_has_capability(seat, cap)]
            if caps:
                self.lib.ei_seat_bind_capabilities(
                    seat, *[ctypes.c_int(cap) for cap in caps], ctypes.c_void_p(None))
            log.info('input capture seat bound: %s', caps)
            return
        if kind == EI_EVENT_DEVICE_ADDED:
            self.devices.add(self._key(self.lib.ei_event_get_device(event)))
            log.info('input capture device %d of %d', len(self.devices), 3)
            return
        if kind == EI_EVENT_DEVICE_REMOVED:
            self.devices.discard(self._key(self.lib.ei_event_get_device(event)))
            return
        if kind in (EI_EVENT_DEVICE_START_EMULATING, EI_EVENT_DEVICE_STOP_EMULATING,
                    EI_EVENT_DEVICE_PAUSED, EI_EVENT_DEVICE_RESUMED):
            return
        self.events += 1
        if kind == EI_EVENT_POINTER_MOTION:
            self.handler('motion', self.lib.ei_event_pointer_get_dx(event),
                         self.lib.ei_event_pointer_get_dy(event))
        elif kind == EI_EVENT_POINTER_MOTION_ABSOLUTE:
            self.handler('absolute', self.lib.ei_event_pointer_get_absolute_x(event),
                         self.lib.ei_event_pointer_get_absolute_y(event))
        elif kind == EI_EVENT_BUTTON_BUTTON:
            self.handler('button', self.lib.ei_event_button_get_button(event),
                         bool(self.lib.ei_event_button_get_is_press(event)))
        elif kind == EI_EVENT_SCROLL_DELTA:
            self.handler('scroll', self.lib.ei_event_scroll_get_dx(event),
                         self.lib.ei_event_scroll_get_dy(event))
        elif kind == EI_EVENT_SCROLL_DISCRETE:
            self.handler('discrete', self.lib.ei_event_scroll_get_discrete_dx(event),
                         self.lib.ei_event_scroll_get_discrete_dy(event))
        elif kind in (EI_EVENT_SCROLL_STOP, EI_EVENT_SCROLL_CANCEL):
            self.handler('scroll_stop', bool(self.lib.ei_event_scroll_get_stop_x(event)),
                         bool(self.lib.ei_event_scroll_get_stop_y(event)))
        elif kind == EI_EVENT_KEYBOARD_KEY:
            self.handler('key', self.lib.ei_event_keyboard_get_key(event),
                         bool(self.lib.ei_event_keyboard_get_key_is_press(event)))
        elif kind == EI_EVENT_FRAME:
            self.handler('frame', 0, 0)
        else:
            self.events -= 1

    @staticmethod
    def _key(device) -> int:
        return int(device.value if isinstance(device, ctypes.c_void_p) else device)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self.ei is not None:
            self.lib.ei_unref(self.ei)
            self.ei = None
