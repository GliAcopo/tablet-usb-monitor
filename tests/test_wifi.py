"""wifi.py: pairing records, the tablet id in the advert, the certificate
the app pins, and the saved settings a Wi-Fi start leaves out."""
import hashlib
import json
import os
import socket
import ssl
import stat
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / 'src'))

import settings  # noqa: E402
import wifi  # noqa: E402


class Pairing(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())

    def test_save_load_and_private(self):
        record = wifi.save_pairing('sm_x910', 'SM X910', '2960x1848', self.dir)
        self.assertRegex(record['secret'], r'^[0-9a-f]{64}$')   # same shape as the USB token
        self.assertEqual(wifi.load_pairing('sm_x910', self.dir), record)
        mode = stat.S_IMODE(os.stat(wifi.record_path('sm_x910', self.dir)).st_mode)
        self.assertEqual(mode & 0o077, 0)
        again = wifi.save_pairing('sm_x910', 'SM X910', '2960x1848', self.dir)
        self.assertNotEqual(again['secret'], record['secret'])   # pairing again revokes the old one

    def test_bad_records_ignored(self):
        (self.dir / 'broken.json').write_text('{"secret": "short"}')
        (self.dir / 'junk.json').write_text('not json')
        self.assertEqual(wifi.paired(self.dir), [])
        with self.assertRaises(ValueError):
            wifi.record_path('../etc', self.dir)

    def test_choose(self):
        wifi.save_pairing('sm_x910', 'SM X910', None, self.dir)
        wifi.save_pairing('hmw_w09', 'HMW W09', None, self.dir)
        records = wifi.paired(self.dir)
        self.assertEqual(wifi.choose('sm', records)['slug'], 'sm_x910')
        self.assertEqual(wifi.choose('HMW-W09', records)['slug'], 'hmw_w09')
        with self.assertRaisesRegex(LookupError, 'more than one'):
            wifi.choose(None, records)
        with self.assertRaisesRegex(LookupError, 'no paired tablet matches'):
            wifi.choose('pixel', records)
        with self.assertRaisesRegex(LookupError, 'tabs9 pair'):
            wifi.choose(None, [])
        self.assertTrue(wifi.forget('hmw_w09', self.dir))
        self.assertEqual(wifi.choose(None, wifi.paired(self.dir))['slug'], 'sm_x910')

    def test_tablet_id_matches_the_app(self):
        # Link.kt: sha256("tabs9-id:" + secret) as hex, first 16 characters.
        secret = 'ab' * 32
        expected = hashlib.sha256(('tabs9-id:' + secret).encode()).hexdigest()[:16]
        self.assertEqual(wifi.tablet_id(secret), expected)
        self.assertNotIn(secret[:16], wifi.tablet_id(secret))


class Certificate(unittest.TestCase):
    def test_pinned_tls_round_trip(self):
        """The fingerprint wifi.py hands the app is the one the TLS server presents."""
        directory = Path(tempfile.mkdtemp())
        context = wifi.server_context(directory)
        cert, key = wifi.cert_paths(directory)
        self.assertEqual(stat.S_IMODE(os.stat(key).st_mode) & 0o077, 0)
        pin = wifi.fingerprint(cert)
        listener = socket.create_server(('127.0.0.1', 0))
        port = listener.getsockname()[1]

        def serve():
            conn, _ = listener.accept()
            with context.wrap_socket(conn, server_side=True) as tls:
                tls.sendall(tls.recv(64))
        threading.Thread(target=serve, daemon=True).start()
        client = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        client.check_hostname = False
        client.verify_mode = ssl.CERT_NONE     # like the app: trust by pin, not by authority
        with socket.create_connection(('127.0.0.1', port)) as raw, client.wrap_socket(raw) as tls:
            presented = hashlib.sha256(tls.getpeercert(binary_form=True)).hexdigest()
            tls.sendall(b'x' * 64)
            self.assertEqual(tls.recv(64), b'x' * 64)
        listener.close()
        self.assertEqual(presented, pin)
        # Created once: a second start keeps the certificate the tablet pinned.
        wifi.ensure_certificate(directory)
        self.assertEqual(wifi.fingerprint(cert), pin)


class Addresses(unittest.TestCase):
    def test_default_route_first_and_down_interfaces_dropped(self):
        addr = ('2: wlp1s0    inet 192.168.1.81/24 brd 192.168.1.255 scope global wlp1s0\n'
                '3: tailscale0    inet 100.104.68.62/32 scope global tailscale0\n'
                '4: virbr0    inet 192.168.122.1/24 scope global virbr0\n'
                '5: eth0    inet 10.0.0.5/24 scope global eth0\n')
        route = '1.1.1.1 via 10.0.0.1 dev eth0 src 10.0.0.5 uid 1000\n'

        def run(cmd, **_):
            return mock.Mock(stdout=route if 'route' in cmd else addr)

        def state(path):
            return 'down\n' if 'virbr0' in str(path) else 'up\n'
        with mock.patch.object(wifi.subprocess, 'run', side_effect=run), \
             mock.patch.object(Path, 'read_text', lambda self: state(self)):
            self.assertEqual(wifi.local_addresses(), ['10.0.0.5', '192.168.1.81', '100.104.68.62'])


class SavedSettings(unittest.TestCase):
    def test_wifi_start_leaves_usb_sized_settings_out(self):
        saved = {'side': 'left', 'profile': 'smooth', 'bitrate': 60000, 'scale': 2.0}
        with mock.patch.object(settings, 'for_tablet', return_value=saved):
            self.assertIn('--profile', settings.start_args('sm_x910'))
            wifi_args = settings.start_args('sm_x910', wifi=True)
        self.assertEqual(wifi_args, ['--side', 'left', '--scale', '2.0'])


if __name__ == '__main__':
    unittest.main()
