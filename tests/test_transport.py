import asyncio
import collections
from pathlib import Path
import socket
import struct
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from host import Host, RESYNC


async def reader_with_deadline(reader, seconds):
    return await asyncio.wait_for(reader.readexactly(1), seconds)


class TransportTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.host = Host.__new__(Host)
        self.host.token = 'a' * 64
        self.host.clients = set()
        self.host.sent = collections.OrderedDict()
        self.host.resyncs = 0
        self.host.pipeline = None
        self.host.client_features = set()
        self.host.video_generation = 0
        self.host.video_connects = self.host.video_disconnects = self.host.heartbeats_sent = 0
        self.host.last_video_disconnect = None
        self.keyframe_requests = []
        self.idle_add = patch('host.GLib.idle_add', side_effect=lambda fn, *a: self.keyframe_requests.append(fn))
        self.idle_add.start()
        self.server = await asyncio.start_server(self.host.video, '127.0.0.1', 0)
        self.port = self.server.sockets[0].getsockname()[1]

    async def asyncTearDown(self):
        self.idle_add.stop()
        self.server.close()
        await self.server.wait_closed()

    async def read_packet(self, reader, timeout=1):
        length = struct.unpack('!I', await asyncio.wait_for(reader.readexactly(4), timeout))[0]
        return await asyncio.wait_for(reader.readexactly(length), timeout)

    async def connect(self, token):
        reader, writer = await asyncio.open_connection('127.0.0.1', self.port)
        writer.write(token)
        await writer.drain()
        await asyncio.sleep(.02)
        return reader, writer

    async def test_unauthorized_client_receives_no_video(self):
        reader, writer = await self.connect(b'b' * 64)
        self.assertEqual(await asyncio.wait_for(reader.read(), 1), b'')
        self.assertFalse(self.host.clients)
        writer.close()
        await writer.wait_closed()

    async def test_new_client_waits_for_keyframe_and_disconnect_cleans_up(self):
        reader, writer = await self.connect(b'a' * 64)
        self.assertEqual(len(self.host.clients), 1)
        self.host.distribute(b'not-a-keyframe', False, 1)
        payload = b'\x01' + struct.pack('!I', 2) + b'\x00\x00\x00\x01synthetic-hevc-access-unit'
        packet = struct.pack('!I', len(payload)) + payload
        self.host.distribute(packet, True, 2)
        length = struct.unpack('!I', await asyncio.wait_for(reader.readexactly(4), 1))[0]
        self.assertEqual(await reader.readexactly(length), payload)
        writer.close()
        await writer.wait_closed()
        await asyncio.sleep(.02)
        self.assertFalse(self.host.clients)

    async def test_slow_client_resyncs_at_a_keyframe_instead_of_skipping_reference_frames(self):
        queue = asyncio.Queue(maxsize=2)
        self.host.clients.add(queue)
        self.host.distribute(b'frame1', True, 1)
        self.host.distribute(b'frame2', False, 2)
        self.host.distribute(b'frame3', False, 3)
        # The client stays connected; its backlog is replaced by a resync marker.
        self.assertIn(queue, self.host.clients)
        self.assertIs(queue.get_nowait(), RESYNC)
        self.assertTrue(queue.empty())
        self.assertEqual(self.host.resyncs, 1)

    async def test_resynced_client_skips_dependent_frames_until_next_keyframe(self):
        reader, writer = await self.connect(b'a' * 64)
        queue = next(iter(self.host.clients))
        keyframe = struct.pack('!I', 5) + b'\x01' + struct.pack('!I', 1)
        self.host.distribute(keyframe, True, 1)
        self.assertEqual(await asyncio.wait_for(reader.readexactly(9), 1), keyframe)
        queue.put_nowait(RESYNC)
        self.host.distribute(struct.pack('!I', 5) + b'\x01' + struct.pack('!I', 2), False, 2)
        idr = struct.pack('!I', 5) + b'\x01' + struct.pack('!I', 3)
        self.host.distribute(idr, True, 3)
        self.assertEqual(await asyncio.wait_for(reader.readexactly(9), 1), idr)
        writer.close()
        await writer.wait_closed()

    async def test_new_video_client_triggers_a_keyframe_request(self):
        reader, writer = await self.connect(b'a' * 64)
        self.assertEqual(self.keyframe_requests, [self.host.request_keyframe])
        self.assertEqual(self.host.video_connects, 1)
        writer.close()
        await writer.wait_closed()

    async def test_heartbeats_flow_only_to_clients_that_advertised_them(self):
        """A static desktop produces no frames; the heartbeat keeps the
        client's read deadline from firing. Older clients (no feature
        advertised) treat an unknown packet type as a framing error, so
        they must get nothing at all while idle."""
        Host.HEARTBEAT_INTERVAL = 0.05
        try:
            reader, writer = await self.connect(b'a' * 64)
            with self.assertRaises(asyncio.TimeoutError):
                await reader_with_deadline(reader, .3)
            self.host.client_features = {'video_heartbeat'}
            beats = [await self.read_packet(reader) for _ in range(3)]
            self.assertEqual([b[0] for b in beats], [2, 2, 2])
            self.assertEqual([struct.unpack('!I', b[1:])[0] for b in beats], [1, 2, 3])
            self.assertEqual(len(beats[0]), 5)  # never a 1-byte packet: clients reject size <= 1
            self.assertGreaterEqual(self.host.heartbeats_sent, 3)
            # Frames still interleave whole: a keyframe arriving between beats
            # comes out as one packet, on the same writer.
            idr = struct.pack('!I', 5) + b'\x01' + struct.pack('!I', 3)
            self.host.distribute(idr, True, 3)
            packets = [await self.read_packet(reader) for _ in range(4)]
            self.assertIn(idr[4:], packets)
            self.assertTrue(all(p[0] in (1, 2) for p in packets))
            writer.close()
            await writer.wait_closed()
        finally:
            Host.HEARTBEAT_INTERVAL = 1.0

    async def test_blocked_reader_is_dropped_after_the_drain_timeout(self):
        Host.DRAIN_TIMEOUT = 0.3
        try:
            reader, writer = await self.connect(b'a' * 64)
            writer.get_extra_info('socket').setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
            payload = b'\x01' + struct.pack('!I', 1) + b'x' * (1 << 20)
            packet = struct.pack('!I', len(payload)) + payload
            for seq in range(4):
                self.host.distribute(packet, True, seq)
                await asyncio.sleep(.05)
            await asyncio.sleep(.6)
            self.assertFalse(self.host.clients)
            self.assertEqual(self.host.last_video_disconnect, 'write timeout')
            writer.close()
            await writer.wait_closed()
        finally:
            Host.DRAIN_TIMEOUT = 2.0

    async def test_drop_video_clients_closes_sockets_without_touching_control(self):
        reader, writer = await self.connect(b'a' * 64)
        self.host.aio = asyncio.get_running_loop()
        self.host.drop_video_clients()
        self.assertEqual(await asyncio.wait_for(reader.read(), 1), b'')
        self.assertFalse(self.host.clients)
        self.assertEqual(self.host.last_video_disconnect, 'client closed')
        writer.close()
        await writer.wait_closed()

    async def test_timing_history_is_bounded(self):
        for seq in range(1300): self.host.distribute(b'', False, seq)
        self.assertEqual(len(self.host.sent), 1200)
        self.assertNotIn(0, self.host.sent)


if __name__ == '__main__':
    unittest.main()
