import struct
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import audio  # noqa: E402
import settings  # noqa: E402


class AudioTest(unittest.TestCase):
    def test_packet_is_length_prefixed_type_3_with_a_sequence(self):
        pcm = bytes(range(16))
        packet = audio.audio_packet(0x1_0000_0002, pcm)
        (length,) = struct.unpack('!I', packet[:4])
        self.assertEqual(length, len(packet) - 4)
        self.assertEqual(packet[4:5], b'\x03')
        self.assertEqual(struct.unpack('!I', packet[5:9])[0], 2)   # wraps at 32 bits
        self.assertEqual(packet[9:], pcm)

    def test_digital_silence_is_recognised(self):
        self.assertTrue(audio.is_silent(bytes(4096)))
        self.assertFalse(audio.is_silent(bytes(4095) + b'\x01'))

    def test_the_sink_never_claims_the_default_by_itself(self):
        args = audio.module_args('sm_x910', 'SM "X910"')
        self.assertEqual(args[0], 'module-null-sink')
        self.assertIn('sink_name=tabs9.sm_x910', args)
        props = args[-1]
        self.assertTrue(props.startswith('sink_properties='))
        self.assertIn('priority.session=0', props)
        self.assertIn('monitor.channel-volumes=true', props)
        self.assertIn('device.description="tabs9 SM \'X910\'"', props)

    def test_only_this_tablets_leftover_sink_is_unloaded(self):
        listing = ('7\tmodule-null-sink\tsink_name=tabs9.sm_x910 rate=48000\t\n'
                   '8\tmodule-null-sink\tsink_name=tabs9.hmw_w09 rate=48000\t\n'
                   '9\tmodule-null-sink\tsink_name=tabs9.sm_x9100\t\n'
                   '10\tmodule-loopback\tsink_name=tabs9.sm_x910\t\n')
        self.assertEqual(audio.our_modules(listing, 'sm_x910'), ['7'])

    def test_option_matches_the_host(self):
        import host
        parser = host.build_parser()
        self.assertEqual(parser.get_default('audio'), settings.DEFAULTS['audio'])
        action = next(a for a in parser._actions if a.dest == 'audio')
        self.assertEqual(sorted(action.choices), sorted(settings.OPTIONS['audio'][0]))


if __name__ == '__main__':
    unittest.main()
