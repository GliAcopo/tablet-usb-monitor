"""The light picture: luma inverted, chroma and layout untouched."""
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

import picture  # noqa: E402


class LumaTableTests(unittest.TestCase):
    def test_black_and_white_swap_in_limited_range(self):
        self.assertEqual(picture.LUMA_TABLE[16], 235)
        self.assertEqual(picture.LUMA_TABLE[235], 16)
        self.assertEqual(picture.LUMA_TABLE[125], 126)      # mid grey stays mid grey

    def test_out_of_range_values_are_clamped_not_wrapped(self):
        self.assertEqual(picture.LUMA_TABLE[0], 235)
        self.assertEqual(picture.LUMA_TABLE[255], 16)


class InvertLumaTests(unittest.TestCase):
    def test_tight_layout(self):
        w, h = 4, 2
        y = bytes([16, 235, 100, 200] * h)
        uv = bytes([128, 128, 50, 200])
        out = picture.invert_luma(y + uv, w, h)
        self.assertEqual(out[:w * h], bytes([235, 16, 151, 51] * h))
        self.assertEqual(out[w * h:], uv)                    # chroma untouched

    def test_padded_layout_from_video_meta(self):
        w, h, stride, offset = 2, 2, 4, 8
        head = bytes(range(offset))
        rows = bytes([16, 235, 7, 7]) * h                     # 2 padding bytes per row
        uv = bytes([128] * 4)
        out = picture.invert_luma(head + rows + uv, w, h, stride, offset)
        self.assertEqual(out[:offset], head)
        self.assertEqual(out[offset:offset + 4], bytes([235, 16, 235, 235]))   # padding may change
        self.assertEqual(out[offset + stride * h:], uv)


class FragmentTests(unittest.TestCase):
    def test_fragment_bridges_an_appsink_to_an_appsrc(self):
        fragment = picture.LightPicture(1872, 1404, 30).fragment()
        self.assertTrue(fragment.startswith('! video/x-raw,format=NV12'))
        self.assertIn('appsink name=picture_in', fragment)
        self.assertIn('drop=false', fragment)                # never lose a ring slot
        self.assertIn('appsrc name=picture_out', fragment)
        self.assertIn('width=1872,height=1404', fragment)
        self.assertTrue(fragment.endswith('! vapostproc '))

    def test_host_defaults_and_settings_agree(self):
        import host, settings
        parser = host.build_parser()
        self.assertEqual(parser.get_default('light_picture'), settings.DEFAULTS['light_picture'])
        self.assertEqual(settings.OPTIONS['light_picture'][0], ['off', 'on'])


if __name__ == '__main__':
    unittest.main()
