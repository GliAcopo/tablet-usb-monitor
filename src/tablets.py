"""Which tablet: listing the devices ADB sees and choosing one by name.

Everything here keeps serial numbers out of print statements and logs: a
tablet is shown as its model (`SM_X910`, `HMW_W09`) and product name, and
the serial only travels inside `adb -s` arguments.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
import subprocess

ROOT = Path(__file__).resolve().parents[1]
ADB = ROOT / '.local/platform-tools/adb'


@dataclass(frozen=True)
class Tablet:
    serial: str
    state: str          # device | unauthorized | offline | no permissions | ...
    model: str = ''     # ro.product.model with spaces as underscores, from `adb devices -l`
    product: str = ''
    transport: str = '' # usb:3-1 etc.

    @property
    def label(self) -> str:
        """Human name without the serial."""
        return self.model.replace('_', ' ') or self.product or 'Android device'

    @property
    def usb(self) -> bool:
        return self.transport.startswith('usb:')

    def target(self) -> list[str]:
        """The `adb` arguments that address this tablet and no other."""
        return ['-s', self.serial]

    @property
    def slug(self) -> str:
        """File- and unit-safe name for this tablet's host instance (no serial)."""
        return slugify(self.model or self.product or 'tablet')


def slugify(text: str) -> str:
    return re.sub(r'[^a-z0-9]+', '_', text.lower()).strip('_') or 'tablet'


def parse_devices(output: str) -> list[Tablet]:
    tablets = []
    for line in output.splitlines()[1:]:
        parts = line.split()
        if len(parts) < 2 or parts[0] == '*':
            continue
        serial, state = parts[0], parts[1]
        if state == 'no' and len(parts) > 2 and parts[2] == 'permissions':
            state = 'no permissions'
        info = {}
        for part in parts[2:]:
            if ':' in part:
                key, value = part.split(':', 1)
                info[key] = value
        transport = next((p for p in parts[2:] if p.startswith('usb:')), '')
        tablets.append(Tablet(serial, state, info.get('model', ''), info.get('product', ''), transport))
    return tablets


def list_tablets(adb: Path | str = ADB, timeout: int = 30) -> list[Tablet]:
    """Every device ADB knows, USB ones first; [] when ADB is unavailable."""
    try:
        result = subprocess.run([str(adb), 'devices', '-l'], capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return []
    tablets = parse_devices(result.stdout)
    return sorted(tablets, key=lambda t: (not t.usb, t.label))


class TabletChoice(Exception):
    """A selector did not name exactly one attached tablet; str() says why."""


def choose(selector: str | None, tablets: list[Tablet]) -> Tablet:
    """Resolve `--tablet` (model, product, part of either, or a serial) to one tablet.

    No selector: the only attached device, or the only authorized one when
    the others are still unauthorized/offline. Raises TabletChoice with a
    message that lists models, never serials.
    """
    usb = [t for t in tablets if t.usb] or tablets
    if selector:
        wanted = re.sub(r'[\s-]+', '_', selector.strip().lower())
        exact = [t for t in usb if wanted in (t.serial.lower(), t.model.lower(), t.product.lower())]
        loose = exact or [t for t in usb if wanted in t.model.lower() or wanted in t.product.lower()
                          or wanted in t.label.lower()]
        if len(loose) == 1:
            return loose[0]
        if not loose:
            raise TabletChoice(f'no attached tablet matches "{selector}"; attached: '
                               + (', '.join(t.label for t in usb) or 'none'))
        raise TabletChoice(f'"{selector}" matches more than one tablet: ' + ', '.join(t.label for t in loose))
    if len(usb) == 1:
        return usb[0]
    if not usb:
        raise TabletChoice('no tablet is attached over USB')
    ready = [t for t in usb if t.state == 'device']
    if len(ready) == 1:
        return ready[0]
    raise TabletChoice('more than one tablet is attached (' + ', '.join(t.label for t in usb)
                       + '); pick one with --tablet MODEL')


if __name__ == '__main__':
    # `python3 src/tablets.py [--tablet X]` prints the chosen tablet's slug and
    # label (for the tabs9 shell script); `list` prints one "slug\tstate\tlabel"
    # line per attached tablet. Never a serial.
    import argparse
    import sys
    parser = argparse.ArgumentParser()
    parser.add_argument('command', nargs='?', default='choose', choices=['choose', 'list'])
    parser.add_argument('--tablet', default=None)
    ns = parser.parse_args()
    attached = list_tablets()
    if ns.command == 'list':
        for t in attached:
            if t.usb:
                print(f'{t.slug}\t{t.state}\t{t.label}')
        sys.exit(0)
    try:
        chosen = choose(ns.tablet, attached)
    except TabletChoice as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)
    print(f'{chosen.slug}\t{chosen.label}')
