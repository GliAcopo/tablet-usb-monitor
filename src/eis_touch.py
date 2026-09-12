"""Multitouch injection through libei (RemoteDesktop.ConnectToEIS).

Why not the portal's NotifyTouch* calls: xdg-desktop-portal-kde (checked at
6.6.6) never sends `touch_frame` to KWin's fake-input protocol, and Wayland
clients only dispatch touch on a frame, so those touches reach no window.  It
also injects them workspace-global while xdg-desktop-portal validates them
stream-relative.  KWin's EIS backend has neither problem: it exposes one
absolute device with a region per output, and every touch is framed.

Only the sender-side subset of libei is bound, through ctypes, and everything
runs on the caller's thread (libei is not thread-safe).  Coordinates handed to
`down`/`motion` are logical pixels relative to the target output; the region
that matches the target supplies the workspace offset.
"""
from __future__ import annotations

import ctypes
import ctypes.util
import contextlib
from dataclasses import dataclass
import logging
from typing import Callable

log = logging.getLogger(__name__)

EI_DEVICE_CAP_POINTER_ABSOLUTE = 1 << 1
EI_DEVICE_CAP_TOUCH = 1 << 3
EI_DEVICE_CAP_BUTTON = 1 << 5

BTN_LEFT = 0x110  # Linux evdev code; libei buttons use evdev codes.

EI_EVENT_CONNECT = 1
EI_EVENT_DISCONNECT = 2
EI_EVENT_SEAT_ADDED = 3
EI_EVENT_SEAT_REMOVED = 4
EI_EVENT_DEVICE_ADDED = 5
EI_EVENT_DEVICE_REMOVED = 6
EI_EVENT_DEVICE_PAUSED = 7
EI_EVENT_DEVICE_RESUMED = 8


class EisError(RuntimeError):
    """libei is unavailable or the EIS handshake failed."""


@dataclass(frozen=True)
class Region:
    x: int
    y: int
    width: int
    height: int


def _load():
    name = ctypes.util.find_library('ei') or 'libei.so.1'
    try:
        lib = ctypes.CDLL(name)
    except OSError as error:
        raise EisError(f'libei not loadable: {error}') from error
    P = ctypes.c_void_p
    sigs = {
        'ei_new_sender': (P, [P]),
        'ei_unref': (P, [P]),
        'ei_configure_name': (None, [P, ctypes.c_char_p]),
        'ei_setup_backend_fd': (ctypes.c_int, [P, ctypes.c_int]),
        'ei_get_fd': (ctypes.c_int, [P]),
        'ei_dispatch': (None, [P]),
        'ei_get_event': (P, [P]),
        'ei_now': (ctypes.c_uint64, [P]),
        'ei_event_unref': (P, [P]),
        'ei_event_get_type': (ctypes.c_int, [P]),
        'ei_event_get_seat': (P, [P]),
        'ei_event_get_device': (P, [P]),
        'ei_device_get_name': (ctypes.c_char_p, [P]),
        'ei_device_ref': (P, [P]),
        'ei_device_unref': (P, [P]),
        'ei_device_has_capability': (ctypes.c_bool, [P, ctypes.c_int]),
        'ei_device_get_region': (P, [P, ctypes.c_size_t]),
        'ei_region_get_x': (ctypes.c_uint32, [P]),
        'ei_region_get_y': (ctypes.c_uint32, [P]),
        'ei_region_get_width': (ctypes.c_uint32, [P]),
        'ei_region_get_height': (ctypes.c_uint32, [P]),
        'ei_device_start_emulating': (None, [P, ctypes.c_uint32]),
        'ei_device_stop_emulating': (None, [P]),
        'ei_device_frame': (None, [P, ctypes.c_uint64]),
        'ei_device_touch_new': (P, [P]),
        'ei_touch_down': (None, [P, ctypes.c_double, ctypes.c_double]),
        'ei_touch_motion': (None, [P, ctypes.c_double, ctypes.c_double]),
        'ei_touch_up': (None, [P]),
        'ei_touch_unref': (P, [P]),
        'ei_device_pointer_motion_absolute': (None, [P, ctypes.c_double, ctypes.c_double]),
        'ei_device_button_button': (None, [P, ctypes.c_uint32, ctypes.c_bool]),
    }
    for fname, (restype, argtypes) in sigs.items():
        fn = getattr(lib, fname)
        fn.restype = restype
        fn.argtypes = argtypes
    # Variadic, NULL-terminated list of capabilities: only the fixed argument is typed.
    lib.ei_seat_bind_capabilities.restype = None
    lib.ei_seat_bind_capabilities.argtypes = [P]
    return lib


class EisTouch:
    """One libei sender bound to KWin's absolute device, driving one output."""

    def __init__(self, fd: int, target_region: Callable[[], tuple[int, int, int, int]], *,
                 name: str = 'tabs9-usb-display', layout_changed: Callable[[], None] | None = None):
        self.lib = _load()
        self.ei = self.lib.ei_new_sender(None)
        if not self.ei:
            raise EisError('ei_new_sender failed')
        self.lib.ei_configure_name(self.ei, name.encode())
        if self.lib.ei_setup_backend_fd(self.ei, fd) != 0:
            self.lib.ei_unref(self.ei)
            raise EisError('ei_setup_backend_fd failed')
        self.fd = self.lib.ei_get_fd(self.ei)
        self._target_region = target_region
        # KWin re-announces its EIS devices whenever the output layout changes,
        # so device events are the moment to drop any cached KScreen geometry.
        self._layout_changed = layout_changed
        self.device = None
        self.region: Region | None = None
        self.ready = False
        self.connected = False
        self._sequence = 0
        self._touches: dict[int, ctypes.c_void_p] = {}
        self._pen_down = False
        self._devices: dict[int, ctypes.c_void_p] = {}
        self._resumed: set[int] = set()
        self._closed = False

    # -- event pump (call from the thread that owns this object) --------------
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
        elif kind == EI_EVENT_DISCONNECT:
            self._release_contacts()
            self.connected = False
            self.ready = False
            self.device = None
            self.region = None
        elif kind == EI_EVENT_SEAT_ADDED:
            seat = self.lib.ei_event_get_seat(event)
            self.lib.ei_seat_bind_capabilities(seat, ctypes.c_int(EI_DEVICE_CAP_TOUCH),
                ctypes.c_int(EI_DEVICE_CAP_POINTER_ABSOLUTE), ctypes.c_int(EI_DEVICE_CAP_BUTTON),
                ctypes.c_void_p(None))
        elif kind == EI_EVENT_DEVICE_ADDED:
            device = self.lib.ei_event_get_device(event)
            if self.lib.ei_device_has_capability(device, EI_DEVICE_CAP_TOUCH):
                key = self._key(device)
                if key not in self._devices:
                    self._devices[key] = self.lib.ei_device_ref(device)
                name = self.lib.ei_device_get_name(device) or b''
                log.info('EIS touch candidate %r regions: %s',
                         name.decode(errors='replace'), self._regions(device))
                self.refresh_binding(layout_changed=True)
        elif kind == EI_EVENT_DEVICE_REMOVED:
            removed = self.lib.ei_event_get_device(event)
            key = self._key(removed)
            if self.device is not None and key == self._key(self.device):
                self._release_contacts()
                self.device = None
                self.region = None
                self.ready = False
            owned = self._devices.pop(key, None)
            self._resumed.discard(key)
            if owned:
                self.lib.ei_device_unref(owned)
            self.refresh_binding(layout_changed=True)
        elif kind == EI_EVENT_DEVICE_RESUMED:
            device = self.lib.ei_event_get_device(event)
            self._resumed.add(self._key(device))
            self.refresh_binding(layout_changed=True)
        elif kind == EI_EVENT_DEVICE_PAUSED:
            device = self.lib.ei_event_get_device(event)
            key = self._key(device)
            self._resumed.discard(key)
            if self.device is not None and key == self._key(self.device):
                self._release_contacts()
                with contextlib.suppress(Exception):
                    self.lib.ei_device_stop_emulating(self.device)
                self.ready = False

    @staticmethod
    def _key(device) -> int:
        return int(device.value if isinstance(device, ctypes.c_void_p) else device)

    def _regions(self, device) -> list[Region]:
        regions = []
        index = 0
        while True:
            region = self.lib.ei_device_get_region(device, index)
            if not region:
                break
            regions.append(Region(self.lib.ei_region_get_x(region), self.lib.ei_region_get_y(region),
                self.lib.ei_region_get_width(region), self.lib.ei_region_get_height(region)))
            index += 1
        return regions

    def _match_region(self, device) -> Region | None:
        tx, ty, tw, th = self._target_region()
        regions = self._regions(device)
        for region in regions:
            if (region.x, region.y) == (tx, ty) and abs(region.width - tw) <= 1 and abs(region.height - th) <= 1:
                return region
        log.warning('no EIS region matches the target %s; regions: %s', (tx, ty, tw, th), regions)
        return None

    def refresh_binding(self, layout_changed: bool = False) -> bool:
        """Bind the one resumed touch device whose region matches live KScreen state."""
        if layout_changed and self._layout_changed is not None:
            self._layout_changed()
        matches = []
        for key, device in self._devices.items():
            if key in self._resumed:
                region = self._match_region(device)
                if region is not None:
                    matches.append((device, region))
        if len(matches) != 1:
            if self.device is not None:
                self._release_contacts()
            self.device = None
            self.region = None
            self.ready = False
            if len(matches) > 1:
                log.warning('multiple EIS touch devices match the virtual output; refusing to choose')
            return False
        device, region = matches[0]
        changed = self.device is None or self._key(self.device) != self._key(device)
        if changed:
            self._release_contacts()
            self._sequence += 1
            self.lib.ei_device_start_emulating(device, self._sequence)
        self.device, self.region, self.ready = device, region, True
        if changed:
            log.info('EIS device bound; pen (absolute pointer + button): %s', self.pen_capable)
        return True

    @property
    def pen_capable(self) -> bool:
        """Whether the bound device can also carry the S Pen as an absolute pointer."""
        return bool(self.device) and all(self.lib.ei_device_has_capability(self.device, cap)
            for cap in (EI_DEVICE_CAP_POINTER_ABSOLUTE, EI_DEVICE_CAP_BUTTON))

    # -- injection --------------------------------------------------------------
    def _absolute(self, x: float, y: float) -> tuple[float, float]:
        if self.region is None:
            raise EisError('EIS device has no region for the virtual output')
        # Keep the contact strictly inside the region; libei drops outside points.
        ax = min(max(self.region.x + x, self.region.x), self.region.x + self.region.width - 0.001)
        ay = min(max(self.region.y + y, self.region.y), self.region.y + self.region.height - 0.001)
        return ax, ay

    def _require_ready(self):
        if not (self.ready and self.device):
            raise EisError('EIS touch device is not ready')

    def down(self, slot: int, x: float, y: float) -> None:
        # Binding is refreshed on device events; the touch path only retries a
        # binding that is currently missing, without re-reading the layout.
        if not self.ready:
            self.refresh_binding()
        self._require_ready()
        if slot in self._touches:
            raise EisError('duplicate EIS touch slot')
        touch = self.lib.ei_device_touch_new(self.device)
        if not touch:
            raise EisError('ei_device_touch_new failed')
        ax, ay = self._absolute(x, y)
        self.lib.ei_touch_down(touch, ax, ay)
        self._touches[slot] = touch
        self.lib.ei_device_frame(self.device, self.lib.ei_now(self.ei))

    def motion(self, slot: int, x: float, y: float) -> None:
        self._require_ready()
        touch = self._touches.get(slot)
        if touch is None:
            raise EisError('EIS touch slot is not active')
        ax, ay = self._absolute(x, y)
        self.lib.ei_touch_motion(touch, ax, ay)
        self.lib.ei_device_frame(self.device, self.lib.ei_now(self.ei))

    def up(self, slot: int) -> None:
        touch = self._touches.pop(slot, None)
        if touch is None:
            raise EisError('EIS touch slot is not active')
        if self.ready and self.device:
            self.lib.ei_touch_up(touch)
            self.lib.ei_device_frame(self.device, self.lib.ei_now(self.ei))
        self.lib.ei_touch_unref(touch)

    # -- S Pen as an absolute pointer on the same device -----------------------
    def _require_pen(self):
        self._require_ready()
        if not self.pen_capable:
            raise EisError('EIS device has no absolute pointer for the pen')

    def pen_motion(self, x: float, y: float) -> None:
        """Hover or tip motion: move the pointer without changing the button."""
        self._require_pen()
        ax, ay = self._absolute(x, y)
        self.lib.ei_device_pointer_motion_absolute(self.device, ax, ay)
        self.lib.ei_device_frame(self.device, self.lib.ei_now(self.ei))

    def pen_down(self, x: float, y: float) -> None:
        self._require_pen()
        if self._pen_down:
            raise EisError('duplicate EIS pen down')
        ax, ay = self._absolute(x, y)
        self.lib.ei_device_pointer_motion_absolute(self.device, ax, ay)
        self.lib.ei_device_button_button(self.device, BTN_LEFT, True)
        self.lib.ei_device_frame(self.device, self.lib.ei_now(self.ei))
        self._pen_down = True

    def pen_up(self) -> None:
        if not self._pen_down:
            raise EisError('EIS pen is not down')
        self._pen_down = False
        self._require_pen()
        self.lib.ei_device_button_button(self.device, BTN_LEFT, False)
        self.lib.ei_device_frame(self.device, self.lib.ei_now(self.ei))

    def release_all(self) -> None:
        for slot in list(self._touches):
            try:
                self.up(slot)
            except Exception:
                pass
        if self._pen_down:
            try:
                self.pen_up()
            except Exception:
                self._pen_down = False

    def _release_contacts(self) -> None:
        for touch in self._touches.values():
            if self.ready and self.device:
                with contextlib.suppress(Exception):
                    self.lib.ei_touch_up(touch)
            with contextlib.suppress(Exception):
                self.lib.ei_touch_unref(touch)
        if self._pen_down and self.ready and self.device:
            with contextlib.suppress(Exception):
                self.lib.ei_device_button_button(self.device, BTN_LEFT, False)
        if (self._touches or self._pen_down) and self.ready and self.device:
            with contextlib.suppress(Exception):
                self.lib.ei_device_frame(self.device, self.lib.ei_now(self.ei))
        self._touches.clear()
        self._pen_down = False

    def close(self) -> None:
        if self._closed:
            return
        self.release_all()
        if self.device and self.ready:
            try:
                self.lib.ei_device_stop_emulating(self.device)
            except Exception:
                pass
        for device in self._devices.values():
            self.lib.ei_device_unref(device)
        self._devices.clear()
        self._resumed.clear()
        self._closed = True
        self.lib.ei_unref(self.ei)
        self.ei = None
