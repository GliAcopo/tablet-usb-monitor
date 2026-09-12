"""Safe, stream-relative touch injection through the RemoteDesktop portal.

This module deliberately does not create portal sessions.  The caller must bind
it to a *combined* RemoteDesktop + ScreenCast session after the user grants it,
and after proving that the selected monitor stream is the virtual output.

UScreen sends normalized coordinates (0.0..1.0) and touch actions 0=down,
1=up, 2=motion. Basic S Pen position and tip clicks can also use the portal's
pointer device; pressure, tilt and eraser semantics require a tablet-tool API
that RemoteDesktop does not provide. XDG RemoteDesktop expects coordinates in the selected stream's
logical coordinate space, so pixel resolution and desktop scale must not be
mixed.  ``LiveKScreenTarget`` re-reads the named output as its layout changes;
the PipeWire node keeps every absolute event relative to that stream, regardless
of whether the output is currently left or right of the laptop display.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import math
import subprocess
import time
from typing import Any, Callable, Mapping, Protocol


KEYBOARD = 1
POINTER = 2
TOUCHSCREEN = 4

TOUCH_DOWN = 0
TOUCH_UP = 1
TOUCH_MOTION = 2

# Linux evdev BTN_LEFT, as required by NotifyPointerButton.
BTN_LEFT = 0x110
BUTTON_RELEASED = 0
BUTTON_PRESSED = 1


class TouchInputError(RuntimeError):
    """The selected stream or a received input event is unsafe to use."""


class Portal(Protocol):
    def NotifyTouchDown(self, session: str, options: Mapping[str, Any], stream: int,
                        slot: int, x: float, y: float) -> None: ...
    def NotifyTouchMotion(self, session: str, options: Mapping[str, Any], stream: int,
                          slot: int, x: float, y: float) -> None: ...
    def NotifyTouchUp(self, session: str, options: Mapping[str, Any], slot: int) -> None: ...
    def NotifyPointerMotionAbsolute(self, session: str, options: Mapping[str, Any],
                                    stream: int, x: float, y: float) -> None: ...
    def NotifyPointerButton(self, session: str, options: Mapping[str, Any],
                            button: int, state: int) -> None: ...


@dataclass(frozen=True)
class OutputGeometry:
    """Current geometry of the one output the input stream is allowed to drive."""

    name: str
    pixel_width: int
    pixel_height: int
    scale: float
    x: int
    y: int

    @property
    def logical_size(self) -> tuple[int, int]:
        # Match kscreen/host.py's conversion for fractional output scale.
        return (round(self.pixel_width / self.scale),
                round(self.pixel_height / self.scale))


class Target(Protocol):
    def geometry(self) -> OutputGeometry: ...


class LiveKScreenTarget:
    """Track one explicitly named KScreen output without retaining screen data."""

    def __init__(self, output_name: str, *, cache_seconds: float = 1.0,
                 read_outputs: Callable[[], list[Mapping[str, Any]]] | None = None):
        if not output_name:
            raise ValueError("output_name must be non-empty")
        self.output_name = output_name
        self.cache_seconds = max(0.0, float(cache_seconds))
        self._read_outputs = read_outputs or self._kscreen_outputs
        self._cached: OutputGeometry | None = None
        self._read_at = -math.inf

    @staticmethod
    def _kscreen_outputs() -> list[Mapping[str, Any]]:
        raw = subprocess.check_output(["kscreen-doctor", "-j"], timeout=2)
        value = json.loads(raw)
        outputs = value.get("outputs")
        if not isinstance(outputs, list):
            raise TouchInputError("KScreen returned no output list")
        return outputs

    def geometry(self) -> OutputGeometry:
        now = time.monotonic()
        if self._cached is not None and now - self._read_at < self.cache_seconds:
            return self._cached

        try:
            current_outputs = self._read_outputs()
        except TouchInputError:
            raise
        except Exception as error:
            # Host callbacks turn TouchInputError into an immediate release of
            # every active slot.  Normalize command/JSON failures here so a
            # transient KScreen failure cannot leave a contact stuck down.
            raise TouchInputError("could not refresh bound output geometry") from error
        matches = [item for item in current_outputs
                   if item.get("name") == self.output_name]
        if len(matches) != 1 or not matches[0].get("enabled", False):
            raise TouchInputError("bound virtual output is missing or disabled")
        item = matches[0]
        modes = item.get("modes")
        if not isinstance(modes, list):
            raise TouchInputError("bound output has no mode list")
        mode = next((candidate for candidate in modes
                     if candidate.get("id") == item.get("currentModeId")), None)
        size = mode.get("size") if isinstance(mode, Mapping) else None
        pos = item.get("pos")
        scale = item.get("scale")
        try:
            geometry = OutputGeometry(
                name=self.output_name,
                pixel_width=int(size["width"]),
                pixel_height=int(size["height"]),
                scale=float(scale),
                x=int(pos["x"]),
                y=int(pos["y"]),
            )
        except (KeyError, TypeError, ValueError, OverflowError) as error:
            raise TouchInputError("bound output geometry is incomplete") from error
        if (geometry.pixel_width <= 0 or geometry.pixel_height <= 0 or
                not math.isfinite(geometry.scale) or geometry.scale <= 0):
            raise TouchInputError("bound output geometry is invalid")
        logical_width, logical_height = geometry.logical_size
        if logical_width <= 0 or logical_height <= 0:
            raise TouchInputError("bound output logical size is invalid")
        self._cached = geometry
        self._read_at = now
        return geometry


class FixedTarget:
    """Small target implementation useful when layout updates are supplied externally."""

    def __init__(self, geometry: OutputGeometry):
        self.current = geometry

    def geometry(self) -> OutputGeometry:
        return self.current


class PortalTouchInput:
    """Validate UScreen touch lifecycles and inject them into one portal stream."""

    def __init__(self, portal: Portal, session_handle: str, stream_id: int,
                 target: Target, granted_devices: int, *, max_slots: int = 16):
        if not session_handle or not isinstance(session_handle, str):
            raise ValueError("session_handle must be a portal object path")
        if isinstance(stream_id, bool) or not isinstance(stream_id, int) or stream_id < 0:
            raise ValueError("stream_id must be an unsigned PipeWire node id")
        if not 1 <= max_slots <= 256:
            raise ValueError("max_slots must be between 1 and 256")
        self.portal = portal
        self.session_handle = session_handle
        self.stream_id = stream_id
        self.target = target
        self.max_slots = max_slots
        self.touch_enabled = bool(granted_devices & TOUCHSCREEN)
        self.pointer_enabled = bool(granted_devices & POINTER)
        self.pointer_fallback = not self.touch_enabled and self.pointer_enabled
        if not self.touch_enabled and not self.pointer_fallback:
            raise TouchInputError("portal granted neither touchscreen nor pointer control")
        self.active_slots: set[int] = set()
        self.pointer_slot: int | None = None
        self.pen_down = False
        # Optional libei backend (see eis_touch.py); when set, touch contacts
        # bypass the portal's NotifyTouch* calls.  Pen/pointer stay on the portal.
        self.touch_backend = None

    @classmethod
    def bind(cls, portal: Portal, session_handle: str, stream: tuple[Any, Mapping[str, Any]],
             target: Target, granted_devices: int, *, max_slots: int = 16) -> "PortalTouchInput":
        """Bind only a monitor stream whose dimensions match the named target.

        The caller must pass the exact stream tuple returned by RemoteDesktop.Start.
        A window/virtual-creation stream, stale output, or dimension mismatch is
        rejected before input can be sent.
        """
        try:
            stream_id, props = stream
            source_type = int(props.get("source_type", 0))
            stream_size = tuple(int(value) for value in props["size"])
        except (KeyError, TypeError, ValueError) as error:
            raise TouchInputError("portal stream metadata is incomplete") from error
        if source_type != 1:
            raise TouchInputError("touch requires an existing monitor stream")
        geometry = target.geometry()
        allowed_sizes = {
            (geometry.pixel_width, geometry.pixel_height),
            geometry.logical_size,
        }
        if stream_size not in allowed_sizes:
            raise TouchInputError("portal stream does not match the bound virtual output")
        if "position" in props:
            try:
                stream_position = tuple(int(value) for value in props["position"])
            except (TypeError, ValueError) as error:
                raise TouchInputError("portal stream position is invalid") from error
            if stream_position != (geometry.x, geometry.y):
                raise TouchInputError("portal stream position does not match the target")
        if "logical_size" in props:
            try:
                portal_logical = tuple(int(value) for value in props["logical_size"])
            except (TypeError, ValueError) as error:
                raise TouchInputError("portal logical stream size is invalid") from error
            if (len(portal_logical) != 2 or
                    any(abs(a - b) > 1 for a, b in zip(portal_logical, geometry.logical_size))):
                raise TouchInputError("portal logical stream size does not match the target")
        return cls(portal, session_handle, int(stream_id), target, granted_devices,
                   max_slots=max_slots)

    @property
    def mode(self) -> str:
        if self.touch_enabled:
            if self.touch_backend is None:
                return "touchscreen-portal-unverified"
            return "touchscreen-eis-ready" if self.touch_backend.ready else "touchscreen-eis-waiting"
        return "pointer-fallback"

    def _position(self, message: Mapping[str, Any]) -> tuple[float, float]:
        x = message.get("x")
        y = message.get("y")
        if (isinstance(x, bool) or isinstance(y, bool) or
                not isinstance(x, (int, float)) or not isinstance(y, (int, float))):
            raise TouchInputError("touch coordinates must be numbers")
        x = float(x)
        y = float(y)
        if not math.isfinite(x) or not math.isfinite(y) or not (0.0 <= x <= 1.0) or not (0.0 <= y <= 1.0):
            raise TouchInputError("touch coordinates must be finite and normalized")
        width, height = self.target.geometry().logical_size
        # The portal rejects x >= width / y >= height. Android can report the
        # inclusive normalized endpoint 1.0, so keep that endpoint inside the
        # stream while preserving sub-pixel precision for every other value.
        return (min(x * width, math.nextafter(float(width), 0.0)),
                min(y * height, math.nextafter(float(height), 0.0)))

    def handle_message(self, message: Mapping[str, Any]) -> bool:
        """Handle one UScreen touch message; return whether it was injected.

        Malformed messages and invalid state transitions raise TouchInputError.
        Other message types return False so the host can route them elsewhere.
        """
        if not isinstance(message, Mapping):
            return False
        if message.get("type") == "pen":
            return self._handle_pen(message)
        if message.get("type") != "touch":
            return False
        action = message.get("action")
        slot = message.get("slot")
        if isinstance(action, bool) or action not in (TOUCH_DOWN, TOUCH_UP, TOUCH_MOTION):
            raise TouchInputError("unsupported touch action")
        if isinstance(slot, bool) or not isinstance(slot, int) or not (0 <= slot < self.max_slots):
            raise TouchInputError("touch slot is out of range")

        if self.touch_enabled:
            return self._handle_touch(action, slot, message)
        return self._handle_pointer(action, slot, message)

    def _handle_touch(self, action: int, slot: int, message: Mapping[str, Any]) -> bool:
        if action == TOUCH_DOWN:
            if slot in self.active_slots:
                raise TouchInputError("touch slot received a duplicate down")
            x, y = self._position(message)
            if self.touch_backend is not None:
                self.touch_backend.down(slot, x, y)
            else:
                self.portal.NotifyTouchDown(self.session_handle, {}, self.stream_id, slot, x, y)
            self.active_slots.add(slot)
            return True
        if slot not in self.active_slots:
            raise TouchInputError("touch slot is not active")
        if action == TOUCH_MOTION:
            x, y = self._position(message)
            if self.touch_backend is not None:
                self.touch_backend.motion(slot, x, y)
            else:
                self.portal.NotifyTouchMotion(self.session_handle, {}, self.stream_id, slot, x, y)
        else:
            self.active_slots.remove(slot)
            if self.touch_backend is not None:
                self.touch_backend.up(slot)
            else:
                self.portal.NotifyTouchUp(self.session_handle, {}, slot)
        return True

    def _handle_pointer(self, action: int, slot: int, message: Mapping[str, Any]) -> bool:
        # A pointer cannot represent multiple contacts.  Ignore secondary slots
        # rather than allowing them to steal or release the primary drag.
        if action == TOUCH_DOWN:
            if self.pointer_slot is not None or self.pen_down:
                return False
            x, y = self._position(message)
            self.portal.NotifyPointerMotionAbsolute(
                self.session_handle, {}, self.stream_id, x, y)
            self.portal.NotifyPointerButton(
                self.session_handle, {}, BTN_LEFT, BUTTON_PRESSED)
            self.pointer_slot = slot
            return True
        if slot != self.pointer_slot:
            return False
        if action == TOUCH_MOTION:
            x, y = self._position(message)
            self.portal.NotifyPointerMotionAbsolute(
                self.session_handle, {}, self.stream_id, x, y)
        else:
            self.portal.NotifyPointerButton(
                self.session_handle, {}, BTN_LEFT, BUTTON_RELEASED)
            self.pointer_slot = None
        return True

    def _handle_pen(self, message: Mapping[str, Any]) -> bool:
        """Map basic S Pen hover/tip input onto the portal pointer device."""
        if not self.pointer_enabled:
            return False
        action = message.get("action")
        if isinstance(action, bool) or not isinstance(action, int):
            raise TouchInputError("unsupported pen action")
        # Side-button, eraser, pressure and tilt do not have faithful mappings
        # in RemoteDesktop. Leave those events unhandled instead of inventing
        # desktop actions the user did not request.
        if action in (5, 6):
            return False
        if action not in (0, 1, 2, 3, 4):
            raise TouchInputError("unsupported pen action")

        if action == 0:  # tip down
            if self.pen_down:
                raise TouchInputError("pen received a duplicate down")
            if self.pointer_slot is not None:
                return False
            x, y = self._position(message)
            self.portal.NotifyPointerMotionAbsolute(
                self.session_handle, {}, self.stream_id, x, y)
            self.portal.NotifyPointerButton(
                self.session_handle, {}, BTN_LEFT, BUTTON_PRESSED)
            self.pen_down = True
            return True
        if action == 2:  # tip motion
            if not self.pen_down:
                raise TouchInputError("pen is not down")
            x, y = self._position(message)
            self.portal.NotifyPointerMotionAbsolute(
                self.session_handle, {}, self.stream_id, x, y)
            return True
        if action == 3:  # hover
            if self.pen_down or self.pointer_slot is not None:
                return False
            x, y = self._position(message)
            self.portal.NotifyPointerMotionAbsolute(
                self.session_handle, {}, self.stream_id, x, y)
            return True
        if action == 1:  # tip up
            if not self.pen_down:
                raise TouchInputError("pen is not down")
            self.portal.NotifyPointerButton(
                self.session_handle, {}, BTN_LEFT, BUTTON_RELEASED)
            self.pen_down = False
            return True

        # Hover exit has no pointer equivalent. If Android omitted tip-up,
        # release here defensively so disconnect is not the only recovery.
        if self.pen_down:
            self.portal.NotifyPointerButton(
                self.session_handle, {}, BTN_LEFT, BUTTON_RELEASED)
            self.pen_down = False
        return True

    def release_all(self) -> None:
        """Best-effort release for websocket disconnect and host shutdown."""
        if self.touch_enabled:
            for slot in sorted(self.active_slots):
                try:
                    if self.touch_backend is not None:
                        self.touch_backend.up(slot)
                    else:
                        self.portal.NotifyTouchUp(self.session_handle, {}, slot)
                except Exception:
                    # Continue releasing the other contacts. The portal session
                    # itself will also be closed by the host after this call.
                    pass
            self.active_slots.clear()
        if self.pointer_slot is not None or self.pen_down:
            try:
                self.portal.NotifyPointerButton(
                    self.session_handle, {}, BTN_LEFT, BUTTON_RELEASED)
            except Exception:
                pass
            self.pointer_slot = None
            self.pen_down = False
