"""Wi-Fi transport: pairing, TLS and the mDNS advert.

Over USB the app dials 127.0.0.1 through `adb reverse` and learns a fresh
session token from `am start`. Over Wi-Fi there is no adb, so a tablet is
paired once over USB (`./tabs9 pair`) and from then on:

  * the host listens on the LAN with TLS, using one self-signed certificate
    per computer (`.local/state/wifi/host.pem`). The app pins its SHA-256
    fingerprint, which it received during pairing: nobody else on the
    network can pose as the host or read the desktop picture;
  * the tablet authenticates with a per-tablet secret (64 hex characters,
    the same shape as the USB session token), sent inside the TLS channel;
  * the host announces itself as `_tabs9._tcp` through Avahi. The TXT record
    carries `id`, a hash of the tablet's secret, so each tablet finds the
    host running for it (one host per tablet, as over USB) and nothing in
    the advert names the tablet or the user.

Pairing records live in `.local/state/wifi/<slug>.json` (mode 0600) and
hold the secret, the tablet's label and its panel size, which the host
needs before the app connects and can no longer ask `wm size` for.

    python3 src/wifi.py pair [--tablet X]   (over USB; `./tabs9 pair`)
    python3 src/wifi.py list            -> "slug\\tlabel" per paired tablet
    python3 src/wifi.py choose [--tablet X]
    python3 src/wifi.py forget --tablet X
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import socket
import ssl
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
WIFI_DIR = ROOT / '.local/state/wifi'
SERVICE_TYPE = '_tabs9._tcp'
# Bumped when the TXT record or the handshake changes incompatibly.
ADVERT_VERSION = '1'


def cert_paths(directory: Path = WIFI_DIR) -> tuple[Path, Path]:
    return directory / 'host.pem', directory / 'host.key'


def ensure_certificate(directory: Path = WIFI_DIR) -> tuple[Path, Path]:
    """The computer's TLS certificate and key, created on first use.

    Self-signed on purpose: the app trusts exactly this certificate (by its
    fingerprint, learned during pairing), not any authority. P-256, ten
    years: re-pairing is the way to rotate it (`./tabs9 pair --new-key`)."""
    cert, key = cert_paths(directory)
    if cert.exists() and key.exists():
        return cert, key
    directory.mkdir(parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
    with tempfile.TemporaryDirectory(dir=directory) as tmp:
        tmp_cert, tmp_key = Path(tmp) / 'host.pem', Path(tmp) / 'host.key'
        subprocess.run(['openssl', 'req', '-x509', '-newkey', 'ec', '-pkeyopt', 'ec_paramgen_curve:prime256v1',
                        '-nodes', '-days', '3650', '-subj', '/CN=tabs9 host', '-keyout', str(tmp_key),
                        '-out', str(tmp_cert)], check=True, capture_output=True, timeout=30)
        os.chmod(tmp_key, 0o600)
        os.replace(tmp_key, key)
        os.replace(tmp_cert, cert)
    return cert, key


def fingerprint(cert: Path) -> str:
    """SHA-256 of the certificate's DER encoding, lowercase hex (what the app pins)."""
    der = ssl.PEM_cert_to_DER_cert(cert.read_text())
    return hashlib.sha256(der).hexdigest()


def server_context(directory: Path = WIFI_DIR) -> ssl.SSLContext:
    cert, key = ensure_certificate(directory)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(cert, key)
    return context


def tablet_id(secret: str) -> str:
    """What the advert says instead of the tablet's name: derived from the
    secret, so only the paired tablet recognises it and it reveals nothing."""
    return hashlib.sha256(b'tabs9-id:' + secret.encode()).hexdigest()[:16]


def record_path(slug: str, directory: Path = WIFI_DIR) -> Path:
    if not re.fullmatch(r'[a-z0-9_]+', slug):
        raise ValueError('bad tablet slug')
    return directory / f'{slug}.json'


def save_pairing(slug: str, label: str, resolution: str | None, directory: Path = WIFI_DIR) -> dict:
    """A new secret for this tablet (the previous pairing stops working)."""
    directory.mkdir(parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
    record = {'slug': slug, 'label': label, 'secret': secrets.token_hex(32), 'resolution': resolution}
    path = record_path(slug, directory)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix='.pair-')
    with os.fdopen(fd, 'w') as handle:
        json.dump(record, handle, indent=2)
    os.replace(tmp, path)
    return record


def load_pairing(slug: str, directory: Path = WIFI_DIR) -> dict | None:
    try:
        record = json.loads(record_path(slug, directory).read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(record, dict) or not re.fullmatch(r'[0-9a-f]{64}', str(record.get('secret', ''))):
        return None
    return record


def paired(directory: Path = WIFI_DIR) -> list[dict]:
    if not directory.is_dir():
        return []
    records = (load_pairing(p.stem, directory) for p in sorted(directory.glob('*.json')))
    return [r for r in records if r]


def forget(slug: str, directory: Path = WIFI_DIR) -> bool:
    try:
        record_path(slug, directory).unlink()
        return True
    except FileNotFoundError:
        return False


def choose(selector: str | None, records: list[dict]) -> dict:
    """`--tablet` among the paired tablets (model, part of it, or the slug)."""
    if selector:
        wanted = re.sub(r'[\s-]+', '_', selector.strip().lower())
        found = [r for r in records if wanted == r['slug']] or \
                [r for r in records if wanted in r['slug'] or wanted in r.get('label', '').lower()]
    else:
        found = records
    if len(found) == 1:
        return found[0]
    if not records:
        raise LookupError('no tablet is paired for Wi-Fi yet: plug it in and run ./tabs9 pair')
    names = ', '.join(r.get('label') or r['slug'] for r in (found or records))
    if not found:
        raise LookupError(f'no paired tablet matches "{selector}"; paired: {names}')
    raise LookupError(f'more than one tablet is paired ({names}); pick one with --tablet MODEL')


def local_addresses() -> list[str]:
    """IPv4 addresses the tablet could reach, for the log line (best effort)."""
    try:
        out = subprocess.run(['ip', '-4', '-o', 'addr', 'show', 'scope', 'global'], capture_output=True,
                             text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    found = []
    for m in re.finditer(r'^\d+:\s+(\S+)\s+inet (\d+\.\d+\.\d+\.\d+)/', out, re.M):
        # A bridge or hotspot with nothing on it (operstate "down") is no way in.
        try:
            state = (Path('/sys/class/net') / m.group(1) / 'operstate').read_text().strip()
        except OSError:
            state = 'unknown'
        if state != 'down':
            found.append(m.group(2))
    # The address of the default route first (the network the tablet is most
    # likely on), then other LAN ranges, VPN/CGNAT ones (Tailscale) last.
    try:
        route = subprocess.run(['ip', '-4', 'route', 'get', '1.1.1.1'], capture_output=True,
                               text=True, timeout=5).stdout
        default = re.search(r'\bsrc (\S+)', route)
    except (OSError, subprocess.SubprocessError):
        default = None
    lan = ('192.168.', '10.', *(f'172.{n}.' for n in range(16, 32)))
    return sorted(found, key=lambda a: (not (default and a == default.group(1)), not a.startswith(lan)))


class Advert:
    """`_tabs9._tcp` on the LAN through Avahi's D-Bus API, withdrawn on close()."""

    IF_UNSPEC = -1
    PROTO_INET = 0   # IPv4 only: an IPv6 link-local answer is useless to the app

    def __init__(self, bus, name: str, port: int, txt: dict[str, str]):
        import dbus
        server = dbus.Interface(bus.get_object('org.freedesktop.Avahi', '/'), 'org.freedesktop.Avahi.Server')
        self.group = dbus.Interface(bus.get_object('org.freedesktop.Avahi', server.EntryGroupNew()),
                                    'org.freedesktop.Avahi.EntryGroup')
        records = dbus.Array([dbus.Array(f'{k}={v}'.encode(), signature='y') for k, v in txt.items()],
                             signature='ay')
        self.group.AddService(self.IF_UNSPEC, self.PROTO_INET, dbus.UInt32(0), name, SERVICE_TYPE, '', '',
                              dbus.UInt16(port), records)
        self.group.Commit()

    def close(self):
        self.group.Free()


def advert_name(label: str) -> str:
    host = socket.gethostname().split('.')[0][:24]
    return f'tabs9 {label} on {host}'[:63]


APP = 'local.tabs9.usbdisplay/.MainActivity'


def pair(selector: str | None) -> dict:
    """Pair the tablet on USB: a new secret, handed to the app with the
    certificate's fingerprint and this computer's addresses (a fallback when
    the network drops mDNS). The extras travel over the cable only."""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from tablets import ADB, choose as choose_usb, list_tablets
    import host  # noqa: F401  (for tablet_panel_size; imports GStreamer bindings)
    tablet = choose_usb(selector, list_tablets(ADB))
    if tablet.state != 'device':
        raise LookupError(f'{tablet.label} is {tablet.state}: allow USB debugging on it first')
    host.ADB_TARGET[:] = tablet.target()
    cert, _key = ensure_certificate()
    record = save_pairing(tablet.slug, tablet.label, host.tablet_panel_size())
    subprocess.run([str(ADB), *tablet.target(), 'shell', 'am', 'start', '-n', APP,
                    '--es', 'pair_secret', record['secret'], '--es', 'pair_pin', fingerprint(cert),
                    '--es', 'pair_name', socket.gethostname().split('.')[0][:40] or 'computer',
                    '--es', 'pair_hosts', ','.join(local_addresses())],
                   check=True, capture_output=True, timeout=30)
    return record


if __name__ == '__main__':
    import argparse
    import sys
    parser = argparse.ArgumentParser()
    parser.add_argument('command', choices=['pair', 'list', 'choose', 'forget'])
    parser.add_argument('--tablet', default=None)
    ns = parser.parse_args()
    if ns.command == 'pair':
        try:
            record = pair(ns.tablet)
        except Exception as error:  # no tablet (TabletChoice, LookupError), adb failed
            print(f'Pairing failed: {error}', file=sys.stderr)
            sys.exit(1)
        print(f"Paired {record['label']} for Wi-Fi. The tablet shows \"Paired with this computer\".\n"
              f"From now on: ./tabs9 start --wifi --tablet {record['slug']}  (no cable needed), "
              'then open tabs9 on the tablet.')
        sys.exit(0)
    records = paired()
    if ns.command == 'forget':
        record = choose(ns.tablet, records) if records else None
        if record and forget(record['slug']):
            print(f"Forgot the Wi-Fi pairing of {record.get('label') or record['slug']}.")
        sys.exit(0)
    if ns.command == 'list':
        for r in records:
            print(f"{r['slug']}\t{r.get('label') or r['slug']}")
        sys.exit(0)
    try:
        chosen = choose(ns.tablet, records)
    except LookupError as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)
    print(f"{chosen['slug']}\t{chosen.get('label') or chosen['slug']}")
