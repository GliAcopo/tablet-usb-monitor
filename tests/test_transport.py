import asyncio
import collections
from pathlib import Path
import struct
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from host import Host


class TransportTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.host = Host.__new__(Host)
        self.host.token = 'a' * 64
        self.host.clients = set()
        self.host.sent = collections.OrderedDict()
        self.server = await asyncio.start_server(self.host.video, '127.0.0.1', 0)
        self.port = self.server.sockets[0].getsockname()[1]

    async def asyncTearDown(self):
        self.server.close()
        await self.server.wait_closed()

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

    async def test_slow_client_is_reset_instead_of_skipping_reference_frames(self):
        queue = asyncio.Queue(maxsize=2)
        self.host.clients.add(queue)
        self.host.distribute(b'frame1', True, 1)
        self.host.distribute(b'frame2', False, 2)
        self.host.distribute(b'frame3', False, 3)
        self.assertNotIn(queue, self.host.clients)
        self.assertIsNone(queue.get_nowait())
        self.assertTrue(queue.empty())

    async def test_timing_history_is_bounded(self):
        for seq in range(1300): self.host.distribute(b'', False, seq)
        self.assertEqual(len(self.host.sent), 1200)
        self.assertNotIn(0, self.host.sent)


if __name__ == '__main__':
    unittest.main()
