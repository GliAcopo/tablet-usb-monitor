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
import sys
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
# GstAllocators/GstVideo typelibs come from gir1.2-gst-plugins-base-1.0, unpacked
# under .local/sysroot by scripts/setup-native.sh (nothing installed system-wide).
_SYSROOT_TYPELIBS = str(Path(__file__).resolve().parents[1] / '.local/sysroot/usr/lib/x86_64-linux-gnu/girepository-1.0')
if os.path.isdir(_SYSROOT_TYPELIBS):
    os.environ['GI_TYPELIB_PATH'] = _SYSROOT_TYPELIBS + os.pathsep + os.environ.get('GI_TYPELIB_PATH', '')
gi.require_version('Gst', '1.0')
from gi.repository import GLib, GLibUnix, Gst
try:
    gi.require_version('GstAllocators', '1.0')
    gi.require_version('GstVideo', '1.0')
    from gi.repository import GstAllocators, GstVideo
except (ValueError, ImportError):
    GstAllocators = GstVideo = None
from native_capture import NativeCapture, NativeCaptureError
from websockets.asyncio.server import serve
from touch_input import LiveKScreenTarget, PortalTouchInput, TouchInputError
from eis_touch import EisTouch, EisError
from gestures import GestureFilter

# Measured on Qt 6.10 (QScrollArea): 1 axis unit is 12 angle units, so 10
# units of finger travel are one wheel notch (three lines). 0.2 makes the
# content move about as far as the fingers do there; other toolkits were
# not measured.
DEFAULT_SCROLL_GAIN = 0.2
from status import StatusWriter
from tokens import load_tokens, save_token, discard_token

ROOT = Path(__file__).resolve().parents[1]
ADB = ROOT / '.local/platform-tools/adb'
STATE_DIR = ROOT / '.local/state'
STATUS_FILE = STATE_DIR / 'host.status.json'
TOKENS_FILE = STATE_DIR / 'portal_tokens.json'

# Cap on retrying the capture session after the user selects the wrong output
# (e.g. the laptop screen instead of the Virtual Output), so a confused user
# gets a clear failure instead of an unbounded prompt loop that the
# supervised `tabs9 start` would otherwise report as a silent 60s hang.
MAX_WRONG_SOURCE_ATTEMPTS = 3
CAPTURE_DIALOG_HINT = 'Click Allow in the KDE remote-control dialog (keep "Allow restoring" ticked).'


def notify(summary, body, urgency='normal'):
    """Best-effort desktop notification; the terminal is hidden behind the portal dialog."""
    with contextlib.suppress(Exception):
        subprocess.run(['notify-send', '-a', 'Tab S9 USB display', '-u', urgency, '-t', '15000', summary, body],
            capture_output=True, timeout=5)


def describe_wrong_source(size, outputs_now, expected):
    """Name what the user actually picked so the retry hint is concrete."""
    w, h = size if len(size) == 2 else (0, 0)
    enabled = [o for o in outputs_now if isinstance(o, dict) and o.get('enabled', False)]
    logical = {}
    for o in enabled:
        scale = o.get('scale', 1.0) or 1.0
        pw, ph = _resolve_mode_size(o)
        if _is_rotated_90(o.get('rotation', 1)):
            pw, ph = ph, pw
        logical[o['name']] = ((o.get('pos') or {}).get('x', 0), (o.get('pos') or {}).get('y', 0),
            round(pw / scale), round(ph / scale), pw, ph)
    if len(logical) > 1:
        union_w = max(x + lw for x, y, lw, lh, pw, ph in logical.values())
        union_h = max(y + lh for x, y, lw, lh, pw, ph in logical.values())
        if (w, h) == (union_w, union_h):
            return 'the full Workspace (all screens together)'
    for name, (x, y, lw, lh, pw, ph) in logical.items():
        if (w, h) in ((lw, lh), (pw, ph)) and (w, h) != expected:
            return f'the screen {name}'
    return f'a {w}x{h} source'


def select_virtual_stream(streams, expected_logical, expected_pixels, virtual_x=None):
    """Pick the portal stream that is the virtual output, or None.

    A stream matches by size (KWin reports logical or native pixels depending
    on version); when several match and positions are present, prefer the one
    at the virtual output's logical x.
    """
    candidates = []
    for entry in streams:
        try:
            node, props = entry
        except (TypeError, ValueError):
            continue
        if int(props.get('source_type', 1) or 1) != 1:
            continue
        size = tuple(int(x) for x in props.get('size', []))
        if size not in (tuple(expected_logical), tuple(expected_pixels)):
            continue
        candidates.append((node, props))
    if not candidates:
        return None
    if len(candidates) > 1 and isinstance(virtual_x, int):
        for node, props in candidates:
            pos = props.get('position')
            if pos and int(pos[0]) == virtual_x:
                return (node, props)
    return candidates[0]


def adb(*args):
    result = subprocess.run([str(ADB), '-d', *args], capture_output=True, timeout=30)
    if result.returncode:
        raise RuntimeError('USB device command failed; check cable and debugging authorization')
    return result.stdout

def outputs():
    return json.loads(subprocess.check_output(['kscreen-doctor', '-j']))['outputs']

# libkscreen's Output::Rotation bitmask (as reported by `kscreen-doctor -j`):
# None=1, Left=2, Inverted=4, Right=8. Left/Right are 90-degree turns that
# swap logical width and height; Inverted (180 degrees) does not. Some
# kscreen-doctor builds report the name instead of the bitmask.
_ROTATED_90_INTS = (2, 8)
_ROTATED_90_NAMES = ('left', 'right')

# Used only when an output's currentModeId cannot be resolved against its own
# modes list (incomplete/malformed KScreen data). Guessing a plausible
# physical size is safer than dropping the output's extent, which would let
# the virtual output land at (0, 0) directly on top of it.
_FALLBACK_MODE_SIZE = (1920, 1080)


def _is_rotated_90(rotation):
    if isinstance(rotation, bool):
        return False
    if isinstance(rotation, int):
        return rotation in _ROTATED_90_INTS
    if isinstance(rotation, str):
        return rotation.strip().lower() in _ROTATED_90_NAMES
    return False


def _resolve_mode_size(output):
    """Return (pixel_width, pixel_height) for an output's current mode.

    Falls back to the widest advertised mode, then to a conservative
    constant, rather than ever reporting a zero-sized extent for an enabled
    output whose mode data is incomplete.
    """
    modes = output.get('modes')
    modes = [m for m in modes if isinstance(m, dict)] if isinstance(modes, list) else []
    current_mode_id = output.get('currentModeId')
    mode = next((m for m in modes if m.get('id') == current_mode_id), None)
    if mode is None:
        mode = max(modes, key=lambda m: (m.get('size') or {}).get('width', 0) or 0,
                   default=None)
    size = (mode.get('size') if mode else None) or {}
    width, height = size.get('width'), size.get('height')
    if not isinstance(width, (int, float)) or isinstance(width, bool) or width <= 0:
        width = _FALLBACK_MODE_SIZE[0]
    if not isinstance(height, (int, float)) or isinstance(height, bool) or height <= 0:
        height = _FALLBACK_MODE_SIZE[1]
    return width, height


def compute_virtual_position(current_outputs, previous_names, gap=1, scale=None):
    """Compute the position to place the virtual output to the RIGHT of all
    existing enabled physical outputs, using non-negative coordinates.

    Accounts for 90-degree output rotation (which swaps logical width and
    height) and never lets an incomplete or malformed output description
    collapse the result to (0, 0) when an enabled physical output exists --
    that would place the virtual output directly on top of it.

    The right edge is the physical output's *integer* logical width, rounded
    the way KWin's LogicalOutput::geometry() rounds it (2560 / 1.75 =
    1462.86 -> 1463).  ``gap`` logical pixels are then left between the two
    outputs.  With ``gap == 0`` the outputs touch exactly as KWin itself would
    place them, but a window whose frame sits on that shared edge leaks a
    1-2 device pixel column into the virtual output whenever an effect
    repaints it translated (KWin's Slide desktop switch clips per screen with
    both the clip and the window rectangle rounded outward).  A gap of one
    logical pixel keeps the two rounded edges apart.  KWin then treats the
    shared boundary as an outer screen edge (quick-tile on drag, edge
    actions); the pointer still crosses, it picks the nearest output.

    When ``scale`` (the virtual output's scale) is given, x is nudged right by
    up to a few pixels so that ``x * scale`` is a whole device pixel: a
    half-pixel offset (1463 * 1.5 = 2194.5) makes KWin round paint and damage
    differently and re-render a 2 px column on every repaint next to the
    boundary.  Only applied when a gap is requested.

    Returns (x, y) where y is 0.
    """
    right_edge = 0
    seen_enabled = False
    for o in current_outputs:
        if not isinstance(o, dict) or o.get('name') not in previous_names or not o.get('enabled', False):
            continue
        seen_enabled = True
        pos_x = (o.get('pos') or {}).get('x', 0)
        if not isinstance(pos_x, (int, float)) or isinstance(pos_x, bool):
            pos_x = 0
        o_scale = o.get('scale', 1.0) or 1.0
        if not isinstance(o_scale, (int, float)) or isinstance(o_scale, bool) or o_scale <= 0:
            o_scale = 1.0
        pixel_w, pixel_h = _resolve_mode_size(o)
        if _is_rotated_90(o.get('rotation', 1)):
            pixel_w, pixel_h = pixel_h, pixel_w
        logical_w = round(pixel_w / o_scale)
        edge = pos_x + logical_w
        if edge > right_edge:
            right_edge = edge
    # If no enabled previously-known output contributed a usable extent,
    # there is nothing to avoid overlapping; place at the origin.
    if not seen_enabled:
        return (0, 0)
    gap = max(0, int(gap))
    x = max(0, right_edge) + gap
    if gap and isinstance(scale, (int, float)) and not isinstance(scale, bool) and scale > 0:
        for extra in range(4):
            device = (x + extra) * scale
            if abs(device - round(device)) < 1e-6:
                x += extra
                break
    return (x, 0)


RESYNC = object()  # queue marker: the client must wait for the next keyframe
TRACE_CAPTURE = bool(os.environ.get('TABS9_TRACE_CAPTURE'))  # diagnostics: raw capture intervals


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
        self.native = None
        self.native_pushed = collections.deque()   # slots in encoder order
        self.native_last_encoded = None
        self.native_dropped = 0
        self.native_ready_lag = collections.deque(maxlen=1200)
        self.native_last_seq = None
        self.native_seq_gaps = 0        # KWin sequence numbers never delivered
        self.pipeline_bus = None
        self.report_timer = None
        self.clients = set()
        self.controls = set()
        self.control_owner = None
        self.control_generation = 0
        self.tablet_stats = {}
        self.input_messages = 0
        self.input_rejected = 0
        self.input_followon_rejected = 0
        self.gestures_fired = 0
        self.scrolls = 0
        self.kglobalaccel = None
        # Multi-finger swipes and two-finger scrolls are picked out here,
        # before the desktop sees the contacts (see gestures.py); off
        # delivers every touch as is.
        self.gestures = None
        scroll = getattr(args, 'scroll', 'natural')
        # Content follows the fingers ('natural', as one finger does in
        # touch-aware apps) or moves against them like a mouse wheel.
        self.scroll_sign = {'natural': -1.0, 'standard': 1.0}.get(scroll, 0.0)
        self.scroll_gain = float(getattr(args, 'scroll_gain', DEFAULT_SCROLL_GAIN))
        if getattr(args, 'gestures', 'on') == 'on':
            self.gestures = GestureFilter(self._deliver_input, self.perform_gesture,
                schedule=lambda ms, callback: GLib.timeout_add(ms, self._input_timer, callback),
                cancel=GLib.source_remove,
                hold_ms=getattr(args, 'gesture_hold_ms', 120),
                scroll=self.perform_scroll if scroll != 'off' else None)
        self.resyncs = 0
        # Video-socket liveness (see video()): features the tablet advertised
        # on the control channel, per-connection generation, and counters.
        self.client_features = set()
        self.video_generation = 0
        self.video_connects = 0
        self.video_disconnects = 0
        self.last_video_disconnect = None
        self.heartbeats_sent = 0
        self.tablet_panel = None
        self.panel_mismatch_reported = False
        self.sent = collections.OrderedDict()
        self.stats = collections.deque(maxlen=1200)
        # Capture arrival carried to the ack: pts -> arrival at the capture
        # probe, then (the encoder never drops or reorders) a FIFO of arrivals
        # for the frames that entered the encoder.
        self.capture_arrivals = collections.OrderedDict()
        self.encoder_fifo = collections.deque()             # capture arrivals in encoder order
        self.encoded_arrivals = collections.OrderedDict()   # encoded pts -> capture arrival
        self.capture_to_ack = collections.deque(maxlen=1200)
        self.ack_intervals = collections.deque(maxlen=1200)
        self.ack_wall = None
        self.render_intervals = collections.deque(maxlen=1200)   # tablet-reported render stamps
        self.tablet_render_ns = None
        self.frames = 0
        self.capture_frames = 0
        self.capture_pts = None
        self.capture_wall = None
        self.capture_intervals = collections.deque(maxlen=1200)
        self.capture_trace = []
        self.capture_seq = None
        self.capture_pts_intervals = collections.deque(maxlen=1200)
        self.capture_caps = None
        self.rendered = 0
        self.last_report = (time.monotonic(), 0, 0, 0)
        self.caps_reported = None
        self.previous = {o['name'] for o in outputs()}
        self.virtual_name = None
        self.touch = None
        self.eis = None
        self.closing = False
        self.session_watches = []
        self.reverse_ports = []
        self.ready = threading.Event()
        self.aio = asyncio.new_event_loop()
        # Status reporting
        self.status = StatusWriter(STATUS_FILE)
        # Token persistence
        self.tokens_file = TOKENS_FILE
        self._capture_token_used = False
        self._virtual_token_used = False
        self._wrong_source_attempts = 0
        self.failed = False

    def _fail(self, message):
        """Report failure and quit the main loop.

        Idempotent: a pipeline error can be followed by the session-closed
        watcher firing for the same teardown, and the first failure (not a
        secondary symptom of it) is the one worth keeping in the status file.
        Also guarantees loop.quit() is requested at most once.
        """
        if self.failed:
            return
        self.failed = True
        with contextlib.suppress(Exception):
            self.status.write('failed', message)
        print(message, flush=True)
        self.loop.quit()

    def request(self, method, args, callback):
        token = 'tabs9_' + secrets.token_hex(8)
        path = '/org/freedesktop/portal/desktop/request/' + self.bus.get_unique_name()[1:].replace('.', '_') + '/' + token
        def response(code, result):
            match.remove()
            if code:
                self._handle_portal_rejection(code, callback)
            else:
                try:
                    callback(result)
                except Exception as error:
                    self._fail('Portal setup failed: ' + type(error).__name__)
        match = self.bus.add_signal_receiver(response, signal_name='Response',
            dbus_interface='org.freedesktop.portal.Request', path=path)
        args[-1]['handle_token'] = token
        try:
            method(*args)
        except Exception:
            match.remove()
            raise

    def _handle_portal_rejection(self, code, callback):
        """Handle portal response code != 0.

        code 1 = user cancelled; code 2 = other error.
        If we used a restore token for this phase, discard it and retry once
        interactively.  The token flag is set exactly once per phase, so
        at most one interactive retry is possible.

        Both phases can hold a restore token (creation: 'screencast_create',
        capture: 'remotedesktop_capture'); each is discarded at most once.
        """
        if self._virtual_token_used and self.creation_session is None:
            self._virtual_token_used = False
            discard_token(self.tokens_file, 'screencast_create')
            if self.session:
                with contextlib.suppress(Exception):
                    dbus.Interface(self.bus.get_object('org.freedesktop.portal.Desktop', self.session),
                        'org.freedesktop.portal.Session').Close()
                self.session = None
            print('Stored virtual-screen token was stale; retrying with interactive consent.', flush=True)
            self.create()
            return
        # Capture (RemoteDesktop+ScreenCast) token was attempted and the
        # rejection arrived at SelectDevices, SelectSources, or Start on the
        # capture session.  Restart the full capture session because the
        # portal may have closed the session handle on failure.
        if self._capture_token_used:
            self._capture_token_used = False
            discard_token(self.tokens_file, 'remotedesktop_capture')
            if self.session:
                with contextlib.suppress(Exception):
                    dbus.Interface(self.bus.get_object('org.freedesktop.portal.Desktop', self.session),
                        'org.freedesktop.portal.Session').Close()
                self.session = None
            print('Stored capture token was stale; retrying with interactive consent.', flush=True)
            self.request_capture_session()
            return
        if code == 1:
            self._fail('Portal sharing was cancelled by the user.')
        else:
            self._fail('Portal sharing failed (error code ' + str(code) + ').')

    def create(self):
        self.status.write('waiting_virtual_consent')
        self.request(self.portal.CreateSession, [dbus.Dictionary({
            'session_handle_token': 'tabs9_' + secrets.token_hex(8)}, signature='sv')], self.created)

    def created(self, result):
        self.session = result['session_handle']
        # xdg-desktop-portal-kde 6.6.6 restores a screencast selection by output
        # uniqueId, and the "Share virtual screen" entry has the fixed id
        # "Virtual" (screencast.cpp / outputsmodel.cpp), so the creation session
        # can be restored like the capture one: one dialog on the first start.
        options = dbus.Dictionary({
            'types': dbus.UInt32(4), 'multiple': False,
            'cursor_mode': dbus.UInt32(2),
            'persist_mode': dbus.UInt32(2),
        }, signature='sv')
        tokens = load_tokens(self.tokens_file)
        if 'screencast_create' in tokens:
            options['restore_token'] = tokens['screencast_create']
            self._virtual_token_used = True
        self.request(self.portal.SelectSources, [self.session, options], self.selected)

    def selected(self, result):
        # restore_token is returned by Start, not SelectSources; proceed to Start.
        api = self.portal if self.creation_session is None else self.remote
        self.request(api.Start, [self.session, '', dbus.Dictionary({}, signature='sv')], self.started)

    def started(self, result):
        # A newly created virtual output can initially share the laptop's logical
        # output. KWin binds the first stream to that logical output permanently.
        # Configure extension first, then request a fresh stream of that output.
        if self.creation_session is None:
            if not result.get('streams') or int(result['streams'][0][1].get('source_type', 0)) != 4:
                self._fail('Refusing a non-virtual creation source.')
                return
            restore_token = result.get('restore_token')
            if isinstance(restore_token, str) and restore_token:
                save_token(self.tokens_file, 'screencast_create', str(restore_token))
            self.creation_session = self.session
            self.session = None
            self.watch_session(self.creation_session)
            self.status.write('configuring_output')
            GLib.timeout_add_seconds(1, self.configure_output)
            return
        if not result.get('streams'):
            self._fail('No streams returned by portal.')
            return
        # KDE's RemoteDesktop portal never shows a screen chooser: with
        # `multiple` it streams every screen, without it the whole workspace.
        # So ask for all of them and pick the one that is the virtual output.
        expected = (round(self.args.width / self.args.scale), round(self.args.height / self.args.scale))
        chosen = select_virtual_stream(result['streams'], expected, (self.args.width, self.args.height),
            self._virtual_x())
        sizes = [tuple(int(x) for x in props.get('size', [])) for _, props in result['streams']]
        print('Portal streams:', sizes, flush=True)
        if chosen is None:
            picked = describe_wrong_source(sizes[0] if sizes else (), outputs(), expected)
            print(f'Wrong source: the portal shared {picked}, not the Virtual Output.', flush=True)
            notify('Wrong screen shared', f'KDE shared {picked} instead of the Virtual Output; retrying.', 'critical')
            if self._capture_token_used:
                discard_token(self.tokens_file, 'remotedesktop_capture')
                self._capture_token_used = False
            if self.session:
                with contextlib.suppress(Exception):
                    dbus.Interface(self.bus.get_object('org.freedesktop.portal.Desktop', self.session),
                        'org.freedesktop.portal.Session').Close()
                self.session = None
            self._wrong_source_attempts += 1
            if self._wrong_source_attempts > MAX_WRONG_SOURCE_ATTEMPTS:
                self._fail('Too many incorrect source selections; giving up.')
                return
            self.request_capture_session()
            return
        # Virtual Output source is verified; save the capture restore token now
        restore_token = result.get('restore_token')
        if restore_token and isinstance(restore_token, str) and len(restore_token) > 0:
            save_token(self.tokens_file, 'remotedesktop_capture', str(restore_token))
        # Cached for the touch path (no kscreen-doctor call per contact); EIS
        # device events and output changes invalidate it.
        target = LiveKScreenTarget(self.virtual_name, cache_seconds=5.0)
        geometry = target.geometry()
        node, props = chosen
        # Same-instant geometry evidence (no pixels): portal stream vs KScreen.
        print('Selected capture size:', tuple(int(x) for x in props.get('size', [])),
              'position:', tuple(int(x) for x in props.get('position', [])),
              'kscreen:', (geometry.x, geometry.y, *geometry.logical_size), flush=True)
        self.touch = PortalTouchInput.bind(self.remote, str(self.session), chosen,
            target, int(result.get('devices', 0)))
        self.watch_session(self.session)
        self.connect_eis(target)
        print('Tablet input mode:', self.touch.mode, flush=True)
        if self.gestures is not None:
            print(f'Tablet gestures: 3 fingers = windows (up: Overview, down: Grid), '
                  f'4 fingers = desktops (fingers land within {self.gestures.hold_ms} ms)', flush=True)
            if self.gestures.scroll is not None:
                print(f'Tablet scrolling: 2 fingers = scroll ({self.args.scroll}, '
                      f'gain {self.scroll_gain:g}; needs the libei device)', flush=True)
        self.fd = self.portal.OpenPipeWireRemote(self.session, dbus.Dictionary({}, signature='sv')).take()
        self.capture_node = int(node)
        self.start_pipeline()

    def start_pipeline(self):
        if self.memory_mode == 'native':
            return self.start_native_pipeline()
        if self.memory_mode == 'va':
            # Same-GPU zero copy: KWin's DMA-BUF is imported by the Intel VA
            # driver that also composites it; no readback, no cross-GPU hop.
            if self.args.rate_control == 'cqp':
                rc = f'rate-control=cqp qpi={self.args.qp} qpp={self.args.qp}'
            else:
                rc = f'rate-control={self.args.rate_control} bitrate={self.args.bitrate}'
            # The queue puts the converter and the encoder on separate threads so
            # the RGB->NV12 job of frame N+1 overlaps the encode of frame N
            # (about 6 ms + 3 ms of GPU time per frame; serial they miss 8.33 ms).
            encode = ('! vapostproc ! video/x-raw(memory:VAMemory),format=NV12 '
                      '! queue max-size-buffers=2 max-size-bytes=0 max-size-time=0 leaky=downstream '
                      f'! vah265enc name=encoder {rc} '
                      f'key-int-max={self.args.fps} b-frames=0 ref-frames=1 target-usage=7 ')
        else:
            conversion = ('! glupload ! glcolorconvert ! video/x-raw(memory:GLMemory),format=RGBA '
                          if self.memory_mode == 'gl' else '! video/x-raw,format=BGRx ')
            encode = (conversion +
                f'! nvh265enc name=encoder preset=p1 tune=ultra-low-latency rc-mode=cbr bitrate={self.args.bitrate} '
                f'gop-size={self.args.fps} bframes=0 zerolatency=true repeat-sequence-header=true ')
        # KWin offers only 2..4 buffers (default 3).  Ask for 4 and never park
        # more than one of them in the queue, or KWin has nothing to render into
        # at 120 Hz and drops frames.
        description = (
            f'pipewiresrc name=capture fd={self.fd} path={self.capture_node} do-timestamp=true '
            f'keepalive-time={os.environ.get("TABS9_KEEPALIVE_MS", "1000")} '
            + os.environ.get('TABS9_PWSRC_EXTRA', '') +
            f'min-buffers={os.environ.get("TABS9_PW_BUFFERS", "4")} max-buffers={os.environ.get("TABS9_PW_BUFFERS", "4")} '
            '! queue max-size-buffers=1 max-size-bytes=0 max-size-time=0 leaky=downstream '
            + os.environ.get('TABS9_CAPTURE_CAPS', '')
            + encode +
            '! video/x-h265,profile=main '
            '! h265parse config-interval=-1 ! video/x-h265,stream-format=byte-stream,alignment=au '
            # The appsink must never block the encoder (a blocked encoder stops
            # returning KWin's four capture buffers).  sample() drains it at
            # once, so the drop here only fires if Python itself stalls; the
            # per-client queues in distribute() hold the real backlog policy.
            '! appsink name=encoded emit-signals=true sync=false max-buffers=4 drop=true'
        )
        if os.environ.get('TABS9_DEBUG_TAIL'):
            # Diagnostics only: replace everything after pipewiresrc's queue.
            head = description.split('leaky=downstream ', 1)[0] + 'leaky=downstream '
            description = head + os.environ['TABS9_DEBUG_TAIL']
            print('DEBUG pipeline tail:', os.environ['TABS9_DEBUG_TAIL'], flush=True)
        try:
            self.pipeline = Gst.parse_launch(description)
            self.pipeline.get_by_name('capture').get_static_pad('src').add_probe(
                Gst.PadProbeType.BUFFER, self.capture_probe)
            encoder_element = self.pipeline.get_by_name('encoder')
            if encoder_element is not None:
                encoder_element.get_static_pad('sink').add_probe(Gst.PadProbeType.BUFFER, self.encoder_in_probe)
            encoded = self.pipeline.get_by_name('encoded')
            if encoded is not None:
                encoded.get_static_pad('sink').add_probe(Gst.PadProbeType.BUFFER, self.encoded_probe)
                encoded.connect('new-sample', self.sample)
            self.force_live_encoder()
            if os.environ.get('TABS9_STAGE_PROBES'):
                self.install_stage_probes()
                def latency_probe():
                    query = Gst.Query.new_latency()
                    enc = self.pipeline.get_by_name('encoder')
                    ok = enc.get_static_pad('sink').peer_query(query) if enc else False
                    print('DEBUG latency query at encoder sink:', ok and query.parse_latency(), flush=True)
                    return False
                GLib.timeout_add_seconds(3, latency_probe)
            bus = self.pipeline.get_bus()
            self.pipeline_bus = bus
            bus.add_signal_watch()
            bus.connect('message::error', self.pipeline_error)
            self.pipeline.set_state(Gst.State.PLAYING)
            if self.report_timer is None:
                self.report_timer = GLib.timeout_add_seconds(5, self.report)
            self.status.write('streaming', self.capture_status())
            print('Capture authorized; starting encoder. Memory path:', self.memory_mode, flush=True)
        except Exception as error:
            print('Encoder setup failed:', type(error).__name__, str(error)[:300], flush=True)
            self.fallback_or_stop()
        return False

    def pipeline_error(self, bus, message):
        error, debug = message.parse_error()
        print('Video pipeline error:', error.message, flush=True)
        self.fallback_or_stop()

    def capture_status(self):
        message = (f'Capture: {self.memory_mode}; {self.args.width}x{self.args.height}, '
                   f'target {self.args.fps} fps (not measured throughput).')
        if self.memory_mode != self.args.capture_memory:
            message += f' WARNING: fallback from {self.args.capture_memory}; performance may be reduced.'
        return message

    def fallback_or_stop(self):
        if self.memory_mode not in ('gl', 'va', 'native'):
            self._fail('Video pipeline failed.')
            return
        if self.native is not None:
            self.native.close()
            self.native = None
        previous_mode = self.memory_mode
        self.memory_mode = 'va' if previous_mode == 'native' else 'system'
        if self.pipeline_bus:
            self.pipeline_bus.remove_signal_watch()
            self.pipeline_bus = None
        if self.pipeline:
            self.pipeline.set_state(Gst.State.NULL)
            self.pipeline = None
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
        try:
            self.fd = self.portal.OpenPipeWireRemote(self.session, dbus.Dictionary({}, signature='sv')).take()
        except dbus.DBusException:
            self._fail('Screen-sharing session is unavailable; restart the USB display.')
            return
        self.capture_caps = None
        self.capture_wall = None
        self.capture_pts = None
        message = f'Capture path {previous_mode} failed; falling back to {self.memory_mode}. Performance may be reduced.'
        print(message, flush=True)
        notify('USB display using fallback capture', message, urgency='critical')
        GLib.idle_add(self.start_pipeline)

    def force_live_encoder(self):
        """Make vah265enc take its low-latency path.

        GstVaBaseEnc asks upstream for liveness once, in set_format, and only a
        live answer selects preferred_output_delay=0 (finish each frame as soon
        as its coded buffer is ready).  pipewiresrc 1.6 answers that query from
        a field it only fills once the stream is STREAMING, and the caps event
        that triggers set_format arrives during negotiation, before that: the
        encoder is told "not live".  It then polls readiness only on the next
        input, on the reconstruct surface that frame N+1 is still reading as a
        reference, so every frame left the encoder two frame periods after it
        entered (measured 33.8 ms at 60 Hz, 67.6 ms at 30 Hz, 17 ms at 120 Hz).

        The probe answers the latency query on the encoder's sink peer as
        live.  It edits the query through its raw pointer because a PyGObject
        wrapper would hold a second reference and make the query read-only.
        """
        encoder = self.pipeline.get_by_name('encoder')
        if encoder is None or os.environ.get('TABS9_NO_FORCE_LIVE'):
            return
        peer = encoder.get_static_pad('sink').get_peer()
        if peer is None:
            return
        import ctypes
        lib = ctypes.CDLL('libgstreamer-1.0.so.0')
        lib.gst_query_set_latency.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_uint64, ctypes.c_uint64]
        lib.gst_query_set_latency.restype = None
        # GstQuery is {GstMiniObject (64 bytes on LP64); GstQueryType type;}.
        type_offset = 64 if ctypes.sizeof(ctypes.c_void_p) == 8 else 36
        latency = int(Gst.QueryType.LATENCY)
        def probe(pad, info):
            if ctypes.c_int.from_address(info.data + type_offset).value == latency:
                lib.gst_query_set_latency(info.data, 1, 0, Gst.CLOCK_TIME_NONE)
                return Gst.PadProbeReturn.HANDLED
            return Gst.PadProbeReturn.OK
        self._live_probe = probe   # keep the closure alive for the pad's lifetime
        peer.add_probe(Gst.PadProbeType.QUERY_UPSTREAM, probe)

    # ---- native capture (native/tabs9-capture) ------------------------------
    def start_native_pipeline(self):
        """Capture through the native helper; encode its NV12 ring with GStreamer.

        The helper returns KWin's buffers inside PipeWire's process callback
        (see native/tabs9-capture.c for the measured reason).  Each converted
        frame arrives here as a slot index; the slot's DMA-BUF is wrapped once
        in a GstMemory and pushed into an appsrc that never drops (block=true):
        a slot is handed back to the helper only after the encoder has finished
        with it, so ring accounting stays exact.  The helper drops on its side
        when the ring is full, and reports that count.
        """
        if GstAllocators is None or GstVideo is None:
            print('GstAllocators/GstVideo typelibs unavailable; run scripts/setup-native.sh', flush=True)
            return self.fallback_or_stop()
        width, height = self.args.width, self.args.height
        try:
            encoder = Gst.ElementFactory.make('vah265enc')
            if encoder is None:
                raise NativeCaptureError('VA HEVC encoder unavailable')
            render_node = encoder.get_property('device-path')
            self.native = NativeCapture(self.fd, self.capture_node, width, height,
                                        slots=int(os.environ.get('TABS9_NATIVE_SLOTS', '6')),
                                        render_node=render_node,
                                        on_frame=self.native_frame, on_exit=self.native_exit)
        except NativeCaptureError as error:
            print('Native capture unavailable:', error, flush=True)
            return self.fallback_or_stop()
        ring = self.native.ring
        allocator = GstAllocators.DmaBufAllocator.new()
        self.native_memories = [GstAllocators.DmaBufAllocator.alloc(allocator, os.dup(fd), size)
                                for fd, size in zip(self.native.fds, ring.sizes)]
        self.native_pushed = collections.deque()   # slots in encoder order
        self.native_last_encoded = None
        self.native_dropped = 0
        self.native_ready_lag = collections.deque(maxlen=1200)
        if self.args.rate_control == 'cqp':
            rc = f'rate-control=cqp qpi={self.args.qp} qpp={self.args.qp}'
        else:
            rc = f'rate-control={self.args.rate_control} bitrate={self.args.bitrate}'
        caps = (f'video/x-raw(memory:DMABuf),format=DMA_DRM,drm-format={ring.drm_format},'
                f'width={width},height={height},framerate=0/1,max-framerate={self.args.fps}/1,'
                'colorimetry=bt709')
        description = (
            f'appsrc name=capture is-live=true format=time do-timestamp=true block=true max-buffers=2 '
            f'caps="{caps}" '
            # vapostproc imports the ring slot (cached per GstMemory) and copies
            # it into its own VAMemory pool, exactly the boundary the encoder
            # already handles; feeding the encoder our DMA-BUFs directly made it
            # re-import every frame and fail on its reconstruct pool.
            '! vapostproc ! video/x-raw(memory:VAMemory),format=NV12,colorimetry=bt709 '
            f'! vah265enc name=encoder {rc} '
            f'key-int-max={self.args.fps} b-frames=0 ref-frames=1 target-usage=7 '
            '! video/x-h265,profile=main '
            '! h265parse config-interval=-1 ! video/x-h265,stream-format=byte-stream,alignment=au '
            '! appsink name=encoded emit-signals=true sync=false max-buffers=4 drop=true')
        try:
            self.pipeline = Gst.parse_launch(description)
            self.pipeline.get_by_name('capture').get_static_pad('src').add_probe(
                Gst.PadProbeType.BUFFER, self.capture_probe)
            self.pipeline.get_by_name('encoder').get_static_pad('sink').add_probe(
                Gst.PadProbeType.BUFFER, self.encoder_in_probe)
            self.pipeline.get_by_name('encoded').get_static_pad('sink').add_probe(
                Gst.PadProbeType.BUFFER, self.encoded_probe)
            self.pipeline.get_by_name('encoded').connect('new-sample', self.sample)
            if os.environ.get('TABS9_STAGE_PROBES'):
                self.install_stage_probes()
            bus = self.pipeline.get_bus()
            self.pipeline_bus = bus
            bus.add_signal_watch()
            bus.connect('message::error', self.pipeline_error)
            self.pipeline.set_state(Gst.State.PLAYING)
            if self.report_timer is None:
                self.report_timer = GLib.timeout_add_seconds(5, self.report)
            self.status.write('streaming', self.capture_status())
            print('Capture authorized; starting encoder. Memory path: native '
                  f'(ring of {ring.slots} {ring.drm_format} surfaces, pitch {ring.pitches[0][0]})', flush=True)
        except Exception as error:
            print('Encoder setup failed:', type(error).__name__, str(error)[:300], flush=True)
            self.fallback_or_stop()
        return False

    def native_frame(self, frame):
        """Reader thread: wrap the slot and push it (blocks while the encoder is busy)."""
        source = self.pipeline.get_by_name('capture') if self.pipeline else None
        if source is None or self.closing:
            self.native.release(frame.slot)
            return
        ring = self.native.ring
        buffer = Gst.Buffer.new()
        buffer.append_memory(self.native_memories[frame.slot])
        offsets = list(ring.offsets[frame.slot]) + [0, 0]
        strides = list(ring.pitches[frame.slot]) + [0, 0]
        GstVideo.buffer_add_video_meta_full(buffer, GstVideo.VideoFrameFlags.NONE,
                                            GstVideo.VideoFormat.NV12, ring.width, ring.height,
                                            2, offsets, strides)
        buffer.offset = frame.seq
        now = time.monotonic_ns()
        self.native_ready_lag.append((now - frame.dequeued_ns) / 1e6)
        self.native_dropped = frame.dropped
        if self.capture_pts is not None and frame.pts_ns > self.capture_pts:
            self.capture_pts_intervals.append((frame.pts_ns - self.capture_pts) / 1e6)
        self.capture_pts = frame.pts_ns
        if self.native_last_seq is not None and frame.seq > self.native_last_seq + 1:
            self.native_seq_gaps += frame.seq - self.native_last_seq - 1
        self.native_last_seq = frame.seq
        self.native_pushed.append(frame.slot)
        if source.emit('push-buffer', buffer) != Gst.FlowReturn.OK:
            self.native_pushed.remove(frame.slot)
            self.native.release(frame.slot)

    def encoded_probe(self, pad, info):
        """Every encoded frame, before the appsink may drop it (drop=true).

        Pairs the frame with its capture arrival by output pts (the appsink
        pulls by pts, so a dropped sample cannot shift later pairings), and
        on the native path returns the ring slot before this one, which the
        encoder has certainly finished with."""
        buffer = info.get_buffer()
        if buffer is None:
            return Gst.PadProbeReturn.OK
        self.encoded_arrivals[buffer.pts] = self.encoder_fifo.popleft() if self.encoder_fifo else None
        while len(self.encoded_arrivals) > 16:
            self.encoded_arrivals.popitem(last=False)
        if self.native is not None and self.native_pushed:
            slot = self.native_pushed.popleft()
            if self.native_last_encoded is not None:
                self.native.release(self.native_last_encoded)
            self.native_last_encoded = slot
        return Gst.PadProbeReturn.OK

    def native_exit(self, code):
        if not self.closing:
            print(f'Native capture helper exited ({code}); falling back.', flush=True)
            GLib.idle_add(self.fallback_or_stop)

    def install_stage_probes(self):
        """Diagnostics only: per-stage dwell time (ms) from in-process pad probes."""
        # Marks are keyed by PTS: a FIFO pairing drifts by one frame for every
        # buffer a leaky queue drops between two probes and then reports a
        # whole frame period as "dwell" forever.
        self.stage_marks = collections.defaultdict(dict)
        self.stage_times = collections.defaultdict(lambda: collections.deque(maxlen=600))
        def remember(name, pts, now):
            marks = self.stage_marks[name]
            marks[pts] = now
            if len(marks) > 64:
                for old in sorted(marks)[:32]:
                    del marks[old]
        def mark(name):
            def probe(pad, info):
                buffer = info.get_buffer()
                if buffer is not None:
                    remember(name, buffer.pts, time.monotonic())
                return Gst.PadProbeReturn.OK
            return probe
        def since(name, previous, fifo=False):
            # fifo: the encoder rewrites timestamps but never drops, so its
            # output pairs with the oldest pending input mark.
            def probe(pad, info):
                buffer = info.get_buffer()
                if buffer is None:
                    return Gst.PadProbeReturn.OK
                now = time.monotonic()
                marks = self.stage_marks[previous]
                start = marks.pop(min(marks), None) if fifo and marks else marks.pop(buffer.pts, None)
                if start is not None:
                    self.stage_times[name].append((now - start) * 1000)
                remember(name, buffer.pts, now)
                return Gst.PadProbeReturn.OK
            return probe
        stages = [('capture', 'capture', 'src', None), ('queue', 'vapostproc0', 'sink', 'capture'),
                  ('convert', 'vapostproc0', 'src', 'queue'), ('encqueue', 'encoder', 'sink', 'convert'),
                  ('encode', 'encoder', 'src', 'encqueue')]
        for name, element, padname, previous in stages:
            el = self.pipeline.get_by_name(element)
            if el is None:
                continue
            pad = el.get_static_pad(padname)
            pad.add_probe(Gst.PadProbeType.BUFFER, mark(name) if previous is None
                          else since(name, previous, fifo=(name == 'encode')))

    def encoder_in_probe(self, pad, info):
        buffer = info.get_buffer()
        if buffer is not None:
            # Order is enough between here and the appsink pad (b-frames=0,
            # nothing drops); encoded_probe re-keys by output pts because the
            # encoder re-bases timestamps and the appsink may drop samples.
            self.encoder_fifo.append(self.capture_arrivals.pop(buffer.pts, None))
            while len(self.encoder_fifo) > 16:
                self.encoder_fifo.popleft()
        return Gst.PadProbeReturn.OK

    def capture_probe(self, pad, info):
        buffer = info.get_buffer()
        if buffer is not None:
            now = time.monotonic()
            self.capture_frames += 1
            if self.capture_wall is not None:
                self.capture_intervals.append((now - self.capture_wall) * 1000)
                if TRACE_CAPTURE:
                    # arrival interval / KWin pts interval / KWin seq delta
                    pts_delta = ((buffer.pts - self.capture_pts) / 1e6 if self.capture_pts is not None
                                 and buffer.pts != Gst.CLOCK_TIME_NONE else float('nan'))
                    seq_delta = (buffer.offset - self.capture_seq) if self.capture_seq is not None else 0
                    self.capture_trace.append(f'{(now - self.capture_wall) * 1000:.1f}/{pts_delta:.1f}/{seq_delta}')
                    if len(self.capture_trace) >= 90:
                        print('TRACE capture arrival/pts/seq:', ' '.join(self.capture_trace), flush=True)
                        self.capture_trace.clear()
            self.capture_seq = buffer.offset
            self.capture_wall = now
            self.capture_arrivals[buffer.pts] = now
            while len(self.capture_arrivals) > 16:
                self.capture_arrivals.popitem(last=False)
            if buffer.pts != Gst.CLOCK_TIME_NONE and self.native is None:
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
            self._fail('Display setup failed: ' + type(error).__name__)
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
        # Place virtual output to the RIGHT of all existing physical outputs
        x, y = compute_virtual_position(current, self.previous, gap=a.gap, scale=a.scale)
        configure(f'output.{name}.mode.{a.width}x{a.height}@{a.fps}',
            f'output.{name}.scale.{a.scale}', f'output.{name}.position.{x},{y}', f'output.{name}.enable')
        final = next(o for o in outputs() if o['name'] == name)
        mode = next(m for m in final['modes'] if m['id'] == final['currentModeId'])
        print('Extended output:', json.dumps({'size': mode['size'], 'Hz': mode['refreshRate'],
              'scale': final['scale'], 'position': final['pos']}), flush=True)
        self.aio.call_soon_threadsafe(lambda: asyncio.create_task(self.broadcast_settings()))
        self.request_capture_session()
        return False

    def connect_eis(self, target):
        """Prefer libei for touch; fall back to the portal's own calls."""
        if not self.touch.touch_enabled:
            return
        try:
            fd = self.remote.ConnectToEIS(self.session, dbus.Dictionary({}, signature='sv')).take()
            self.eis = EisTouch(fd, lambda: ((g := target.geometry()).x, g.y, *g.logical_size),
                                layout_changed=target.invalidate)
        except (dbus.DBusException, EisError, AttributeError) as error:
            print('EIS unavailable, using portal touch:', type(error).__name__, error, flush=True)
            return
        def pump(_fd, condition):
            if condition & (GLib.IOCondition.HUP | GLib.IOCondition.ERR):
                print('EIS connection closed; touch is not ready. Reconnect the display session.', flush=True)
                self.touch.touch_backend = None
                self.eis.close()
                self.eis = None
                return False
            try:
                self.eis.dispatch()
            except Exception as error:
                print('EIS dispatch failed:', type(error).__name__, error, flush=True)
                return False
            return True
        GLib.io_add_watch(self.eis.fd, GLib.PRIORITY_DEFAULT,
                          GLib.IOCondition.IN | GLib.IOCondition.HUP | GLib.IOCondition.ERR, pump)
        self.touch.touch_backend = self.eis
        # A touch before the EIS device is resumed raises and is counted as rejected.

    def _virtual_x(self):
        with contextlib.suppress(Exception):
            return next(o['pos']['x'] for o in outputs() if o['name'] == self.virtual_name)
        return '?'

    def request_capture_session(self):
        self.status.write('waiting_capture_consent')
        self.request(self.remote.CreateSession, [dbus.Dictionary({
            'session_handle_token': 'tabs9_capture_' + secrets.token_hex(8)}, signature='sv')], self.capture_created)

    def watch_session(self, session):
        def closed(*args):
            if not self.closing:
                self._fail('KDE sharing session ended unexpectedly.')
        self.session_watches.append(self.bus.add_signal_receiver(closed,
            signal_name='Closed', dbus_interface='org.freedesktop.portal.Session', path=str(session)))

    def capture_created(self, result):
        self.session = result['session_handle']
        options = dbus.Dictionary({
            'types': dbus.UInt32(6),
            'persist_mode': dbus.UInt32(2),
        }, signature='sv')
        tokens = load_tokens(self.tokens_file)
        if 'remotedesktop_capture' in tokens:
            options['restore_token'] = tokens['remotedesktop_capture']
            self._capture_token_used = True
            print('Presenting the stored capture token; no dialog is expected.', flush=True)
        else:
            print('Choose the existing virtual monitor in the sharing dialog.', flush=True)
            notify('Allow screen sharing and input', CAPTURE_DIALOG_HINT)
        self.request(self.remote.SelectDevices, [self.session, options], self.capture_devices_selected)

    def capture_devices_selected(self, result):
        self._do_capture_select_sources()

    def _do_capture_select_sources(self):
        """Issue SelectSources for the capture session."""
        # multiple=True: KDE then streams one node per screen (see started()).
        options = dbus.Dictionary({
            'types': dbus.UInt32(1), 'multiple': True,
            'cursor_mode': dbus.UInt32(2),
        }, signature='sv')
        self.request(self.portal.SelectSources, [self.session, options], self.selected)

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
        arrival = self.encoded_arrivals.pop(buf.pts, None)
        self.aio.call_soon_threadsafe(self.distribute, packet, keyframe, seq, arrival)
        return Gst.FlowReturn.OK

    def request_keyframe(self):
        """Ask the encoder for an IDR so a client can resume at the next AU."""
        if self.pipeline is None:
            return False
        encoder = self.pipeline.get_by_name('encoder')
        if encoder is not None:
            # Same layout as gst_video_event_new_upstream_force_key_unit (the
            # GstVideo typelib is not installed here).
            structure = Gst.Structure.from_string(
                f'GstForceKeyUnit, running-time=(guint64){Gst.CLOCK_TIME_NONE}, '
                'all-headers=(boolean)true, count=(uint)0')[0]
            encoder.send_event(Gst.Event.new_custom(Gst.EventType.CUSTOM_UPSTREAM, structure))
        return False

    def distribute(self, packet, keyframe, seq, arrival=None):
        self.sent[seq] = (time.monotonic(), arrival)
        while len(self.sent) > 1200:
            self.sent.popitem(last=False)
        for queue in tuple(self.clients):
            if queue.full():
                # The socket is behind by more than the queue: abandon this
                # GOP (never send a dependent frame without its references),
                # and ask for an IDR so the client resumes at the next
                # keyframe instead of waiting for the periodic one.
                while not queue.empty(): queue.get_nowait()
                queue.put_nowait(RESYNC)
                self.resyncs += 1
                GLib.idle_add(self.request_keyframe)
            else:
                queue.put_nowait((packet, keyframe))

    # Idle-liveness heartbeat on the video socket. KWin sends no frame while
    # the desktop is static, so without it a client cannot tell "nothing
    # changed" from "the host is gone" and its read deadline fires. Sent only
    # to clients that advertised `video_heartbeat`: an older client treats an
    # unknown packet type as a framing error and reconnects.
    HEARTBEAT_INTERVAL = 1.0
    DRAIN_TIMEOUT = 2.0
    PACKET_HEARTBEAT = b'\x02'

    async def video(self, reader, writer):
        queue = asyncio.Queue(maxsize=8)
        watcher = None
        generation = None
        reason = 'stopped'
        try:
            token = await asyncio.wait_for(reader.readexactly(64), 3)
            if not hmac.compare_digest(token, self.token.encode()):
                reason = 'unauthorized'
                return
            writer.get_extra_info('socket').setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            self.video_generation += 1
            self.video_connects += 1
            generation = self.video_generation
            print(f'Video client {generation} connected; requesting a keyframe.', flush=True)
            self.clients.add(queue)
            # A fresh connection starts at an IDR (`waiting` below); ask for one
            # now instead of leaving the client to wait for the periodic one.
            GLib.idle_add(self.request_keyframe)
            async def disconnected():
                await reader.read(1)
                self.clients.discard(queue)
                while not queue.empty(): queue.get_nowait()
                queue.put_nowait(None)
            watcher = asyncio.create_task(disconnected())
            waiting = True
            heartbeat = 0
            while True:
                try:
                    item = await asyncio.wait_for(queue.get(), self.HEARTBEAT_INTERVAL)
                except asyncio.TimeoutError:
                    if 'video_heartbeat' not in self.client_features:
                        continue
                    heartbeat += 1
                    payload = self.PACKET_HEARTBEAT + struct.pack('!I', heartbeat & 0xffffffff)
                    writer.write(struct.pack('!I', len(payload)) + payload)
                    await asyncio.wait_for(writer.drain(), self.DRAIN_TIMEOUT)
                    self.heartbeats_sent += 1
                    continue
                if item is None:
                    reason = 'client closed'
                    break
                if item is RESYNC:
                    waiting = True
                    continue
                packet, keyframe = item
                if waiting and not keyframe: continue
                waiting = False
                writer.write(packet)
                await asyncio.wait_for(writer.drain(), self.DRAIN_TIMEOUT)
        except asyncio.TimeoutError:
            reason = 'write timeout' if generation is not None else 'auth timeout'
        except (ConnectionError, asyncio.IncompleteReadError):
            reason = 'connection error'
        finally:
            self.clients.discard(queue)
            if generation is not None:
                self.video_disconnects += 1
                self.last_video_disconnect = reason
                print(f'Video client {generation} disconnected: {reason}.', flush=True)
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
            self.client_features = set()   # re-learned from this client's config
            self.control_generation += 1
            generation = self.control_generation
            GLib.idle_add(self.begin_control, generation)
            self.controls.add(ws)
            await ws.send(json.dumps(self.settings()))
            async for raw in ws:
                msg = json.loads(raw)
                if msg.get('type') == 'rendered':
                    self.rendered += 1
                    now = time.monotonic()
                    if self.ack_wall is not None:
                        self.ack_intervals.append((now - self.ack_wall) * 1000)
                    self.ack_wall = now
                    sent = self.sent.get(int(msg['seq']))
                    if sent is not None:
                        self.stats.append((now - sent[0]) * 1000)
                        if sent[1] is not None:
                            self.capture_to_ack.append((now - sent[1]) * 1000)
                    render_ns = msg.get('render_ns')
                    if isinstance(render_ns, int) and not isinstance(render_ns, bool):
                        # Tablet clock domain only: consecutive render stamps.
                        if self.tablet_render_ns is not None and 0 < render_ns - self.tablet_render_ns < 5_000_000_000:
                            self.render_intervals.append((render_ns - self.tablet_render_ns) / 1e6)
                        self.tablet_render_ns = render_ns
                elif msg.get('type') == 'keyframe':
                    GLib.idle_add(self.request_keyframe)
                elif msg.get('type') == 'config':
                    features = msg.get('features')
                    if isinstance(features, list):
                        self.client_features = {f for f in features if isinstance(f, str) and len(f) <= 32}
                    protocol = msg.get('protocol')
                    if type(protocol) is not int:
                        print('Tablet client sent no protocol version: treating it as protocol 1 '
                              '(render timestamps and keyframe requests are used if it sends them).',
                              flush=True)
                    elif protocol > self.PROTOCOL:
                        print(f'Tablet client speaks protocol {protocol}, host {self.PROTOCOL}: '
                              'update the host; continuing with the common subset.', flush=True)
                    GLib.idle_add(self.apply_settings, msg, generation)
                elif msg.get('type') == 'stats':
                    self.tablet_stats = {k: round(float(msg[k]), 2)
                        for k in ('panel_hz', 'decoder_fps', 'received_mbps', 'stream_fps')
                        if isinstance(msg.get(k), (int, float)) and math.isfinite(msg[k]) and 0 <= msg[k] <= 1000}
                elif msg.get('type') in ('touch', 'pen'):
                    self.input_messages += 1
                    GLib.idle_add(self.handle_touch, msg, generation)
                elif msg.get('type') == 'resolution':
                    self.note_tablet_panel(msg)
                elif msg.get('type') == 'mode':
                    # Pen-only (tablet as a graphics tablet for the laptop's own
                    # screen) is not implemented host-side. Answer with the mode
                    # actually in force so the tablet's toggle snaps back instead
                    # of showing a state the host never entered.
                    await ws.send(json.dumps(self.settings()))
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
            if self.gestures is not None and message.get('type') == 'touch':
                self._guard_input(self.gestures.handle, message)
            else:
                self._guard_input(self.touch.handle_message, message)
        return False

    def _deliver_input(self, message):
        # Also reached from a hold-window timer, after the input may be gone.
        if self.touch is not None:
            self.touch.handle_message(message)

    def _input_timer(self, callback):
        # A gesture hold window ended: whatever was held back is delivered
        # now, under the same error handling as a message that just arrived.
        self._guard_input(callback)
        return False

    def _guard_input(self, deliver, *args):
        try:
            deliver(*args)
        except (TouchInputError, dbus.DBusException, EisError) as error:
            followon = 'slot is not active' in str(error) or 'pen is not down' in str(error)
            if followon:
                self.input_followon_rejected += 1
            else:
                self.input_rejected += 1
            rejected = self.input_followon_rejected if followon else self.input_rejected
            label = 'Follow-on input ignored' if followon else 'Tablet input rejected'
            if rejected <= 5 or rejected % 100 == 0:
                print(f'{label} ({rejected}): {type(error).__name__}: {error}', flush=True)
            self.release_touch()

    # Swipe -> KWin global shortcut, by name (kglobalaccel's kwin component).
    # Fingers move left, content moves left: the window/desktop "to the
    # right" comes in, as with KWin's own touchpad gestures. Invoked without a
    # modifier held, "Walk Through Windows" is KWin's one-step switch: it
    # activates the next window immediately, no popup. Up/down mirror the
    # laptop touchpad's four-finger swipes (Overview and the desktop grid).
    GESTURE_SHORTCUTS = {
        (3, 'left'): 'Walk Through Windows',
        (3, 'right'): 'Walk Through Windows (Reverse)',
        (3, 'up'): 'Overview',
        (3, 'down'): 'Grid View',
        (4, 'left'): 'Switch One Desktop to the Right',
        (4, 'right'): 'Switch One Desktop to the Left',
    }

    def perform_gesture(self, fingers, direction):
        name = self.GESTURE_SHORTCUTS.get((fingers, direction))
        if name is None:
            return False
        self.gestures_fired += 1
        print(f'Gesture: {fingers} fingers {direction} -> {name}', flush=True)
        self.invoke_kwin_shortcut(name)
        return True

    def perform_scroll(self, phase, x, y):
        """Two fingers moving together: pointer axes aimed at where they landed."""
        if self.touch is None:
            return False
        if phase == 'begin':
            if not self.touch.scroll_begin(x, y):
                return False
            self.scrolls += 1
            return True
        if phase == 'move':
            self.touch.scroll(x * self.scroll_sign * self.scroll_gain,
                              y * self.scroll_sign * self.scroll_gain)
        else:
            self.touch.scroll_end()
        return True

    def invoke_kwin_shortcut(self, name):
        if self.kglobalaccel is None:
            self.kglobalaccel = dbus.Interface(
                self.bus.get_object('org.kde.kglobalaccel', '/component/kwin'),
                'org.kde.kglobalaccel.Component')
        # Asynchronous: the touch path must not wait on KWin.
        self.kglobalaccel.invokeShortcut(name, reply_handler=lambda *_: None,
            error_handler=lambda error: print(f'KWin shortcut {name!r} failed: {error}', flush=True))

    def note_tablet_panel(self, message):
        """Record the tablet's own panel size and warn once if it disagrees.

        The tablet announces its native landscape resolution on connect. The
        host cannot resize a virtual output that KWin has already bound to a
        stream, so a mismatch is reported rather than silently stretched: the
        picture would be rescaled on the tablet and touch coordinates would land
        on the wrong pixels.
        """
        width, height = message.get('width'), message.get('height')
        if (isinstance(width, bool) or isinstance(height, bool) or
                type(width) is not int or type(height) is not int or
                not (320 <= width <= 8192 and 240 <= height <= 8192)):
            return False
        self.tablet_panel = (width, height)
        if (width, height) != (self.args.width, self.args.height) and not self.panel_mismatch_reported:
            self.panel_mismatch_reported = True
            print(f'Tablet panel is {width}x{height} but the virtual output is '
                  f'{self.args.width}x{self.args.height}; restart with '
                  f'--width {width} --height {height} for a pixel-exact image.', flush=True)
        return True

    def release_touch(self):
        if self.gestures is not None:
            self.gestures.reset()
        if self.touch is not None:
            self.touch.release_all()
        return False

    # Control-channel contract. 2 adds: render_ns in acks, {"type":"keyframe"}
    # requests, a 'protocol' field in the tablet's config message. The video
    # framing (4-byte length, type 0x01, 4-byte sequence) is unchanged, so a
    # protocol-1 client (the previous APK) still streams: legacy mode.
    PROTOCOL = 2

    def settings(self):
        return {'status': 'connected', 'protocol': self.PROTOCOL, 'width': self.args.width,
                'height': self.args.height, 'codec': 'hevc', 'pen_only': False,
                'fps': self.args.fps, 'bitrate': self.args.bitrate,
                'features': ['keyframe_request', 'render_ns', 'video_heartbeat']}

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
        if generation is not None and getattr(self.args, 'settings_locked', False) \
                and (fps, bitrate) != (self.args.fps, self.args.bitrate):
            # The host was launched with an explicit profile: the tablet's
            # remembered settings are a request, answered with what is in force.
            print(f'Tablet asked for {fps} fps / {bitrate} kbps; keeping the launch profile '
                  f'({self.args.fps} fps / {self.args.bitrate} kbps).', flush=True)
            self.aio.call_soon_threadsafe(lambda: asyncio.create_task(self.broadcast_settings()))
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

    def drop_video_clients(self):
        print(f'Dropping {len(self.clients)} video client(s) on request (recovery drill).', flush=True)
        def drop():
            for queue in tuple(self.clients):
                while not queue.empty(): queue.get_nowait()
                queue.put_nowait(None)
        self.aio.call_soon_threadsafe(drop)
        return True

    def report(self):
        stats = sorted(self.stats)
        self.stats.clear()
        acks = sorted(self.ack_intervals)
        self.ack_intervals.clear()
        renders = sorted(self.render_intervals)
        self.render_intervals.clear()
        c2a = sorted(self.capture_to_ack)
        self.capture_to_ack.clear()
        def pct(values, q):
            return round(values[min(len(values) - 1, int(len(values) * q))], 1) if values else None
        now = time.monotonic()
        before, frames, rendered, captured = self.last_report
        elapsed = now - before
        self.last_report = (now, self.frames, self.rendered, self.capture_frames)
        arrivals = sorted(self.capture_intervals)
        self.capture_intervals.clear()
        if os.environ.get('TABS9_STAGE_PROBES') and getattr(self, 'stage_times', None):
            summary = {}
            for name, values in self.stage_times.items():
                v = sorted(values)
                if v:
                    summary[name] = (round(v[len(v)//2], 1), round(v[int(len(v)*0.9)], 1), round(v[-1], 1))
            print('STAGES ms p50/p90/max:', summary, flush=True)
            for values in self.stage_times.values():
                values.clear()
        timestamps = sorted(self.capture_pts_intervals)
        self.capture_pts_intervals.clear()
        print(json.dumps({'encoded_frames': self.frames, 'tablet_rendered_acks': self.rendered,
            'capture_memory': self.memory_mode,
            'capture_fps': round((self.capture_frames - captured) / elapsed, 1),
            'encoded_fps': round((self.frames - frames) / elapsed, 1),
            'tablet_ack_fps': round((self.rendered - rendered) / elapsed, 1),
            'tablet_input_messages': self.input_messages,
            'tablet_input_rejected': self.input_rejected,
            'tablet_input_followon_rejected': self.input_followon_rejected,
            'tablet_gestures': self.gestures_fired,
            'tablet_scrolls': self.scrolls,
            'client_resyncs': self.resyncs,
            'video_clients': len(self.clients), 'video_connects': self.video_connects,
            'video_disconnects': self.video_disconnects, 'last_video_disconnect': self.last_video_disconnect,
            'heartbeats_sent': self.heartbeats_sent,
            'native_dropped': self.native_dropped if self.native is not None else None,
            'native_pending': len(self.native_pushed) if self.native is not None else None,
            'native_seq_gaps': self.native_seq_gaps if self.native is not None else None,
            'native_convert_ms_p50': pct(sorted(self.native_ready_lag), 0.5) if self.native is not None else None,
            'tablet_input_mode': self.touch.mode if self.touch else None,
            'tablet': self.tablet_stats,
            'capture_interval_ms_p50': round(arrivals[len(arrivals)//2], 2) if arrivals else None,
            'capture_interval_ms_p90': round(arrivals[int(len(arrivals)*0.9)], 2) if arrivals else None,
            'capture_interval_ms_p95': pct(arrivals, 0.95),
            'capture_interval_ms_max': round(arrivals[-1], 1) if arrivals else None,
            'capture_stalls_over_100ms': sum(1 for v in arrivals if v > 100),
            'capture_pts_interval_ms_p50': round(timestamps[len(timestamps)//2], 2) if timestamps else None,
            'encode_to_render_ms_p50': round(stats[len(stats)//2], 1) if stats else None,
            # Host-side software measurement including the return path; not pixel latency.
            'capture_to_ack_ms_p50': pct(c2a, 0.5),
            'capture_to_ack_ms_p95': pct(c2a, 0.95),
            'capture_to_ack_ms_max': pct(c2a, 1.0),
            'capture_to_ack_over_100ms': sum(1 for v in c2a if v > 100),
            # Interval between render acknowledgements as seen on the host (a
            # proxy until the tablet reports its own render timestamps).
            'ack_interval_ms_p50': pct(acks, 0.5),
            'ack_interval_ms_p95': pct(acks, 0.95),
            'ack_interval_ms_max': pct(acks, 1.0),
            # Intervals between the tablet's own OnFrameRendered timestamps.
            'render_interval_ms_p50': pct(renders, 0.5),
            'render_interval_ms_p95': pct(renders, 0.95),
            'render_interval_ms_max': pct(renders, 1.0),
            'render_stalls_over_100ms': sum(1 for v in renders if v > 100)}), flush=True)
        return True

    def run(self):
        # A failing status write here must never prevent startup or skip the
        # `finally` cleanup below -- nothing has been created yet, so there is
        # nothing to leak, but the host should still try to run.
        with contextlib.suppress(Exception):
            self.status.write('starting')
        def worker():
            asyncio.set_event_loop(self.aio)
            self.aio.run_until_complete(self.servers())
        try:
            threading.Thread(target=worker, daemon=True).start()
            if not self.ready.wait(5): raise RuntimeError('Local streaming ports unavailable')
            # Report from the moment the sockets are up, not from the moment
            # capture starts: while the KDE sharing dialogs are still open this
            # is the only signal that the tablet is connected and its input is
            # arriving over USB.
            self.report_timer = GLib.timeout_add_seconds(5, self.report)
            for port in (8890, 8891):
                adb('reverse', '--no-rebind', f'tcp:{port}', f'tcp:{port}')
                self.reverse_ports.append(port)
            adb('shell', 'am', 'start', '-n', 'local.tabs9.usbdisplay/.MainActivity', '--es', 'token', self.token)
            self.create()
            for sig in (signal.SIGINT, signal.SIGTERM):
                GLibUnix.signal_add(GLib.PRIORITY_DEFAULT, sig, lambda: self.loop.quit() or False)
            # Recovery drill: SIGUSR1 drops every video client (the socket
            # closes, the tablet must reconnect and resume at a keyframe)
            # without touching the control channel or the capture pipeline.
            GLibUnix.signal_add(GLib.PRIORITY_DEFAULT, signal.SIGUSR1, self.drop_video_clients)
            self.loop.run()
        except Exception as error:
            # RuntimeErrors raised in this method carry static, non-sensitive
            # messages we wrote ourselves (e.g. "Local streaming ports
            # unavailable", adb()'s "USB device command failed..."); keep
            # them, since they are far more actionable than the type name
            # alone. Other exception types may carry paths or other detail
            # from lower-level libraries, so only the type name is reported.
            detail = str(error) if isinstance(error, RuntimeError) else type(error).__name__
            self._fail('Host startup error: ' + detail)
        finally:
            self.closing = True
            if not getattr(self, 'failed', False):
                with contextlib.suppress(Exception):
                    self.status.write('stopped')
            with contextlib.suppress(Exception):
                self.release_touch()
            if getattr(self, 'eis', None) is not None:
                with contextlib.suppress(Exception):
                    self.eis.close()
            if getattr(self, 'native', None) is not None:
                with contextlib.suppress(Exception):
                    self.native.close()
            if self.pipeline:
                with contextlib.suppress(Exception):
                    self.pipeline.set_state(Gst.State.NULL)
            for session in (self.session, self.creation_session):
                if session:
                    with contextlib.suppress(Exception):
                        dbus.Interface(self.bus.get_object('org.freedesktop.portal.Desktop', session),
                            'org.freedesktop.portal.Session').Close()
            for watch in self.session_watches:
                with contextlib.suppress(Exception):
                    watch.remove()
            if self.fd is not None:
                with contextlib.suppress(OSError):
                    os.close(self.fd)
            for port in self.reverse_ports:
                with contextlib.suppress(Exception):
                    adb('reverse', '--remove', f'tcp:{port}')

# Presets for `--profile`; explicit --fps/--bitrate still win.  All of them
# stay on the zero-copy VA path; they only change how much the compositor and
# encoder have to do per second when the tablet's content is moving.
PROFILES = {
    'smooth': {'fps': 120, 'bitrate': 60000},    # full panel rate
    'balanced': {'fps': 60, 'bitrate': 30000},   # half the GPU/USB work
    'light': {'fps': 30, 'bitrate': 15000},      # static-content use
}

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--profile', choices=sorted(PROFILES), default='smooth')
    parser.add_argument('--resolution', default='2960x1848', metavar='WIDTHxHEIGHT',
                        help='pixel resolution, independently of --fps (default: 2960x1848)')
    parser.add_argument('--fps', type=int, choices=[30, 60, 90, 120])
    parser.add_argument('--bitrate', type=int)
    parser.add_argument('--scale', type=float, default=1.5)
    parser.add_argument('--gap', type=int, default=1, metavar='PX',
                        help='logical pixels left between the laptop and the virtual output '
                             '(default 1: stops windows on the shared edge from painting a '
                             '1-2 px column onto the tablet during desktop-switch animations; '
                             '0 makes the outputs touch exactly as KWin would place them)')
    parser.add_argument('--capture-memory', choices=['native', 'va', 'system', 'gl'], default='native',
                        help='native: PipeWire consumer in native/tabs9-capture (default; falls back to va)')
    parser.add_argument('--gestures', choices=['on', 'off'], default='on',
                        help='three-finger swipes switch windows, four-finger swipes switch '
                             'virtual desktops (default on)')
    parser.add_argument('--gesture-hold-ms', type=int, default=120, metavar='MS',
                        help='how long the fingers of a swipe may take to all land; also the '
                             'most a single-finger drag is delayed (default 120)')
    parser.add_argument('--scroll', choices=['natural', 'standard', 'off'], default='natural',
                        help='two fingers moving together scroll what is under them: natural '
                             '(content follows the fingers, default), standard (mouse-wheel '
                             'direction) or off (two fingers reach the desktop as touches)')
    parser.add_argument('--scroll-gain', type=float, default=DEFAULT_SCROLL_GAIN, metavar='FACTOR',
                        help='pointer-axis distance per logical pixel of finger travel (default '
                             f'{DEFAULT_SCROLL_GAIN}: content keeps pace with the fingers in Qt '
                             'apps, which read 10 axis units as one wheel notch)')
    parser.add_argument('--rate-control', choices=['cbr', 'vbr', 'cqp'], default='cbr')
    parser.add_argument('--qp', type=int, default=24)
    args = parser.parse_args()
    try:
        args.width, args.height = (int(part) for part in args.resolution.lower().split('x', 1))
    except (TypeError, ValueError):
        parser.error('--resolution must be WIDTHxHEIGHT, for example 2960x1848')
    for key, value in PROFILES[args.profile].items():
        if getattr(args, key) is None:
            setattr(args, key, value)
    # An explicit --profile/--fps/--bitrate on the command line wins over the
    # tablet's remembered settings; the bare default stays adjustable from it.
    args.settings_locked = any(a.split('=', 1)[0] in ('--profile', '--fps', '--bitrate')
                               for a in sys.argv[1:])
    if args.capture_memory == 'gl':
        os.environ['__NV_PRIME_RENDER_OFFLOAD'] = '1'
        os.environ['GST_GL_PLATFORM'] = 'egl'
        os.environ['GST_GL_WINDOW'] = 'surfaceless'
    if not (320 <= args.width <= 4096 and 240 <= args.height <= 4096 and
            args.width % 2 == 0 and args.height % 2 == 0 and
            1 <= args.scale <= 3 and 1000 <= args.bitrate <= 150000):
        parser.error('Invalid resolution, scale or bitrate')
    if not 1 <= args.gesture_hold_ms <= 1000:
        parser.error('--gesture-hold-ms must be between 1 and 1000')
    if not 0.1 <= args.scroll_gain <= 10:
        parser.error('--scroll-gain must be between 0.1 and 10')
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
