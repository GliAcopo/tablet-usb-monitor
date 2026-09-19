"""Remembered start settings, per tablet.

`.local/state/settings.json` holds, for each tablet model, the `./tabs9
start` options the user chose in the control panel (side, profile, fps,
bitrate, scale, gestures...). `./tabs9 start` applies them as defaults and
anything typed on the command line still wins. Keys are tablet slugs
(sm_x910, hmw_w09), never serials.

    python3 src/settings.py args [--tablet X]   -> the saved options, one per line
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile

ROOT = Path(__file__).resolve().parents[1]
SETTINGS_FILE = ROOT / '.local/state/settings.json'

# option name -> (host flag, allowed values or a validator, human label)
OPTIONS = {
    'side': (['left', 'right', 'top', 'bottom'], 'Which side of the laptop screen'),
    'profile': (['smooth', 'balanced', 'light'], 'Profile (120 / 60 / 30 fps)'),
    'fps': ([30, 60, 90, 120], 'Frame rate'),
    'bitrate': ((1000, 150000), 'Bitrate, kbit/s'),
    'resolution': ('resolution', 'Resolution WIDTHxHEIGHT (empty: the tablet\'s own)'),
    'scale': ((1.0, 3.0), 'Scale (1 = tiny text, 2 = big)'),
    'gap': ((0, 64), 'Gap next to the laptop screen, px'),
    'gestures': (['on', 'off'], 'Three/four-finger swipes'),
    'scroll': (['natural', 'standard', 'off'], 'Two-finger scrolling'),
    'scroll_gain': ((0.1, 10.0), 'Scroll speed'),
    'two_finger_tap': (['right-click', 'off'], 'Two-finger tap'),
    'pen_button': (['actions', 'launcher', 'off'], 'S Pen button (Samsung)'),
    'remote': (['on', 'off'], 'Remote control shortcuts'),
    'remote_edge': (['left', 'right', 'top', 'bottom', 'none'], 'Hand the mouse over at this edge'),
    'capture_memory': (['native', 'va', 'system', 'gl'], 'Capture path'),
}

# What the host does when an option is not set (src/host.py's argparse
# defaults; tests/test_ui.py checks they match). The panel shows these as
# the selected value so an untouched form reads the way the host will run.
DEFAULTS = {
    'side': 'right', 'profile': 'smooth', 'scale': 1.5, 'gap': 1, 'gestures': 'on',
    'scroll': 'natural', 'scroll_gain': 0.2, 'two_finger_tap': 'right-click',
    'pen_button': 'actions', 'remote': 'on', 'remote_edge': 'none', 'capture_memory': 'native',
}


def load() -> dict:
    try:
        data = json.loads(SETTINGS_FILE.read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save(data: dict) -> None:
    SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=SETTINGS_FILE.parent, prefix='.settings-')
    with os.fdopen(fd, 'w') as handle:
        json.dump(data, handle, indent=2, sort_keys=True)
    os.chmod(temporary, 0o600)
    os.replace(temporary, SETTINGS_FILE)


def validate(values: dict) -> dict:
    """Keep only known options with acceptable values (strings normalised)."""
    clean = {}
    for key, value in (values or {}).items():
        if key not in OPTIONS or value in (None, ''):
            continue
        rule = OPTIONS[key][0]
        if isinstance(rule, list):
            if isinstance(rule[0], int):
                try:
                    value = int(value)
                except (TypeError, ValueError):
                    continue
            if value in rule:
                clean[key] = value
        elif isinstance(rule, tuple):
            try:
                number = float(value)
            except (TypeError, ValueError):
                continue
            lo, hi = rule
            if lo <= number <= hi:
                clean[key] = int(number) if isinstance(lo, int) else round(number, 3)
        elif rule == 'resolution':
            text = str(value).lower().strip()
            parts = text.split('x')
            if len(parts) == 2 and all(p.isdigit() for p in parts) and \
                    320 <= int(parts[0]) <= 4096 and 240 <= int(parts[1]) <= 4096:
                clean[key] = f'{int(parts[0])}x{int(parts[1])}'
    return clean


def for_tablet(slug: str) -> dict:
    return validate(load().get('tablets', {}).get(slug, {}))


def set_for_tablet(slug: str, values: dict) -> dict:
    data = load()
    tablets = data.setdefault('tablets', {})
    tablets[slug] = validate(values)
    save(data)
    return tablets[slug]


def start_args(slug: str) -> list[str]:
    """`--flag value` pairs for host.py from the saved settings of one tablet."""
    saved = for_tablet(slug)
    args = []
    for key in OPTIONS:                 # declaration order, so the line is stable
        if key in saved:
            args += [f'--{key.replace("_", "-")}', str(saved[key])]
    return args


if __name__ == '__main__':
    import argparse
    import sys
    sys.path.insert(0, str(ROOT / 'src'))
    from tablets import TabletChoice, choose, list_tablets
    parser = argparse.ArgumentParser()
    parser.add_argument('command', choices=['args'])
    parser.add_argument('--tablet', default=None)
    ns = parser.parse_args()
    try:
        tablet = choose(ns.tablet, list_tablets())
    except TabletChoice:
        sys.exit(0)          # no saved settings without a tablet; the host will complain itself
    for arg in start_args(tablet.slug):
        print(arg)
