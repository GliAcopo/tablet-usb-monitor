"""A light picture for E-ink: every frame's brightness turned upside down.

`--light-picture on` makes a dark desktop read as a light page on the tablet
without touching the desktop itself: black backgrounds become white, white
text becomes black, and colours keep their hue (only the luma plane is
inverted; the chroma planes pass through untouched, so a green accent stays
green -- lighter or darker). On a monochrome E-ink panel, which only shows
luma, this is exactly a light theme for whatever the desktop shows.

Where it happens: between the converter and the encoder, on the NV12 frame
GStreamer's VA postprocessor hands out in system memory. The VA plugin marks
that memory read-only, so the frame cannot be changed in place; instead an
appsink takes it, one `bytes.translate` over the Y plane produces the
inverted copy (about 2 ms at 1872x1404, 6 ms at 2960x1848) and an appsrc
feeds it to the encoder's own VA upload. Frames stay in order and none is
dropped here (the appsink blocks instead), which keeps the native ring
accounting in host.py exact.
"""
from __future__ import annotations

import collections
import time

# Limited-range BT.709 luma: 16 is black, 235 white. Everything in between is
# mirrored; the few code values outside the range are clamped, not wrapped.
LUMA_TABLE = bytes(max(16, min(235, 251 - value)) for value in range(256))


def invert_luma(frame: bytes, width: int, height: int, stride: int = 0, offset: int = 0) -> bytes:
    """`frame` (NV12) with its Y plane inverted; the chroma plane copied as is."""
    stride = stride or width
    y_end = offset + stride * height
    return frame[:offset] + frame[offset:y_end].translate(LUMA_TABLE) + frame[y_end:]


class LightPicture:
    """The appsink/appsrc pair and the callback between them (see module doc)."""

    def __init__(self, width: int, height: int, fps: int):
        self.width, self.height, self.fps = width, height, fps
        self.frames = 0
        self.errors = 0
        self.times_ms = collections.deque(maxlen=600)
        self.source = None

    def fragment(self) -> str:
        """The pipeline description between the converter and the encoder's upload.

        The converter before this fragment must end in `vapostproc`; the
        fragment ends the first chain in an appsink and starts the second
        with an appsrc that leads into a fresh `vapostproc`."""
        return ('! video/x-raw,format=NV12,colorimetry=bt709 '
                '! appsink name=picture_in emit-signals=true sync=false async=false max-buffers=2 drop=false '
                'appsrc name=picture_out is-live=true format=time block=true max-buffers=2 '
                f'caps="video/x-raw,format=NV12,width={self.width},height={self.height},'
                f'framerate=0/1,max-framerate={self.fps}/1,colorimetry=bt709" '
                '! vapostproc ')

    def attach(self, pipeline) -> None:
        self.source = pipeline.get_by_name('picture_out')
        pipeline.get_by_name('picture_in').connect('new-sample', self.on_sample)

    def on_sample(self, sink):
        from gi.repository import Gst, GstVideo
        sample = sink.emit('pull-sample')
        buffer = sample.get_buffer() if sample is not None else None
        if buffer is None or self.source is None:
            return Gst.FlowReturn.OK
        started = time.monotonic()
        ok, info = buffer.map(Gst.MapFlags.READ)
        if not ok:
            self.errors += 1
            return Gst.FlowReturn.OK
        try:
            frame = bytes(info.data)
        finally:
            buffer.unmap(info)
        meta = GstVideo.buffer_get_video_meta(buffer)
        if meta is not None:
            inverted = invert_luma(frame, meta.width, meta.height, meta.stride[0], meta.offset[0])
        else:
            inverted = invert_luma(frame, self.width, self.height)
        out = Gst.Buffer.new_wrapped(inverted)
        # Same timestamps and sequence number: the host's probes pair capture
        # and encoder frames by pts and read the KWin sequence from offset.
        out.pts, out.dts, out.duration = buffer.pts, buffer.dts, buffer.duration
        out.offset, out.offset_end = buffer.offset, buffer.offset_end
        if meta is not None:
            GstVideo.buffer_add_video_meta_full(out, meta.flags, meta.format, meta.width, meta.height,
                                                meta.n_planes, meta.offset, meta.stride)
        self.times_ms.append((time.monotonic() - started) * 1000)
        result = self.source.emit('push-buffer', out)
        if result != Gst.FlowReturn.OK:
            self.errors += 1
            return result
        self.frames += 1
        return Gst.FlowReturn.OK

    def stats(self) -> dict:
        times = sorted(self.times_ms)
        return {'light_picture_frames': self.frames, 'light_picture_errors': self.errors,
                'light_picture_ms_p50': round(times[len(times) // 2], 2) if times else None,
                'light_picture_ms_max': round(times[-1], 2) if times else None}
