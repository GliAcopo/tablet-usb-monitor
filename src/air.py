"""S Pen button and air gestures, recognised on the host.

Samsung's pen reports its side button and its motion in the air over
Bluetooth, not through the digitizer: the tablet app receives them from the
S Pen Remote SDK (``SpenUnit.TYPE_BUTTON`` and ``TYPE_AIR_MOTION``) and
forwards them here.  Air motion arrives as a stream of small deltas while
the button is held — the same thing Samsung's own "Air actions" watch — so a
gesture is whatever happened between the button going down and coming up:

* no real motion                      -> ``click``
* a flick                             -> ``up`` / ``down`` / ``left`` / ``right``
* a loop                              -> ``clockwise`` / ``counterclockwise``

Each name is mapped to a desktop action by the host (a KDE global shortcut
or a command), so the pen ends up driving the same things a key would.

The recogniser is pure and deliberately simple; the thresholds are in the
pen's own units, which no documentation gives, so they are tunable and the
host logs what a gesture measured.
"""
from __future__ import annotations

import math
from typing import Any, Callable

DIRECTIONS = ('up', 'down', 'left', 'right')
CIRCLES = ('clockwise', 'counterclockwise')
GESTURES = ('click',) + DIRECTIONS + CIRCLES

# Travel (in the pen's own air-motion units, summed over one press) that
# tells a flick from a hand that simply is not perfectly still.
DEFAULT_THRESHOLD = 1.5
# A loop has to enclose some area and wander much further than it ends up
# from where it started; both are measured in the same units.
DEFAULT_CIRCLE_AREA = 2.0
DEFAULT_CIRCLE_PATH = 4.0


class AirGestures:
    """Button and air motion in, gesture names out.

    ``act(name)`` is called once per button release with one of
    :data:`GESTURES`; whatever it returns is ignored (the host decides
    whether the name is bound to anything).
    """

    def __init__(self, act: Callable[[str], Any], *,
                 threshold: float = DEFAULT_THRESHOLD,
                 circle_area: float = DEFAULT_CIRCLE_AREA,
                 circle_path: float = DEFAULT_CIRCLE_PATH):
        if threshold <= 0 or circle_area <= 0 or circle_path <= 0:
            raise ValueError('air-gesture thresholds must be positive')
        self.act = act
        self.threshold = threshold
        self.circle_area = circle_area
        self.circle_path = circle_path
        self.down = False
        self.recognised = 0
        # What the last gesture measured, for the log line that makes the
        # thresholds tunable without guessing.
        self.last: tuple[str, float, float, float, int] = ('', 0.0, 0.0, 0.0, 0)
        self._reset()

    def _reset(self) -> None:
        self.sx = self.sy = 0.0   # where the pen ended up, relative to the start
        self.area = 0.0           # twice the signed area the path encloses
        self.path = 0.0           # how far the pen travelled in total
        self.samples = 0

    def button(self, down: bool) -> None:
        """The side button went down (True) or up (False)."""
        if down:
            self.down = True
            self._reset()
            return
        if not self.down:
            return
        self.down = False
        name = self.classify()
        self.last = (name, round(self.sx, 3), round(self.sy, 3), round(self.area, 3), self.samples)
        self.recognised += 1
        self.act(name)

    def motion(self, dx: float, dy: float) -> None:
        """One air-motion sample; ignored unless the button is held."""
        if not self.down:
            return
        if (isinstance(dx, bool) or isinstance(dy, bool) or
                not isinstance(dx, (int, float)) or not isinstance(dy, (int, float)) or
                not math.isfinite(dx) or not math.isfinite(dy)):
            return
        # Shoelace, one segment at a time: positive means the path wound
        # clockwise on a screen (y pointing down).
        self.area += self.sx * dy - self.sy * dx
        self.sx += dx
        self.sy += dy
        self.path += math.hypot(dx, dy)
        self.samples += 1

    def classify(self) -> str:
        travel = math.hypot(self.sx, self.sy)
        if (self.path >= self.circle_path and abs(self.area) >= self.circle_area
                and travel < self.path / 2):
            return 'clockwise' if self.area > 0 else 'counterclockwise'
        if travel < self.threshold:
            return 'click'
        if abs(self.sx) >= abs(self.sy):
            return 'right' if self.sx > 0 else 'left'
        return 'down' if self.sy > 0 else 'up'

    def reset(self) -> None:
        """Forget a press in progress (the pen or the tablet went away)."""
        self.down = False
        self._reset()
