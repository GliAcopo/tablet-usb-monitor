# Performance analysis

## Result

The observed ceiling is about 60 frames/s, but neither NVENC nor the Python/TCP
transport has a demonstrated 60 frames/s limit.  The next run should measure the
cadence at three boundaries: PipeWire input, encoded output, and tablet rendered
acknowledgement.  That single comparison identifies the responsible stage without
recording screen contents.

The leading host-side hypothesis is KWin's screencast scheduling plus the cost of
rendering a 2960x1848 frame.  KWin 6.6.6 emits an output screencast frame when its
screencast layer schedules a repaint.  It then rate-limits from the time the
previous frame **finished** and uses whole milliseconds.  At 120 Hz that interval
is truncated to 8 ms; the synchronous scene-render/readback time is additional.
At this resolution, enough render time or timer drift can turn an 8.33 ms target
into roughly a 16 ms cadence.  This is a hypothesis until the input timestamps
are captured.  A recent upstream report describes the same whole-millisecond,
previous-completion scheduling problem on a 120 Hz KWin screencast.

Sources:

- [KWin 6.6.6: output capture is driven by `repaintScheduled`](https://invent.kde.org/plasma/kwin/-/blob/v6.6.6/src/plugins/screencast/outputscreencastsource.cpp#L103-106)
- [KWin 6.6.6: rate limit and record path](https://invent.kde.org/plasma/kwin/-/blob/v6.6.6/src/plugins/screencast/screencaststream.cpp#L483-665)
- [KWin 6.6.6: advertised maximum comes from output refresh](https://invent.kde.org/plasma/kwin/-/blob/v6.6.6/src/plugins/screencast/screencaststream.cpp#L742-762)
- [KDE Bug 524129: fractional timing drift at 120 Hz](https://bugs.kde.org/show_bug.cgi?id=524129)

## Measurements

The live measurements below were taken with the correct existing virtual output
selected, configured as 2960x1848 at 120 Hz and scale 1.5.  PipeWire advertised
2960x1848, variable frame rate (`0/1`), and `maxFramerate=120/1`.

| Boundary or test | Result | Interpretation |
|---|---:|---|
| Live host encoded output | about 60 fps | Confirms the deficit exists by encoded output |
| Live tablet rendered acks | about 60 fps | Tracks the encoded producer; does not yet isolate Android |
| Qt 8 ms motion test | 52-60 fps | Continuous motion still did not produce 120 encoded fps |
| Live RTX 4050 | about 54 C, 11 W, 16% encoder, 23% GPU | No sign of saturated NVENC hardware |
| Synthetic SystemMemory BGRx to NVENC, 240 frames | 1.60 s, about 150 fps | Exact raw format clears 120 fps after warm-up, with limited 1.25x headroom |
| Synthetic SystemMemory NV12 to NVENC, 240 frames | 0.99 s, about 242 fps | Format and raw byte volume materially affect throughput |
| Synthetic GLMemory RGBA to NVENC on default Intel EGL, 120 frames | 0.80 s, about 150 fps | It runs, but debug logs prove NVENC falls back to a system copy |
| Synthetic GLMemory RGBA with NVIDIA EGL offload, 240 frames | 1.21 s, about 198 fps | Native NVIDIA GL/NVENC interop works in isolation |
| Intel VA HEVC, 240 frames | 26.81 s, about 9 fps | Intel VA should not be the default on this machine |

One cold BGRx run took 2.95 s for 120 frames.  The 240-frame warm run above is
the better sustained-throughput measurement; the cold result warns that short
benchmarks need warm-up before comparison.

The uncompressed BGRx path moves 21,880,320 bytes per frame, or 2.626 GB/s at
120 fps, before encoding.  The current caps filter is ordinary `video/x-raw`,
which denotes system memory.  Locally, `nvh265enc` advertises SystemMemory,
CUDAMemory, and GLMemory inputs, while KWin and PipeWire can negotiate DMA-BUF
when both sides support it.  The present route can therefore require a KWin GPU
readback plus a host-to-NVIDIA upload even though NVENC itself is underused.

- [GStreamer caps features and SystemMemory](https://gstreamer.freedesktop.org/documentation/additional/design/capsfeatures.html)
- [PipeWire DMA-BUF negotiation](https://docs.pipewire.org/devel/page_dma_buf.html)
- [GStreamer `nvh265enc` input memory types](https://gstreamer.freedesktop.org/documentation/nvcodec/nvh265enc.html)

The synthetic transport benchmark models 60 Mbit/s at 120 fps, whose mean
encoded access unit is 62,500 bytes.  On this machine it measured:

| Operation | Result |
|---|---:|
| Synthetic `extract_dup`-like copy | median 1.09 us, p95 1.13 us |
| Python packet framing | median 2.16 us, p95 2.25 us |
| GLib-thread to asyncio-thread handoff | median 5.85 us, p95 8.74 us |
| Ephemeral loopback TCP | 14.9 Gbit/s, about 248x required payload rate |

These figures exclude ADB, USB, Android decode, and display.  They show that the
Python copy, framing, thread handoff, and loopback leg are too small to explain a
60 fps ceiling.  Encoded access units are bursty, but the measured transport
margin is large enough that burst shape is unlikely to reverse this conclusion.
Reproduce the test with:

```bash
scripts/benchmark_transport.py --bitrate-kbps 60000 --fps 120 --frames 240
```

The benchmark uses only generated zero bytes and an ephemeral local port.  It
does not connect to the host service, ADB, or the tablet.

## Instrumentation for the next run

Aggregate these fields over five-second windows and print only timing metadata:

1. On the `pipewiresrc` source pad, count buffers and collect wall-arrival and PTS
   deltas.  Report fps plus p50, p95, and maximum delta.
2. At the encoder output, collect the same values.
3. Keep the existing tablet ack fps and encode-to-render latency, and add p95.
4. Log the negotiated input caps including caps features, and the first buffer's
   memory type.  Read the appsink `in`, `out`, and `dropped` counters.

The pad probes should only do counter increments and timestamp collection.  They
must not map buffers or inspect payload bytes.

| Observation | Conclusion | Next experiment |
|---|---|---|
| PipeWire input is about 60 fps | KWin/source cadence is the limit | Measure at 1920x1200 and 2960x1848; compare render cost and inspect KWin timing fix |
| PipeWire input is near 120, encoder output about 60 | Raw memory/encode path is the limit | Try DMA-BUF to `glupload` to GLMemory to NVENC, with automatic SystemMemory fallback |
| Encoded output is near 120, tablet ack about 60 | Android decode/render pacing is the limit | Validate MediaCodec output cadence, display mode, and operating rate |
| All counters approach 120 but motion looks 60 | Test content or display presentation is the limit | Use a precise timer and record paint/presentation counts |

The resolution A/B test is diagnostic, not a proposed permanent downgrade.  If
input cadence rises substantially at the lower size, KWin's capture render or
readback cost is confirmed.

## Optimized pipeline direction

Keep NVIDIA HEVC P1, ultra-low-latency tuning, zero B-frames, leaky queues, and
the current bounded client queue.  Those choices match the latency goal and the
measurements do not justify switching to Intel VA.

If the probes show that SystemMemory is active and input falls below 120, test
this memory route behind a capability check:

```text
KWin DMA-BUF -> pipewiresrc -> glupload -> GLMemory BGRx -> nvh265enc
```

The hybrid-GPU detail matters.  With the normal environment, GStreamer's EGL
context is Mesa Intel Arc.  `nvh265enc` logs `CUDA_ERROR_INVALID_GRAPHICS_CONTEXT`
and `Not a CUDA buffer, system copy`; GLMemory alone therefore does not establish
zero-copy.  With `__NV_PRIME_RENDER_OFFLOAD=1`, EGL is owned by the RTX 4050 and
the same debug run has neither fallback message.  The isolated 240-frame test
then sustained about 198 fps including process startup.  NVIDIA documents that
this variable selects the NVIDIA GPU for EGL; the GLX vendor variable is only
needed for GLX.

- [NVIDIA PRIME render offload documentation](https://download.nvidia.com/XFree86/Linux-x86_64/570.169/README/primerenderoffload.html)

The focused live candidate is therefore:

```bash
env __NV_PRIME_RENDER_OFFLOAD=1 \
    GST_GL_PLATFORM=egl \
    GST_GL_WINDOW=surfaceless \
    python3 src/host.py --capture-memory gl
```

This can remove the full-frame CPU readback/upload path only if NVIDIA EGL can
import the DMA-BUF and modifier produced by the Intel KWin compositor.  The
installed NVIDIA EGL reports DMA-BUF import and modifier extensions, but that is
capability evidence rather than proof that this exact cross-GPU negotiation will
succeed.  The live test must verify `memory:DMABuf` before `glupload`, GLMemory
after it, absence of NVENC's `system copy` fallback, and actual capture cadence.
It must automatically return to the current SystemMemory pipeline if negotiation
or startup fails.  `__GLX_VENDOR_LIBRARY_NAME=nvidia` is unnecessary for this EGL
route and should not be added unless the pipeline changes to GLX.

A custom C or Rust transport helper is not warranted by current evidence.  The
measured Python and TCP work has orders of magnitude of headroom.  Native code
would become relevant only if a prototype must directly import a negotiated
DMA-BUF into CUDA/NVENC because the GStreamer GL route cannot sustain 120 fps.
That decision should follow measured memory type, capture cadence, and GL-route
throughput rather than language preference.

Do not insert `videorate` merely to report 120 fps: duplicated frames increase
the counter and bitrate without improving motion.  Preserve the variable-rate
source and solve the stage that is producing unique frames at 60 Hz.

## Live result: the cross-GPU DMA-BUF import works

The open question above — whether NVIDIA EGL can import the DMA-BUF and modifier
that the Intel-composited KWin session exports — is now answered on this
machine.  A live `--capture-memory gl` run negotiated:

```text
video/x-raw(memory:DMABuf), drm-format=AR24, format=DMA_DRM,
interlace-mode=progressive, width=2960, height=1848,
framerate=0/1, max-framerate=120/1
```

and encoded at the native `(2960, 1848)`.  The automatic SystemMemory fallback
in `Host.fallback_or_stop` was never triggered, so `glupload` accepted the
imported buffer and `nvh265enc` accepted GLMemory.  The run sustained 5 min 28 s
and consumed 19.7 s of host CPU time (about 6 % of one core), which is
consistent with no full-frame CPU readback.

This closes the "capability evidence rather than proof" caveat: the route
`KWin DMA-BUF -> pipewiresrc -> glupload -> GLMemory -> nvh265enc` is live on
an Intel-composited session with an RTX 4050 encoder.

### Settled: the frame-rate comparison (live, 2026-09-12)

Measured with the host streaming and `scripts/gpu-motion-test.py` (OpenGL,
repaints on every frame callback) on the virtual output, 20 s windows, same
content. `swap_fps` is what the compositor presented; the host columns are
its 5 s telemetry; `pts p50` is the spacing of KWin's own frame timestamps.

| mode | compositor swap fps | KWin pts p50 | capture / encoded / tablet ack fps | encode→render p50 | notes |
|------|--------------------:|-------------:|-----------------------------------:|------------------:|-------|
| `system` (readback → NVENC) | 80–87 | 25 ms | 35 / 35 / 35 | 15.6 ms | KWin's synchronous `glReadPixels` per frame is the ceiling and slows the compositor itself |
| `gl` (DMA-BUF → NVIDIA EGL → NVENC) | — | 66.7 ms | 13.4 / 13.4 / 13.4 | 16.2 ms | cross-GPU import stalls; CPU low because it is waiting |
| `va`, 3 buffers, queue of 2 | 119.5 | 8.33 ms | 57 / 57 / 57 | 12.3 ms | KWin produces 120; consumer holds too many of the 3 buffers |
| `va`, 4 buffers, queue of 1, conversion‖encode | 117–120 | 8.33 ms | **110 / 110 / 110** | 11.2 ms | default now; NVIDIA idle, host 20–40 % of a core |

Isolated Intel numbers on this Core Ultra 7 155H (Arc iGPU), 2960×1848,
300 frames including `videotestsrc` cost: `vapostproc` BGRA→NV12 2.45 s,
`vapostproc → vah265enc` 4.51 s (target-usage 4 and 7 identical) — the
encoder is roughly 7 ms/frame, conversion 3–4 ms, which is why overlapping
them mattered. The earlier "9 fps" Intel VA figure in this file measured a
software-fed path and is not representative of the hardware.

Take-aways:

- The encoder was never the limit; moving 22 MB frames off the Intel GPU was.
  Encoding where the frame already lives beats a faster encoder elsewhere.
- KWin's screencast offers `SPA_PARAM_BUFFERS_buffers` in the range 2–4. Any
  element that parks a buffer costs a frame at 8 ms cadence; negotiate 4 and
  keep the leaky queue at 1.
- The raster Qt motion test (`motion-test.py`) repaints at ~30–40 fps at
  this size and cannot exercise 120; use the OpenGL source for cadence.

The remaining ~10 fps gap to 120 has not been chased; the tablet decoder
already reports 111 fps and the compositor 117–120, so it is a small,
distributed cost rather than one stall.

### Not settled: the 40 fps mode (investigation of 2026-09-12 afternoon)

The 110 fps result above is not stable. Across many restarts with the same
configuration the capture rate sits at either ~110 or ~30–60 fps, while the
compositor presents the virtual output at 117–120 in every run. Measurements
taken to locate the cause (all with the OpenGL motion source):

- Signature in the slow mode: bursts of 4 frames 8.33 ms apart, then a stall
  of ~75–100 ms (`capture_interval_ms_p90` ≈ 75). KWin offers exactly 4
  buffers; the stall is KWin waiting for them to come back.
- With `GST_DEBUG=pipewiresrc:6` each KWin buffer was held by the chain for
  ~113 ms p50 (got → recycle) — far longer than the GPU work on it.
- Per-stage pad probes (`TABS9_STAGE_PROBES=1`): converter submit 3.5 ms;
  the encoder stage waits ~70 ms per frame — that wait is the GPU completing
  the conversion + encode of that frame, not queueing (the pipeline reports
  `live=True`, so the VA encoder runs synchronously, depth 1).
- Isolated GPU costs at 2960×1848 (`gst-launch`, tiny source upscaled on the
  GPU so the source is free): HEVC encode ≈ 3 ms/frame, unchanged while KWin
  composites 120 fps and the host streams; RGB→NV12 via the scaler path
  2.4 ms; writing a full-size RGB surface 24 ms (EU kernels); full-size
  BGRA→NV12 ≈ 6–7 ms by subtraction. CQP vs CBR, H.264, AV1, 2 slices,
  target-usage: no meaningful difference. AV1 is slower (7.7 ms).
- Ruled out: rate control (CQP identical), explicit sync (the plain
  `datas:1` buffer layout was negotiated), GPU clock floor (raising
  `rps_min_freq` of the render GT to 1500 MHz changed nothing), other GPU
  clients (idle), the encoder's output delay (live → 0), the KWin pin script.
- Not testable through GStreamer: a linear modifier (`vapostproc` imports
  AR24 only as Tile4 `0x0100000000000009`), `always-copy` and
  `use-bufferpool=false` (both break DMA-BUF negotiation and fall back to
  the readback path).

What remains consistent with everything: inside the live pipeline the GPU
takes ~60–70 ms to finish the conversion+encode of a frame whose source is
KWin's imported Tile4 DMA-BUF, whereas the same operations on VA-allocated
surfaces take under 10 ms. That points at the DMA-BUF import path in the
iHD driver (a detiling or synchronisation slow path for foreign Tile4
buffers) rather than at any GStreamer setting, and the bistability comes
from KWin's 4-buffer limit: once the chain falls 4 frames behind, KWin
stops producing until buffers return.

The way to settle it, and the only remaining "low-level" optimisation, is a
consumer that does not go through GStreamer: libpipewire + libva in C,
importing each DMA-BUF once (cached per fd), submitting VPP into a ring of
NV12 surfaces, syncing that VPP and returning KWin's buffer *inside the
PipeWire process callback*, and encoding asynchronously. That gives
deterministic buffer return, a direct measurement of the import cost, and
removes Python from the data path. It is an estimated 600–900 lines of C.
