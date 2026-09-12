import ctypes
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from eis_touch import EisTouch  # noqa: E402


class FakeLib:
    def __init__(self, regions):
        self.regions = regions
        self.started = []
        self.stopped = []

    def ei_device_get_region(self, device, index):
        values = self.regions[int(device)]
        return (int(device), index) if index < len(values) else None

    def _region(self, value):
        return self.regions[value[0]][value[1]]

    def ei_region_get_x(self, value): return self._region(value)[0]
    def ei_region_get_y(self, value): return self._region(value)[1]
    def ei_region_get_width(self, value): return self._region(value)[2]
    def ei_region_get_height(self, value): return self._region(value)[3]
    def ei_device_start_emulating(self, device, sequence): self.started.append((int(device), sequence))
    def ei_device_stop_emulating(self, device): self.stopped.append(int(device))


def controller(regions, target):
    value = EisTouch.__new__(EisTouch)
    value.lib = FakeLib(regions)
    value.ei = 99
    value._target_region = lambda: target[0]
    value.device = None
    value.region = None
    value.ready = False
    value._sequence = 0
    value._touches = {}
    value._devices = {key: key for key in regions}
    value._resumed = set(regions)
    value._closed = False
    return value


class EisBindingTests(unittest.TestCase):
    def test_logged_stale_geometry_rebinds_to_live_tablet_region(self):
        target = [(1463, 0, 1973, 1232)]
        touch = controller({1: [(1974, 0, 1463, 914), (0, 0, 1973, 1232)]}, target)
        self.assertFalse(touch.refresh_binding())
        target[0] = (0, 0, 1973, 1232)
        self.assertTrue(touch.refresh_binding())
        self.assertEqual((touch.region.x, touch.region.y), (0, 0))

    def test_nonmatching_later_device_does_not_replace_match(self):
        touch = controller({1: [(0, 0, 1973, 1232)], 2: [(1974, 0, 1463, 914)]},
                           [(0, 0, 1973, 1232)])
        self.assertTrue(touch.refresh_binding())
        self.assertEqual(touch.device, 1)

    def test_ambiguous_matching_devices_fail_closed(self):
        touch = controller({1: [(0, 0, 1973, 1232)], 2: [(0, 0, 1973, 1232)]},
                           [(0, 0, 1973, 1232)])
        self.assertFalse(touch.refresh_binding())
        self.assertFalse(touch.ready)

    def test_layout_change_drops_old_binding_until_matching_region_arrives(self):
        target = [(0, 0, 1973, 1232)]
        touch = controller({1: [(0, 0, 1973, 1232)]}, target)
        self.assertTrue(touch.refresh_binding())
        target[0] = (1463, 0, 1973, 1232)
        self.assertFalse(touch.refresh_binding())
        self.assertIsNone(touch.device)


if __name__ == '__main__':
    unittest.main()
