# Plan: controlling the tablet from the PC (DeX + input forwarding)

Status: **plan only, nothing built.** The pieces below were checked on this
machine on 2026-09-15 where noted; everything else is to be verified in
phase 0 before writing the real thing.

## What it should feel like

1. A global shortcut on the PC (say `Meta+Shift+T`) puts the tablet into
   Samsung DeX and minimises the USB display app, so the tablet shows its
   own desktop. A notification on the PC says: *"Tablet is in DeX. Press
   `Meta+Shift+T` again to send your mouse and keyboard to it."*
2. The same shortcut again captures the PC's mouse and keyboard and
   forwards them to the tablet, as if they were plugged into it (DeX is
   built for that). The PC pointer disappears; the notification changes to
   *"Controlling the tablet — `Meta+Shift+T` returns to the PC."*
3. The shortcut once more releases the capture: mouse and keyboard are the
   PC's again, the tablet keeps its DeX desktop. A fourth press (or a
   long press / a second shortcut) brings the USB display app back to the
   front and leaves DeX.
4. Installing the host registers the shortcut in KDE's System Settings →
   Shortcuts under the host's name, where it can be rebound like any other.

The capture must never trap the user: the toggle key is handled by our own
capture code (we see every key while capturing), not by KWin, so it works
even if KWin's global shortcuts are inhibited; a second escape (`Ctrl+Alt+
Esc`, or unplugging the cable, or the host exiting) always releases.

## Building blocks

### PC side: taking the keyboard and mouse

The host must not use a hack (a fullscreen window that steals focus) when
the desktop offers the right primitive. Two are available in this session
(`busctl --user introspect org.freedesktop.portal.Desktop
/org/freedesktop/portal/desktop`, both implemented by `kde.portal`):

- **`org.freedesktop.portal.InputCapture`** (version 1, `SupportedCapabilities`
  = 7: keyboard + pointer + touchscreen). This is the portal written for
  Input Leap / Deskflow: the compositor keeps rendering the pointer for us
  or hides it, and delivers the raw events over a **libei receiver**
  context (the host already speaks libei as a sender, `src/eis_touch.py`;
  the receiver side is the same library, `ei_new_receiver`). Capture is
  consent-gated like RemoteDesktop and can persist with a restore token,
  so it fits the existing one-time-consent model. Its activation model is
  *pointer barriers* (capture starts when the pointer crosses a line at a
  screen edge); whether KWin 6.6 also lets a session activate on demand
  (`Enable` + a zero-length barrier, or an all-edges barrier that is
  crossed by a synthetic pointer move from our own EIS sender) is the
  first thing to verify. If it does, the user experience is the one above
  with no window at all.
- **`org.freedesktop.portal.GlobalShortcuts`** (version 2): the host binds
  its shortcuts once (`CreateSession` + `BindShortcuts`); KDE shows the
  binding dialog on first use, stores them under the app's name in System
  Settings → Shortcuts, and calls us back with `Activated`/`Deactivated`
  whenever they are pressed, whether or not our window has focus. This is
  the "automatic adding of the shortcut inside the KDE settings" and the
  "choose your own" in one, without editing `kglobalshortcutsrc`.

Fallback if InputCapture cannot be activated on demand: a borderless
fullscreen window (SDL2 through `pygame`/`PySDL2`, or a small C program)
with `SDL_SetRelativeMouseMode` (pointer lock + relative pointer on
Wayland) and `SDL_SetWindowKeyboardGrab` (KWin honours
`zwp_keyboard_shortcuts_inhibit_v1`). Uglier: it takes over the laptop
screen while active, but it works today on KDE and needs no portal.

### Tablet side: injecting what we captured

`scripts/mt-inject/MtInject.java` already does the hard part: as the shell
user over adb, `InputManager.injectInputEvent` delivers events to whatever
is on screen, DeX included (it is what UI Automator uses). The remote
control needs the same thing as a **long-lived process** reading a compact
event stream from stdin (`adb shell` keeps the pipe open; one process for
the whole session, no per-event `app_process` start), translating:

- pointer: relative deltas from libei → an absolute mouse position kept
  on the host (clamped to the tablet's panel) → `MotionEvent` with
  `SOURCE_MOUSE`: `ACTION_HOVER_MOVE` while no button is down, `DOWN`/
  `MOVE`/`UP` plus `BUTTON_PRESS`/`BUTTON_RELEASE` with `actionButton` set
  (the dispatcher refuses button actions without it — learned while
  testing the S Pen button), `ACTION_SCROLL` with `AXIS_VSCROLL`/`AXIS_HSCROLL`
  for the wheel.
- keyboard: evdev keycodes from libei → Android `KeyEvent` keycodes (a
  table; the Linux and Android codes differ) with the meta state kept on
  the host; key repeat left to Android (it repeats a held key itself only
  for hardware devices, so the host repeats).

Injected events carry the `FLAG_IS_SYNTHETIC`-style marking; system UI
accepts them (screenshots, the notification shade and DeX's launcher all
work under UI Automator), the lock screen does not, which is fine.

### DeX itself

The tablet's quick settings has the DeX tile
(`com.sec.android.app.launcher/com.honeyspace.dexservice.DesktopModeTile`
in `sysui_qs_tiles`); Android's `adb shell cmd statusbar click-tile
<component>` presses a tile without opening the shade, so DeX on/off is
one adb command — to be verified on this tablet (One UI 8.5 / Android 16
calls it desktop mode; `settings` shows `desktop_mode`, `new_dex`,
`SPEN_INPUT_MODE_DEX`). Minimising the app is `input keyevent HOME` or
starting the launcher; bringing it back is the `am start` the host already
does at connect. While the app is not in front it stops decoding; the
host should pause capture/encode for that time (the video-socket liveness
logic already survives an app that stops answering).

### Telling the user

KDE notifications over `org.freedesktop.Notifications` (`notify-send` or
`dbus`), with the shortcut's current binding read back from the
GlobalShortcuts session so the text is always right. Persistent while
forwarding, replaced (same id) on each state change.

## Phases

0. **Probe (an evening, host code only):** (a) InputCapture on KWin 6.6:
   create a session, add a barrier along the laptop's edge, `Enable`, and
   see whether capture can be forced without the user reaching the edge
   (synthetic pointer move from `EisTouch`, or `Activated` on `Enable`);
   record which of pointer/keyboard arrive through the libei receiver and
   at what rate. (b) `cmd statusbar click-tile` toggles DeX here. (c) A
   throwaway injector reading stdin: measure adb-shell-to-screen latency
   for mouse moves (expect a few ms; the touch path today is ~10 ms end to
   end). Decide portal vs SDL fallback from (a).
1. **Tablet injector** (`scripts/tabs9-remote/` next to mt-inject): stdin
   protocol (fixed 16-byte records: type, code, value, x, y), keycode
   table, tests with `MotionEvent` round-trips in the JVM.
2. **Host `remote` mode** (`src/remote.py`, wired into `host.py`):
   GlobalShortcuts registration at start (`./tabs9 start` prints the
   binding), the four-state machine above, InputCapture/libei receiver or
   SDL grab, the adb child process, notifications, and every exit path
   releasing the capture (SIGTERM, adb loss, tablet app gone).
3. **Polish:** clipboard both ways while forwarding (the tablet→PC half
   exists), hide the PC pointer's last position, a `--remote-shortcut`
   option for headless installs, README.

## Open questions

- Default shortcut. `Meta+Shift+T` is free in this session; it can be
  anything the KDE dialog accepts.
- Should DeX and forwarding be one shortcut cycling through states (as
  described) or two shortcuts (DeX on/off, forward on/off)? Two is
  simpler to explain and to escape from.
- While DeX is on, keep streaming the PC desktop to a DeX window (the app
  as a floating window) or stop the stream? Stopping saves power and USB
  bandwidth; keeping it makes "drag a window from the PC into the tablet"
  possible later.
