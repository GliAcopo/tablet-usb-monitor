"""The tablet as a sound output: a PipeWire sink whose audio plays on the tablet.

`--audio on` (the default) gives the computer one more output device while
the host runs, "tabs9 <tablet>", next to the speakers in the volume applet
and in every app's output menu. What is played to it reaches the tablet as
raw PCM (48 kHz, stereo, 16-bit: 1.5 Mbit/s, small next to the video) over
the video socket and plays through the tablet's own speakers or headphones.
`--audio default` also makes it the computer's default output for as long
as the host runs and puts the previous default back when it stops.

The sink is PipeWire's null sink (module-null-sink through pipewire-pulse)
and the host records its monitor. The module is unloaded when the host
stops; one left behind by a host that was killed outright is unloaded at
the next start. (A stream of the host's own declared as an Audio/Sink would
vanish with the process by itself, but it only works while the session
manager happens to configure its format: on this computer it negotiated
once and then hung every later start.) monitor.channel-volumes makes the
sink's volume slider act on what the tablet plays, as on any device.

Digital silence (a paused player still keeps the graph running) is not
sent: the tablet then simply runs out of samples and pauses its own output.
"""
from __future__ import annotations

import struct
import subprocess

RATE = 48000
CHANNELS = 2
BYTES_PER_FRAME = 2 * CHANNELS
# Graph quantum asked for: 10 ms per packet keeps the latency low without
# turning the stream into a flood of tiny packets.
QUANTUM = 480
PACKET_AUDIO = b'\x03'
AUDIO_HEADER = 5     # type byte + 4-byte big-endian sequence number


def node_name(slug: str) -> str:
    """PipeWire node.name of a tablet's sink (also what pactl calls it)."""
    return f'tabs9.{slug}'


def description(label: str) -> str:
    """The name people see in the volume applet."""
    return f'tabs9 {label}'


def sink_properties(label: str) -> str:
    """`sink_properties=` of module-null-sink (pactl's property-list syntax)."""
    quoted = description(label).replace('"', "'")
    return ' '.join([
        f'device.description="{quoted}"',
        # Never chosen as the default by the session manager on its own:
        # appearing must not take the sound away from the speakers.
        'priority.session=0',
        f'node.latency={QUANTUM}/{RATE}',
        'monitor.channel-volumes=true',
        'device.icon_name=tablet',
    ])


def module_args(slug: str, label: str) -> list[str]:
    return ['module-null-sink', f'sink_name={node_name(slug)}', f'rate={RATE}', f'channels={CHANNELS}',
            'channel_map=front-left,front-right', f'sink_properties={sink_properties(label)}']


def our_modules(listing: str, slug: str) -> list[str]:
    """Ids of loaded null-sink modules that are this tablet's sink (`pactl list short modules`)."""
    wanted = f'sink_name={node_name(slug)}'
    ids = []
    for line in listing.splitlines():
        fields = line.split('\t')
        if len(fields) >= 3 and fields[1] == 'module-null-sink' and wanted in fields[2].split():
            ids.append(fields[0])
    return ids


def is_silent(pcm: bytes) -> bool:
    return pcm.count(0) == len(pcm)


def audio_packet(seq: int, pcm: bytes) -> bytes:
    """One length-prefixed packet for the video socket (see android/README.md)."""
    payload = PACKET_AUDIO + struct.pack('!I', seq & 0xffffffff) + pcm
    return struct.pack('!I', len(payload)) + payload


def _pactl(*args: str) -> str | None:
    try:
        done = subprocess.run(['pactl', *args], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return done.stdout.strip() if done.returncode == 0 else None


class TabletSpeaker:
    """The sink's GStreamer pipeline; hands each non-silent chunk to `deliver`.

    `deliver(pcm)` runs on a GStreamer streaming thread, so it must only hand
    the bytes over (host.py posts them to its asyncio loop).
    """

    def __init__(self, Gst, slug: str, label: str, deliver, make_default: bool = False):
        self.Gst = Gst
        self.slug, self.label = slug, label
        self.deliver = deliver
        self.make_default = make_default
        self.previous_default = None
        self.pipeline = None
        self.module = None
        self.chunks = 0
        self.silent_chunks = 0

    def start(self) -> None:
        Gst = self.Gst
        for stale in our_modules(_pactl('list', 'short', 'modules') or '', self.slug):
            _pactl('unload-module', stale)
        self.module = _pactl('load-module', *module_args(self.slug, self.label))
        if not self.module:
            raise RuntimeError('pactl could not create the sink (is pipewire-pulse running?)')
        self.pipeline = Gst.parse_launch(
            'pipewiresrc name=monitor always-copy=true ! '
            f'audio/x-raw,format=S16LE,rate={RATE},channels={CHANNELS},layout=interleaved ! '
            'appsink name=pcm emit-signals=true sync=false max-buffers=16 drop=true')
        source = self.pipeline.get_by_name('monitor')
        source.set_property('target-object', node_name(self.slug))
        properties = Gst.Structure.new_empty('props')
        for key, value in {'stream.capture.sink': 'true', 'node.latency': f'{QUANTUM}/{RATE}',
                           'node.dont-reconnect': 'true', 'node.description': 'tabs9 to the tablet',
                           'media.role': 'Music'}.items():
            properties.set_value(key, value)
        source.set_property('stream-properties', properties)
        self.pipeline.get_by_name('pcm').connect('new-sample', self._sample)
        if self.pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
            self.close()
            raise RuntimeError('the sink could not be recorded')

    def _sample(self, sink):
        sample = sink.emit('pull-sample')
        if sample is None:
            return self.Gst.FlowReturn.OK
        buffer = sample.get_buffer()
        pcm = buffer.extract_dup(0, buffer.get_size())
        if is_silent(pcm):
            self.silent_chunks += 1
        else:
            self.chunks += 1
            self.deliver(pcm)
        return self.Gst.FlowReturn.OK

    def present(self) -> bool:
        """True once the sound server lists the sink."""
        listing = _pactl('list', 'short', 'sinks') or ''
        return any(line.split('\t')[1:2] == [node_name(self.slug)] for line in listing.splitlines())

    def take_default(self) -> bool:
        """Make the sink the default output, remembering the one it replaces."""
        current = _pactl('get-default-sink')
        if current == node_name(self.slug):
            return True
        if _pactl('set-default-sink', node_name(self.slug)) is None:
            return False
        self.previous_default = current
        return True

    def close(self) -> None:
        # Hand the default back while our sink still exists: once it is gone
        # the session manager would pick some other device on its own.
        if self.previous_default and _pactl('get-default-sink') == node_name(self.slug):
            _pactl('set-default-sink', self.previous_default)
        self.previous_default = None
        if self.pipeline is not None:
            self.pipeline.set_state(self.Gst.State.NULL)
            self.pipeline = None
        if self.module:
            _pactl('unload-module', self.module)
            self.module = None

    def stats(self) -> dict:
        return {'audio_chunks': self.chunks, 'audio_silent_chunks': self.silent_chunks}
