#!/usr/bin/env python3
"""Minimal, privacy-preserving checks for this machine's USB display stack."""
import argparse
import importlib
import os
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--check', action='store_true')
    parser.parse_args()
    failed = []
    def check(name, okay):
        print(f'{name}: {"ready" if okay else "missing / unavailable"}')
        if not okay: failed.append(name)
    check('KDE Wayland session', os.environ.get('XDG_SESSION_TYPE') == 'wayland' and
          'KDE' in os.environ.get('XDG_CURRENT_DESKTOP', ''))
    for command in ['kscreen-doctor', 'gst-launch-1.0', 'systemd-run']:
        check(command, bool(shutil.which(command)))
    for module in ['dbus', 'gi', 'websockets.asyncio.server']:
        try: importlib.import_module(module); okay = True
        except ImportError: okay = False
        check(module, okay)
    adb = ROOT / '.local/platform-tools/adb'
    check('Local ADB', adb.is_file())
    if adb.is_file():
        result = subprocess.run([str(adb), '-d', 'get-state'], capture_output=True, timeout=10)
        check('One authorized USB tablet', result.returncode == 0 and result.stdout.strip() == b'device')
    for directory in Path('/sys/bus/usb/devices').glob('*'):
        try:
            if (directory / 'idVendor').read_text().strip() == '04e8':
                speed = (directory / 'speed').read_text().strip()
                print(f'Samsung USB negotiated link: {speed} Mbit/s')
        except OSError: pass
    if failed:
        print('Resolve the unavailable checks before starting. See README.md.')
        return 1
    return 0

if __name__ == '__main__':
    sys.exit(main())
