"""tablets.py (device parsing, choice, slug), settings.py (validation, start
arguments) and the --side layout in host.py."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / 'src'))

import settings  # noqa: E402
from host import compute_layout  # noqa: E402
from tablets import Tablet, TabletChoice, choose, parse_devices, slugify  # noqa: E402

DEVICES = ('List of devices attached\n'
           'R52W60AX1MA            device usb:4-2 product:gts9uwifi model:SM_X910 device:gts9uwifi transport_id:3\n'
           'TGPYD22718200258       unauthorized usb:3-1 transport_id:2\n'
           'emulator-5554          device product:sdk_gphone64 model:sdk_gphone64_x86_64 transport_id:1\n')


class Tablets(unittest.TestCase):
    def test_parse(self):
        tablets = parse_devices(DEVICES)
        self.assertEqual([t.state for t in tablets], ['device', 'unauthorized', 'device'])
        self.assertEqual(tablets[0].label, 'SM X910')
        self.assertEqual(tablets[0].slug, 'sm_x910')
        self.assertTrue(tablets[0].usb and tablets[1].usb and not tablets[2].usb)
        self.assertEqual(tablets[0].target(), ['-s', 'R52W60AX1MA'])
        self.assertEqual(tablets[1].label, 'Android device')

    def test_no_permissions(self):
        t = parse_devices('List of devices attached\nabc no permissions (user in plugdev group; are your udev rules wrong?); see [url] usb:3-1\n')
        self.assertEqual(t[0].state, 'no permissions')

    def test_choose_by_model_part(self):
        tablets = parse_devices(DEVICES)
        self.assertEqual(choose('sm', tablets).slug, 'sm_x910')
        self.assertEqual(choose('SM-X910', tablets).slug, 'sm_x910')
        with self.assertRaises(TabletChoice):
            choose('pixel', tablets)

    def test_choose_without_selector(self):
        tablets = parse_devices(DEVICES)
        # Two USB devices, one authorized: that one.
        self.assertEqual(choose(None, tablets).slug, 'sm_x910')
        both = [Tablet('a', 'device', 'SM_X910', transport='usb:1'), Tablet('b', 'device', 'HMW_W09', transport='usb:2')]
        with self.assertRaises(TabletChoice) as caught:
            choose(None, both)
        self.assertNotIn('a', str(caught.exception).split())   # models, not serials
        self.assertIn('HMW W09', str(caught.exception))
        self.assertEqual(choose(None, both[:1]).slug, 'sm_x910')

    def test_slugify(self):
        self.assertEqual(slugify('HMW-W09'), 'hmw_w09')
        self.assertEqual(slugify(''), 'tablet')


class Settings(unittest.TestCase):
    def test_validate(self):
        clean = settings.validate({'side': 'left', 'profile': 'nope', 'fps': '60', 'bitrate': 999,
                                   'scale': '2', 'resolution': '1872X1404', 'gap': 3, 'bogus': 1,
                                   'scroll_gain': 0.5, 'remote': ''})
        self.assertEqual(clean, {'side': 'left', 'fps': 60, 'scale': 2.0, 'resolution': '1872x1404',
                                 'gap': 3, 'scroll_gain': 0.5})

    def test_roundtrip_and_args(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings.SETTINGS_FILE = Path(tmp) / 'settings.json'
            settings.set_for_tablet('hmw_w09', {'side': 'left', 'profile': 'light', 'pen_button': 'off'})
            settings.set_for_tablet('sm_x910', {'scale': 2})
            self.assertEqual(settings.for_tablet('hmw_w09'), {'side': 'left', 'profile': 'light', 'pen_button': 'off'})
            args = settings.start_args('hmw_w09')
            self.assertEqual(args, ['--side', 'left', '--profile', 'light', '--pen-button', 'off'])
            self.assertEqual(settings.start_args('unknown'), [])
            data = json.loads(settings.SETTINGS_FILE.read_text())
            self.assertEqual(set(data['tablets']), {'hmw_w09', 'sm_x910'})


def out(name, x, y, w, h, scale=1.0):
    return {'name': name, 'enabled': True, 'pos': {'x': x, 'y': y}, 'scale': scale, 'rotation': 1,
            'currentModeId': '1', 'modes': [{'id': '1', 'size': {'width': w, 'height': h}}]}


class Sides(unittest.TestCase):
    laptop = [out('eDP-1', 0, 0, 2560, 1600, scale=1.75)]      # 1463x914 logical

    def test_right_moves_nothing(self):
        pos, moves = compute_layout(self.laptop, {'eDP-1'}, side='right', gap=1, scale=1.5, width=1872, height=1404)
        self.assertEqual((pos, moves), ((1464, 0), []))

    def test_left_puts_tablet_at_origin_and_shifts_laptop(self):
        pos, moves = compute_layout(self.laptop, {'eDP-1'}, side='left', gap=1, scale=1.5, width=1872, height=1404)
        self.assertEqual(pos, (0, 0))
        # 1872 / 1.5 = 1248 logical + 1 gap = 1249, nudged to 1252 so that
        # 1252 * 1.75 = 2191 is a whole device pixel on the laptop.
        self.assertEqual(moves, [('eDP-1', 1252, 0)])

    def test_top_and_bottom(self):
        pos, moves = compute_layout(self.laptop, {'eDP-1'}, side='top', gap=1, scale=1.5, width=1872, height=1404)
        self.assertEqual(pos, (0, 0))
        self.assertEqual(moves[0][0], 'eDP-1')
        self.assertGreaterEqual(moves[0][2], 936 + 1)
        pos, moves = compute_layout(self.laptop, {'eDP-1'}, side='bottom', gap=1, scale=1.5, width=1872, height=1404)
        self.assertEqual(moves, [])
        self.assertGreaterEqual(pos[1], 915)

    def test_no_outputs(self):
        self.assertEqual(compute_layout([], set(), side='left', width=100, height=100), ((0, 0), []))


if __name__ == '__main__':
    unittest.main()
