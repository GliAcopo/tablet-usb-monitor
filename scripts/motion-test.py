#!/usr/bin/env python3
"""Synthetic motion on the virtual output, without reading desktop content.

Repaints as fast as an 8 ms timer allows, so the capture path is measured
against continuous damage rather than against whatever the desktop happens to
be doing. Also reports whether injected touches arrive as native input.
"""
import argparse
import sys
import time
from PyQt6.QtWidgets import QApplication, QWidget
from PyQt6.QtCore import QTimer, Qt, QEvent
from PyQt6.QtGui import QPainter, QColor, QFont

parser = argparse.ArgumentParser()
parser.add_argument('--seconds', type=float, default=20.0,
                    help='how long to keep the pattern moving')
options, rest = parser.parse_known_args()
if not 1 <= options.seconds <= 600:
    parser.error('--seconds must be between 1 and 600')

app = QApplication([sys.argv[0], *rest])
screens = [s for s in app.screens() if s.name().startswith('Virtual-')]
if len(screens) != 1:
    raise SystemExit('Expected exactly one virtual screen; refusing ambiguous placement')

class Pattern(QWidget):
    def __init__(self):
        super().__init__()
        self.started = time.monotonic()
        self.setWindowTitle('USB display motion test')
        self.setAttribute(Qt.WidgetAttribute.WA_AcceptTouchEvents, True)
        self.setWindowFlag(Qt.WindowType.FramelessWindowHint)
        self.create()
        self.windowHandle().setScreen(screens[0])
        self.setGeometry(screens[0].geometry())
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.update)
        self.timer.start(8)
        self.touch_count = 0
        self.pointer_count = 0

    def event(self, event):
        if event.type() in (QEvent.Type.TouchBegin, QEvent.Type.TouchUpdate, QEvent.Type.TouchEnd):
            if event.type() == QEvent.Type.TouchBegin:
                self.touch_count += 1
                print('Native touch received:', self.touch_count, flush=True)
            event.accept()
            return True
        return super().event(event)

    def mousePressEvent(self, event):
        self.pointer_count += 1
        print('Pointer press received:', self.pointer_count, flush=True)
        super().mousePressEvent(event)

    def paintEvent(self, event):
        t = time.monotonic() - self.started
        p = QPainter(self)
        p.fillRect(self.rect(), QColor('#101c32'))
        p.fillRect(int((t * 400) % self.width()), 0, 24, self.height(), QColor('#53eed3'))
        p.setPen(QColor('white'))
        p.setFont(QFont('Sans', 34))
        p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter,
                   'USB EXTENDED DISPLAY\n2960 × 1848 · target 120 Hz\nSynthetic motion test\n' + f'{t:.1f} seconds\nTouches: {self.touch_count}')

window = Pattern()
window.showFullScreen()
QTimer.singleShot(int(options.seconds * 1000), app.quit)
app.exec()
print(f'Motion test finished after {options.seconds:g} s; '
      f'native touches: {window.touch_count}, pointer presses: {window.pointer_count}',
      flush=True)
