"""Choosing the virtual output among the streams KDE's RemoteDesktop returns."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'src'))
from host import select_virtual_stream, describe_wrong_source

LOGICAL = (1973, 1232)
PIXELS = (2960, 1848)


def stream(node, size, position=None, source_type=1):
    props = {'size': size, 'source_type': source_type}
    if position is not None:
        props['position'] = position
    return (node, props)


class SelectVirtualStreamTests(unittest.TestCase):
    def test_picks_the_virtual_node_among_all_screens(self):
        streams = [stream(41, (1463, 914), (0, 0)), stream(42, LOGICAL, (1463, 0))]
        self.assertEqual(select_virtual_stream(streams, LOGICAL, PIXELS, 1463)[0], 42)

    def test_accepts_native_pixel_size_too(self):
        self.assertEqual(select_virtual_stream([stream(7, PIXELS)], LOGICAL, PIXELS)[0], 7)

    def test_rejects_workspace_and_laptop_only(self):
        self.assertIsNone(select_virtual_stream([stream(40, (3436, 1232))], LOGICAL, PIXELS, 1463))
        self.assertIsNone(select_virtual_stream([stream(41, (1463, 914))], LOGICAL, PIXELS, 1463))

    def test_rejects_non_monitor_sources(self):
        self.assertIsNone(select_virtual_stream([stream(9, LOGICAL, source_type=4)], LOGICAL, PIXELS))

    def test_prefers_position_match_when_sizes_collide(self):
        streams = [stream(1, LOGICAL, (0, 0)), stream(2, LOGICAL, (1973, 0))]
        self.assertEqual(select_virtual_stream(streams, LOGICAL, PIXELS, 1973)[0], 2)

    def test_tolerates_malformed_entries(self):
        self.assertIsNone(select_virtual_stream([None, (1,), (2, {})], LOGICAL, PIXELS))


class DescribeWrongSourceTests(unittest.TestCase):
    outputs = [
        {'name': 'eDP-1', 'enabled': True, 'pos': {'x': 0, 'y': 0}, 'scale': 1.75, 'rotation': 1,
         'currentModeId': '1', 'modes': [{'id': '1', 'size': {'width': 2560, 'height': 1600}}]},
        {'name': 'Virtual-1', 'enabled': True, 'pos': {'x': 1463, 'y': 0}, 'scale': 1.5, 'rotation': 1,
         'currentModeId': '1', 'modes': [{'id': '1', 'size': {'width': 2960, 'height': 1848}}]},
    ]

    def test_names_the_workspace(self):
        self.assertIn('Workspace', describe_wrong_source((3436, 1232), self.outputs, LOGICAL))

    def test_names_a_screen(self):
        self.assertEqual(describe_wrong_source((2560, 1600), self.outputs, LOGICAL), 'the screen eDP-1')

    def test_falls_back_to_the_size(self):
        self.assertEqual(describe_wrong_source((100, 100), self.outputs, LOGICAL), 'a 100x100 source')


if __name__ == '__main__':
    unittest.main()
