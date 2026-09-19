#!/usr/bin/env python3
"""A banner on every screen saying what the tablet is doing right now.

Started by the host, which writes one JSON object per line to this
process's stdin:

    {"state": "control", "title": "...", "hint": "...", "colour": "#e06c3a"}
    {"state": "screen"}          hides everything
    {"state": "quit"}            ends

There is one strip per screen, so the state is in front of the user
wherever they are looking -- the point being that "the tablet has your
keyboard" must never be something you have to guess.

It runs on XWayland (``QT_QPA_PLATFORM=xcb``) on purpose. A Wayland window
cannot place itself on a chosen screen, and the fullscreen trick that can
asks the compositor to activate the window, which takes the keyboard focus
away from whatever the user is doing -- exactly the wrong thing for a
window whose whole job is to be ignored. Under X11 the banner is an
override-redirect window (``X11BypassWindowManagerHint``): the window
manager never touches it, never focuses it, and Qt gives it an empty input
shape (``WindowTransparentForInput``) so clicks go straight through.
"""
import json
import os
import sys

os.environ.setdefault('QT_QPA_PLATFORM', 'xcb')

from PyQt6.QtCore import Qt, QSocketNotifier, QRectF                 # noqa: E402
from PyQt6.QtGui import QColor, QFont, QPainter, QPainterPath        # noqa: E402
from PyQt6.QtWidgets import QApplication, QWidget                    # noqa: E402

BACKGROUND = QColor(18, 18, 26, 240)
TEXT = QColor(255, 255, 255)
HINT = QColor(196, 196, 212)
WIDTH = 820
HEIGHT = 92
TOP_MARGIN = 28


class Banner(QWidget):
    """One screen's strip: shown near the top, never in anyone's way."""

    def __init__(self, screen):
        super().__init__()
        self.title = ''
        self.hint = ''
        self.colour = QColor('#7a68ff')
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint
                            | Qt.WindowType.WindowStaysOnTopHint
                            | Qt.WindowType.X11BypassWindowManagerHint
                            | Qt.WindowType.WindowDoesNotAcceptFocus
                            | Qt.WindowType.WindowTransparentForInput
                            | Qt.WindowType.Tool)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setWindowTitle('tabs9 status')
        self.place(screen)

    def place(self, screen):
        self.screen_name = screen.name()
        area = screen.geometry()
        width = min(WIDTH, area.width() - 40)
        self.setGeometry(area.x() + (area.width() - width) // 2,
                         area.y() + TOP_MARGIN, width, HEIGHT)

    def show_state(self, title, hint, colour):
        self.title, self.hint = title, hint
        self.colour = QColor(colour)
        self.show()
        self.raise_()
        self.update()

    def paintEvent(self, event):
        if not self.title:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        box = QRectF(0, 0, self.width(), self.height())

        path = QPainterPath()
        path.addRoundedRect(box, 18, 18)
        painter.fillPath(path, BACKGROUND)
        # A coloured bar down the left edge: the state at a glance.
        bar = QPainterPath()
        bar.addRoundedRect(QRectF(0, 0, 10, box.height()), 5, 5)
        painter.fillPath(bar, self.colour)

        painter.setPen(TEXT)
        painter.setFont(QFont('Noto Sans', 15, QFont.Weight.DemiBold))
        painter.drawText(QRectF(28, 14, box.width() - 46, 28),
                         int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                         self.title)
        if self.hint:
            painter.setPen(HINT)
            painter.setFont(QFont('Noto Sans', 11))
            painter.drawText(QRectF(28, 44, box.width() - 46, 40),
                             int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop),
                             self.hint)


class Banners:
    def __init__(self, app):
        self.app = app
        self.windows = {}
        self.state = None
        app.screenAdded.connect(self.rebuild)
        app.screenRemoved.connect(self.rebuild)
        self.rebuild()

    def rebuild(self, *args):
        live = {screen.name(): screen for screen in self.app.screens()}
        for name, screen in live.items():
            if name in self.windows:
                self.windows[name].place(screen)
            else:
                self.windows[name] = Banner(screen)
        for name in list(self.windows):
            if name not in live:
                self.windows.pop(name).close()
        if self.state:
            self.apply(self.state)

    def apply(self, message):
        self.state = message
        if message.get('state') in (None, 'screen'):
            for window in self.windows.values():
                window.hide()
            return
        for window in self.windows.values():
            window.show_state(message.get('title', ''), message.get('hint', ''),
                              message.get('colour', '#7a68ff'))


def main():
    app = QApplication(sys.argv[:1])
    app.setQuitOnLastWindowClosed(False)
    banners = Banners(app)
    buffer = b''

    def readable(*args):
        nonlocal buffer
        chunk = sys.stdin.buffer.raw.read(4096)
        if not chunk:          # the host is gone: never leave a banner behind
            app.quit()
            return
        buffer += chunk
        while b'\n' in buffer:
            line, buffer = buffer.split(b'\n', 1)
            if not line.strip():
                continue
            try:
                message = json.loads(line)
            except ValueError:
                continue
            if message.get('state') == 'quit':
                app.quit()
                return
            banners.apply(message)

    notifier = QSocketNotifier(sys.stdin.fileno(), QSocketNotifier.Type.Read)
    notifier.activated.connect(readable)
    app.exec()


if __name__ == '__main__':
    main()
