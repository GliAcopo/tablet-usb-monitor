# Performance analysis

## 2026-09-13 incident: video dropouts every few minutes (fixed)

**Symptom.** While using the tablet as a desktop, the picture was replaced
by the app's "waiting for the host" screen for about a second, at
irregular intervals of minutes; touch kept working. The app logged
`Stream read timeout, reconnecting` (14:08:55, 14:10:41, 14:10:52,
14:11:02, 14:11:13, 14:12:42, 14:14:08, 14:14:19 in the reported session).

**Root cause, demonstrated.** Every one of those timestamps falls inside a
run of host telemetry windows reporting `capture_fps 0.0` — twelve
consecutive windows at 14:08:03–14:08:58, nine at 14:10:33–14:11:13, and
so on — with `native_dropped 0`, `import_failures 0`, `client_resyncs 0`
and `native_pending 0` throughout. KWin sends a screencast frame only when
something on the output changed; a static desktop produces nothing, the
host had nothing to write, and the client's 10 s socket read deadline
(`soTimeout = 10000`) expired. The client treated that as a dead
transport: it closed the socket, called `onDisconnected`, and the activity
covered the surface with the startup screen until the *first frame* of the
next connection arrived — which, on a still desktop, was whenever the user
next moved something. Not a capture stall, not a transport fault, not a
decoder failure: an idle desktop misread as a broken link. The "every few
minutes" cadence is simply how often nothing changed for 10 s.

Why the earlier soaks never saw it: they ran continuous synthetic motion.
And why an "idle" tablet usually still captured ~15 fps here: KWin rounds
the laptop panel's logical width (2560 / 1.75 = 1462.86 → 1463), so the
virtual output placed at x = 1463 shares one logical pixel with it, and any
repaint near the laptop's right edge (a terminal, a browser) re-renders a
2 px column of the tablet (`TABS9_TRACE_DAMAGE=1` logs the rectangles:
`damage 2x1333+0+40`). The dropouts therefore only appeared when the laptop
was idle too. That overlap is a separate placement issue, left as a
follow-up (a 1 px gap would stop the pointer from crossing).

**Fix.** Video liveness is now separate from frame production:

- The host writes a 5-byte heartbeat packet (type `2`) on the video socket
  once per second whenever it has had nothing else to write — from the same
  single-writer coroutine, never queued behind frames — but only to a client
  that advertised `video_heartbeat` in its `config` message (an older client
  reconnects on an unknown packet type). The host greeting lists
  `video_heartbeat` in `features`. A new video connection also asks the
  encoder for an IDR immediately instead of waiting for the periodic one.
- The client consumes heartbeats without decoding or counting them and keeps
  the 10 s deadline as a *transport* deadline: with a heartbeat-capable host
  its expiry is a real fault and the client reconnects; with a legacy host
  silence keeps the connection and the picture. A deadline expiring inside a
  packet, an impossible length or an unknown type is a framing failure
  (`StreamFramer`, pure Kotlin, JVM-tested).
- Each connection owns its socket and callbacks (`Connection` generation);
  `stop()` closes only its own; a decoder rebuild keeps the socket and
  resumes at the IDR it asks for.
- States `WAITING / STREAMING / RECOVERING / DISCONNECTED`: `STREAMING`
  only after the first *rendered* frame of the connection; `RECOVERING`
  keeps the last picture and shows a small "Reconnecting video…" chip;
  the startup screen returns only if recovery fails for 15 s. While
  recovering, gestures pause and any held contact is lifted at its last
  position; the remainder of that gesture is dropped until its next down.
- Decoder watchdog: frames submitted but nothing rendered for 3 s →
  keyframe request; still nothing → one decoder rebuild; still nothing →
  reconnect with 0.5/1/2/4 s backoff. It cannot fire on an idle desktop
  (nothing is submitted).
- Diagnostics: host telemetry `video_clients`, `video_connects`,
  `video_disconnects`, `last_video_disconnect`, `heartbeats_sent`; host log
  lines per video connection with the reason it ended; app log lines per
  connection (packets, last seq received/rendered, reason) and per state
  transition. `SIGUSR1` to the host drops the video clients; debug APKs
  accept `DRILL_DROP_VIDEO` / `DRILL_RESET_DECODER` broadcasts
  (`-n local.tabs9.usbdisplay/.DrillReceiver`).

Not done on purpose: the timeout was not raised, the connection screen was
not merely hidden, the host service is never restarted for a client fault,
and the user's fps setting is untouched.

**Evidence (all on the same hardware, host `--profile balanced` unless
stated, synthetic motion from `scripts/gpu-motion-test.py`).**

| check | result |
|---|---|
| idle soak, 60 s motion / 70 s no repaint, 14:55:38–15:22:58 (27 min, stopped early on request) | 156 motion windows: **58.6 fps median, min 56.0**, capture→ack p95 **27.4 ms** (worst 28.8), render p95 19.6 ms; **140 s of 0.0 fps windows** covered by heartbeats (1 → 141); **0 disconnects, 0 state transitions, 0 resyncs**; no window with acks behind capture |
| 5× host-side socket drop (`SIGUSR1`), motion running | fault seen +0.19 s, **streaming again +0.72–0.74 s** |
| 5× app-side socket drop (drill) | **+0.77–0.80 s** |
| 5× decoder rebuild (drill) | codec back **+0.28–0.32 s**, one video connection throughout, ack rate stayed 57.6–59.4 fps |
| `--profile balanced --fps 120`, 200 s, 3 socket drops + 2 rebuilds | **110.5 fps median (min 100.6)**, capture→ack p95 22.3 ms, render p95 10.9 ms; drops recovered in 0.53 s; rebuilds 0.06 s |
| touch, motion running | corner and centre taps landed in all 5 zones; a two-finger pinch held through a socket drop: app lifted 2 contacts, **0 rejected messages**, the next pinch delivered (274 updates, 2 points) |
| pixels | the synthetic pattern was on the tablet for every run (rendered acks track capture within 0.4 fps) |
| tests | host: 156 unit tests (`python3 -m unittest discover -s tests`); client: 5 JVM tests run by `scripts/build-android.sh` |

Reproduce: `./tabs9 start --profile balanced`, then
`QT_QPA_PLATFORM=wayland python3 scripts/gpu-motion-test.py --seconds 300 --motion 60 --static 70`
and watch `./tabs9 logs` for `heartbeats_sent` rising during the static
phases with `video_disconnects` unchanged; `kill -USR1 $(systemctl --user
show tab-s9-usb-display.service -p MainPID --value)` for a recovery drill.

**Remaining limits.** Recovery timings were measured with synthetic motion;
on a still desktop a reconnect shows the last picture until the next change
(the encoder cannot produce an IDR without a frame). The one-pixel output
overlap above is unfixed. The decoder watchdog's escalation past the first
rebuild was exercised only in code review, not live (the drills recover at
the first step). Pen contacts are lifted by the same path as touch but were
not exercised with a real pen during a drop.

## 2026-09-13 regression: native capture selected the wrong GPU

The reported unusable `./tabs9 start --profile balanced` session did not use
the native path measured below. The helper hardcoded `/dev/dri/renderD128`.
On this boot that node belongs to NVIDIA (PCI vendor `0x10de`); Intel
(`0x8086`) is `/dev/dri/renderD129`. Startup attempted `nvidia_drv_video.so`,
failed `vaInitialize`, and fell back to the all-GStreamer `va` path. The
fallback message incorrectly called this system-memory capture, while CLI
status said only "streaming". Thus a successful launch did not establish
that the optimized path was active.

The host now passes the VA encoder's device path explicitly to the helper,
validates its Intel identity through sysfs, and never assumes a numbered GPU.
The standalone helper requires `--render-node`. Failed helper startup cleans
up its process/socket, and a missing portal during fallback produces failed
status rather than an uncaught callback exception. CLI status and telemetry
identify the actual capture route; fallback produces a visible warning.
Benchmarks identify actual paths rather than matching one warning string.

The initial post-fix 100-second synthetic-motion session negotiated Intel
iHD, Tile4 NV12, and native 2960x1848 at a 60 Hz target. Inspected steady-state
windows delivered approximately 56–57 fps, capture-to-ack p95 around 30 ms,
and no >100 ms stalls. The synthetic image was visually verified on the
tablet, and a tablet-side test tap produced one center-region touch in the
host test window. These results establish recovery of the native path and
responsive delivery; they are not a claim of a locked 60 fps or a new
30-minute soak. Earlier benchmark results below describe a different boot
and must not be treated as a universal guarantee.

Two further fresh starts, each with ten seconds of warmup and thirty seconds
of recorded synthetic motion, confirmed the fix:

| measured result | run 1 | run 2 |
|---|---:|---:|
| actual capture path | native | native |
| captured / encoded / acknowledged fps (median window) | 56.8 | 56.6 |
| capture-to-ack p95 (worst window) | 31.1 ms | 30.9 ms |
| render interval p95 (worst window) | 22.6 ms | 21.8 ms |
| capture or render stalls >100 ms | 0 | 0 |
| fallback | no | no |

Reproduce with `./tabs9 stop` followed by
`./tabs9 bench-capture --seconds 30 --runs 2 --modes native --json .local/bench/gpu-selection-fix-2026-09-13.json -- --profile balanced`.
This benchmark stops the host afterward; restore normal use with
`./tabs9 start --profile balanced`. Results are saved in the named JSON file.

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
commits f2218fb/29c5b4b (slot return and arrival pairing moved to the
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
for 3 s under load (`/proc/<pid>/status` read `T (stopped)` while held) →
the window covering the freeze shows capture→ack max 3154 ms, one render
stall, `client_resyncs` 0 → 6 (host queue full: GOP abandoned, IDR
requested) and the app logged "Compressed backlog over budget; resuming at
the next keyframe (total 1)"; the next window is back at 56.8 fps acked,
capture→ack p95 31.9 ms, `native_pending` 0, and the resync counter stays
at 6. Both resync paths — host and app — have now fired once and recovered. Protocol negotiation was exercised on both sides: the app logs
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
