#!/bin/bash
# Compile the tablet-side input receiver (see Remote.java) with the
# project's Android toolchain and push it to the tablet. The host starts it
# over adb when remote control is turned on; to run it by hand:
#   .local/platform-tools/adb shell "CLASSPATH=/data/local/tmp/tabs9-remote.dex app_process / Remote"
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
T="$ROOT/.local/android-toolchain"
OUT="$ROOT/.local/build/tabs9-remote"
mkdir -p "$OUT"
"$T/jdk-17.0.16+8/bin/javac" -source 11 -target 11 -cp "$T/sdk/platforms/android-34/android.jar" -d "$OUT" "$ROOT/scripts/tabs9-remote/Remote.java" "$ROOT/scripts/tabs9-remote/Uhid.java"
JAVA_HOME="$T/jdk-17.0.16+8" "$T/sdk/build-tools/34.0.0/d8" --output "$OUT" "$OUT"/*.class
if [[ "${1:-}" != "--no-push" ]]; then
    "$ROOT/.local/platform-tools/adb" push "$OUT/classes.dex" /data/local/tmp/tabs9-remote.dex
fi
