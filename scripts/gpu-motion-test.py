#!/usr/bin/env python3
"""GPU-only moving-bar test for the existing KDE virtual output.

The test renders generated colors with OpenGL and records aggregate timing and
input counts. It never reads, captures, or stores desktop pixels or input data.
"""

from __future__ import annotations

import argparse
import ctypes
import ctypes.util
import json
import sys
import time

from PyQt6.QtCore import QEvent, QTimer, Qt
from PyQt6.QtGui import QGuiApplication, QMouseEvent, QSurfaceFormat
from PyQt6.QtOpenGL import QOpenGLWindow


GL_COLOR_BUFFER_BIT = 0x00004000
GL_SCISSOR_TEST = 0x0C11


def load_gl() -> ctypes.CDLL:
    library = ctypes.util.find_library("GL")
    if library is None:
        raise SystemExit("OpenGL library not found")
    gl = ctypes.CDLL(library)
    gl.glClearColor.argtypes = [ctypes.c_float] * 4
    gl.glClearColor.restype = None
    gl.glClear.argtypes = [ctypes.c_uint]
    gl.glClear.restype = None
    gl.glEnable.argtypes = [ctypes.c_uint]
    gl.glEnable.restype = None
    gl.glDisable.argtypes = [ctypes.c_uint]
    gl.glDisable.restype = None
    gl.glScissor.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int]
    gl.glScissor.restype = None
    gl.glViewport.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int]
    gl.glViewport.restype = None
    return gl


class GpuPattern(QOpenGLWindow):
    def __init__(self, screen, seconds: float, gl: ctypes.CDLL) -> None:
        super().__init__(QOpenGLWindow.UpdateBehavior.NoPartialUpdate)
        self.gl = gl
        self.seconds = seconds
        self.started = time.monotonic()
        self.last_report = self.started
        self.last_paints = 0
        self.last_swaps = 0
        self.paints = 0
        self.swaps = 0
        self.touch_begins = 0
        self.touch_updates = 0
        self.max_touch_points = 0
        self.touch_zones: dict[str, int] = {}
        self.button_presses = 0
        self.final_reported = False

        self.setTitle("USB display GPU motion test")
        self.setFlag(Qt.WindowType.FramelessWindowHint, True)
        self.setScreen(screen)
        self.setPosition(screen.geometry().topLeft())
        self.resize(screen.geometry().size())
        self.frameSwapped.connect(self.on_frame_swapped)

        self.report_timer = QTimer(self)
        self.report_timer.setTimerType(Qt.TimerType.PreciseTimer)
        self.report_timer.setInterval(1000)
        self.report_timer.timeout.connect(self.report)
        self.report_timer.start()

        QTimer.singleShot(round(seconds * 1000), self.finish)
        print(json.dumps({
            "event": "start",
            "screen": "virtual",
            "screen_refresh_hz": round(float(screen.refreshRate()), 3),
            "logical_size": [screen.geometry().width(), screen.geometry().height()],
            "device_pixel_ratio": round(float(screen.devicePixelRatio()), 3),
            "seconds": seconds,
        }), flush=True)

    def initializeGL(self) -> None:
        self.gl.glDisable(GL_SCISSOR_TEST)

    def resizeGL(self, width: int, height: int) -> None:
        ratio = float(self.devicePixelRatio())
        self.gl.glViewport(0, 0, round(width * ratio), round(height * ratio))

    def paintGL(self) -> None:
        elapsed = time.monotonic() - self.started
        ratio = float(self.devicePixelRatio())
        width = max(1, round(self.width() * ratio))
        height = max(1, round(self.height() * ratio))
        bar_width = max(16, round(24 * ratio))
        x = round((elapsed * 800 * ratio) % (width + bar_width)) - bar_width

        self.gl.glDisable(GL_SCISSOR_TEST)
        self.gl.glClearColor(0.0627, 0.1098, 0.1961, 1.0)
        self.gl.glClear(GL_COLOR_BUFFER_BIT)

        visible_x = max(0, x)
        visible_width = min(width, x + bar_width) - visible_x
        if visible_width > 0:
            self.gl.glEnable(GL_SCISSOR_TEST)
            self.gl.glScissor(visible_x, 0, visible_width, height)
            self.gl.glClearColor(0.3255, 0.9333, 0.8275, 1.0)
            self.gl.glClear(GL_COLOR_BUFFER_BIT)
            self.gl.glDisable(GL_SCISSOR_TEST)
        self.paints += 1

    def on_frame_swapped(self) -> None:
        self.swaps += 1
        if time.monotonic() - self.started < self.seconds:
            self.update()

    def zone(self, point) -> str:
        # Coarse 3x3 zone name only (no positions are stored).
        column = min(2, max(0, int(point.x() * 3 / max(1, self.width()))))
        row = min(2, max(0, int(point.y() * 3 / max(1, self.height()))))
        return ("top", "middle", "bottom")[row] + "-" + ("left", "center", "right")[column]

    def event(self, event: QEvent) -> bool:
        kind = event.type()
        if kind in (QEvent.Type.TouchBegin, QEvent.Type.TouchUpdate, QEvent.Type.TouchEnd):
            points = event.points()
            self.max_touch_points = max(self.max_touch_points, len(points))
            if kind == QEvent.Type.TouchBegin:
                self.touch_begins += 1
                for point in points:
                    name = self.zone(point.position())
                    self.touch_zones[name] = self.touch_zones.get(name, 0) + 1
            elif kind == QEvent.Type.TouchUpdate:
                self.touch_updates += 1
            event.accept()
            return True
        return super().event(event)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        self.button_presses += 1
        event.accept()

    def report(self) -> None:
        now = time.monotonic()
        elapsed = now - self.last_report
        print(json.dumps({
            "event": "stats",
            "elapsed_s": round(now - self.started, 3),
            "paint_fps": round((self.paints - self.last_paints) / elapsed, 1),
            "swap_fps": round((self.swaps - self.last_swaps) / elapsed, 1),
            "paints": self.paints,
            "swaps": self.swaps,
            "touch_begins": self.touch_begins,
            "touch_updates": self.touch_updates,
            "max_touch_points": self.max_touch_points,
            "button_presses": self.button_presses,
        }), flush=True)
        self.last_report = now
        self.last_paints = self.paints
        self.last_swaps = self.swaps

    def finish(self) -> None:
        self.final_report()
        self.close()
        QGuiApplication.quit()

    def final_report(self) -> None:
        if not self.final_reported:
            self.final_reported = True
            elapsed = max(time.monotonic() - self.started, 1e-9)
            print(json.dumps({
                "event": "final",
                "elapsed_s": round(elapsed, 3),
                "paint_fps": round(self.paints / elapsed, 1),
                "swap_fps": round(self.swaps / elapsed, 1),
                "paints": self.paints,
                "swaps": self.swaps,
                "touch_begins": self.touch_begins,
                "touch_updates": self.touch_updates,
                "max_touch_points": self.max_touch_points,
                "touch_zones": self.touch_zones,
                "button_presses": self.button_presses,
            }), flush=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seconds", type=float, default=20.0)
    args = parser.parse_args()
    if not (0.5 <= args.seconds <= 30.0):
        parser.error("--seconds must be between 0.5 and 30")
    return args


def main() -> None:
    args = parse_args()
    surface_format = QSurfaceFormat()
    surface_format.setRenderableType(QSurfaceFormat.RenderableType.OpenGL)
    surface_format.setSwapInterval(1)
    surface_format.setDepthBufferSize(0)
    surface_format.setStencilBufferSize(0)
    surface_format.setSamples(0)
    QSurfaceFormat.setDefaultFormat(surface_format)

    app = QGuiApplication(sys.argv[:1])
    screens = [screen for screen in app.screens() if screen.name().startswith("Virtual-")]
    if len(screens) != 1:
        raise SystemExit("Expected exactly one existing virtual screen; refusing ambiguous placement")

    window = GpuPattern(screens[0], args.seconds, load_gl())
    app.aboutToQuit.connect(window.final_report)
    window.showFullScreen()
    window.update()
    app.exec()


if __name__ == "__main__":
    main()
