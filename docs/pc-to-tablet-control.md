# Driving the tablet from the computer: how it is put together

What the feature does is in the README ("Driving the tablet from the
computer"). This is the inside: the pieces, the things about KWin's input
capture that had to be discovered by experiment, and what is still open.

Everything here was checked on this hardware on 2026-09-15: KDE Plasma
6.6.6 on Wayland (KWin 6.6.6, libei 1.5.0), Tab S9 Ultra on One UI 8.5 /
Android 16.

## The pieces

| where | what |
| --- | --- |
| `src/shortcuts.py` | the host's actions in kglobalaccel, so KDE owns the key bindings |
| `src/remote.py` | the state machine, the capture, the socket to the tablet |
| `src/eis_receive.py` | libei *receiver* (the mirror of `eis_touch.py`'s sender) |
| `scripts/tabs9-remote/Remote.java` | the tablet's receiver: events in, Android input out |
| `scripts/tabs9-remote/Uhid.java` | the real mouse and keyboard it makes on the tablet |
| `scripts/tabs9-banner.py` | the strip on every screen saying what state this is |
| `scripts/test-mouse.py` | a uinput mouse/keyboard, to test all of it without hands |

The flow, once the shortcut has taken the input:

```
mouse/keyboard -> libinput -> KWin -> EisInputCaptureFilter -> libei socket
   -> EisReceiver (host) -> RemoteControl.handle -> 12-byte records
   -> adb forward -> LocalServerSocket -> Remote.java -> /dev/uhid
   -> the kernel's HID layer -> Android's InputReader -> a visible pointer
```

## KWin's input capture, as it actually behaves

KWin exposes captures on `org.kde.KWin.EIS.InputCaptureManager`
(`/org/kde/KWin/EIS/InputCapture`): `addInputCapture(capabilities)` returns
an object with `connectToEIS()`, `enable(barriers)`, `disable()` and
`release(QPointF, bool)`, plus `activated`/`deactivated` signals. That is
the same interface `xdg-desktop-portal-kde` drives on the other side of
`org.freedesktop.portal.InputCapture`; the portal adds a consent dialog per
session and no restore token, so the host talks to KWin.

Four things had to be learned the hard way; all of them are why the code
looks the way it does.

1. **A capture is entered only by crossing a barrier, and only at a screen
   edge.** `BarrierSpy::pointerMotion` fires when the current *and* the
   previous pointer position are both on the barrier and the motion carries
   an orthogonal delta — which happens exactly when the pointer is clamped
   at the edge of the workspace and keeps being pushed outwards. So the
   shortcut parks the pointer on the tablet's outer edge with the host's
   libei sender and nudges it outwards six times; the first nudge only
   sets the spy's "previous position", the next one crosses.
   (`kwin/src/plugins/eis/eisinputcapturemanager.cpp`.)
2. **The capture must exist well before it is activated.** Creating it and
   activating it in the same moment gives an active capture that *swallows*
   the input: the desktop stops seeing it and nothing arrives at the
   receiver. Binding the seat and waiting for the three devices KWin
   creates is not enough on its own. The host therefore creates the capture
   as soon as it is up and only arms a barrier when the shortcut is
   pressed.
3. **The barriers must not change while a capture is active.** Calling
   `enable([])` to disarm the edge right after activating (so nothing could
   be entered by accident later) stops the events dead, in the same way.
   The portal's own state machine only allows `Enable`/`SetPointerBarriers`
   while the session is disabled, which is the same rule seen from
   outside. The host now clears the barriers when the capture is
   *released* instead.
4. **Absolute pointer motion is not captured at all.**
   `EisInputCaptureFilter` overrides `pointerMotion`, `pointerButton`,
   `pointerFrame`, `pointerAxis`, `keyboardKey` and the touch and gesture
   events — but there is no `pointerMotionAbsolute`. Tablet-style absolute
   motion goes to the desktop even while a capture is active, which is why
   the host's own absolute injection (the pen, the touches) has to be
   stopped by the host itself in remote mode rather than being captured.

5. **No key reaches KDE while a capture is active.** The capture filter sits
   at `InputFilterOrder::EisInput`, above `GlobalShortcut`, so the shortcut
   that started the capture cannot end it — and a test that releases over
   D-Bus (as the first ones did) never notices. The host watches the
   captured key stream for the combination KDE has bound to its own action
   and releases on it, swallowing those keys rather than sending them to
   the tablet. KWin's "Disable Active Input Capture" (Meta+Shift+Escape)
   works because it is handled in the barrier *spy*, and spies run before
   filters.

Two more practical notes: every call into the capture is made
asynchronously, because they are made from inside D-Bus signal handlers and
the same process has to keep the libei socket serviced; and the barrier is
a segment in *workspace* coordinates, so it is recomputed whenever the
outputs move (a host restart puts the tablet on the right until
`kscreen-doctor` is told otherwise).

## The tablet's receiver

`Remote.java` runs as the shell user (`app_process`, the UI Automator
route) and listens on the abstract socket `tabs9-remote`, which the host
reaches through `adb forward tcp:PORT localabstract:tabs9-remote` (PORT is
the instance's third port: 8892 for the first host, 8896 for the second).
The compiled receiver, `tabs9-remote.dex`, is kept next to the sources and
the host pushes it to `/data/local/tmp` the first time a tablet needs it;
`build-and-push.sh [--tablet MODEL]` rebuilds it after a change. The
protocol is twelve bytes per event: `u8 type, u8 flags, u16 code, i32 a,
i32 b` — move (relative, in tablet pixels), button (evdev code), scroll
(thousandths of a wheel notch), key (evdev code), reset.

**It makes a real mouse and keyboard** (`Uhid.java`): `/dev/uhid` is
group-owned by `uhid`, and `adb shell` is in that group, so the receiver
can write a `UHID_CREATE2` event with an HID report descriptor and then
`UHID_INPUT2` reports. The kernel builds an input device from it and
Android's InputReader lists it as `CURSOR | EXTERNAL` — which is the whole
point: **injected events draw no pointer**, so the first version had an
invisible cursor. Only those two writes are needed; the kernel zero-fills
the rest of its event struct, so a short write is a complete event. The
mouse report is five bytes (buttons, dx, dy, wheel, hwheel) with motion
split into ±127 steps, and the keyboard is boot protocol (modifier byte,
reserved, six usages) with the evdev→HID usage table generated from the
kernel's own `hid_keyboard[]`. `IsWaking: false`, so moving the mouse does
not wake a sleeping tablet.

* **ADB's forward accepts a local connection whether or not anything
  listens on the device**, so "the socket connected" says nothing. The
  receiver greets every connection with two bytes (`T9`) and the host
  starts a receiver only if that greeting does not come.
* The host tracks the *socket*, not the process it started: when a receiver
  is already running (from an earlier session, or by hand) the new
  `app_process` exits because the abstract socket is taken, and that is
  fine.
* Its output goes to logcat (`adb logcat -s tabs9Remote`); a pipe from
  `adb shell` would have nobody reading it and would eventually block.
* Pointer position is kept on the host, in tablet pixels, and sent as
  absolute moves: Android's mouse pointer in desktop mode follows
  `ACTION_HOVER_MOVE`, and a drag is `ACTION_DOWN`/`MOVE`/`UP` with
  `ACTION_BUTTON_PRESS`/`RELEASE` carrying `setActionButton` (the
  dispatcher refuses a button action without it).
* The fallback path keeps the absolute position on the tablet side and maps
  evdev codes to Android keycodes with a table generated from Android 16's
  `Generic.kl`, injecting with `KeyCharacterMap.VIRTUAL_KEYBOARD`.
* The greeting's third byte says which path is in use: `u` real devices,
  `i` injection. Both tested tablets take the real devices: the Tab S9
  Ultra (Android 16) and the MatePad Paper (Android 10, where the whole
  cycle — receiver pushed by the host, pointer handed over at the tablet's
  outer edge, motion delivered, handed back, video back in under a second —
  was verified on 2026-09-19).

## The banner

`scripts/tabs9-banner.py` is a separate process (the host writes one JSON
line per state change to its stdin) because it needs a Qt event loop of its
own. It draws a strip at the top of every screen.

It runs on **XWayland**, which is not a compromise but the only way to get
the two properties this needs at once: a window placed on a chosen screen,
and a window that never takes the keyboard focus. Under Wayland a client
cannot position itself, so the only way to pick a screen is `setScreen()`
plus `showFullScreen()` — and that asks the compositor to activate the
window, which takes focus away from whatever the user is typing into
(measured: a key probe stopped receiving keys the moment the banner
appeared; `WA_ShowWithoutActivating`, `Qt::Tool`, `WindowDoesNotAcceptFocus`
and `WindowTransparentForInput` did not change that, and Qt even warns that
it called `requestActivate()` itself). Under X11 the same window is
override-redirect (`X11BypassWindowManagerHint`): KWin never manages it,
never focuses it, and Qt gives it an empty input shape so clicks pass
through. Verified with the same key probe: typing kept working with the
banner up.

## What is not done

* **The tablet's own screen is still being streamed** while it shows its
  own desktop: the host keeps capturing and encoding into a video socket
  nobody is reading (the app is in the background). Pausing the pipeline in
  `desktop`/`control` mode would save GPU and USB bandwidth.
* **DeX proper is not toggled.** On this build the tablet's windows are
  already freeform "desks", so putting the display app in the background is
  all that is needed; the quick-settings tile
  (`com.sec.android.app.launcher/com.honeyspace.dexservice.DesktopModeTile`,
  pressed with `cmd statusbar click-tile`) opens a "Starting DeX — Connect
  display" dialog on this device rather than switching a mode, so it is not
  used.
* **Touch forwarding the other way** (the tablet's touchscreen as a
  touchpad for the computer while it is being driven) is not attempted; the
  host simply ignores the tablet's touches in `control` mode.
* **The clipboard is one-way** (tablet → computer, see the README);
  sending the computer's clipboard to the tablet would fit naturally next
  to it.
* **The portal path** (`org.freedesktop.portal.InputCapture`) is not
  implemented: it needs the Request/Response dance and a dialog every time
  a session is created. It is what a portable version of this would use.
* The pointer is returned to the middle of the computer's screen on
  release, not to where it was when the capture started (KWin reports the
  latter, but that is the parked position on the barrier, which is worse).
