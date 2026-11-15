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
needs before the app connects and can no longer ask `wm size` for, and the
tablet's own addresses (where it was last seen), for the knock.

Finding each other does not rest on mDNS alone, because either direction of
it can be missing (a network that drops multicast, a Wi-Fi card that hears
no broadcasts, a firewall):

  * the app dials every address it knows at once: the announced one, the
    last that worked, the computer's LAN addresses and its Tailscale address
    and name. The host sends its current ones in every greeting, so the list
    follows a changed DHCP lease without pairing again;
  * the host knocks: while no tablet is connected it opens a short TCP
    connection to the tablet's known addresses (KNOCK_PORT) saying which
    ports it listens on. The app then dials the knock's sender. A knock
    carries nothing secret and is trusted with nothing: the app still checks
    the pinned certificate before it sends its secret;
  * over Tailscale (when both run it) the same TLS connection goes through
    the tailnet, which also passes a firewall that blocks the LAN ports.

    python3 src/wifi.py pair [--tablet X]   (over USB; `./tabs9 pair`)
    python3 src/wifi.py list            -> "slug\\tlabel" per paired tablet
    python3 src/wifi.py choose [--tablet X]
    python3 src/wifi.py forget --tablet X
"""
from __future__ import annotations

import hashlib
import ipaddress
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
# The app listens here (TCP) while it is open and paired; see knock().
KNOCK_PORT = 8889
# Tailscale hands out addresses from the carrier-grade NAT range.
TAILNET = ipaddress.ip_network('100.64.0.0/10')
# How many of the tablet's addresses a pairing record remembers.
MAX_TABLET_ADDRESSES = 6


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


def valid_ipv4(address) -> bool:
    """A plain unicast IPv4 address worth dialling (what a tablet reports about itself)."""
    if not isinstance(address, str) or not re.fullmatch(r'\d{1,3}(\.\d{1,3}){3}', address):
        return False
    try:
        ip = ipaddress.IPv4Address(address)
    except ValueError:
        return False
    return not (ip.is_loopback or ip.is_multicast or ip.is_unspecified or ip.is_link_local
                or ip == ipaddress.IPv4Address('255.255.255.255'))


def is_tailnet(address: str) -> bool:
    try:
        return ipaddress.IPv4Address(address) in TAILNET
    except ValueError:
        return False


def remember_addresses(slug: str, addresses, directory: Path = WIFI_DIR) -> list[str]:
    """Put the tablet's addresses (newest first) at the front of its record's
    list; the secret and everything else stay as they are. Returns the list."""
    record = load_pairing(slug, directory)
    if record is None:
        return []
    known = [a for a in record.get('addresses', []) if valid_ipv4(a)]
    fresh = [a for a in dict.fromkeys(addresses) if valid_ipv4(a)]
    merged = (fresh + [a for a in known if a not in fresh])[:MAX_TABLET_ADDRESSES]
    if merged != known:
        record['addresses'] = merged
        fd, tmp = tempfile.mkstemp(dir=directory, prefix='.pair-')
        with os.fdopen(fd, 'w') as handle:
            json.dump(record, handle, indent=2)
        os.replace(tmp, record_path(slug, directory))
    return merged


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


def tailscale_status(timeout: float = 4) -> dict | None:
    """`tailscale status --json`, or None when Tailscale is absent, stopped or slow."""
    try:
        out = subprocess.run(['tailscale', 'status', '--json'], capture_output=True, text=True, timeout=timeout)
        status = json.loads(out.stdout) if out.returncode == 0 else None
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    if not isinstance(status, dict) or status.get('BackendState') != 'Running':
        return None
    return status


def tailscale_addresses(status: dict | None) -> list[str]:
    """This computer on the tailnet: its IPv4 address, then its MagicDNS name."""
    me = (status or {}).get('Self') or {}
    found = [a for a in me.get('TailscaleIPs') or [] if valid_ipv4(a)]
    name = str(me.get('DNSName') or '').rstrip('.')
    if found and re.fullmatch(r'[A-Za-z0-9]([A-Za-z0-9.-]{0,251}[A-Za-z0-9])?', name) and '.' in name:
        found.append(name)
    return found


def tailscale_path(status: dict | None, address: str) -> str | None:
    """How the tailnet reaches the peer with this address: 'direct', 'relay'
    (through a DERP server: slow, for a still picture at most) or None when
    it is not a known peer."""
    for peer in ((status or {}).get('Peer') or {}).values():
        if address in (peer.get('TailscaleIPs') or []):
            return 'direct' if peer.get('CurAddr') else 'relay'
    return None


def host_candidates(status: dict | None = None) -> list[str]:
    """Where the app may dial this computer, best first: LAN addresses, then
    the Tailscale address and name (stable wherever the computer is)."""
    lan = [a for a in local_addresses() if not is_tailnet(a)]
    return list(dict.fromkeys(lan + tailscale_addresses(status)))


def knock(address: str, ident: str, video_port: int, control_port: int, timeout: float = 1.5) -> bool:
    """Tell the app at `address` that its host is up (see the module text).

    The connection comes from here, so it works where the tablet cannot find
    or even reach the computer first: no mDNS, and a tablet whose ARP request
    this computer never hears learns the way back from our packet."""
    line = json.dumps({'tabs9': 1, 'id': ident, 'video': video_port, 'control': control_port}) + '\n'
    try:
        with socket.create_connection((address, KNOCK_PORT), timeout=timeout) as conn:
            conn.sendall(line.encode())
        return True
    except OSError:
        return False


def firewall_blocks_lan() -> bool | None:
    """True when ufw is on (it denies incoming connections unless a rule
    allows the ports), False when it is off, None when that cannot be told.
    Its rules are root-only, so "on" is all that can be seen from here."""
    try:
        text = Path('/etc/ufw/ufw.conf').read_text()
    except OSError:
        return None
    return bool(re.search(r'^\s*ENABLED\s*=\s*yes\b', text, re.M | re.I))


def hears_broadcasts(interface: str | None = None) -> bool | None:
    """False when the default route's interface has received plenty of
    packets and not one multicast or broadcast frame: the tablet's mDNS
    query and its ARP request never arrive (seen with iwlwifi on a Wi-Fi 7
    multi-link connection). None when it cannot be told."""
    try:
        if interface is None:
            route = subprocess.run(['ip', '-4', 'route', 'get', '1.1.1.1'], capture_output=True,
                                   text=True, timeout=5).stdout
            interface = re.search(r'\bdev (\S+)', route).group(1)
        stats = Path('/sys/class/net') / interface / 'statistics'
        packets = int((stats / 'rx_packets').read_text())
        multicast = int((stats / 'multicast').read_text())
    except (OSError, subprocess.SubprocessError, AttributeError, ValueError):
        return None
    if packets < 20000:
        return None
    return multicast > 0


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
    certificate's fingerprint and this computer's addresses, Tailscale's
    included (for when the network drops mDNS). The extras travel over the
    cable only. The tablet's own addresses are kept for the knock."""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from tablets import ADB, choose as choose_usb, list_tablets
    import host  # noqa: F401  (for tablet_panel_size; imports GStreamer bindings)
    tablet = choose_usb(selector, list_tablets(ADB))
    if tablet.state != 'device':
        raise LookupError(f'{tablet.label} is {tablet.state}: allow USB debugging on it first')
    host.ADB_TARGET[:] = tablet.target()
    cert, _key = ensure_certificate()
    record = save_pairing(tablet.slug, tablet.label, host.tablet_panel_size())
    try:
        shown = subprocess.run([str(ADB), *tablet.target(), 'shell', 'ip', '-4', '-o', 'addr', 'show',
                                'scope', 'global'], capture_output=True, text=True, timeout=15).stdout
        record['addresses'] = remember_addresses(
            tablet.slug, re.findall(r'inet (\d+\.\d+\.\d+\.\d+)/', shown))
    except (OSError, subprocess.SubprocessError):
        pass
    subprocess.run([str(ADB), *tablet.target(), 'shell', 'am', 'start', '-n', APP,
                    '--es', 'pair_secret', record['secret'], '--es', 'pair_pin', fingerprint(cert),
                    '--es', 'pair_name', socket.gethostname().split('.')[0][:40] or 'computer',
                    '--es', 'pair_hosts', ','.join(host_candidates(tailscale_status()))],
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
