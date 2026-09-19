# Porting the host to Windows and macOS: what it would take

Asked on 2026-09-19: could tabs9 ship as a "dummy-proof" `.exe` and a
macOS `.app`? Short answer: **yes for both, and the Android side needs no
change** — but each is a new host program, not a port of this one. This file
is the reasoning, so the decision can be made with the costs in view.

## What carries over unchanged

- **The Android app** (`android/`): it dials `127.0.0.1:8890/8891` over
  `adb reverse`, decodes HEVC, sends touches, pen, clipboard and its panel
  facts. It does not know or care what the host OS is.
- **The protocol** ([android/README.md](../android/README.md)): the
  length-prefixed video framing, the JSON control channel, heartbeats,
  keyframe requests, the settings/config negotiation.
- **ADB** (Google ships `platform-tools` for all three OSes) and the
  checksum-pinned download logic in `scripts/setup.py`.
- **The control panel** (`src/ui.html` + the tiny HTTP API): plain HTML,
  any browser; the server side is ~250 lines of standard-library Python that
  translate one-to-one to any language.
- The ideas that cost the most to find: the idle-desktop heartbeat, the
  decoder that holds a frame until the next one arrives (feed the last frame
  again), per-tablet settings, one host instance per tablet.

## What is KDE-specific in this host (and has to be replaced)

| Piece | Here | Windows | macOS |
|---|---|---|---|
| Create a virtual monitor | KDE portal "Share virtual screen" | **Indirect Display Driver (IDD).** Windows has no user-space API for a virtual monitor; a signed kernel-mode driver is required. Open-source, signed ones exist (e.g. the *Virtual Display Driver* projects built on Microsoft's IddCx sample) and are installed once with admin rights; the installer would bundle one. | **`CGVirtualDisplay`**, a private CoreGraphics API used by BetterDisplay, Deskreen-style tools and others. Works on Apple Silicon and Intel, no kernel extension, but undocumented and can change between macOS releases. |
| Capture that monitor | PipeWire DMA-BUF (zero copy) | **Windows Graphics Capture** or DXGI Desktop Duplication: a GPU texture per frame, zero copy into the encoder on the same GPU. Mature, documented. | **ScreenCaptureKit**: `CMSampleBuffer`s backed by IOSurface, zero copy into VideoToolbox. Mature, documented; needs the Screen Recording permission once. |
| Encode HEVC | VA-API `vah265enc` on Intel | **Media Foundation** HEVC encoder: NVENC, Intel QSV, AMD AMF all expose it, so any recent GPU works (broader than the Intel-only path here). | **VideoToolbox** HEVC: hardware on every Mac since 2017, one API. |
| Touch and pen into the desktop | libei through KDE's RemoteDesktop portal | **`InjectTouchInput`** (user32): real multi-touch, no driver, no admin. Pen via `InjectSyntheticPointerInput`. The easiest of the three platforms for input. | **`CGEvent`**: mouse and keyboard only. There is no public API to inject multi-touch or a pen; gestures would have to be mapped to mouse/scroll events, as the host already does for two-finger scrolling. Needs the Accessibility permission once. |
| Position the monitor | `kscreen-doctor` | `SetDisplayConfig` / `ChangeDisplaySettingsEx` | `CGConfigureDisplayOrigin` |
| Run in the background | systemd user unit | A tray icon and a Windows service or a startup entry | A menu-bar app (`LSUIElement`) |

## Effort and risk, honestly

- **Windows**: the driver is the whole story. With an existing signed IDD
  the rest is a few thousand lines of C# (WinUI/WPF tray app + Media
  Foundation + Graphics Capture) or Rust; packaged with an installer that
  also drops `platform-tools`. Touch is the *best* of the three platforms.
  Risk: driver signing and Windows updates. Rough size: a few weeks for a
  working single-tablet version, plus the same again for polish.
- **macOS**: no driver, but the virtual display API is private, and touch
  cannot be injected — the tablet becomes a screen with a mouse-like pointer
  rather than a touch screen. Distribution "dummy-proof" means a signed and
  notarized `.app` (Apple Developer Program, USD 99/year); without it the
  user has to right-click → Open past Gatekeeper. Swift with ScreenCaptureKit
  and VideoToolbox is the natural fit. Similar size to Windows; the private
  API is the maintenance risk.
- **Linux beyond KDE**: GNOME has the same portals but no virtual-output
  request in Mutter today; wlroots compositors (Sway, Hyprland) have
  `wlr-virtual-output`-style protocols and `wlr-screencopy`, so a wlroots
  host is closer to this one than either Windows or macOS.

## If one of these is started

1. Keep the Android app as the contract: build the host against
   [android/README.md](../android/README.md) and the existing APK, test
   with the same `scripts/mt-inject` touch tests through ADB.
2. Reuse `src/ui.html` as the panel; expose the same `/api/state`,
   `/api/start`, `/api/stop`, `/api/settings` routes.
3. Start with **one tablet, right side, balanced profile**; add the rest
   (sides, two tablets, remote control) only once the picture and touch are
   solid — that order is what worked here.
