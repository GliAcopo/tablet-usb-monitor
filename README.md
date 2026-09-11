# Tab S9 Ultra as a wired Linux monitor

A local Linux host and Android client for a **real extended desktop over USB**.
The host creates a KDE virtual monitor, captures it through PipeWire, compresses
video with NVIDIA's hardware HEVC encoder, and sends it through authenticated
loopback sockets forwarded by ADB. Wi-Fi and USB tethering are not used.

Target: Galaxy Tab S9 Ultra, 2960 × 1848 at 120 Hz. Development machine:
Ubuntu 26.04, KDE Plasma 6.6.6 Wayland, NVIDIA RTX 4050 Laptop GPU.

## Current verification

- USB debugging authorized, wired link negotiated at 5 Gbit/s.
- Separate extended output, native 2960 × 1848, 120 Hz mode, scale 1.5.
- Actual HEVC stream dimensions verified as 2960 × 1848.
- Initial end-to-end stream delivered roughly 60 frames/s. **A 120 Hz output
  mode is not proof of sustained 120 fps delivery.** Further measurements are
  recorded in [docs/performance.md](docs/performance.md).
- Touch implementation uses KDE's RemoteDesktop portal; no kernel input
  permissions or global input injection service is required.

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
packages, load kernel modules, change the firewall or enable autostart. Android
build/install instructions are supplied with the client build scripts.

Connect the tablet directly with a USB 3 data cable, unlock it, enable USB
debugging, and authorize this computer on the tablet. A charging-only cable
cannot work. `doctor` deliberately does not print device serial numbers.

## Start and stop

```sh
./tabs9 start --fps 120 --bitrate 60000
./tabs9 status
./tabs9 logs
./tabs9 stop
```

KDE requires two portal sessions in this implementation:

1. Choose **Share virtual screen** to create the output. The host arranges it to
   the **left** of the laptop, at the tablet's native resolution.
2. Select the **existing Virtual Output** in the subsequent sharing/control
   dialog. Approve input control for touchscreen support. Do not select the
   laptop screen.

The first portal session keeps the output alive. The second captures its
independent logical desktop and authorizes touch. There is only one video
encoder and one transmitted video stream. This sequence avoids KDE binding the
capture to the laptop while the new virtual output initially mirrors it.

The service runs only on request. Stopping it closes both portal sessions,
removes the virtual output, and removes the two ADB reverse mappings it created.
The laptop panel remains enabled. Windows on the removed output are managed by
KDE's normal display-disconnection behavior.

For a lighter profile, start with `--fps 60 --bitrate 30000`. Bitrate is in
kbit/s. Lowering bitrate primarily reduces USB traffic; lowering the frame rate
reduces rendering and encoding work. The application reports the host's applied
settings and measured delivery separately.

## Verification and privacy

```sh
python3 -m unittest discover -s tests -v
./tabs9 test-motion
```

The motion test displays a synthetic moving pattern for 20 seconds on the
virtual output. Logs contain counts, frame dimensions, timing and negotiated
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
