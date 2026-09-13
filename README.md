# Tablet USB monitor

Use an Android tablet as a **real extended monitor** for a Linux laptop over a
USB cable. The host creates a KDE virtual output, captures it through PipeWire
as a DMA-BUF, converts and compresses it on the same Intel GPU with VA-API HEVC
(zero copies, no readback) and sends it through authenticated loopback sockets
forwarded by ADB. Touch and pen come back through KDE's RemoteDesktop portal
and are delivered to the desktop with libei. No root, no kernel module, no
Wi-Fi, no system-wide installation: everything the tools download lives under
`.local/` in this directory.

**Tested hardware — the only combination that has ever run this:**
a **Samsung Galaxy Tab S9 Ultra** (2960 × 1848, 120 Hz) connected to a laptop
running **Ubuntu 26.04, KDE Plasma 6.6.6 on Wayland, Intel Core Ultra 7 155H
with its Arc iGPU (i915)**; an NVIDIA RTX 4050 is present but idle in the
recommended mode. Every number in this file and in
[docs/performance.md](docs/performance.md) comes from that pair.

## Requirements

Read this before anything else; the project is hardware-specific.

**Host**

- Linux with **KDE Plasma 6.6 on Wayland**: the host uses
  `xdg-desktop-portal-kde` (virtual output creation, screen capture,
  RemoteDesktop), `kscreen-doctor` (output placement and modes) and KWin's
  EIS backend for input. GNOME, Sway, Hyprland and X11 sessions are **not
  supported**.
- **Intel GPU, Gen12 or newer** (Tiger Lake / Arc / Meteor Lake and later)
  with the iHD VA-API driver and GStreamer's `vah265enc`. The native capture
  helper hard-codes the Intel `I915_FORMAT_MOD_4_TILED` DMA-BUF modifier and
  is the only path that reaches the numbers below. AMD-only machines are
  **not supported** today; on an NVIDIA-only machine only the slow reference
  path (`--capture-memory system`, CPU readback → NVENC, ~30 fps) exists.
- PipeWire ≥ 1.0, Python 3 with `gi` (GStreamer typelibs), `dbus`,
  `websockets` ≥ 13, and `libei` (`libei.so.1`; on Ubuntu the `libei1` package,
  pulled in by Xwayland).
- `scripts/setup-native.sh` unpacks headers with `apt-get download`, so it is
  Debian/Ubuntu-only; on other distributions build `native/tabs9-capture`
  against your own `libpipewire-0.3`, `libva`, `libdrm` headers with
  `make -C native SYSROOT=/usr/include`.
- A USB 3 **data** cable.

**Tablet**

- Any Android tablet with a hardware HEVC decoder and USB debugging enabled
  should work in principle: the client negotiates resolution, frame rate and
  bitrate from the host, so the `2960x1848` defaults are only fallbacks
  (`--resolution WIDTHxHEIGHT` picks yours). Only the Tab S9 Ultra has been
  verified.
- The client APK is debug-signed. Install it from the GitHub release (the
  SHA-256 is in the release notes) or build it from source with
  `scripts/build-android.sh`.

Internal names keep the `tabs9` prefix from the first tested device: the CLI
is `./tabs9`, the systemd user unit is `tab-s9-usb-display.service`, the app id
is `local.tabs9.usbdisplay`.

## Quick start

```sh
./tabs9 setup                 # checksum-pinned ADB into .local/, nothing system-wide
./tabs9 doctor                # checks portals, GStreamer, kscreen-doctor, USB device
scripts/setup-native.sh       # builds native/tabs9-capture (Debian/Ubuntu)
scripts/build-android.sh      # builds .local/artifacts/tab-s9-usb-display-debug.apk ...
gh release download v0.1.0 -p '*.apk' -D .local/artifacts   # ... or download it (check the SHA-256 in the release notes)
.local/platform-tools/adb -d install -r .local/artifacts/tab-s9-usb-display-debug.apk
./tabs9 start --profile balanced
```

Before the first start: connect the tablet directly with a USB 3 data cable
(a charging-only cable cannot work), unlock it, enable USB debugging and
authorize this computer on the tablet. `setup` does not install system
packages, load kernel modules, change the firewall or enable autostart;
`doctor` deliberately does not print device serial numbers. The build script
prints the APK's SHA-256; the toolchain (JDK, SDK, Gradle caches) lives under
`.local/android-toolchain`. The motion test additionally needs PyQt6. The
control protocol between host and client is described in
[android/README.md](android/README.md).

The first start shows two KDE dialogs — "Share virtual screen" and the
RemoteDesktop/ScreenCast approval. Leave "Allow restoring on future sessions"
ticked in both: the host stores the restore tokens under `.local/state/` and
every later start is silent. The host launches the app on the tablet itself
over ADB; `./tabs9 status`, `./tabs9 logs` and `./tabs9 stop` do what they
say. `--profile balanced` (native resolution, 60 Hz, HEVC 30 Mbit/s) is the
measured usable mode; `--fps 120` is available and reaches 110–113 fps.

## Measured results (2026-09-12, commit 3243ed3, one machine)

All of the following were measured on the Tab S9 Ultra with the synthetic OpenGL
motion pattern on the virtual output (`scripts/gpu-motion-test.py`); nothing
private was captured.

- USB debugging authorized, wired link negotiated at 5 Gbit/s.
- Separate extended output, native 2960 × 1848, 120 Hz mode, scale 1.5,
  placed to the right of the laptop panel.
- **Visible pixels confirmed**: a tablet screenshot shows the synthetic pattern
  rendered on the virtual output at 2960 × 1848.
- **Frame rate, default `--capture-memory native`** (3 fresh runs each,
  10 s warm-up + 60 s, `./tabs9 bench-capture`): at 60 Hz **59.3 / 59.1 /
  59.8 unique fps**, tablet render interval p95 20–22 ms, capture→ack p95
  27–28 ms, no stalls over 100 ms, ~9 % of one core. At 120 Hz 112.5 / 110.6 /
  111.1 fps, render p95 11.7 ms, capture→ack p95 23 ms — the missing ~6 %
  are frames KWin does not record at 120 Hz on this output (the same 111
  fps appears with capture → discard and no pipeline at all). The earlier
  GStreamer-only path was bistable (either ~59 or a stable 30 fps); the
  mechanism and the fix are in [docs/performance.md](docs/performance.md).
- **30-minute soak at the usable mode** (`--profile balanced`, taps every
  15 s): 58.2 unique fps mean and median (worst 5 s window 56.4, during an
  APK build on the same laptop), capture→ack p95 27 ms (worst window 33),
  render p95 20 ms (worst 24), 0 stalls, 2868 input messages, 0 rejected.
  With the bitrate doubled to 60 Mbit/s the tablet receives 59 Mbit/s and
  capture→ack p95 drops to 22 ms: the ADB transport has ≥ 2× headroom.
  **2026-09-13:** the picture dropping to the app's startup screen every
  few minutes was an idle desktop misread as a dead link (KWin sends no
  frame while nothing changes; the app's 10 s read deadline fired). Fixed
  with a negotiated video heartbeat and an explicit recovery state; a 27 min
  motion/idle soak, 15 fault drills (socket loss both sides, decoder
  rebuild) and a held-pinch-through-drop touch check are in
  [docs/performance.md](docs/performance.md). Rebuild the APK and restart
  the host together: an old app with a new host keeps working (no
  heartbeat is sent unless the app asks for it).
  Background/foreground, host restart under a live app, and an app frozen
  for 3 s under load (host and app resync paths both fire) all recover on
  their own within one 5 s window.
- For comparison on the same run type: `system` (CPU readback → NVENC)
  reached 30–36 fps and stalled the compositor itself to ~80 fps;
  `gl` (cross-GPU DMA-BUF import → NVENC) 13 fps. The encoder was never the
  limit; moving the frame off the Intel GPU was. See
  [docs/performance.md](docs/performance.md).
- **Touch confirmed end-to-end**: taps, drags, a two-finger pinch
  (`scripts/mt-inject`) and S Pen tap/stroke delivered on the tablet arrive
  as native Wayland touch/pointer events in a window on the virtual output —
  11/11 contacts in the expected 3×3 zones (corners, centre), 2 touch points
  during the pinch, 0 of 690 input messages rejected. This needed libei — see
  "Touch" below; the pen rides on the same libei device as an absolute
  pointer because KDE 6.6 refuses the portal's `NotifyPointer*` calls on this
  session.
- Consent: **both** portal dialogs are skipped after the first approval via
  stored restore tokens (verified live 2026-09-12: after one accepted "Share
  virtual screen" dialog, `stop`/`start` went straight to streaming twice,
  input mode `touchscreen-eis-ready`, nobody at the keyboard).
- Host → tablet settings sync and tablet → host input transport verified as
  before.

**Read together with the 2026-09-13 fix.** The runs above were recorded on a
boot where `/dev/dri/renderD128` happened to be the Intel GPU. Commit
`ee99d17` made the helper take the render node from the VA encoder instead of
assuming it (on a hybrid laptop the numbering changes across boots, and the
silent fallback landed on a slower path). Two fresh 30 s runs after the fix,
same profile, same synthetic motion: **56.8 / 56.6 unique fps**, capture→ack
p95 31.1 / 30.9 ms (worst window), render p95 22.6 / 21.8 ms, 0 stalls, no
fallback. Those are the figures a fresh install should reproduce; the
59.x runs and the 30-minute soak stand as recorded. Details and the
reproduction command are in [docs/performance.md](docs/performance.md).

## Limitations

- **One tested device pair.** Tab S9 Ultra + the laptop above. Other tablets
  and other Intel machines are expected to work but nobody has tried.
- **KDE Plasma Wayland only**, Intel GPU only for the usable path (see
  Requirements).
- **120 Hz mode delivers 110–113 fps**, not 120: KWin records that many frames
  on this output even with capture → discard and no pipeline at all.
- **Pen-only mode** (tablet as a graphics tablet for the laptop's own screen)
  is not implemented; the host tells the client so rather than ignoring it.
- Video is compressed HEVC; perfect pixel preservation is not promised. The
  tablet's USB port does not become a DisplayPort input.
- The APK is debug-signed and not on any store.

## Reporting problems

Open an issue with the output of `./tabs9 doctor` and `./tabs9 logs`, your
Plasma and GPU model, and the tablet model. Both commands avoid device
serials, tokens and screen content by design; check anyway before pasting.

## Start and stop

Recommended launch (the measured usable mode: native resolution, 60 Hz,
HEVC 30 Mbit/s, native capture):

```sh
./tabs9 start --profile balanced
./tabs9 status
./tabs9 logs
./tabs9 stop
```

`./tabs9 start --profile balanced --fps 120` selects the 120 Hz output mode
(measured 110–113 fps, lower latency, ~15 % of a core). The native capture
helper must be built once with `scripts/setup-native.sh`; without it the host
falls back to the GStreamer `va` path, which is subject to the half-rate
state described in the performance report.

`./tabs9 start` is **supervised**: it waits (up to 60 seconds) for the host to
reach a terminal state (streaming, failed, or stopped) or a consent-needed
phase, instead of printing "started" the moment `systemd-run` succeeds. It
prints precise instructions about the two consent dialogs.

KDE requires two portal sessions in this implementation:

1. **"Share virtual screen"** creates the output. The host places it to the
   right of all existing physical monitors, using non-negative logical
   coordinates derived from the current desktop layout.
2. A RemoteDesktop + ScreenCast session captures it and grants input. On KDE
   this dialog has **no screen chooser** — it only asks to approve "see what's
   on the screen" and "control input devices". `xdg-desktop-portal-kde`'s
   RemoteDesktop portal never shows one: with `multiple=false` and more than
   one screen it streams the *whole workspace*, with `multiple=true` it
   streams one PipeWire node per screen. The host therefore asks for all
   screens and selects the node whose geometry is the virtual output; the
   laptop's node is never consumed (KWin does not render into an unconnected
   stream).

The first session keeps the output alive. The second captures its independent
logical desktop and authorizes touch. There is only one video encoder and one
transmitted video stream.

### Touch: why libei and not the portal's NotifyTouch calls

`xdg-desktop-portal-kde` 6.6.6 forwards `NotifyTouchDown/Motion/Up` to KWin's
fake-input protocol but never sends `touch_frame`; Wayland clients (Qt, GTK,
Chromium) only dispatch touch on a frame, so those touches reach no window.
It also ignores the `stream` argument and forwards the coordinates as
workspace-global, while `xdg-desktop-portal` validates them stream-relative,
so they can only ever land on whichever output sits at the origin. Both were
reproduced live. The host instead calls `RemoteDesktop.ConnectToEIS` on the
same consented session and drives KWin's EIS backend through libei
(`src/eis_touch.py`, ctypes, no extra permissions): KWin exposes one absolute
device with a region per output, and every contact is framed. Pen input
still uses the portal's pointer calls, which KDE maps correctly.

### Portal token persistence (one-time consent)

The capture/RemoteDesktop session requests `persist_mode=2`. Per the XDG
RemoteDesktop spec, if the portal's **"Allow restoring on future sessions"**
checkbox is checked, the portal should return a `restore_token` that the host
stores in `.local/state/portal_tokens.json` (gitignored, 0600 permissions,
atomic writes), and present on subsequent starts to `SelectDevices`. This
round trip is **confirmed live** on KDE 6.6.6: the checkbox is on by default,
the token is returned and stored, and the next start restores the session
without a dialog. If a
stored token is stale or rejected by the portal, it is discarded and the host
retries once with interactive consent — no silent retry loop (this recovery
path is unit-tested).

The first dialog, "Share virtual screen" (virtual-output creation), is
persisted the same way under the `screencast_create` key. This works because
`xdg-desktop-portal-kde` 6.6.6 restores a ScreenCast selection by output
`uniqueId`, and the "Share virtual screen" entry has the fixed id `Virtual`
(`screencast.cpp` / `outputsmodel.cpp`). Confirmed live: after one accepted
dialog with "Allow restoring on future sessions" ticked, later starts show no
dialog at all. A stale creation token is discarded and the host retries once
interactively, like the capture token.

One caveat seen live: if KWin's saved output layout
(`~/.config/kwinoutputconfig.json`) remembers the virtual output as
*disabled*, every new virtual output is created disabled, the portal's Start
fails with "error code 2" (`Could not find output` in the journal) and no
dialog is involved. Enable it once with `kscreen-doctor
output.Virtual-virtual-xdp-kde-.enable` and restart
`plasma-xdg-desktop-portal-kde.service` to drop leftover outputs.

### Status reporting

The host writes its current phase to `.local/state/host.status.json` (atomic,
0600):

| Phase | Meaning |
|---|---|
| `starting` | Host process initializing |
| `waiting_virtual_consent` | First portal dialog is open |
| `configuring_output` | Virtual output appeared, configuring |
| `waiting_capture_consent` | Second portal dialog is open |
| `streaming` | Encoder pipeline is running |
| `failed` | Unrecoverable error (message included) |
| `stopped` | Clean shutdown |

`./tabs9 status` shows the current phase. The status file never contains
desktop content, tokens, or device identifiers.

The service runs only on request. Stopping it closes both portal sessions,
removes the virtual output, and removes the two ADB reverse mappings it created.
The laptop panel remains enabled. Windows on the removed output are managed by
KDE's normal display-disconnection behavior.

`--profile smooth|balanced|light` picks 120/60/30 fps with matching bitrate.
Resolution and frame rate are independent: use `--resolution WIDTHxHEIGHT` and
`--fps 30|60|90|120`; explicit `--fps`/`--bitrate` override the profile. Native
`2960x1848` remains the default; lower resolutions require an explicit choice.
All profiles use the same zero-copy
GPU path — the tablet costs the host 0–1 % CPU while its content is static
and ~30 % of one core at 110 fps of continuous motion, so the profile only
matters when things move on it. Bitrate is in kbit/s. Lowering bitrate primarily reduces USB traffic; lowering the frame rate
reduces rendering and encoding work. The application reports the host's applied
settings and measured delivery separately.

`--capture-memory` selects the capture/encode route:

- `native` (default): `native/tabs9-capture` consumes the PipeWire stream,
  converts each KWin DMA-BUF on the Intel GPU into an owned NV12 ring and
  returns the buffer to KWin inside the process callback (pipewire recycles
  one buffer per graph cycle, so anything that returns buffers later starves
  KWin sooner or later); the host encodes the ring with `vah265enc`. Falls
  back to `va` if the helper is missing or fails. The helper uses the VA
  encoder's detected Intel render device; it does not assume `renderD128`
  belongs to Intel (GPU numbering can change after reboot).
- `va`: `KWin DMA-BUF → pipewiresrc → vapostproc → vah265enc`, all GStreamer.
  Zero copies, but bistable: a start-up or a hiccup can leave it at half the
  refresh rate for the life of the instance. Falls back to `system` if the
  VA negotiation fails.
- `system`: CPU readback (`BGRx`) → `nvh265enc`. Reference path; ~30–36 fps at
  native size because KWin's synchronous readback is the ceiling.
- `gl`: DMA-BUF imported by NVIDIA EGL → `nvh265enc`. Negotiates and encodes,
  but the cross-GPU import stalls (~13 fps); kept for diagnosis only.

`./tabs9 status` shows the active capture path and warns when it differs from
the requested path. The fps shown there is a target, not measured delivery;
the tablet overlay and benchmark telemetry report measured frames. A fallback
also raises a desktop notification. After updating native source, rebuild with
`make -C native` before restarting the service.

## Virtual desktops and the tablet

KWin's virtual desktops are global to the workspace: there is no per-output
current desktop, so "switch desktops on the laptop only" cannot be done
natively or by script. What can be done is to keep the tablet out of it:

```sh
./tabs9 pin-desktop on    # tablet windows stay visible on every desktop
./tabs9 pin-desktop off
```

This installs and enables the KWin script in `kwin/tabs9-pin`: any window
that lands on the `Virtual-*` output is set to *all desktops*, and released
again when it moves back to a physical screen (including when the output is
removed at `./tabs9 stop`). Verified live: with the script on, a window on the
tablet survived a desktop switch; with it off, the tablet went blank.

## Verification and privacy

```sh
python3 -m unittest discover -s tests -v
./tabs9 test-motion --seconds 20
./tabs9 bench-capture --seconds 30
```

`bench-capture` measures the capture paths (`--modes native va system gl`)
against continuous OpenGL motion and prints, per run, unique frame rates at
capture / encoder / tablet, capture-interval and capture→ack percentiles,
stall counts, the tablet's own render-interval p95 and input rejections.
Three separate runs per candidate by default, 10 s warm-up, `--json` for the
aggregate; `--env KEY=VALUE` passes diagnostics such as `TABS9_STAGE_PROBES=1`
or `TABS9_TRACE_CAPTURE=1` to the host. With stored restore tokens no consent
dialog appears, so it runs unattended.

The motion test displays a synthetic moving pattern on the virtual output and
reports how many synthetic touches delivered from the tablet arrived as
native input. Logs contain counts, frame dimensions, timing and negotiated
formats, not desktop pixels, touch coordinates, clipboard data or device
identifiers. Runtime tokens, downloads and signing keys belong in ignored
`.local/` paths. Never commit those files or captured desktop images.

Only authenticated clients can read the video stream or submit input. The
servers bind `127.0.0.1`, using ports 8890 and 8891. The host uses `adb -d`, so a
wireless ADB device is not silently substituted for the USB tablet. It never
calls `adb tcpip`, opens a LAN listening port, or copies the clipboard.

The host and client are still undergoing live integration checks. See the
performance report for the distinction between configured refresh rate,
captured frames, encoded frames and tablet-rendered frames.

## Attribution

The Android client is adapted from [UScreen](https://github.com/majmichu1/UScreen),
commit `402c94ecd04ebbe33cf7c50d16a9f22c0d73164e`. Its license and attribution are
preserved with the client source. The host in this repository replaces UScreen's
EVDI/kernel-module pipeline with KDE/PipeWire and portal input.

Protocol references: [XDG ScreenCast](https://flatpak.github.io/xdg-desktop-portal/docs/doc-org.freedesktop.portal.ScreenCast.html)
and [XDG RemoteDesktop](https://flatpak.github.io/xdg-desktop-portal/docs/doc-org.freedesktop.portal.RemoteDesktop.html).
