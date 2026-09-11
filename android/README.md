# Tab S9 USB Display Android client

This client is adapted from
[UScreen](https://github.com/majmichu1/UScreen) commit
`402c94ecd04ebbe33cf7c50d16a9f22c0d73164e`. The original MIT license is in
`LICENSE-USCREEN`.

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
{"status":"connected","codec":"hevc","width":2960,"height":1848,"fps":120,"bitrate":60000,"pen_only":false}
```

The client uses those values, rather than local preferences, to configure the
decoder and populate the UI. A settings request remains compatible with
UScreen:

```json
{"type":"config","fps":120,"bitrate":60000}
```

Once per second while video is decoding, the client sends measured tablet
state independently of whether the local overlay is visible:

```json
{"type":"stats","panel_hz":120.0,"decoder_fps":119.8,"received_mbps":42.1,"stream_fps":120,"width":2960,"height":1848}
```

Frame acknowledgements and touch/pen messages retain the upstream format.
The video socket receives a four-byte big-endian packet length, followed by
packet type `1`, a four-byte big-endian sequence number, and one Annex-B HEVC
access unit. Codec headers may be carried in-band.
