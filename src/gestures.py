"""Two-finger scrolling and three-/four-finger swipes, recognised on the host.

The tablet forwards every contact as it happens; KWin only gets to see them
here, so this is the one place a multi-finger swipe can be kept off the
desktop.  The desktop must never see the first fingers of a swipe: a contact
that goes down and is lifted a few milliseconds later is a tap to every
toolkit, and a three-finger swipe would click whatever it started on.  So
contacts are held back for a short window (``hold_ms``) after the first one
lands.  A third finger inside that window makes the sequence a gesture and
nothing from it is delivered; otherwise the held contacts are replayed, in
order, and the rest of the sequence goes straight through.  A tap or lift
inside the window is replayed immediately, so clicks are not delayed at all;
a drag starts on the desktop ``hold_ms`` late and then catches up.

KWin's own touchscreen gestures do the same job for a locally attached panel
(GlobalShortcutFilter, 250 ms between fingers) but cover three fingers only
and cannot be remapped; they never fire through this path because the
gesture's contacts are not forwarded.

Two fingers are classified by how they move rather than when they land: once
the hold window has passed with exactly two contacts down, they stay held
until their mean position has travelled ``scroll_threshold`` (a scroll: the
contacts are consumed and the travel goes to ``scroll`` as pointer-axis
deltas) or their spacing has changed by that much (a pinch: the contacts are
replayed and the desktop gets its two-finger gesture as usual).  Two
fingers that lift without having moved that far are a two-finger tap: it
goes to ``tap`` (a right click on the host) and nothing reaches the desktop.

The recogniser is pure: the host supplies delivery, the actions, the clock
and a one-shot timer, so the state machine is exercised in tests without
GLib or a portal.
"""
from __future__ import annotations

import math
import time
from typing import Any, Callable, Mapping

TOUCH_DOWN = 0
TOUCH_UP = 1
TOUCH_MOTION = 2

# Normalised travel (fraction of the tablet's width or height) of the
# fingers' mean position that fires the swipe: about 20 mm on the Tab S9
# Ultra's 285 mm panel.
DEFAULT_THRESHOLD = 0.07
# Fingers must all be down within this many ms of the first for the
# sequence to be a gesture; it is also the most a single-finger drag is
# delayed. KWin's own touchscreen gestures allow 250 ms between fingers.
DEFAULT_HOLD_MS = 120
# Travel of two fingers' mean position (or change of their spacing) that
# tells a scroll from a pinch: about 3 mm on the Tab S9 Ultra's panel.
DEFAULT_SCROLL_THRESHOLD = 0.01

IDLE = 'idle'        # no contact down
HOLD = 'hold'        # contacts buffered until the sequence is classified
PASS = 'pass'        # ordinary touches, forwarded as they arrive
GESTURE = 'gesture'  # a swipe: contacts are consumed here
TWO = 'two'          # two fingers, still held: scroll or pinch is undecided
SCROLL = 'scroll'    # two fingers scrolling: their travel goes to ``scroll``


class GestureFilter:
    """Route tablet touch messages: ordinary ones on, swipes to ``act``,
    two-finger scrolls to ``scroll``.

    ``scroll(phase, x, y)`` is called with ``'begin'`` and the fingers' mean
    landing point (the scroll is aimed there), then ``'move'`` with the mean
    travel since the previous call, then ``'end'`` (zeros) when the first
    finger lifts; all in normalised screen units. A false result from
    ``'begin'`` means the host cannot scroll and the contacts are replayed
    as touches instead. ``None`` leaves two fingers to the desktop.

    ``tap(x, y)`` is called with the mean landing point of two fingers that
    lift without moving; a false result replays them as the two taps they
    were. ``None`` leaves two-finger taps to the desktop.
    """

    def __init__(self, forward: Callable[[Mapping[str, Any]], Any],
                 act: Callable[[int, str], Any], *,
                 schedule: Callable[[int, Callable[[], None]], Any],
                 cancel: Callable[[Any], None],
                 now: Callable[[], float] = time.monotonic,
                 hold_ms: int = DEFAULT_HOLD_MS, threshold: float = DEFAULT_THRESHOLD,
                 min_fingers: int = 3, max_fingers: int = 4,
                 scroll: Callable[[str, float, float], Any] | None = None,
                 scroll_threshold: float = DEFAULT_SCROLL_THRESHOLD,
                 tap: Callable[[float, float], Any] | None = None):
        if not 1 <= hold_ms <= 1000:
            raise ValueError('hold_ms must be between 1 and 1000')
        if not 0.0 < threshold <= 1.0 or not 0.0 < scroll_threshold <= 1.0:
            raise ValueError('threshold must be a fraction of the screen')
        if not 2 <= min_fingers <= max_fingers <= 10:
            raise ValueError('finger counts must satisfy 2 <= min <= max <= 10')
        if (scroll is not None or tap is not None) and min_fingers <= 2:
            raise ValueError('two-finger scrolling and taps need swipes of three fingers or more')
        self.forward = forward
        self.act = act
        self.scroll = scroll
        self.tap = tap
        self.scroll_threshold = scroll_threshold
        self.schedule = schedule
        self.cancel = cancel
        self.now = now
        self.hold_ms = hold_ms
        self.threshold = threshold
        self.min_fingers = min_fingers
        self.max_fingers = max_fingers
        self.state = IDLE
        self.recognised = 0
        self.scrolled = 0
        self.tapped = 0
        # Where the scrolling fingers' mean position was last reported.
        self.last_mean = (0.0, 0.0)
        # slot -> [x0, y0, x, y]: where each finger landed and where it is.
        self.contacts: dict[int, list[float]] = {}
        self.buffer: list[Mapping[str, Any]] = []
        self.timer = None
        self.first_down = -math.inf
        self.last_down = -math.inf
        self.fingers = 0
        self.fired = False
        self.ended = False
        # Slots that landed too late or too many to join a gesture: their
        # whole sequence is dropped, the desktop never saw them start.
        self.ignored: set[int] = set()

    # -- public ---------------------------------------------------------------
    def handle(self, message: Mapping[str, Any]) -> None:
        """Route one ``{"type": "touch", ...}`` message."""
        parsed = self._parse(message)
        if parsed is None:
            # Let the validator behind ``forward`` reject it in its usual way;
            # anything held back goes first so the order is preserved.
            self._flush()
            self.forward(message)
            return
        action, slot, x, y = parsed
        if self.state == IDLE:
            self._idle(message, action, slot, x, y)
        elif self.state == HOLD:
            self._hold(message, action, slot, x, y)
        elif self.state == PASS:
            self._pass(message, action, slot, x, y)
        elif self.state == TWO:
            self._two(message, action, slot, x, y)
        elif self.state == SCROLL:
            self._scroll(action, slot, x, y)
        else:
            self._gesture(action, slot, x, y)

    def reset(self) -> None:
        """Forget every contact and anything held back (the host released input)."""
        self._cancel_timer()
        self.contacts.clear()
        self.buffer.clear()
        self.ignored.clear()
        self.state = IDLE
        self.fingers = 0
        self.fired = False
        self.ended = False

    def expire(self) -> None:
        """The hold window ended without a third finger: ordinary, or two to watch."""
        self.timer = None   # the one-shot has fired; nothing to cancel
        if self.state == HOLD:
            self._leave_hold()

    # -- states ---------------------------------------------------------------
    def _idle(self, message, action, slot, x, y):
        if action != TOUCH_DOWN:
            # A stray motion/up with nothing down: the validator's business.
            self.forward(message)
            return
        self.contacts[slot] = [x, y, x, y]
        self.buffer.append(message)
        self.first_down = self.last_down = self.now()
        self.state = HOLD
        self.timer = self.schedule(self.hold_ms, self.expire)

    def _hold(self, message, action, slot, x, y):
        if self.now() - self.first_down > self.hold_ms / 1000.0:
            # The timer is late (the loop was busy): classify by the clock.
            self._leave_hold()
            self.handle(message)
            return
        if action == TOUCH_DOWN:
            self.contacts[slot] = [x, y, x, y]
            self.buffer.append(message)
            self.last_down = self.now()
            if len(self.contacts) >= self.min_fingers:
                self._begin_gesture()
            return
        if action == TOUCH_MOTION:
            if slot in self.contacts:
                self.contacts[slot][2:] = [x, y]
            self.buffer.append(message)
            return
        # A lift this early is a tap, or a finger that changed its mind:
        # deliver everything now rather than after the window.
        if len(self.contacts) == 2 and self._watching_two():
            self._lift_two(message, slot)
            return
        self.contacts.pop(slot, None)
        self.buffer.append(message)
        self._leave_hold()

    def _pass(self, message, action, slot, x, y):
        if action == TOUCH_DOWN:
            self.contacts[slot] = [x, y, x, y]
        elif action == TOUCH_UP:
            self.contacts.pop(slot, None)
        self.forward(message)
        if not self.contacts:
            self.state = IDLE

    def _two(self, message, action, slot, x, y):
        if action == TOUCH_DOWN:
            # A third finger this late is ordinary input, as after any hold.
            self.contacts[slot] = [x, y, x, y]
            self.buffer.append(message)
            self._flush()
            self.state = PASS
            return
        if action == TOUCH_UP:
            self._lift_two(message, slot)
            return
        if slot in self.contacts:
            self.contacts[slot][2:] = [x, y]
        # Two fingers can rest for a long time: keep one motion per slot so
        # a later replay delivers where they are, not every jitter in between.
        self.buffer = [m for m in self.buffer
                       if not (m.get('action') == TOUCH_MOTION and m.get('slot') == slot)]
        self.buffer.append(message)
        self._classify_two()

    def _classify_two(self):
        a, b = self.contacts.values()
        mx0, my0 = self._landing()
        mx, my = self._mean()
        travel = math.hypot(mx - mx0, my - my0)
        spread = abs(math.hypot(b[2] - a[2], b[3] - a[3]) - math.hypot(b[0] - a[0], b[1] - a[1]))
        if max(travel, spread) < self.scroll_threshold:
            return
        if spread > travel or self.scroll is None or not self.scroll('begin', mx0, my0):
            # A pinch, or nowhere to scroll to: the desktop gets the contacts.
            self._flush()
            self.state = PASS
            return
        self.buffer.clear()
        self.state = SCROLL
        self.fingers = 2
        self.ended = False
        self.scrolled += 1
        self.last_mean = (mx0, my0)
        self._scroll_to(mx, my)

    def _lift_two(self, message, slot):
        """One of two held fingers lifts: a two-finger tap unless they moved."""
        moved = max(math.hypot(c[2] - c[0], c[3] - c[1]) for c in self.contacts.values())
        mx0, my0 = self._landing()
        self.contacts.pop(slot, None)
        if self.tap is not None and moved < self.scroll_threshold and self.tap(mx0, my0):
            # Consumed: the other finger's lift, and any late finger, are
            # dropped the way a swipe's are.
            self._cancel_timer()
            self.buffer.clear()
            self.tapped += 1
            self.state = GESTURE
            self.fingers = 2
            self.fired = self.ended = True
            if not self.contacts:
                self.reset()
            return
        # A finger that changed its mind, or two taps for the desktop.
        self.buffer.append(message)
        self._flush()
        self.state = PASS if self.contacts else IDLE

    def _scroll(self, action, slot, x, y):
        if action == TOUCH_DOWN:
            self.ignored.add(slot)
            return
        if action == TOUCH_MOTION:
            if slot in self.contacts and not self.ended:
                self.contacts[slot][2:] = [x, y]
                self._scroll_to(*self._mean())
            return
        # The first finger to lift ends the scroll; the other just finishes.
        if self.contacts.pop(slot, None) is not None and not self.ended:
            self.ended = True
            self.scroll('end', 0.0, 0.0)
        self.ignored.discard(slot)
        if not self.contacts and not self.ignored:
            self.reset()

    def _landing(self):
        n = len(self.contacts)
        return (sum(c[0] for c in self.contacts.values()) / n,
                sum(c[1] for c in self.contacts.values()) / n)

    def _mean(self):
        n = len(self.contacts)
        return (sum(c[2] for c in self.contacts.values()) / n,
                sum(c[3] for c in self.contacts.values()) / n)

    def _scroll_to(self, mx, my):
        dx, dy = mx - self.last_mean[0], my - self.last_mean[1]
        if dx == 0.0 and dy == 0.0:
            return
        self.last_mean = (mx, my)
        self.scroll('move', dx, dy)

    def _gesture(self, action, slot, x, y):
        if action == TOUCH_DOWN:
            late = self.now() - self.last_down > self.hold_ms / 1000.0
            if self.fired or self.ended or late or len(self.contacts) >= self.max_fingers:
                self.ignored.add(slot)
                return
            self.contacts[slot] = [x, y, x, y]
            self.fingers = len(self.contacts)
            self.last_down = self.now()
            return
        if action == TOUCH_MOTION:
            if slot in self.contacts:
                self.contacts[slot][2:] = [x, y]
                if not self.fired and not self.ended:
                    self._check_swipe()
            return
        # The first finger to lift ends the swipe; the others just finish.
        if self.contacts.pop(slot, None) is not None:
            self.ended = True
        self.ignored.discard(slot)
        if not self.contacts and not self.ignored:
            self.reset()

    # -- helpers --------------------------------------------------------------
    def _leave_hold(self):
        self._cancel_timer()
        if len(self.contacts) == 2 and self._watching_two():
            # Exactly two fingers landed together: keep them until they move.
            self.state = TWO
            return
        self._flush()
        self.state = PASS if self.contacts else IDLE

    def _watching_two(self):
        return self.scroll is not None or self.tap is not None

    def _begin_gesture(self):
        self._cancel_timer()
        self.buffer.clear()
        self.state = GESTURE
        self.fingers = len(self.contacts)
        self.fired = False
        self.ended = False

    def _check_swipe(self):
        n = len(self.contacts)
        dx = sum(c[2] - c[0] for c in self.contacts.values()) / n
        dy = sum(c[3] - c[1] for c in self.contacts.values()) / n
        if max(abs(dx), abs(dy)) < self.threshold:
            return
        if abs(dx) >= abs(dy):
            direction = 'left' if dx < 0 else 'right'
        else:
            direction = 'up' if dy < 0 else 'down'
        self.fired = True
        self.recognised += 1
        self.act(self.fingers, direction)

    def _flush(self):
        self._cancel_timer()
        while self.buffer:
            # Pop first: if delivery raises, the host releases input and
            # resets this filter, and the failed message must not replay.
            self.forward(self.buffer.pop(0))

    def _cancel_timer(self):
        if self.timer is not None:
            timer, self.timer = self.timer, None
            self.cancel(timer)

    @staticmethod
    def _parse(message):
        if not isinstance(message, Mapping) or message.get('type') != 'touch':
            return None
        action = message.get('action')
        slot = message.get('slot')
        x = message.get('x')
        y = message.get('y')
        if (type(action) is not int or type(slot) is not int or
                action not in (TOUCH_DOWN, TOUCH_UP, TOUCH_MOTION)):
            return None
        if action == TOUCH_UP:
            return action, slot, 0.0, 0.0
        if (isinstance(x, bool) or isinstance(y, bool) or
                not isinstance(x, (int, float)) or not isinstance(y, (int, float)) or
                not math.isfinite(x) or not math.isfinite(y)):
            return None
        return action, slot, float(x), float(y)
