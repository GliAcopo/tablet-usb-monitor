# Performance analysis

## Settled (2026-09-12, evening): the 30 fps state, and the native consumer

Everything below in this section was measured live on this machine with
`scripts/gpu-motion-test.py` (OpenGL, native Wayland, one repaint per frame
callback) on the virtual output at 2960×1848, balanced profile (HEVC, 30 Mbit/s),
via `./tabs9 bench-capture`, unless stated otherwise. Nothing private was
captured; the only tablet screenshots are of the synthetic pattern.

### The ladder

Same motion, 20 s windows, `TABS9_DEBUG_TAIL` replacing the pipeline after
`pipewiresrc`:

| path | capture fps | interval p90 / max |
|------|------------:|-------------------:|
| DMA-BUF capture → discard | 58.7 / 59.4 | 16.7 / 33.4 ms |
| capture → `vapostproc` (NV12, VAMemory) → discard | 59.3 / 59.2 | 16.7 / 33.4 ms |
| capture → conversion → `vah265enc` → discard | 59.3 / 59.2 | 16.7 / 33.4 ms |
| full pipeline + tablet, first bench | 33.6 / 50.8 / 33.2 | 50 / 25 / 66.7 ms |

No stage is slow. The full pipeline is bistable: an instance either runs at
~59 fps for its whole life or at 30 fps for its whole life.

### The encoder held every frame for two frame periods (fixed)

Per-stage pad probes (`TABS9_STAGE_PROBES=1`, paired by PTS) put the encoder's
sink→src dwell at 33.8 ms p50 at 60 Hz, 67.6 ms at 30 Hz and 17 ms at 120 Hz
— exactly two frame periods, independent of GPU time (the same encoder does
310 fps on synthetic frames). `GstVaBaseEnc` asks upstream for liveness once,
in `set_format`; pipewiresrc 1.6 answers from a field it fills only once the
stream is STREAMING, and the caps event arrives during negotiation, so the
encoder took its non-live path (`preferred_output_delay = 4`) and polled
readiness of the reconstruct surface that frame N+1 was still reading as a
reference. The host now answers that latency query as live with a pad probe
(edited through the raw pointer: a PyGObject wrapper would make the query
read-only). Encoder dwell: **6.0 ms p50 / 6.1 ms p90**.

### The 30 fps state is a buffer-return ratchet (fixed by the native consumer)

`TABS9_TRACE_CAPTURE=1` prints every capture interval, KWin's own PTS
interval and KWin's sequence delta. In the slow state:

```
arrival/pts/seq: 16.7/16.7/1 16.7/16.7/1 16.7/16.7/1 66.6/66.7/1 16.7/16.7/1 ...
```

Four frames at the refresh period, then a 66–83 ms gap, forever; KWin's own
timestamps show the same gaps and its sequence numbers are contiguous, so
KWin is *not producing* during the gap. With `TABS9_PW_BUFFERS=3` the burst
is three frames, with 2 it is two frames: the burst length is the number of
PipeWire buffers.

Mechanism (pipewire 1.6.2, `src/pipewire/stream.c`, `impl_node_process_input`):
an input stream recycles **at most one buffer per graph cycle**, by writing a
single id into `io->buffer_id` *after* the consumer's process callback has
returned, and KWin only runs a cycle when it has a free buffer to render
into. GStreamer's `pipewiresrc` hands buffers to a streaming thread and
releases them later, so every late release (start-up while the pipeline
warms, any later hiccup) moves one of KWin's 2–4 buffers to the consumer for
good. Once KWin holds none it records N frames whenever the batch trickles
back, then waits again: a stable half-rate state. The bench harness, which
starts motion the instant capture starts, reproduced it 5/5 times.

`native/tabs9-capture` (C, libpipewire + libva, ~450 lines, built against
headers unpacked under `.local/sysroot` by `scripts/setup-native.sh`)
consumes the stream with `PW_STREAM_FLAG_RT_PROCESS`, imports each DMA-BUF
once per `pw_buffer`, converts it on the GPU (VA VPP, sRGB → BT.709 limited
NV12) into a ring of owned surfaces and **queues the buffer back before the
process callback returns**, so the same cycle recycles it and KWin never runs
dry. A completion thread polls the VPP and only then hands the slot to the
host over a unix socket (the ring is exported once as DMA-BUF fds). The host
wraps each slot in a `GstMemory` and runs `appsrc ! vapostproc ! vah265enc !
h265parse ! appsink`; a slot is released after the encoder finished the frame
that followed it, so ring accounting is exact and the helper drops (and
counts) only when the ring is full. Nothing in the callback waits for the GPU
or the network. Colours on the synthetic pattern match the GStreamer path to
within one code value; bar edges are aligned across rows (no tearing seen).

### Results (3 fresh runs each, 10 s warm-up + 60 s recorded, native path)

| mode | unique fps (capture = encoded = acked) | capture interval p95 / max | capture→ack p50 / p95 | tablet render interval p95 | stalls > 100 ms | host CPU |
|------|----------------------------------------|---------------------------:|----------------------:|---------------------------:|----------------:|---------:|
| 60 Hz output, 60 fps | **59.3 / 59.1 / 59.8** | 18.0–19.4 / 19.6–62.5 ms | 22.3–22.6 / 27.2–28.4 ms | 19.9–22.0 ms | 0 | ~9 % of a core |
| 90 Hz output, 90 fps | 82.1 / 81.9 / 82.3 | 13.8–14.5 / 17.5–19.4 ms | 20.6–20.8 / 24.9–25.8 ms | 15.2–15.8 ms | 0 | ~11 % |
| 120 Hz output, 120 fps | 112.5 / 110.6 / 111.1 | 10.7–11.0 / 14.0–15.6 ms | 18.4–18.9 / 23.1–23.6 ms | 11.7 ms | 0 | ~14.6 % |

Definitions: "capture→ack" is a host-side software measurement from the
capture probe to the tablet's rendered acknowledgement (return path
included; not pixel latency). "Tablet render interval" is computed from the
tablet's own `OnFrameRendered` timestamps, in the tablet's clock domain.
Touch rejections during all runs: 0.

Against the acceptance table: **60 fps passes every usable-mode gate**
(≥ 58 fps, render p95 ≤ 25 ms, capture→ack p95 ≤ 60 ms, no stalls, zero
rejections). 120 fps passes the latency and stall gates (p95 11.7 ms ≤ 16.7,
23.6 ms ≤ 40) but records 110–113 unique frames, not ≥ 115.

### 30-minute soak, transport headroom, lifecycle (2026-09-12, night)

Soak: `./tabs9 bench-capture --seconds 1800 --runs 1 --modes native --
--profile balanced` with the motion pattern on the virtual output and an
`adb shell input` tap or swipe on the tablet every 15 s
(`.local/bench/soak-60fps-1800s.json`, per-window telemetry in
`soak-60fps-windows.jsonl`). Steady state (361 five-second windows, 30.1
min, after the client connected):

| unique fps mean / median / min / max | capture→ack p95 median / worst window | render interval p95 median / worst | stalls > 100 ms | input messages / rejected | helper drops / KWin seq gaps |
|---|---|---|---|---|---|
| **58.2 / 58.2 / 56.4 / 59.8** | 27.2 / 33.4 ms | 20.2 / 23.9 ms | 0 (capture, render, ack) | 2868 / 0 | 1 / 1 |

Every usable-mode gate holds for the whole half hour. The worst three-minute
segment (56.4 fps, 33.4 ms) coincides with a Gradle build of the APK on the
same laptop (minute 18–19): the soak was not run on an idle machine. The
fps sits ~1 fps under the 60 s runs because the periodic ADB taps land on
the pattern window and cost a repaint or two per second; the totals confirm
it (105 208 encoded frames, 105 207 acked). The host under test predates
commits 0c05b1a/062c7c0 (slot return and arrival pairing moved to the
appsink pad probe); those were re-checked with a 60 s run on HEAD and the
rebuilt APK: 58.9 fps, capture→ack p95 28.6 ms, render p95 19.6 ms,
`native_pending` 0.

Transport headroom: the same 60 s bench with `--bitrate 60000` (twice the
profile) received 58–59 Mbit/s on the tablet at 58.8 unique fps,
capture→ack p95 **21.6 ms**, render p95 19.6 ms, 0 stalls, 11 % of a core.
ADB forwarding over the 5 Gbit/s link has at least 2× headroom, so no
transport replacement is warranted (plan phase 4, criterion satisfied).

Lifecycle, HEAD host + protocol-2 APK, motion running (log in
`.local/bench/lifecycle-2026-09-12.log`): HOME then relaunch → 58.7 fps
acked again within 8 s; host `stop` + `start` while the app stays up →
reconnects, 58.8 fps, `native_pending` bounded; app frozen with `SIGSTOP`
for 1.5 s and 3 s under load → picture and acks recover, no stall counters
left behind. Protocol negotiation was exercised on both sides: the app logs
"Host speaks protocol 3, this client 2; using the common subset" against a
host with `PROTOCOL` bumped, and the host logs the legacy line against the
previous APK. Not covered tonight: the overload run was meant to hold a
two-finger contact during the freeze, but an earlier step in the same script
put the tablet to sleep (`KEYCODE_SLEEP`) and the lock screen has a
password, so touches from then on went to the keyguard, not the app — the
input counters read 0 for those steps. Re-run
`scripts/mt-inject` + freeze once the tablet is unlocked; never send
`KEYCODE_SLEEP` to it from a script.

### The remaining limit at 120 Hz is KWin's recording, not the pipeline

At 120 Hz the source swaps at 120.0 fps, the helper dropped 7 of ~2400
frames (ring full, counted) and KWin's sequence numbers show no other gaps,
yet KWin recorded only ~113 frames per second. The trivial consumer —
`pipewiresrc → fakesink`, no conversion, no encoding, no tablet — records
the same 111 fps from the same source. So ~6 % of vblanks are frames KWin
does not record on this virtual output at 120 Hz; nothing downstream of
KWin can recover them. (See the KWin sources and bug cited under "Current
result" below for the whole-millisecond rate limiter.)

Other measured facts from this pass:

- Through XWayland the pattern paints at 60 fps but KWin records ~56 unique
  frames per second (3 × 60 s: 56.4, 56.4, 56.3); on native Wayland 59.3,
  59.1, 59.8. Benchmarks now force `QT_QPA_PLATFORM=wayland`.
- GPU clocks are not the discriminator: gt0 sat at 0/800 MHz with 40–80 % RC6
  residency in both the 59 fps and the 30 fps runs.
- `pipewiresrc keepalive-time` keeps a copy of the last buffer (via a parent
  meta) and therefore pins one of KWin's buffers; disabling it did not change
  the ratchet.
- Tablet decoder: `c2.qti.hevc.decoder`; `FEATURE_LowLatency` is not declared
  so `KEY_LOW_LATENCY` is not requested; the Qualcomm vendor hint is not
  applied by the codec (checked in the applied input format). On-device
  arrival → release is 12–13 ms, release → on screen ~0.6 ms.

## Current result

Native 2960x1848 at 60 fps is the reliability milestone. The existing live
records do not yet demonstrate that it is repeatable: earlier runs ranged from
about 30 to 110 fps under motion, while the latest roughly 1 fps samples were an
idle screen plus the one-second keepalive and are not a throughput measurement.
Keep 90 and 120 fps selectable, but do not describe either as stable until three
fresh OpenGL-motion runs reproduce it without the 75–100 ms stalls below.

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

## Pipeline direction

The later live measurements supersede the early Intel/NVIDIA recommendation in
this document. The default remains the same-Intel-GPU VA path because it was the
only path to reach roughly 110 fps in a live run; the system and cross-GPU GL
paths remain diagnostic. The result is still bistable, so this is a best-known
candidate rather than proof of a stable 120 fps profile.

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

The gap is not known to be small or distributed. Later measurements found a
repeatable slow signature: four quick frames followed by a 75–100 ms stall.

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

The next bounded experiment is to separate capture, VPP and encoding with
compatible DMA-BUF caps, then test an owned surface pool that returns the source
after the actual conversion fence completes. A complete native consumer is only
justified if that experiment shows that ownership changes the source cadence.
Any native process callback must stay non-blocking; it cannot wait for VPP,
encoding, or socket writes.

### Instrumentation repaired on 2026-09-12

Capture wall-clock and PTS interval samples are now cleared after each
five-second telemetry report. Previously the 1200-entry retained deque mixed
idle keepalive samples with later motion and made p90/max unsuitable for a
single benchmark run. `bench-capture` now uses the OpenGL motion source, performs
three separate runs per candidate by default, and reports capture/encoded/tablet
rates, capture interval p50/p90/max, input rejection deltas, and host CPU as a
percentage of one core. GPU load remains an external observation when available.

No fresh post-change hardware run is recorded here yet because applying the
touch fix requires a coordinated service restart and KDE consent. The first
candidate command is:

```sh
./tabs9 start --resolution 2960x1848 --fps 60 --bitrate 30000 --capture-memory va
```
