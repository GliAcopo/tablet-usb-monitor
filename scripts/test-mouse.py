#!/usr/bin/env python3
"""A throwaway mouse and keyboard, for testing input capture without hands.

Remote control (see docs/pc-to-tablet-control.md) takes the *real* mouse and
keyboard away from the desktop, so it cannot be tested by driving the
desktop the way the rest of this project is tested: the host's own libei
sender is not what KWin's input capture forwards.  This creates a genuine
kernel input device through uinput -- as far as libinput, KWin and the
capture are concerned it is a mouse plugged into the machine -- moves it,
and removes it again.

    python3 scripts/test-mouse.py demo          # moves, a click, a wheel notch, a key
    python3 scripts/test-mouse.py push-left     # shove the pointer against the left edge
    python3 scripts/test-mouse.py type kde      # type letters

It needs write access to /dev/uinput (on this laptop the seat ACL grants
it; otherwise run it as root or add yourself to the right group). Whatever
it does lands on whatever has the pointer at that moment, so run it when
the desktop is free -- or when the tablet has the input, which is the point.
"""
import fcntl
import struct
import sys
import time

EV_SYN, EV_KEY, EV_REL = 0, 1, 2
SYN_REPORT = 0
REL_X, REL_Y, REL_WHEEL = 0, 1, 8
BTN_LEFT = 0x110
UI_DEV_CREATE, UI_DEV_DESTROY = 0x5501, 0x5502
UI_SET_EVBIT, UI_SET_KEYBIT, UI_SET_RELBIT = 0x40045564, 0x40045565, 0x40045566
UI_DEV_SETUP = 0x405c5503

# Enough of the evdev key codes to type a word (see Android's Generic.kl for
# the other side of the same table).
KEYS = {'a': 30, 'b': 48, 'c': 46, 'd': 32, 'e': 18, 'f': 33, 'g': 34, 'h': 35,
        'i': 23, 'j': 36, 'k': 37, 'l': 38, 'm': 50, 'n': 49, 'o': 24, 'p': 25,
        'q': 16, 'r': 19, 's': 31, 't': 20, 'u': 22, 'v': 47, 'w': 17, 'x': 45,
        'y': 21, 'z': 44, ' ': 57, '\n': 28}


class VirtualInput:
    def __init__(self, name=b'tabs9 test mouse'):
        self.fd = open('/dev/uinput', 'wb', buffering=0)
        for bit in (EV_KEY, EV_REL, EV_SYN):
            fcntl.ioctl(self.fd, UI_SET_EVBIT, bit)
        for code in {BTN_LEFT, *KEYS.values()}:
            fcntl.ioctl(self.fd, UI_SET_KEYBIT, code)
        for code in (REL_X, REL_Y, REL_WHEEL):
            fcntl.ioctl(self.fd, UI_SET_RELBIT, code)
        fcntl.ioctl(self.fd, UI_DEV_SETUP,
                    struct.pack('HHHH80sI', 0x03, 0x1234, 0x5678, 1, name, 0))
        fcntl.ioctl(self.fd, UI_DEV_CREATE)
        # libinput has to notice the new device before it carries anything.
        time.sleep(1.2)

    def emit(self, kind, code, value):
        self.fd.write(struct.pack('llHHi', 0, 0, kind, code, value))

    def move(self, dx, dy):
        self.emit(EV_REL, REL_X, dx)
        self.emit(EV_REL, REL_Y, dy)
        self.emit(EV_SYN, SYN_REPORT, 0)

    def click(self, button=BTN_LEFT):
        self.emit(EV_KEY, button, 1)
        self.emit(EV_SYN, SYN_REPORT, 0)
        time.sleep(0.05)
        self.emit(EV_KEY, button, 0)
        self.emit(EV_SYN, SYN_REPORT, 0)

    def wheel(self, clicks):
        self.emit(EV_REL, REL_WHEEL, clicks)
        self.emit(EV_SYN, SYN_REPORT, 0)

    def type(self, text):
        for character in text:
            code = KEYS.get(character.lower())
            if code is None:
                continue
            self.emit(EV_KEY, code, 1)
            self.emit(EV_SYN, SYN_REPORT, 0)
            time.sleep(0.06)
            self.emit(EV_KEY, code, 0)
            self.emit(EV_SYN, SYN_REPORT, 0)
            time.sleep(0.12)

    def close(self):
        time.sleep(0.3)
        fcntl.ioctl(self.fd, UI_DEV_DESTROY)
        self.fd.close()


def main():
    what = sys.argv[1] if len(sys.argv) > 1 else 'demo'
    device = VirtualInput()
    try:
        if what == 'push-left':
            for _ in range(60):
                device.move(-40, 0)
                time.sleep(0.004)
        elif what == 'type':
            device.type(sys.argv[2] if len(sys.argv) > 2 else 'kde')
        else:
            for _ in range(20):
                device.move(12, 6)
                time.sleep(0.01)
            device.click()
            time.sleep(0.2)
            device.wheel(-1)
            time.sleep(0.2)
            device.type('a')
    finally:
        device.close()
    print('done', what)


if __name__ == '__main__':
    main()
