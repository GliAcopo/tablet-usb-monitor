# Tab S9 USB Display Android client

This client is adapted from
[UScreen](https://github.com/majmichu1/UScreen) commit
`402c94ecd04ebbe33cf7c50d16a9f22c0d73164e`. The original MIT license is in
`LICENSE-USCREEN`.

The client is not tied to one tablet model: it decodes HEVC with the
device's hardware `MediaCodec` and takes resolution, frame rate and bitrate
from the host greeting, so any Android tablet with an HEVC decoder should
work. Only the Galaxy Tab S9 Ultra has been verified.

The custom build uses application ID `local.tabs9.usbdisplay`, so it can be
installed beside the upstream `com.uscreen` app. Its launcher activity is:

```text
local.tabs9.usbdisplay/.MainActivity
```

Run `scripts/build-android.sh` from the repository root. It installs a pinned
JDK and Android command-line tools under `.local/android-toolchain`, keeps all
SDK, Gradle, and signing caches under `.local`, and copies the APK to
`.local/artifacts/tab-s9-usb-display-debug.apk`. It does not install or launch
the app.

## Control protocol

The first WebSocket message from the client is authentication:

```json
{"type":"auth","token":"<64 lowercase hex characters>"}
```

The host greeting and every reply after applying settings carry the complete
applied stream state:

```json
{"status":"connected","protocol":2,"codec":"hevc","width":2960,"height":1848,"fps":120,"bitrate":60000,"pen_only":false,
 "features":["keyframe_request","render_ns","video_heartbeat"]}
```

`features` names optional behaviours the host implements; the client names
the ones it implements in its `config` message (`"features":["video_heartbeat"]`),
and each side enables a feature only when the other named it. A greeting
without `features` is a legacy host.

The client uses those values, rather than local preferences, to configure the
decoder and populate the UI. A settings request remains compatible with
UScreen:

```json
{"type":"config","protocol":2,"features":["video_heartbeat"],"fps":120,"bitrate":60000}
```

Once per second while video is decoding, the client sends measured tablet
state independently of whether the local overlay is visible:

```json
{"type":"stats","panel_hz":120.0,"decoder_fps":119.8,"received_mbps":42.1,"stream_fps":120,"width":2960,"height":1848}
```

On connect the client also announces its own panel, before any video has
arrived:

```json
{"type":"resolution","width":2960,"height":1848,"width_mm":314,"height_mm":195}
```

The host records it and warns once if it disagrees with the virtual output it
already created; it cannot resize an output KWin has bound to a live stream, so
the fix is to restart with matching `--width`/`--height`. The millimetre fields
are accepted and currently unused.

The client can also ask to be used as a graphics tablet for the laptop's own
screen instead of as a second screen:

```json
{"type":"mode","pen_only":true}
```

This mode is **not implemented host-side**. The host replies with its full
applied state (`pen_only` false), which the client treats as authoritative, so
the toggle reverts instead of showing a mode the host never entered.

Frame acknowledgements and touch/pen messages retain the upstream format.

## Video socket

Every packet is a four-byte big-endian length followed by a payload whose
first byte is the type:

| type | payload | meaning |
|---|---|---|
| `0` | codec configuration | optional; headers are normally in-band with each IDR |
| `1` | four-byte big-endian sequence number + one Annex-B HEVC access unit | a frame |
| `2` | four-byte big-endian counter (payload is exactly 5 bytes) | heartbeat |

The compositor sends no frame while the desktop is static, so without the
heartbeat a client cannot tell "nothing changed" from "the host is gone".
A host that both sides negotiated `video_heartbeat` with writes a heartbeat
once per second whenever it has had nothing else to write for a second
(never queued behind frames, same writer, whole packets only). The client
consumes it without decoding or counting it and refreshes its 10 s
transport deadline. With a heartbeat-capable host, that deadline expiring is a
fault and the client reconnects; with a legacy host, silence keeps the
connection and the last picture. A deadline expiring *inside* a packet, an
impossible length or an unknown type is a framing failure and always
reconnects.

Every video connection starts at an IDR: the host asks its encoder for one
when a client connects, and the client discards dependent frames until it
arrives. The client reports `streaming` only once a frame of the current
connection has been rendered; a connection lost after that keeps the
picture on screen and shows a small "Reconnecting video" indicator, and
gestures pause (held contacts are lifted) until rendering resumes. Debug
builds accept two drills over adb (`DrillReceiver`): socket loss and decoder
rebuild.
