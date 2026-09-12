"""Placement of the virtual output to the right of the physical ones."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'src'))
from host import compute_virtual_position


def out(name, x, y, w, h, scale=1.0, enabled=True, rotation=1):
    return {'name': name, 'enabled': enabled, 'pos': {'x': x, 'y': y}, 'scale': scale,
            'rotation': rotation, 'currentModeId': '1',
            'modes': [{'id': '1', 'size': {'width': w, 'height': h}}]}


class PlacementTests(unittest.TestCase):
    def test_single_output_right_edge_uses_logical_width(self):
        outputs = [out('eDP-1', 0, 0, 2560, 1600, scale=1.75)]
        self.assertEqual(compute_virtual_position(outputs, {'eDP-1'}), (1463, 0))

    def test_multiple_outputs_take_the_rightmost_edge(self):
        outputs = [out('eDP-1', 0, 0, 2560, 1600, scale=2.0), out('HDMI-1', 1280, 0, 1920, 1080)]
        self.assertEqual(compute_virtual_position(outputs, {'eDP-1', 'HDMI-1'}), (3200, 0))

    def test_ignores_disabled_and_new_outputs(self):
        outputs = [out('eDP-1', 0, 0, 1000, 500), out('HDMI-1', 5000, 0, 1000, 500, enabled=False),
                   out('Virtual-1', 9000, 0, 100, 100)]
        self.assertEqual(compute_virtual_position(outputs, {'eDP-1', 'HDMI-1'}), (1000, 0))

    def test_rotation_swaps_dimensions(self):
        outputs = [out('eDP-1', 0, 0, 1920, 1080, rotation=2)]
        self.assertEqual(compute_virtual_position(outputs, {'eDP-1'}), (1080, 0))

    def test_negative_position_never_yields_negative_result(self):
        outputs = [out('eDP-1', -3000, 0, 1920, 1080)]
        self.assertEqual(compute_virtual_position(outputs, {'eDP-1'}), (0, 0))

    def test_malformed_mode_falls_back_to_a_positive_extent(self):
        outputs = [{'name': 'eDP-1', 'enabled': True, 'pos': {'x': 0, 'y': 0}, 'scale': None,
                    'currentModeId': 'nope', 'modes': 'garbage'}]
        x, y = compute_virtual_position(outputs, {'eDP-1'})
        self.assertGreater(x, 0)
        self.assertEqual(y, 0)

    def test_no_outputs_places_at_origin(self):
        self.assertEqual(compute_virtual_position([], set()), (0, 0))


if __name__ == '__main__':
    unittest.main()
