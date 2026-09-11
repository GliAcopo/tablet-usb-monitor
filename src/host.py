#!/usr/bin/env python3
"""USB-only KDE portal display host, compatible with UScreen Android 1.1.0.

Never logs screen pixels, device identifiers, input messages, or session tokens.
"""
import argparse
import asyncio
import collections
import contextlib
import fcntl
import hmac
import json
import math
import os
from pathlib import Path
import secrets
import signal
import socket
import struct
import subprocess
import threading
import time

import dbus
from dbus.mainloop.glib import DBusGMainLoop
import gi
gi.require_version('Gst', '1.0')
from gi.repository import GLib, GLibUnix, Gst
from websockets.asyncio.server import serve
from touch_input import LiveKScreenTarget, FixedTarget, PortalTouchInput, TouchInputError

ROOT = Path(__file__).resolve().parents[1]
ADB = ROOT / '.local/platform-tools/adb'

def adb(*args):
    result = subprocess.run([str(ADB), '-d', *args], capture_output=True, timeout=30)
    if result.returncode:
        raise RuntimeError('USB device command failed; check cable and debugging authorization')
    return result.stdout

def outputs():
    return json.loads(subprocess.check_output(['kscreen-doctor', '-j']))['outputs']

class Host:
    def __init__(self, args):
        self.args = args
        self.token = secrets.token_hex(32)
        self.loop = GLib.MainLoop()
        self.bus = dbus.SessionBus()
        self.portal = dbus.Interface(self.bus.get_object('org.freedesktop.portal.Desktop',
            '/org/freedesktop/portal/desktop'), 'org.freedesktop.portal.ScreenCast')
        self.remote = dbus.Interface(self.bus.get_object('org.freedesktop.portal.Desktop',
            '/org/freedesktop/portal/desktop'), 'org.freedesktop.portal.RemoteDesktop')
        self.session = None
        self.creation_session = None
        self.pipeline = None
        self.fd = None
        self.capture_node = None
        self.memory_mode = args.capture_memory
        self.pipeline_bus = None
        self.report_timer = None
        self.clients = set()
        self.controls = set()
        self.control_owner = None
        self.control_generation = 0
        self.tablet_stats = {}
        self.sent = collections.OrderedDict()
        self.stats = collections.deque(maxlen=1200)
        self.frames = 0
        self.capture_frames = 0
        self.capture_pts = None
        self.capture_wall = None
        self.capture_intervals = collections.deque(maxlen=1200)
        self.capture_pts_intervals = collections.deque(maxlen=1200)
        self.capture_caps = None
        self.rendered = 0
        self.last_report = (time.monotonic(), 0, 0, 0)
        self.caps_reported = None
        self.previous = {o['name'] for o in outputs()}
        self.virtual_name = None
        self.touch = None
        self.closing = False
        self.session_watches = []
        self.reverse_ports = []
        self.ready = threading.Event()
        self.aio = asyncio.new_event_loop()

    def request(self, method, args, callback):
        token = 'tabs9_' + secrets.token_hex(8)
        path = '/org/freedesktop/portal/desktop/request/' + self.bus.get_unique_name()[1:].replace('.', '_') + '/' + token
        def response(code, result):
            match.remove()
            if code:
                print('Sharing was declined or failed.', flush=True)
                self.loop.quit()
            else:
                try:
                    callback(result)
                except Exception as error:
                    print('Portal setup failed:', type(error).__name__, flush=True)
                    self.loop.quit()
        match = self.bus.add_signal_receiver(response, signal_name='Response',
            dbus_interface='org.freedesktop.portal.Request', path=path)
        args[-1]['handle_token'] = token
        try:
            method(*args)
        except Exception:
            match.remove()
            raise

    def create(self):
        self.request(self.portal.CreateSession, [dbus.Dictionary({
            'session_handle_token': 'tabs9_' + secrets.token_hex(8)}, signature='sv')], self.created)

    def created(self, result):
        self.session = result['session_handle']
        self.request(self.portal.SelectSources, [self.session, dbus.Dictionary({
            'types': dbus.UInt32(4), 'multiple': False,
            'cursor_mode': dbus.UInt32(2)}, signature='sv')], self.selected)

    def selected(self, result):
        api = self.portal if self.creation_session is None else self.remote
        self.request(api.Start, [self.session, '', dbus.Dictionary({}, signature='sv')], self.started)

    def started(self, result):
        # A newly created virtual output can initially share the laptop's logical
        # output. KWin binds the first stream to that logical output permanently.
        # Configure extension first, then request a fresh stream of that output.
        if self.creation_session is None:
            self.creation_session = self.session
            self.session = None
            self.watch_session(self.creation_session)
            if int(result['streams'][0][1].get('source_type', 0)) != 4:
                print('Refusing a non-virtual creation source.', flush=True)
                self.loop.quit()
                return
            GLib.timeout_add_seconds(1, self.configure_output)
            return
        node, props = result['streams'][0]
        expected = (round(self.args.width / self.args.scale), round(self.args.height / self.args.scale))
        size = tuple(int(x) for x in props.get('size', []))
        print('Selected capture size:', size, flush=True)
        if int(props.get('source_type', 0)) != 1 or size not in (expected, (self.args.width, self.args.height)):
            print('Wrong source: select the existing Virtual Output, not the laptop.', flush=True)
            dbus.Interface(self.bus.get_object('org.freedesktop.portal.Desktop', self.session),
                'org.freedesktop.portal.Session').Close()
            self.request_capture_session()
            return
        geometry = LiveKScreenTarget(self.virtual_name).geometry()
        self.touch = PortalTouchInput.bind(self.remote, str(self.session), result['streams'][0],
            FixedTarget(geometry), int(result.get('devices', 0)))
        self.watch_session(self.session)
        print('Tablet input mode:', self.touch.mode, flush=True)
        self.fd = self.portal.OpenPipeWireRemote(self.session, dbus.Dictionary({}, signature='sv')).take()
        self.capture_node = int(node)
        self.start_pipeline()

    def start_pipeline(self):
        conversion = ('! glupload ! glcolorconvert ! video/x-raw(memory:GLMemory),format=RGBA '
                      if self.memory_mode == 'gl' else '! video/x-raw,format=BGRx ')
        description = (
            f'pipewiresrc name=capture fd={self.fd} path={self.capture_node} do-timestamp=true keepalive-time=1000 '
            '! queue max-size-buffers=2 max-size-bytes=0 max-size-time=0 leaky=downstream '
            + conversion +
            f'! nvh265enc name=encoder preset=p1 tune=ultra-low-latency rc-mode=cbr bitrate={self.args.bitrate} '
            f'gop-size={self.args.fps} bframes=0 zerolatency=true repeat-sequence-header=true '
            '! video/x-h265,profile=main '
            '! h265parse config-interval=-1 ! video/x-h265,stream-format=byte-stream,alignment=au '
            '! appsink name=encoded emit-signals=true sync=false max-buffers=2 drop=true'
        )
        try:
            self.pipeline = Gst.parse_launch(description)
            self.pipeline.get_by_name('capture').get_static_pad('src').add_probe(
                Gst.PadProbeType.BUFFER, self.capture_probe)
            self.pipeline.get_by_name('encoded').connect('new-sample', self.sample)
            bus = self.pipeline.get_bus()
            self.pipeline_bus = bus
            bus.add_signal_watch()
            bus.connect('message::error', self.pipeline_error)
            self.pipeline.set_state(Gst.State.PLAYING)
            if self.report_timer is None:
                self.report_timer = GLib.timeout_add_seconds(5, self.report)
            print('Capture authorized; starting encoder. Memory path:', self.memory_mode, flush=True)
        except Exception as error:
            print('Encoder setup failed:', type(error).__name__, flush=True)
            self.fallback_or_stop()
        return False

    def pipeline_error(self, bus, message):
        error, debug = message.parse_error()
        print('Video pipeline error:', error.message, flush=True)
        self.fallback_or_stop()

    def fallback_or_stop(self):
        if self.memory_mode != 'gl':
            self.loop.quit()
            return
        self.memory_mode = 'system'
        if self.pipeline_bus:
            self.pipeline_bus.remove_signal_watch()
            self.pipeline_bus = None
        if self.pipeline:
            self.pipeline.set_state(Gst.State.NULL)
            self.pipeline = None
        if self.fd is not None:
            os.close(self.fd)
        self.fd = self.portal.OpenPipeWireRemote(self.session, dbus.Dictionary({}, signature='sv')).take()
        self.capture_caps = None
        self.capture_wall = None
        self.capture_pts = None
        print('GPU-memory import unavailable; falling back to the system-memory capture path.', flush=True)
        GLib.idle_add(self.start_pipeline)

    def capture_probe(self, pad, info):
        buffer = info.get_buffer()
        if buffer is not None:
            now = time.monotonic()
            self.capture_frames += 1
            if self.capture_wall is not None:
                self.capture_intervals.append((now - self.capture_wall) * 1000)
            self.capture_wall = now
            if buffer.pts != Gst.CLOCK_TIME_NONE:
                if self.capture_pts is not None and buffer.pts > self.capture_pts:
                    self.capture_pts_intervals.append((buffer.pts - self.capture_pts) / 1000000)
                self.capture_pts = buffer.pts
            if self.capture_caps is None:
                caps = pad.get_current_caps()
                if caps is not None:
                    self.capture_caps = caps.to_string()
                    print('Capture format:', self.capture_caps, flush=True)
        return Gst.PadProbeReturn.OK

    def configure_output(self):
        try:
            return self._configure_output()
        except Exception as error:
            print('Display setup failed:', type(error).__name__, flush=True)
            self.loop.quit()
            return False

    def _configure_output(self):
        current = outputs()
        new = [o for o in current if o['name'] not in self.previous]
        if len(new) != 1:
            print('Waiting for KDE to activate a separate virtual output.', flush=True)
            return True
        name = new[0]['name']
        self.virtual_name = name
        a = self.args
        def configure(*settings):
            subprocess.run(['kscreen-doctor', *settings], capture_output=True, check=True)
        configure(f'output.{name}.addCustomMode.{a.width}.{a.height}.{a.fps * 1000}.reduced')
        left = min((o.get('pos', {}).get('x', 0)
                    for o in current if o['name'] in self.previous and o['enabled']), default=0)
        x = left - round(a.width / a.scale)
        configure(f'output.{name}.mode.{a.width}x{a.height}@{a.fps}',
            f'output.{name}.scale.{a.scale}', f'output.{name}.position.{x},0', f'output.{name}.enable')
        final = next(o for o in outputs() if o['name'] == name)
        mode = next(m for m in final['modes'] if m['id'] == final['currentModeId'])
        print('Extended output:', json.dumps({'size': mode['size'], 'Hz': mode['refreshRate'],
              'scale': final['scale'], 'position': final['pos']}), flush=True)
        self.aio.call_soon_threadsafe(lambda: asyncio.create_task(self.broadcast_settings()))
        self.request_capture_session()
        return False

    def request_capture_session(self):
        self.request(self.remote.CreateSession, [dbus.Dictionary({
            'session_handle_token': 'tabs9_capture_' + secrets.token_hex(8)}, signature='sv')], self.capture_created)

    def watch_session(self, session):
        def closed(*args):
            if not self.closing:
                print('KDE sharing session ended.', flush=True)
                self.loop.quit()
        self.session_watches.append(self.bus.add_signal_receiver(closed,
            signal_name='Closed', dbus_interface='org.freedesktop.portal.Session', path=str(session)))

    def capture_created(self, result):
        self.session = result['session_handle']
        print('Choose the existing virtual monitor in the sharing dialog.', flush=True)
        self.request(self.remote.SelectDevices, [self.session, dbus.Dictionary({
            'types': dbus.UInt32(6)}, signature='sv')], self.capture_devices_selected)

    def capture_devices_selected(self, result):
        self.request(self.portal.SelectSources, [self.session, dbus.Dictionary({
            'types': dbus.UInt32(1), 'multiple': False,
            'cursor_mode': dbus.UInt32(2)}, signature='sv')], self.selected)

    def sample(self, sink):
        sample = sink.emit('pull-sample')
        if sample is None:
            return Gst.FlowReturn.OK
        caps = sample.get_caps().get_structure(0)
        dimensions = (caps.get_value('width'), caps.get_value('height'))
        if dimensions != self.caps_reported:
            self.caps_reported = dimensions
            print('Encoded pixel dimensions:', dimensions, flush=True)
        if dimensions != (self.args.width, self.args.height):
            return Gst.FlowReturn.OK
        buf = sample.get_buffer()
        data = buf.extract_dup(0, buf.get_size())
        keyframe = not buf.has_flags(Gst.BufferFlags.DELTA_UNIT)
        self.frames += 1
        seq = self.frames & 0xffffffff
        # Only aggregate timing metadata is retained, never screen data on disk.
        payload = b'\x01' + struct.pack('!I', seq) + data
        packet = struct.pack('!I', len(payload)) + payload
        self.aio.call_soon_threadsafe(self.distribute, packet, keyframe, seq)
        return Gst.FlowReturn.OK

    def distribute(self, packet, keyframe, seq):
        self.sent[seq] = time.monotonic()
        while len(self.sent) > 1200:
            self.sent.popitem(last=False)
        for queue in tuple(self.clients):
            if queue.full():
                # Reconnect at a keyframe rather than deliver a corrupt or stale GOP.
                self.clients.discard(queue)
                while not queue.empty(): queue.get_nowait()
                queue.put_nowait(None)
            else:
                queue.put_nowait((packet, keyframe))

    async def video(self, reader, writer):
        queue = asyncio.Queue(maxsize=8)
        watcher = None
        try:
            token = await asyncio.wait_for(reader.readexactly(64), 3)
            if not hmac.compare_digest(token, self.token.encode()): return
            writer.get_extra_info('socket').setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            self.clients.add(queue)
            async def disconnected():
                await reader.read(1)
                self.clients.discard(queue)
                while not queue.empty(): queue.get_nowait()
                queue.put_nowait(None)
            watcher = asyncio.create_task(disconnected())
            waiting = True
            while True:
                item = await queue.get()
                if item is None: break
                packet, keyframe = item
                if waiting and not keyframe: continue
                waiting = False
                writer.write(packet)
                await asyncio.wait_for(writer.drain(), 2)
        except (asyncio.TimeoutError, ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            self.clients.discard(queue)
            if watcher:
                watcher.cancel()
                with contextlib.suppress(asyncio.CancelledError, ConnectionError):
                    await watcher
            writer.close()
            with contextlib.suppress(ConnectionError):
                await writer.wait_closed()

    async def control(self, ws):
        owns_control = False
        try:
            auth = json.loads(await asyncio.wait_for(ws.recv(), 3))
            if auth.get('type') != 'auth' or not hmac.compare_digest(str(auth.get('token', '')), self.token):
                await ws.close(); return
            if self.control_owner is not None:
                await ws.close(code=1008, reason='A tablet controller is already connected')
                return
            self.control_owner = ws
            owns_control = True
            self.control_generation += 1
            generation = self.control_generation
            GLib.idle_add(self.begin_control, generation)
            self.controls.add(ws)
            await ws.send(json.dumps(self.settings()))
            async for raw in ws:
                msg = json.loads(raw)
                if msg.get('type') == 'rendered':
                    self.rendered += 1
                    sent = self.sent.get(int(msg['seq']))
                    if sent is not None: self.stats.append((time.monotonic() - sent) * 1000)
                elif msg.get('type') == 'config':
                    GLib.idle_add(self.apply_settings, msg, generation)
                elif msg.get('type') == 'stats':
                    self.tablet_stats = {k: round(float(msg[k]), 2)
                        for k in ('panel_hz', 'decoder_fps', 'received_mbps', 'stream_fps')
                        if isinstance(msg.get(k), (int, float)) and math.isfinite(msg[k]) and 0 <= msg[k] <= 1000}
                elif msg.get('type') in ('touch', 'pen'):
                    GLib.idle_add(self.handle_touch, msg, generation)
        except Exception:
            pass
        finally:
            self.controls.discard(ws)
            if owns_control and self.control_owner is ws:
                self.control_owner = None
                self.control_generation += 1
                GLib.idle_add(self.release_control, self.control_generation)

    def begin_control(self, generation):
        if generation == self.control_generation and self.control_owner is not None:
            self.release_touch()
        return False

    def release_control(self, generation):
        if generation == self.control_generation and self.control_owner is None:
            self.release_touch()
        return False

    def handle_touch(self, message, generation):
        if generation != self.control_generation or self.control_owner is None:
            return False
        if self.touch is not None:
            try:
                self.touch.handle_message(message)
            except (TouchInputError, dbus.DBusException):
                self.release_touch()
        return False

    def release_touch(self):
        if self.touch is not None:
            self.touch.release_all()
        return False

    def settings(self):
        return {'status': 'connected', 'width': self.args.width, 'height': self.args.height,
                'codec': 'hevc', 'pen_only': False, 'fps': self.args.fps, 'bitrate': self.args.bitrate}

    async def broadcast_settings(self):
        payload = json.dumps(self.settings())
        for ws in tuple(self.controls):
            with contextlib.suppress(Exception):
                await ws.send(payload)

    def apply_settings(self, message, generation=None):
        if generation is not None and (generation != self.control_generation or self.control_owner is None):
            return False
        fps = message.get('fps', self.args.fps)
        bitrate = message.get('bitrate', self.args.bitrate)
        if type(fps) is not int or fps not in (30, 60, 90, 120) or type(bitrate) is not int or not 1000 <= bitrate <= 150000:
            return False
        try:
            if self.virtual_name and fps != self.args.fps:
                output = next(o for o in outputs() if o['name'] == self.virtual_name)
                match = next((m for m in output['modes'] if m['size'] == {'width': self.args.width,
                    'height': self.args.height} and abs(m['refreshRate'] - fps) < .1), None)
                if match is None:
                    subprocess.run(['kscreen-doctor', f'output.{self.virtual_name}.addCustomMode.{self.args.width}.{self.args.height}.{fps*1000}.reduced'],
                        capture_output=True, check=True)
                subprocess.run(['kscreen-doctor', f'output.{self.virtual_name}.mode.{self.args.width}x{self.args.height}@{fps}'],
                    capture_output=True, check=True)
            if self.pipeline:
                self.pipeline.get_by_name('encoder').set_property('bitrate', bitrate)
            self.args.fps, self.args.bitrate = fps, bitrate
            self.aio.call_soon_threadsafe(lambda: asyncio.create_task(self.broadcast_settings()))
        except (subprocess.CalledProcessError, StopIteration):
            print('Requested settings could not be applied; keeping current settings.', flush=True)
        return False

    async def servers(self):
        async with await asyncio.start_server(self.video, '127.0.0.1', 8890), \
                   serve(self.control, '127.0.0.1', 8891, max_size=4096):
            self.ready.set()
            await asyncio.Future()

    def report(self):
        stats = sorted(self.stats)
        now = time.monotonic()
        before, frames, rendered, captured = self.last_report
        elapsed = now - before
        self.last_report = (now, self.frames, self.rendered, self.capture_frames)
        arrivals = sorted(self.capture_intervals)
        timestamps = sorted(self.capture_pts_intervals)
        print(json.dumps({'encoded_frames': self.frames, 'tablet_rendered_acks': self.rendered,
            'capture_fps': round((self.capture_frames - captured) / elapsed, 1),
            'encoded_fps': round((self.frames - frames) / elapsed, 1),
            'tablet_ack_fps': round((self.rendered - rendered) / elapsed, 1),
            'tablet': self.tablet_stats,
            'capture_interval_ms_p50': round(arrivals[len(arrivals)//2], 2) if arrivals else None,
            'capture_pts_interval_ms_p50': round(timestamps[len(timestamps)//2], 2) if timestamps else None,
            'encode_to_render_ms_p50': round(stats[len(stats)//2], 1) if stats else None}), flush=True)
        return True

    def run(self):
        def worker():
            asyncio.set_event_loop(self.aio)
            self.aio.run_until_complete(self.servers())
        threading.Thread(target=worker, daemon=True).start()
        try:
            if not self.ready.wait(5): raise RuntimeError('Local streaming ports unavailable')
            for port in (8890, 8891):
                adb('reverse', '--no-rebind', f'tcp:{port}', f'tcp:{port}')
                self.reverse_ports.append(port)
            adb('shell', 'am', 'start', '-n', 'local.tabs9.usbdisplay/.MainActivity', '--es', 'token', self.token)
            self.create()
            for sig in (signal.SIGINT, signal.SIGTERM):
                GLibUnix.signal_add(GLib.PRIORITY_DEFAULT, sig, lambda: self.loop.quit() or False)
            self.loop.run()
        finally:
            self.closing = True
            self.release_touch()
            if self.pipeline: self.pipeline.set_state(Gst.State.NULL)
            for session in (self.session, self.creation_session):
                if session:
                    with contextlib.suppress(dbus.DBusException):
                        dbus.Interface(self.bus.get_object('org.freedesktop.portal.Desktop', session),
                            'org.freedesktop.portal.Session').Close()
            for watch in self.session_watches: watch.remove()
            if self.fd is not None:
                with contextlib.suppress(OSError): os.close(self.fd)
            for port in self.reverse_ports:
                try: adb('reverse', '--remove', f'tcp:{port}')
                except Exception: pass

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--width', type=int, default=2960)
    parser.add_argument('--height', type=int, default=1848)
    parser.add_argument('--fps', type=int, choices=[30, 60, 90, 120], default=120)
    parser.add_argument('--bitrate', type=int, default=60000)
    parser.add_argument('--scale', type=float, default=1.5)
    parser.add_argument('--capture-memory', choices=['system', 'gl'], default='system')
    args = parser.parse_args()
    if args.capture_memory == 'gl':
        os.environ['__NV_PRIME_RENDER_OFFLOAD'] = '1'
        os.environ['GST_GL_PLATFORM'] = 'egl'
        os.environ['GST_GL_WINDOW'] = 'surfaceless'
    if not (320 <= args.width <= 4096 and 240 <= args.height <= 4096 and
            args.width % 2 == 0 and args.height % 2 == 0 and
            1 <= args.scale <= 3 and 1000 <= args.bitrate <= 150000):
        parser.error('Invalid resolution, scale or bitrate')
    os.umask(0o077)
    state_dir = ROOT / '.local/state'
    state_dir.mkdir(parents=True, exist_ok=True)
    lock = (state_dir / 'host.lock').open('w')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit('The USB display host is already running')
    DBusGMainLoop(set_as_default=True)
    Gst.init(None)
    Host(args).run()
