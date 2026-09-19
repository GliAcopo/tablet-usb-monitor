#!/usr/bin/env python3
"""The tabs9 control panel: a small local web page for the tablets.

`./tabs9 ui` serves it on http://127.0.0.1:8899 (this computer only) and
opens the browser. The page lists every attached tablet with its state, a
Start/Stop button, what the tablet is (panel, refresh rate, decoder, USB
speed), live figures while it streams, the remembered start settings (side,
profile, scale...; saved per tablet in .local/state/settings.json and applied
by `./tabs9 start`), the doctor's report and the host log.

Plain HTML and JavaScript, no framework and no build step, so it renders in
any browser; the server is Python's standard library. Nothing is served to
other machines, and nothing here prints a serial number.
"""
from __future__ import annotations

import argparse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import time
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
sys.path.insert(0, str(ROOT / 'scripts'))

import settings as settings_store  # noqa: E402
from status import status_summary  # noqa: E402
from tablets import ADB, list_tablets  # noqa: E402

PAGE = ROOT / 'src/ui.html'
STATE_DIR = ROOT / '.local/state'
PROFILES = {'smooth': {'fps': 120, 'bitrate': 60000},
            'balanced': {'fps': 60, 'bitrate': 30000},
            'light': {'fps': 30, 'bitrate': 15000}}


def run(cmd, timeout=30, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=ROOT, **kw)


class Cache:
    """Small time-based cache: the page polls every two seconds and `adb
    shell` round trips add up with two tablets."""
    def __init__(self):
        self.lock = threading.Lock()
        self.items = {}

    def get(self, key, ttl, compute):
        with self.lock:
            stamp, value = self.items.get(key, (0, None))
            if time.monotonic() - stamp < ttl:
                return value
        value = compute()
        with self.lock:
            self.items[key] = (time.monotonic(), value)
        return value

    def drop(self, key):
        with self.lock:
            self.items.pop(key, None)


cache = Cache()


def unit_for(slug):
    return f'app-tabs9.{slug}.service'


def unit_pid(unit):
    result = run(['systemctl', '--user', 'show', '--property=MainPID', '--value', unit], timeout=10)
    pid = result.stdout.strip()
    return int(pid) if pid.isdigit() and pid != '0' else None


def unit_active(unit):
    return run(['systemctl', '--user', 'is-active', '--quiet', unit], timeout=10).returncode == 0


def journal(unit, lines=80):
    result = run(['journalctl', '--user', '-u', unit, '--no-pager', '-n', str(lines), '-o', 'cat'], timeout=20)
    return result.stdout


def last_stats(unit):
    """The newest JSON stats line the host logged, or {}."""
    for line in reversed(journal(unit, 40).splitlines()):
        if line.startswith('{'):
            try:
                return json.loads(line)
            except ValueError:
                return {}
    return {}


def tablet_facts(tablet):
    """What the tablet says about itself; cached, and only when authorized."""
    if tablet.state != 'device':
        return {}
    def compute():
        import setup  # scripts/setup.py: the same probe the doctor uses
        setup.TARGET[:] = tablet.target()
        try:
            return setup.tablet_facts()
        except Exception:
            return {}
    return cache.get(('facts', tablet.serial), 60, compute)


def tablet_entry(tablet):
    unit = unit_for(tablet.slug)
    active = unit_active(unit)
    summary = status_summary(STATE_DIR / f'host-{tablet.slug}.status.json', unit_pid(unit) if active else None)
    phase = None if summary['stale'] else summary['phase']
    if active and phase in (None, 'stopped', 'failed'):
        phase = 'starting'
    elif not active and phase not in ('failed',):
        phase = 'stopped'
    facts = tablet_facts(tablet)
    return {
        'slug': tablet.slug, 'label': tablet.label, 'model': tablet.model, 'state': tablet.state,
        'usb': tablet.usb, 'running': active, 'phase': phase, 'message': summary['message'],
        'stats': cache.get(('stats', tablet.slug), 2, lambda: last_stats(unit)) if active else {},
        'facts': {k: v for k, v in facts.items() if k != 'hevc_decoders'},
        'settings': settings_store.for_tablet(tablet.slug),
    }


def state():
    tablets = [t for t in list_tablets(ADB) if t.usb]
    return {
        'tablets': [tablet_entry(t) for t in tablets],
        'adb': ADB.is_file(),
        'profiles': PROFILES,
        'options': {k: {'values': v[0] if isinstance(v[0], list) else None, 'label': v[1]}
                    for k, v in settings_store.OPTIONS.items()},
        'time': time.time(),
    }


class Handler(BaseHTTPRequestHandler):
    server_version = 'tabs9/1'

    def log_message(self, fmt, *args):     # quiet; the terminal shows only the URL
        pass

    # -- helpers -----------------------------------------------------------
    def send_json(self, payload, status=HTTPStatus.OK):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(body)

    def send_text(self, text, content_type='text/plain; charset=utf-8', status=HTTPStatus.OK):
        body = text.encode()
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(body)

    def local_request(self):
        """Only the page itself may change anything: same-origin header and,
        when the browser sends one, a loopback Origin."""
        origin = self.headers.get('Origin')
        if self.headers.get('X-Requested-With') != 'tabs9':
            return False
        if origin and not (origin.startswith('http://127.0.0.1:') or origin.startswith('http://localhost:')):
            return False
        return True

    def body_json(self):
        length = int(self.headers.get('Content-Length') or 0)
        if length <= 0 or length > 65536:
            return {}
        try:
            return json.loads(self.rfile.read(length) or b'{}')
        except ValueError:
            return {}

    def slug_from(self, data, query):
        slug = (data or {}).get('slug') or (query.get('slug') or [''])[0]
        slug = ''.join(ch for ch in str(slug) if ch.isalnum() or ch == '_')[:64]
        return slug

    def tablet_by_slug(self, slug):
        return next((t for t in list_tablets(ADB) if t.usb and t.slug == slug), None)

    # -- routes ------------------------------------------------------------
    def do_GET(self):
        url = urlparse(self.path)
        query = parse_qs(url.query)
        if url.path in ('/', '/index.html'):
            return self.send_text(PAGE.read_text(), 'text/html; charset=utf-8')
        if url.path == '/api/state':
            return self.send_json(state())
        if url.path == '/api/logs':
            slug = self.slug_from(None, query)
            unit = unit_for(slug) if slug else 'app-tabs9.*'
            lines = [l for l in journal(unit, 120).splitlines() if not l.startswith('{')]
            return self.send_text('\n'.join(lines[-80:]))
        if url.path == '/api/settings':
            slug = self.slug_from(None, query)
            return self.send_json(settings_store.for_tablet(slug))
        return self.send_text('not found', status=HTTPStatus.NOT_FOUND)

    def do_POST(self):
        url = urlparse(self.path)
        if not self.local_request():
            return self.send_json({'error': 'refused'}, HTTPStatus.FORBIDDEN)
        data = self.body_json()
        slug = self.slug_from(data, {})
        if url.path == '/api/start':
            tablet = self.tablet_by_slug(slug)
            if tablet is None:
                return self.send_json({'error': 'that tablet is not attached'}, HTTPStatus.BAD_REQUEST)
            # Supervised start in the background; the page follows the phase.
            log = (STATE_DIR / f'ui-start-{slug}.log').open('w')
            subprocess.Popen([str(ROOT / 'tabs9'), 'start', '--tablet', tablet.model or tablet.label],
                             cwd=ROOT, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                             start_new_session=True)
            cache.drop(('stats', slug))
            return self.send_json({'ok': True})
        if url.path == '/api/stop':
            unit = unit_for(slug)
            if unit_active(unit):
                run(['systemctl', '--user', 'stop', unit], timeout=30)
            return self.send_json({'ok': True})
        if url.path == '/api/settings':
            values = data.get('settings') or {}
            saved = settings_store.set_for_tablet(slug, values)
            return self.send_json({'ok': True, 'settings': saved})
        if url.path == '/api/doctor':
            result = run([sys.executable, str(ROOT / 'scripts/setup.py'), '--doctor'], timeout=180)
            return self.send_json({'ok': result.returncode == 0, 'report': result.stdout + result.stderr})
        if url.path == '/api/quit':
            threading.Thread(target=self.server.shutdown, daemon=True).start()
            return self.send_json({'ok': True})
        return self.send_json({'error': 'not found'}, HTTPStatus.NOT_FOUND)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--port', type=int, default=8899)
    parser.add_argument('--open', action='store_true', help='open the page in the default browser')
    args = parser.parse_args(argv)
    server = ThreadingHTTPServer(('127.0.0.1', args.port), Handler)
    server.daemon_threads = True
    url = f'http://127.0.0.1:{args.port}/'
    print(f'tabs9 control panel: {url}  (Ctrl-C stops it)', flush=True)
    if args.open and (os.environ.get('DISPLAY') or os.environ.get('WAYLAND_DISPLAY')) and shutil.which('xdg-open'):
        subprocess.Popen(['xdg-open', url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
