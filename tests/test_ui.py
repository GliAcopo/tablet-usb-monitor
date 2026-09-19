"""HTTP API tests for the tabs9 control panel (src/ui.py).

Covers: empty state, unauthorized tablet, missing adb, stale/missing status
file, phase=failed, invalid settings, two tablets, CSRF header, unknown slug,
version in /api/state, option metadata, /api/quit, settings roundtrip.
"""
import http.client
import json
import socket
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / 'src'))
sys.path.insert(0, str(REPO_ROOT / 'scripts'))

import settings as settings_store
import ui
from status import StatusWriter
from tablets import Tablet


def make_tablet(serial, state, model='', product='', transport=''):
    return Tablet(serial, state, model, product, transport)


def _free_port():
    s = socket.socket()
    s.bind(('127.0.0.1', 0))
    port = s.getsockname()[1]
    s.close()
    return port


class UITestBase(unittest.TestCase):
    """Base class that starts a test server with mocked tablets."""

    TABLETS = []
    ADB_EXISTS = True

    @classmethod
    def setUpClass(cls):
        cls.tmpdir = tempfile.TemporaryDirectory()
        cls.state_dir = Path(cls.tmpdir.name)
        cls.settings_file = cls.state_dir / 'settings.json'

        cls.patches = [
            mock.patch.object(ui, 'STATE_DIR', cls.state_dir),
            mock.patch.object(settings_store, 'SETTINGS_FILE', cls.settings_file),
            mock.patch.object(ui, 'list_tablets', return_value=cls.TABLETS),
            mock.patch.object(ui, 'ADB',
                              mock.MagicMock(is_file=mock.MagicMock(return_value=cls.ADB_EXISTS))),
            mock.patch.object(ui, 'unit_active', return_value=False),
            mock.patch.object(ui, 'unit_pid', return_value=None),
            mock.patch.object(ui, 'journal', return_value=''),
            mock.patch.object(ui, 'tablet_facts', return_value={}),
        ]
        for p in cls.patches:
            p.start()

        cls.port = _free_port()
        from http.server import ThreadingHTTPServer
        cls.server = ThreadingHTTPServer(('127.0.0.1', cls.port), ui.Handler)
        cls.server.daemon_threads = True
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        for p in cls.patches:
            p.stop()
        cls.tmpdir.cleanup()

    def get(self, path):
        conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=5)
        conn.request('GET', path)
        resp = conn.getresponse()
        body = resp.read()
        conn.close()
        return resp, body

    def get_json(self, path):
        resp, body = self.get(path)
        return resp, json.loads(body)

    def post(self, path, data=None):
        conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=5)
        hdrs = {'Content-Type': 'application/json', 'X-Requested-With': 'tabs9'}
        body = json.dumps(data or {}).encode()
        conn.request('POST', path, body=body, headers=hdrs)
        resp = conn.getresponse()
        rbody = resp.read()
        conn.close()
        return resp, json.loads(rbody)

    def post_no_auth(self, path, data=None):
        conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=5)
        hdrs = {'Content-Type': 'application/json'}
        body = json.dumps(data or {}).encode()
        conn.request('POST', path, body=body, headers=hdrs)
        resp = conn.getresponse()
        rbody = resp.read()
        conn.close()
        return resp, json.loads(rbody)


# ── No tablets ──────────────────────────────────────────────────────

class TestNoTablets(UITestBase):
    TABLETS = []

    def test_state_empty(self):
        resp, data = self.get_json('/api/state')
        self.assertEqual(resp.status, 200)
        self.assertEqual(data['tablets'], [])
        self.assertTrue(data['adb'])

    def test_version_present(self):
        _, data = self.get_json('/api/state')
        self.assertIn('version', data)
        self.assertIsInstance(data['version'], str)
        self.assertGreater(len(data['version']), 0)

    def test_options_every_settings_key(self):
        _, data = self.get_json('/api/state')
        self.assertEqual(list(data["options"]), list(settings_store.OPTIONS))

    def test_options_metadata_list_type(self):
        _, data = self.get_json('/api/state')
        side = data['options']['side']
        self.assertEqual(side['type'], 'list')
        self.assertIsInstance(side['values'], list)
        self.assertIn('label', side)
        self.assertIn('left', side['values'])

    def test_options_metadata_range_type(self):
        _, data = self.get_json('/api/state')
        scale = data['options']['scale']
        self.assertEqual(scale['type'], 'range')
        self.assertEqual(scale['min'], 1.0)
        self.assertEqual(scale['max'], 3.0)
        self.assertEqual(scale['step'], 0.1)

    def test_options_metadata_range_int(self):
        _, data = self.get_json('/api/state')
        gap = data['options']['gap']
        self.assertEqual(gap['type'], 'range')
        self.assertEqual(gap['step'], 1)

    def test_options_metadata_resolution_type(self):
        _, data = self.get_json('/api/state')
        res = data['options']['resolution']
        self.assertEqual(res['type'], 'resolution')

    def test_get_logs_unknown_slug(self):
        resp, _ = self.get('/api/logs?slug=unknown')
        self.assertEqual(resp.status, 200)

    def test_get_not_found(self):
        resp, _ = self.get('/api/nope')
        self.assertEqual(resp.status, 404)


# ── CSRF / auth ─────────────────────────────────────────────────────

class TestCSRF(UITestBase):
    TABLETS = []

    def test_post_without_auth_header_returns_403(self):
        resp, data = self.post_no_auth('/api/start', {'slug': 'test'})
        self.assertEqual(resp.status, 403)
        self.assertIn('error', data)

    def test_post_with_auth_header_accepted(self):
        resp, _ = self.post('/api/start', {'slug': 'nonexistent'})
        # 400 because tablet not found, but NOT 403
        self.assertIn(resp.status, (200, 400))


# ── No ADB ──────────────────────────────────────────────────────────

class TestNoAdb(UITestBase):
    TABLETS = []
    ADB_EXISTS = False

    def test_adb_missing(self):
        _, data = self.get_json('/api/state')
        self.assertFalse(data['adb'])


# ── Unauthorized tablet ─────────────────────────────────────────────

class TestUnauthorized(UITestBase):
    TABLETS = [make_tablet('FAKE002', 'unauthorized', model='HMW_W09', transport='usb:3-1')]

    def test_unauthorized_state(self):
        _, data = self.get_json('/api/state')
        self.assertEqual(len(data['tablets']), 1)
        self.assertEqual(data['tablets'][0]['state'], 'unauthorized')


# ── One device tablet ───────────────────────────────────────────────

class TestOneTablet(UITestBase):
    TABLETS = [make_tablet('FAKE001', 'device', 'SM_X910', transport='usb:4-2')]

    def test_active_unit_no_status_shows_starting(self):
        with mock.patch.object(ui, 'unit_active', return_value=True), \
             mock.patch.object(ui, 'unit_pid', return_value=12345):
            _, data = self.get_json('/api/state')
            self.assertEqual(data['tablets'][0]['phase'], 'starting')

    def test_phase_failed_with_message(self):
        t = self.TABLETS[0]
        status_file = self.state_dir / f'host-{t.slug}.status.json'
        writer = StatusWriter(status_file)
        writer.write('failed', 'Boom something broke')
        with mock.patch.object(ui, 'unit_active', return_value=False):
            _, data = self.get_json('/api/state')
            self.assertEqual(data['tablets'][0]['phase'], 'failed')
            self.assertEqual(data['tablets'][0]['message'], 'Boom something broke')

    def test_settings_save_invalid_values_stripped(self):
        resp, data = self.post('/api/settings', {
            'slug': self.TABLETS[0].slug,
            'settings': {'side': 'left', 'scale': 999.0, 'invalid_key': 'x'}
        })
        self.assertEqual(resp.status, 200)
        self.assertTrue(data['ok'])
        self.assertEqual(data['settings']['side'], 'left')
        self.assertNotIn('scale', data['settings'])
        self.assertNotIn('invalid_key', data['settings'])

    def test_settings_roundtrip(self):
        slug = self.TABLETS[0].slug
        # Save
        resp, data = self.post('/api/settings', {
            'slug': slug,
            'settings': {'side': 'right', 'scale': 1.5, 'fps': 60}
        })
        self.assertEqual(resp.status, 200)
        self.assertEqual(data['settings']['side'], 'right')
        self.assertEqual(data['settings']['scale'], 1.5)
        self.assertEqual(data['settings']['fps'], 60)
        # Read back
        resp2, read_data = self.get_json(f'/api/settings?slug={slug}')
        self.assertEqual(resp2.status, 200)
        self.assertEqual(read_data['side'], 'right')
        self.assertEqual(read_data['scale'], 1.5)
        self.assertEqual(read_data['fps'], 60)

    def test_stopped_phase_when_unit_inactive(self):
        # Remove any leftover status file from previous tests.
        status_file = self.state_dir / f'host-{self.TABLETS[0].slug}.status.json'
        status_file.unlink(missing_ok=True)
        with mock.patch.object(ui, 'unit_active', return_value=False):
            _, data = self.get_json('/api/state')
            self.assertEqual(data['tablets'][0]['phase'], 'stopped')


# ── Two tablets ─────────────────────────────────────────────────────

class TestTwoTablets(UITestBase):
    TABLETS = [
        make_tablet('FAKE001', 'device', 'SM_X910', transport='usb:4-2'),
        make_tablet('FAKE002', 'device', 'HMW_W09', transport='usb:3-1'),
    ]

    def test_two_tablets_present(self):
        _, data = self.get_json('/api/state')
        self.assertEqual(len(data['tablets']), 2)
        slugs = {t['slug'] for t in data['tablets']}
        self.assertEqual(slugs, {'sm_x910', 'hmw_w09'})


# ── /api/quit ───────────────────────────────────────────────────────

class TestQuit(UITestBase):
    """Separate class because /api/quit shuts the server down."""
    TABLETS = []

    def test_quit_returns_ok(self):
        resp, data = self.post('/api/quit')
        self.assertEqual(resp.status, 200)
        self.assertTrue(data['ok'])


# ── Serve page ──────────────────────────────────────────────────────

class TestServePage(UITestBase):
    TABLETS = []

    def test_index_returns_html(self):
        resp, body = self.get('/')
        self.assertEqual(resp.status, 200)
        self.assertIn('text/html', resp.getheader('Content-Type'))
        self.assertIn(b'tabs9', body)


# ── option_meta helper unit tests ───────────────────────────────────

class TestOptionMeta(unittest.TestCase):
    def test_list(self):
        m = ui.option_meta('side', (['left', 'right'], 'Side'))
        self.assertEqual(m['type'], 'list')
        self.assertEqual(m['values'], ['left', 'right'])
        self.assertEqual(m['label'], 'Side')

    def test_range_int(self):
        m = ui.option_meta('gap', ((0, 64), 'Gap'))
        self.assertEqual(m['type'], 'range')
        self.assertEqual(m['min'], 0)
        self.assertEqual(m['max'], 64)
        self.assertEqual(m['step'], 1)

    def test_range_float(self):
        m = ui.option_meta('scale', ((1.0, 3.0), 'Scale'))
        self.assertEqual(m['type'], 'range')
        self.assertEqual(m['step'], 0.1)

    def test_resolution(self):
        m = ui.option_meta('resolution', ('resolution', 'Res'))
        self.assertEqual(m['type'], 'resolution')

    def test_version_not_empty(self):
        self.assertIsInstance(ui.SERVER_VERSION, str)
        self.assertGreater(len(ui.SERVER_VERSION), 0)


if __name__ == '__main__':
    unittest.main()


class TestDefaults(unittest.TestCase):
    """The panel shows the host's own defaults for options that are not set."""

    def test_defaults_match_the_host_parser(self):
        import host
        parser = host.build_parser() if hasattr(host, 'build_parser') else None
        if parser is None:
            self.skipTest('host has no build_parser()')
        for key, value in settings_store.DEFAULTS.items():
            self.assertEqual(parser.get_default(key), value, key)

    def test_defaults_are_in_the_option_metadata(self):
        meta = ui.option_meta('side', settings_store.OPTIONS['side'])
        self.assertEqual(meta['default'], 'right')
        self.assertNotIn('default', ui.option_meta('fps', settings_store.OPTIONS['fps']))
