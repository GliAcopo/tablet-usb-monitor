import os
from pathlib import Path
import socket
import struct
import sys
import threading
import tempfile
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import native_capture  # noqa: E402
from native_capture import (Frame, MAGIC_FRAME, MAGIC_RING, MAX_SLOTS, RING_FORMAT, Ring,  # noqa: E402
                            free_message, recv_ring, NativeCaptureError)


def ring_bytes(slots=6, modifier=0x0100000000000009, pitch=3072, height=1856):
    offsets = []
    pitches = []
    sizes = []
    for i in range(MAX_SLOTS):
        offsets += [0, pitch * height]
        pitches += [pitch, pitch]
        sizes.append(pitch * height * 3 // 2)
    return struct.pack(RING_FORMAT, MAGIC_RING, slots, 2960, 1848, modifier, *offsets, *pitches, *sizes)


class RenderDeviceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.sys = self.root / 'sys'
        self.dev = self.root / 'dev'
        self.dev.mkdir()

    def device(self, number, vendor):
        name = f'renderD{number}'
        directory = self.sys / name / 'device'
        directory.mkdir(parents=True, exist_ok=True)
        (directory / 'vendor').write_text(vendor)
        node = self.dev / name
        node.touch()
        return str(node)

    def select(self, preferred=None):
        return native_capture.select_render_node(preferred, sys_root=self.sys, dev_root=self.dev)

    def test_intel_selection_survives_reversed_boot_order(self):
        self.device(128, '0x10de')
        intel = self.device(129, '0x8086')
        self.assertEqual(self.select(), intel)
        self.device(128, '0x8086')
        self.device(129, '0x10de')
        self.assertEqual(self.select(), str(self.dev / 'renderD128'))

    def test_encoder_device_is_authoritative_and_must_be_intel(self):
        nvidia = self.device(128, '0x10de')
        intel = self.device(129, '0x8086')
        self.assertEqual(self.select(intel), intel)
        with self.assertRaises(NativeCaptureError):
            self.select(nvidia)

    def test_missing_or_ambiguous_intel_requires_explicit_selection(self):
        with self.assertRaises(NativeCaptureError):
            self.select()
        first = self.device(128, '0x8086')
        self.device(129, '0x8086')
        with self.assertRaises(NativeCaptureError):
            self.select()
        self.assertEqual(self.select(first), first)

    def test_selected_device_reaches_helper_and_failed_start_is_reaped(self):
        parent, child = MagicMock(), MagicMock()
        child.fileno.return_value = 99
        process = MagicMock()
        process.poll.return_value = 1
        with patch.object(native_capture, 'select_render_node', return_value='/dev/dri/renderD129'), \
             patch.object(native_capture.socket, 'socketpair', return_value=(parent, child)), \
             patch.object(native_capture.subprocess, 'Popen', return_value=process) as spawn, \
             patch.object(native_capture, 'recv_ring', side_effect=NativeCaptureError('startup failed')):
            with self.assertRaisesRegex(NativeCaptureError, 'startup failed'):
                native_capture.NativeCapture(3, 42, 2960, 1848, helper=Path(__file__),
                                             on_frame=MagicMock(), on_exit=MagicMock())
        argv = spawn.call_args.args[0]
        self.assertEqual(argv[argv.index('--render-node') + 1], '/dev/dri/renderD129')
        child.close.assert_called_once()
        parent.close.assert_called_once()
        process.wait.assert_called_once_with(timeout=2)


class MessageTests(unittest.TestCase):
    def test_ring_message_layout_matches_the_helper(self):
        ring = Ring(ring_bytes())
        self.assertEqual((ring.slots, ring.width, ring.height), (6, 2960, 1848))
        self.assertEqual(ring.drm_format, "NV12:0x0100000000000009")
        self.assertEqual(ring.offsets[0], (0, 3072 * 1856))
        self.assertEqual(ring.pitches[5], (3072, 3072))
        self.assertEqual(len(ring.sizes), 6)

    def test_bad_ring_is_refused(self):
        data = bytearray(ring_bytes())
        struct.pack_into("<I", data, 0, 0xDEADBEEF)
        with self.assertRaises(NativeCaptureError):
            Ring(bytes(data))
        with self.assertRaises(NativeCaptureError):
            Ring(ring_bytes(slots=MAX_SLOTS + 1))

    def test_frame_and_free_messages(self):
        data = struct.pack("<IIQQQQQ", MAGIC_FRAME, 3, 41, 123456789, 1000, 5000, 2)
        frame = Frame(data)
        self.assertEqual((frame.slot, frame.seq, frame.pts_ns, frame.dequeued_ns, frame.ready_ns, frame.dropped),
                         (3, 41, 123456789, 1000, 5000, 2))
        self.assertEqual(free_message(3), struct.pack("<II", 0x46524545, 3))

    def test_recv_ring_carries_one_fd_per_slot(self):
        a, b = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        fds = [os.open(os.devnull, os.O_RDONLY) for _ in range(6)]
        try:
            socket.send_fds(b, [ring_bytes()], fds)
            ring, received = recv_ring(a)
            self.assertEqual(ring.slots, 6)
            self.assertEqual(len(received), 6)
            for fd in received:
                os.close(fd)
        finally:
            for fd in fds:
                os.close(fd)
            a.close()
            b.close()

    def test_recv_ring_reports_a_helper_that_died_early(self):
        a, b = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        b.close()
        with self.assertRaises(NativeCaptureError):
            recv_ring(a)
        a.close()


if __name__ == "__main__":
    unittest.main()
