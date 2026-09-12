import os
from pathlib import Path
import socket
import struct
import sys
import threading
import unittest

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
