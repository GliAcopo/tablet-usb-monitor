# Tab S9 Ultra as a wired Linux monitor

A local Linux host and Android client for a **real extended desktop over USB**.
The host creates a KDE virtual monitor, captures it through PipeWire as a
DMA-BUF, converts and compresses it on the same Intel GPU with VA-API HEVC
(zero copies, no readback), and sends it through authenticated loopback
sockets forwarded by ADB. Touch comes back through KDE's RemoteDesktop portal
and is injected with libei. Wi-Fi and USB tethering are not used.

Target: Galaxy Tab S9 Ultra, 2960 × 1848 at 120 Hz. Development machine:
Ubuntu 26.04, KDE Plasma 6.6.6 Wayland, NVIDIA RTX 4050 Laptop GPU.

## Current verification (live, 2026-09-12)

All of the following were measured on the device with the synthetic OpenGL
motion pattern on the virtual output (`scripts/gpu-motion-test.py`); nothing
private was captured.

- USB debugging authorized, wired link negotiated at 5 Gbit/s.
- Separate extended output, native 2960 × 1848, 120 Hz mode, scale 1.5,
  placed to the right of the laptop panel.
- **Visible pixels confirmed**: a tablet screenshot shows the synthetic pattern
  rendered on the virtual output at 2960 × 1848.
- **Frame rate, default `--capture-memory va`**: KWin presents the virtual
  output at 117–120 fps and stamps screencast frames 8.33 ms apart; the host
  captures, encodes and the tablet decodes and acknowledges **~110 fps**
  sustained, encode-to-render p50 ≈ 11 ms. Host CPU 20–40 % of one core,
  NVIDIA GPU idle.
- For comparison on the same run type: `system` (CPU readback → NVENC)
  reached 30–36 fps and stalled the compositor itself to ~80 fps;
  `gl` (cross-GPU DMA-BUF import → NVENC) 13 fps. The encoder was never the
  limit; moving the frame off the Intel GPU was. See
  [docs/performance.md](docs/performance.md).
- **Touch confirmed end-to-end**: taps on the tablet arrive as native Wayland
  touch events in a window on the virtual output (multitouch slots, correct
  position). This needed libei — see "Touch" below for why the portal's own
  touch calls cannot work on KDE 6.6.
- Consent: **both** portal dialogs are skipped after the first approval via
  stored restore tokens (verified live 2026-09-12: after one accepted "Share
  virtual screen" dialog, `stop`/`start` went straight to streaming twice,
  input mode `touchscreen-eis-ready`, nobody at the keyboard).
- Host → tablet settings sync and tablet → host input transport verified as
  before.
- Pen-only mode (tablet as a graphics tablet for the laptop's own screen) is
  not implemented; the host tells the client so rather than ignoring it.

This is a hardware-specific implementation, not a claim of support for every
Linux compositor or graphics card. It does not turn the tablet USB port into a
DisplayPort input. Video is compressed; perfect pixel preservation is not promised.

## Setup

The following host packages must be available: Python 3 with `gi`, `dbus` and
`websockets`, GStreamer 1.x with PipeWire, HEVC parsing and NVENC plugins,
`kscreen-doctor`, and a KDE Wayland session with desktop portals. The NVIDIA
driver must support hardware encoding. The motion test additionally uses PyQt6.

```sh
./tabs9 setup
./tabs9 doctor
```

`setup` downloads checksum-pinned ADB into `.local/`; it does not install system
packages, load kernel modules, change the firewall or enable autostart.

The Android client is built from source in this repository, with its own pinned
toolchain under `.local/android-toolchain` (nothing is installed system-wide):

```sh
scripts/build-android.sh
.local/platform-tools/adb -d install -r .local/artifacts/tab-s9-usb-display-debug.apk
```

The build script prints the APK's SHA-256. The host launches the installed
client itself over ADB — `local.tabs9.usbdisplay/.MainActivity`, with the
session token as an intent extra — so the app does not need to be started by
hand. See [android/README.md](android/README.md) for the control protocol.

Connect the tablet directly with a USB 3 data cable, unlock it, enable USB
debugging, and authorize this computer on the tablet. A charging-only cable
cannot work. `doctor` deliberately does not print device serial numbers.

## Start and stop

```sh
./tabs9 start --resolution 2960x1848 --fps 60 --bitrate 30000
./tabs9 status
./tabs9 logs
./tabs9 stop
```

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
It also ignores the `stream` argument and injects the coordinates as
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

- `va` (default): `KWin DMA-BUF → vapostproc → vah265enc` on the Intel GPU.
  Zero copies; the frame never leaves the GPU that composited it. Falls back
  to `system` by itself if the VA negotiation fails. Two details matter for
  cadence: KWin offers only 2–4 PipeWire buffers, so the host negotiates 4 and
  queues at most one ahead of the converter, and colour conversion and
  encoding are decoupled by a queue so they overlap.
- `system`: CPU readback (`BGRx`) → `nvh265enc`. Reference path; ~30–36 fps at
  native size because KWin's synchronous readback is the ceiling.
- `gl`: DMA-BUF imported by NVIDIA EGL → `nvh265enc`. Negotiates and encodes,
  but the cross-GPU import stalls (~13 fps); kept for diagnosis only.

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

`bench-capture` measures the capture paths (`--modes va system gl`) against
continuous OpenGL motion and prints capture, encode and tablet-acknowledgement
rates side by side. It performs three separate runs per candidate by default,
with a warm-up; with stored restore tokens no consent dialog appears.

The motion test displays a synthetic moving pattern on the virtual output and
reports how many injected touches arrived as native input. Logs contain counts, frame dimensions, timing and negotiated
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
