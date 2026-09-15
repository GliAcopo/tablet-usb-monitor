"""The host's own actions in KDE's shortcut list.

KDE keeps global shortcuts in ``kglobalaccel``: every application registers
its actions there, KDE stores the key the user chose in
``~/.config/kglobalshortcutsrc`` and shows them all in System Settings →
Shortcuts.  Registering there rather than grabbing keys ourselves is what
makes "the shortcut is installed with the host and you can change it like
any other" true.  The same daemon is asked which actions other components
offer, so ``./tabs9 shortcuts`` can list what a tablet gesture may be
pointed at.

A registration is three D-Bus calls per action:

* ``doRegister`` makes the action known (component, action, and the
  human-readable names the settings UI shows);
* ``setShortcut(..., IsDefault)`` records the key the host proposes, so the
  settings UI can offer "reset to default";
* ``setShortcut(..., SetPresent)`` with autoloading assigns that key the
  first time and afterwards **keeps whatever the user chose** — the daemon
  returns the keys in force, which is what the host prints.

Nothing here grabs a key or blocks one: kglobalaccel refuses a shortcut that
another component already owns, and the user can rebind or clear ours.
"""
from __future__ import annotations

import contextlib
import logging
from typing import Callable, Iterable

import dbus

log = logging.getLogger(__name__)

SERVICE = 'org.kde.kglobalaccel'
ACCEL_PATH = '/kglobalaccel'
ACCEL_IFACE = 'org.kde.KGlobalAccel'
COMPONENT_IFACE = 'org.kde.kglobalaccel.Component'

# kglobalacceld's SetShortcutFlag (see setShortcutKeys in kglobalacceld.cpp).
SET_PRESENT = 2
IS_DEFAULT = 4
NO_AUTOLOADING = 8

SHIFT, CTRL, ALT, META = 0x02000000, 0x04000000, 0x08000000, 0x10000000
MODIFIERS = {'shift': SHIFT, 'ctrl': CTRL, 'control': CTRL, 'alt': ALT,
             'meta': META, 'super': META, 'win': META}
# Qt::Key values for the non-printable keys a shortcut is likely to use.
NAMED_KEYS = {
    'escape': 0x01000000, 'tab': 0x01000001, 'backspace': 0x01000003,
    'return': 0x01000004, 'enter': 0x01000005, 'insert': 0x01000006,
    'delete': 0x01000007, 'pause': 0x01000008, 'print': 0x01000009,
    'home': 0x01000010, 'end': 0x01000011, 'left': 0x01000012, 'up': 0x01000013,
    'right': 0x01000014, 'down': 0x01000015, 'pageup': 0x01000016,
    'pagedown': 0x01000017, 'space': 0x20, 'menu': 0x01000055,
}
NAMED_KEYS.update({f'f{n}': 0x01000030 + n - 1 for n in range(1, 36)})
KEY_NAMES = {value: name.capitalize() for name, value in NAMED_KEYS.items()}


def parse_key(text: str) -> int:
    """'Meta+Shift+T' -> the integer kglobalaccel stores."""
    value = 0
    parts = [part.strip() for part in text.split('+') if part.strip()]
    if not parts:
        raise ValueError('empty shortcut')
    for part in parts[:-1]:
        modifier = MODIFIERS.get(part.lower())
        if modifier is None:
            raise ValueError(f'unknown modifier {part!r}')
        value |= modifier
    last = parts[-1]
    if len(last) == 1:
        return value | ord(last.upper())
    key = NAMED_KEYS.get(last.lower())
    if key is None:
        raise ValueError(f'unknown key {last!r}')
    return value | key


def format_key(value: int) -> str:
    """The inverse of :func:`parse_key`, for printing what is in force."""
    if not value:
        return 'none'
    names = []
    for name, bit in (('Meta', META), ('Ctrl', CTRL), ('Alt', ALT), ('Shift', SHIFT)):
        if value & bit:
            names.append(name)
    key = value & ~(META | CTRL | ALT | SHIFT)
    if key in KEY_NAMES:
        names.append(KEY_NAMES[key])
    elif 0x20 < key < 0x7F:
        names.append(chr(key))
    else:
        names.append(f'0x{key:x}')
    return '+'.join(names)


class KdeShortcuts:
    """The host's own component in kglobalaccel, and what others registered."""

    def __init__(self, bus, component: str = 'tabs9',
                 friendly: str = 'Tab S9 USB display'):
        self.bus = bus
        self.component = component
        self.friendly = friendly
        self.accel = dbus.Interface(bus.get_object(SERVICE, ACCEL_PATH), ACCEL_IFACE)
        self.actions: dict[str, str] = {}
        self._match = None

    def _action_id(self, name: str, label: str = '') -> dbus.Array:
        return dbus.Array([self.component, name, self.friendly, label or name], signature='s')

    def register(self, name: str, label: str, default: str | None) -> str:
        """Register one action; returns the key in force, for printing."""
        action = self._action_id(name, label)
        self.accel.doRegister(action)
        self.actions[name] = label
        keys = dbus.Array([dbus.Int32(parse_key(default))] if default else [], signature='i')
        with contextlib.suppress(dbus.DBusException):
            self.accel.setShortcut(action, keys, dbus.UInt32(IS_DEFAULT))
        active = self.accel.setShortcut(action, keys, dbus.UInt32(SET_PRESENT))
        return format_key(int(active[0])) if active else 'none'

    def listen(self, handler: Callable[[str], None]) -> None:
        """Call ``handler(action_name)`` whenever one of our shortcuts is pressed."""
        def pressed(component, action, timestamp):
            if str(component) == self.component:
                handler(str(action))
        self._match = self.bus.add_signal_receiver(
            pressed, signal_name='globalShortcutPressed', dbus_interface=COMPONENT_IFACE,
            path=f'/component/{self.component}')

    def release(self) -> None:
        """Mark our actions inactive (the user's key bindings are kept)."""
        for name, label in self.actions.items():
            with contextlib.suppress(dbus.DBusException):
                self.accel.setInactive(self._action_id(name, label))
        if self._match is not None:
            with contextlib.suppress(Exception):
                self._match.remove()
            self._match = None

    # -- what other components offer (for ./tabs9 shortcuts) ------------------
    def known_actions(self, component: str) -> list[str]:
        """Every action name a component offers (for ``./tabs9 shortcuts``)."""
        try:
            interface = dbus.Interface(
                self.bus.get_object(SERVICE, f'/component/{component}'), COMPONENT_IFACE)
            return sorted(str(name) for name in interface.shortcutNames())
        except dbus.DBusException:
            return []

    def components(self) -> Iterable[str]:
        with contextlib.suppress(dbus.DBusException):
            for entry in self.accel.allMainComponents():
                yield str(entry[0])
