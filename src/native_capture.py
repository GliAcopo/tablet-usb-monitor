"""Host side of the native capture helper (native/tabs9-capture).

The helper owns the PipeWire stream and the RGB->NV12 conversion; this module
owns the process, the socket, and the ring of NV12 DMA-BUFs it exported:
each slot is wrapped once in a GstMemory, every converted frame becomes a
GstBuffer sharing that memory (plus the video meta the encoder needs to read
a tiled NV12 surface), and a slot goes back to the helper once the encoder
has finished with it.

Message layouts mirror the C structs in native/tabs9-capture.c.
"""
from __future__ import annotations

import os
from pathlib import Path
import socket
import struct
import subprocess
import threading
from typing import Callable

MAX_SLOTS = 8
MAGIC_RING = 0x52494E47
MAGIC_FRAME = 0x4652414D
MAGIC_FREE = 0x46524545
RING_FORMAT = '<IIIIQ' + f'{MAX_SLOTS * 2}I' + f'{MAX_SLOTS * 2}I' + f'{MAX_SLOTS}I'
FRAME_FORMAT = '<IIQQQQQ'
FRAME_SIZE = struct.calcsize(FRAME_FORMAT)
FREE_FORMAT = '<II'

HELPER = Path(__file__).resolve().parents[1] / 'native' / 'tabs9-capture'


class NativeCaptureError(RuntimeError):
    """The helper is missing, failed to negotiate, or died."""


def select_render_node(preferred: str | None = None, *,
                       sys_root: Path = Path('/sys/class/drm'),
                       dev_root: Path = Path('/dev/dri')) -> str:
    """Find Intel by PCI vendor, never by the boot-dependent renderD number.

    The current helper negotiates Intel Tile4 surfaces. When the encoder
    supplies its device, require that exact GPU rather than a cross-GPU copy.
    """
    candidates = [Path(preferred).resolve()] if preferred else sorted(dev_root.glob('renderD*'))
    intel = []
    for node in candidates:
        try:
            vendor = (sys_root / node.name / 'device/vendor').read_text().strip().lower()
        except OSError:
            continue
        if node.exists() and vendor == '0x8086':
            intel.append(node)
    if len(intel) != 1:
        raise NativeCaptureError('native Tile4 capture needs one identified Intel render node'
                                 + (' matching the VA encoder' if preferred else ''))
    return str(intel[0])


class Ring:
    """Geometry of the exported NV12 ring, as sent by the helper."""

    def __init__(self, data: bytes):
        fields = struct.unpack(RING_FORMAT, data)
        magic, self.slots, self.width, self.height, self.modifier = fields[:5]
        if magic != MAGIC_RING or not (2 <= self.slots <= MAX_SLOTS):
            raise NativeCaptureError('bad ring message from the capture helper')
        offsets = fields[5:5 + MAX_SLOTS * 2]
        pitches = fields[5 + MAX_SLOTS * 2:5 + MAX_SLOTS * 4]
        sizes = fields[5 + MAX_SLOTS * 4:]
        self.offsets = [(offsets[2 * i], offsets[2 * i + 1]) for i in range(self.slots)]
        self.pitches = [(pitches[2 * i], pitches[2 * i + 1]) for i in range(self.slots)]
        self.sizes = list(sizes[:self.slots])

    @property
    def drm_format(self) -> str:
        return f'NV12:0x{self.modifier:016x}'


class Frame:
    __slots__ = ('slot', 'seq', 'pts_ns', 'dequeued_ns', 'ready_ns', 'dropped')

    def __init__(self, data: bytes):
        magic, self.slot, self.seq, self.pts_ns, self.dequeued_ns, self.ready_ns, self.dropped = \
            struct.unpack(FRAME_FORMAT, data)
        if magic != MAGIC_FRAME:
            raise NativeCaptureError('bad frame message from the capture helper')


def free_message(slot: int) -> bytes:
    return struct.pack(FREE_FORMAT, MAGIC_FREE, slot)


def recv_ring(sock: socket.socket) -> tuple[Ring, list[int]]:
    """Read the ring message and its SCM_RIGHTS fds (blocking)."""
    size = struct.calcsize(RING_FORMAT)
    data, fds, _flags, _addr = socket.recv_fds(sock, size, MAX_SLOTS)
    if len(data) != size:
        raise NativeCaptureError('capture helper exited before announcing its ring')
    ring = Ring(data)
    if len(fds) != ring.slots:
        raise NativeCaptureError(f'expected {ring.slots} ring fds, got {len(fds)}')
    return ring, fds


class NativeCapture:
    """Runs native/tabs9-capture and delivers its frames to a callback.

    `on_frame(frame)` is called on the reader thread for every converted
    frame; the callee must eventually call `release(slot)`.
    """

    def __init__(self, pipewire_fd: int, node_id: int, width: int, height: int, *,
                 slots: int = 6, on_frame: Callable[[Frame], None], on_exit: Callable[[int], None],
                 helper: Path = HELPER, render_node: str | None = None):
        if not helper.exists():
            raise NativeCaptureError(f'{helper} is not built (run make -C native)')
        self.render_node = select_render_node(render_node)
        print(f'Native capture render device: {self.render_node}', flush=True)
        self.sock, child = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        self.process = None
        try:
            self.process = subprocess.Popen(
                [str(helper), '--node', str(node_id), '--width', str(width), '--height', str(height),
                 '--render-node', self.render_node,
                 '--slots', str(slots), '--pw-fd', str(pipewire_fd), '--sock-fd', str(child.fileno())],
                pass_fds=(pipewire_fd, child.fileno()), stdin=subprocess.DEVNULL, close_fds=True)
        except OSError as error:
            self.sock.close()
            raise NativeCaptureError(f'cannot start capture helper: {error}') from error
        finally:
            child.close()
        self.on_frame = on_frame
        self.on_exit = on_exit
        try:
            self.sock.settimeout(10)
            self.ring, self.fds = recv_ring(self.sock)
            self.sock.settimeout(None)
        except (OSError, NativeCaptureError) as error:
            self.sock.close()
            if self.process.poll() is None:
                self.process.terminate()
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
            raise NativeCaptureError(str(error)) from error
        self.lock = threading.Lock()
        self.busy: set[int] = set()
        self.closed = False
        self.thread = threading.Thread(target=self._reader, name='tabs9-native-reader', daemon=True)
        self.thread.start()

    def _reader(self):
        pending = b''
        while not self.closed:
            try:
                chunk = self.sock.recv(65536)
            except OSError:
                break
            if not chunk:
                break
            pending += chunk
            while len(pending) >= FRAME_SIZE:
                frame = Frame(pending[:FRAME_SIZE])
                pending = pending[FRAME_SIZE:]
                with self.lock:
                    self.busy.add(frame.slot)
                self.on_frame(frame)
        if not self.closed:
            code = self.process.poll()
            self.on_exit(code if code is not None else -1)

    def release(self, slot: int) -> None:
        with self.lock:
            if slot not in self.busy or self.closed:
                return
            self.busy.discard(slot)
            try:
                self.sock.sendall(free_message(slot))
            except OSError:
                pass

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        try:
            self.sock.close()
        except OSError:
            pass
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.kill()
        for fd in self.fds:
            # The GstMemory wrappers own their own duplicates.
            try:
                os.close(fd)
            except OSError:
                pass
