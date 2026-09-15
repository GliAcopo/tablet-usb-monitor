# Platform notes: what Android, Samsung, KWin and Plasma actually do

Findings from building the input and clipboard features, with the source
each one rests on. "Observed" means seen on this hardware (Tab S9 Ultra,
One UI 8.5 / Android 16, build `BP4A.251205.006.X910XXS6EZH3`; KDE Plasma
6.6.6 on Wayland, KWin 6.6.6, libei 1.5.0); "source" means read in the
project's code at the version named. Line numbers are from the files as
fetched on 2026-09-15 and will drift.

## Android input

**A stylus button press while hovering never reaches an app as
`ACTION_BUTTON_PRESS`.** The input dispatcher drops button actions when no
pointer is down, in the branch that handles everything but a new gesture:
"If the pointer is not currently down, then ignore the event" →
`InputEventInjectionResult::FAILED`. A hover is not "down"
(`TouchState::isDown()` counts touching pointers only).
- Source: `frameworks/native/services/inputflinger/dispatcher/InputDispatcher.cpp`,
  `findTouchedWindowTargetsLocked`, "Case 2" — android14-release ≈ line
  2455, android16-release ≈ line 2624; `TouchState.cpp` `isDown()`.
  `https://android.googlesource.com/platform/frameworks/native/+/refs/heads/android16-release/services/inputflinger/dispatcher/InputDispatcher.cpp`
- Observed: injecting `ACTION_BUTTON_PRESS` during a stylus hover logs
  `InputDispatcher: Asynchronous input event injection failed`; the
  surrounding `HOVER_ENTER`/`HOVER_MOVE`/`HOVER_EXIT` are delivered.
- Consequence: read the button from `MotionEvent.getButtonState()` on the
  hover events and act on its edges (`TouchCapture.notePenButtons`). A
  hover move carrying the new button state does arrive before the dropped
  press.

**Button actions are never touch events**, so they never go through
`onTouch`/`dispatchTouchEvent`: `MotionEvent::isTouchEvent` returns true
only for DOWN/MOVE/UP/POINTER_DOWN/POINTER_UP/CANCEL/OUTSIDE, and
`View.dispatchPointerEvent` routes everything else to
`dispatchGenericMotionEvent`. An `ACTION_BUTTON_PRESS` branch inside a
touch listener is dead code.
- Source: `frameworks/native/libs/input/Input.cpp` `MotionEvent::isTouchEvent`
  (android16-release ≈ line 978); `frameworks/base/core/java/android/view/View.java`
  `dispatchPointerEvent`.

**Injected `ACTION_BUTTON_PRESS`/`RELEASE` need `actionButton` set** or the
dispatcher rejects them at validation ("action button should be nonzero
for BUTTON_PRESS"). `MotionEvent.setActionButton(int)` is hidden from the
SDK but callable by reflection from a shell-uid `app_process` (no
hidden-API enforcement there); `scripts/mt-inject` does that.
- Source: `InputDispatcher.cpp` `validateMotionEvent` (android14 ≈ line 185).
- Observed: `InputDispatcher: Injection failed: invalid event: action
  button should be nonzero for BUTTON_PRESS` before the fix.

**Injection as the shell user reaches everything on screen** (including
Samsung DeX windows): `InputManager.injectInputEvent(event, 0)` through
`android.hardware.input.InputManagerGlobal.getInstance()` (Android 14+;
`InputManager.getInstance()` before), the route UI Automator uses. This is
the basis of `scripts/mt-inject` and of the planned PC→tablet remote
control (`docs/pc-to-tablet-control.md`). Injected hover moves are
delivered without a log line; enter/exit log `Delivering touch to (pid)`.

**Pointer ids, not indices, identify fingers** across `ACTION_POINTER_UP`:
indices are repacked when a finger lifts (see `TouchCapture.slotOf`).

## Android clipboard and media

**Reading the clipboard needs the focused window.** Since Android 10 the
clipboard service refuses reads from any app that is neither the default
IME nor the app whose window has focus. A broadcast receiver or a
background thread of a foreground app is fine as long as the app's window
is focused; in DeX another window (a stray Air Command panel, in our
case) can hold the focus while the app is still "resumed".
- Observed: `ClipboardService: Denying clipboard access to
  local.tabs9.usbdisplay, application is not in focus nor is it a system
  service for user 0`; `dumpsys window | grep mCurrentFocus` showed
  `Air_Cmd(Standard)`; `input keyevent KEYCODE_BACK` dismissed it.
- Source: `frameworks/base/services/core/java/com/android/server/clipboard/ClipboardService.java`,
  `clipboardAccessAllowed`.
- Writing is not restricted the same way (the debug drill sets the clip
  from a receiver). Reading shows the system toast "<app> pasted from
  your clipboard" (Android 12+).

**Screenshots** taken with `adb shell input keyevent KEYCODE_SYSRQ` land in
`DCIM/Screenshots` on this tablet (One UI names them
`Screenshot_YYYYMMDD_HHMMSS.jpg`, JPEG, 2960x1848 ≈ 440 KB of a flat
screen) and appear in `MediaStore.Images` with `RELATIVE_PATH` containing
`Screenshots/`. Reading them needs `READ_MEDIA_IMAGES` (API 33+; Android
14 may grant only `READ_MEDIA_VISUAL_USER_SELECTED`), `READ_EXTERNAL_STORAGE`
before. `adb shell pm grant <pkg> android.permission.READ_MEDIA_IMAGES`
grants it to a debug build without the dialog.

## Samsung specifics

**The Tab S9 Ultra's S Pen button is a Bluetooth button.** A real press
arrives in Air Command over BLE, not through the digitizer:
`[AirCmd]_BleDriver: GattCallback : onCharacteristicChanged : ... /
UUID_BUTTON_EVENT`, `[AirCmd]_StickySpenDriver: dispatchButtonData :
Button Up(0), seq=7`, then `[AirCmd]_ButtonPressStarter: BtnClick(x, y)`
when the pen was hovering (`SpenInputDetector:
mPenButtonPressedOnHoverHandler`). The hover `MotionEvent`s an app
receives carry no button state for it, so an app can only get the button
through the S Pen Remote SDK (`com.samsung.android.sdk.penremote`, served
by `[AirCmd]_RemoteSpenService`); see `docs/pc-to-tablet-control.md`.
Observed 2026-09-15 with the physical pen: Air Command opened, the app
logged no button, the host counted none.

**Air Command watches the S Pen button system-wide.** Pressing it while
hovering (even a synthetic press) logs `[AirCmd]_SpenInputDetector:
mPenButtonPressedOnHoverHandler : true` and can start the Air Command
UI (`AirCommandMainController: startCommand ID(2).button_pressed`,
`ButtonPressStarter: BtnClick(x, y)`); the panel that opens takes window
focus. Relevant system settings seen on this tablet:
`open_air_cmd_using_spen_btn=0`, `air_cmd_with_pen_button=1`,
`spen_air_action=1`, `pen_hovering=1`. Flipping
`air_cmd_with_pen_button` to 0 over adb did not stop the detector in the
short test (it may need the settings UI or a restart); left at 1.

**DeX.** The tablet runs the display app as a DeX window when DeX is on:
`dumpsys window` reported `Requested w=2960 h=1683` (title bar and taskbar
take the rest), so touch coordinates injected in display pixels map to the
app view with an offset — a tap at display y=200 reached the desktop at
logical y=59, y=900 → 593, y=1600 → 1128 (slope 0.763 instead of the
fullscreen 1232/1848 = 0.667). Touches and the two-finger right click
agree to the pixel, so the mapping is consistent; only absolute
expectations in tests must account for it. The DeX quick-settings tile is
`com.sec.android.app.launcher/com.honeyspace.dexservice.DesktopModeTile`
(from `sysui_qs_tiles`); `adb shell cmd statusbar click-tile <component>`
should toggle it (untested). Settings keys seen: `desktop_mode`,
`new_dex`, `SPEN_INPUT_MODE_DEX`, `dex_flow_pointer`,
`force_desktop_mode_on_external_displays`.

**`adb shell am broadcast --es`** goes through a remote shell: quote the
value twice (`--es text "'a b c'"`) or only the first word arrives.

## KWin (Wayland) and libei

**The EIS "absolute device" carries pointer-absolute, scroll, button and
touch.** `EisBackend::createAbsoluteDevice` configures
`EIS_DEVICE_CAP_POINTER_ABSOLUTE | SCROLL | BUTTON | TOUCH` and one region
per output; a RemoteDesktop grant with `types` pointer (2) allows
`POINTER | POINTER_ABSOLUTE | BUTTON | SCROLL`, touchscreen (4) allows
`TOUCH`, keyboard (1) allows `KEYBOARD`. Our session asks for pointer +
touchscreen (6), so there is no keyboard capability and no key emulation.
- Source: `kwin/src/plugins/eis/eisbackend.cpp` (Plasma/6.6) lines
  108–124 (portal types → capabilities) and 162–175
  (`createAbsoluteDevice`); `eiscontext.cpp` line 209 (the absolute device
  is created when the seat binds POINTER_ABSOLUTE or TOUCH).

**Scroll deltas become sourceless axis events at the pointer.**
`EIS_EVENT_SCROLL_DELTA` → `pointerAxisChanged(axis, delta, 0,
PointerAxisSource::Unknown)`; `SCROLL_STOP`/`CANCEL` → the same with 0;
`SCROLL_DISCRETE` → `delta * 15/120` with the v120 value. Wayland clients
therefore see a `wl_pointer.axis` without `axis_source`, which they treat
as a wheel; delivered to the surface under the pointer without
activating it.
- Source: `eiscontext.cpp` lines 279–312.
- Measured (Qt 6.10 QScrollArea on the virtual output): 12 `angleDelta`
  units per axis unit, so 10 axis units = one 120-unit notch = three
  lines; `--scroll-gain 0.2` makes content follow the fingers ~1:1
  (400 px of travel → 382–409 px; 1901 px at gain 1.0). Other toolkits not
  measured. `QWheelEvent.source()` does not exist in Qt 6.

**Button press and release** are sent as separate frames
(`ei_device_button_button` + `ei_device_frame` each); `BTN_LEFT` 0x110,
`BTN_RIGHT` 0x111 (evdev codes). libei signatures used:
`ei_device_scroll_delta(dev, double, double)`, `ei_device_scroll_stop(dev,
bool, bool)`, `ei_seat_has_capability`, `ei_device_pointer_motion_absolute`.

**Setting the clipboard from a background process.** KWin accepts
`wl_data_device.set_selection` only with a serial not older than the
current selection's (`SeatInterfacePrivate::updateSelection`: a lower
serial cancels the source) — serials come with input events, so a process
that never received input cannot own the selection that way. Clipboard
managers use the data-control protocol instead; `wl-copy` (wl-clipboard
2.2.1) does and works from a systemd unit on KWin 6.6. It forks a server
that keeps the selection and inherits stdout/stderr: capturing its output
blocks until the clipboard changes hands (a 10 s timeout hit before the
fix), so run it with both redirected to `/dev/null`.
- Source: `kwin/src/wayland/seat.cpp` (Plasma/6.6) `updateSelection`
  ≈ line 312.

**Portals present in this session** (`busctl --user introspect
org.freedesktop.portal.Desktop /org/freedesktop/portal/desktop`, all from
`kde.portal`): RemoteDesktop (used), ScreenCast (used), **InputCapture
version 1 with `SupportedCapabilities` 7** (keyboard, pointer,
touchscreen), **GlobalShortcuts version 2**. KDE 6.6 refuses the
RemoteDesktop portal's `NotifyPointer*` calls on this session type, which
is why the pen, the scroll and the right click ride the libei device.

**Where a virtual output lands.** A host restart places the tablet to the
right of the laptop; this layout is restored with
`kscreen-doctor output.Virtual-virtual-xdp-kde-.position.0,0
output.eDP-1.position.1974,0` (tablet at 0,0 scale 1.5 → 1973x1232
logical; laptop at 1974,0 scale 1.75). `setGeometry` is not honoured for
Wayland windows: put a probe on the tablet with
`windowHandle().setScreen(screen)` then `showFullScreen()`.

## Plasma

**Global shortcuts live in kglobalaccel components**: KWin's under
`/component/kwin` ("Walk Through Windows", "Overview", "Grid View",
"Switch One Desktop to the Right/Left"…), plasmashell's under
`/component/plasmashell` ("activate application launcher", "show
dashboard", "activate task manager entry N", "clipboard_action"…).
`org.kde.kglobalaccel.Component.shortcutNames` lists them,
`invokeShortcut(name)` fires one, `allShortcutInfos` shows the bound keys
(the launcher's are Meta = 16777250 and Alt+F1 = 150994992).

**Which launcher opens.** `ShellCorona::activateLauncherMenu()` asks KWin
for `activeOutputName` (the output with the pointer, under focus-follows-
mouse), looks for a panel containment on that screen with an applet
providing `org.kde.plasma.launchermenu`, then a desktop containment there,
then any screen. So with the pen hovering on the tablet the launcher opens
on the laptop's panel unless the tablet has a panel of its own.
- Source: `plasma-workspace/shell/shellcorona.cpp` (Plasma/6.6) lines
  2943–3001.

**KWin scripting as a probe.** `qdbus6 org.kde.KWin /Scripting
org.kde.kwin.Scripting.loadScript <file> <name>` → `/Scripting/Script<id>
org.kde.kwin.Script.run` → `unloadScript <name>`; `print()` goes to
`journalctl --user -o cat` prefixed `js:`. `workspace.windowList()` includes
popups; Kickoff's window has `resourceClass == "org.kde.plasmashell"`
and becomes the active window while open, the desktop and panel windows
are `plasmashell` (`caption` empty). Useful fields: `w.output.name`,
`w.frameGeometry`, `w.active`, `workspace.activeWindow = w`.

## Tooling quirks met on the way

- `pkill -f scroll-probe.py` matches the shell running the command and
  kills it (exit 144); anchor the pattern: `pkill -f "^python3 /tmp.*probe.py"`.
- zsh `noclobber` blocks `>`; use `>|`.
- Truncating or appending to a log a running process holds open corrupts
  it (NUL padding); use a fresh file per run.
- GitHub raw/API fetches returned 404 from this environment;
  `invent.kde.org` raw URLs, `android.googlesource.com` (`?format=TEXT`,
  base64) and `gitlab.freedesktop.org` worked.
- `grep` may be aliased to ugrep, which prints nothing for binary files.
