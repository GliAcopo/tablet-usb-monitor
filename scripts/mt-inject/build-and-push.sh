#!/bin/bash
# Compile the test-only multitouch source with the project's Android toolchain
# and push it to the tablet. Run a pinch with:
#   .local/platform-tools/adb shell "CLASSPATH=/data/local/tmp/mtinject.dex app_process / MtInject pinch 800 700 2200 1200 30 20 12"
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
T="$ROOT/.local/android-toolchain"
OUT="$ROOT/.local/build/mt-inject"
mkdir -p "$OUT"
"$T/jdk-17.0.16+8/bin/javac" -source 11 -target 11 -cp "$T/sdk/platforms/android-34/android.jar" -d "$OUT" "$ROOT/scripts/mt-inject/MtInject.java"
JAVA_HOME="$T/jdk-17.0.16+8" "$T/sdk/build-tools/34.0.0/d8" --output "$OUT" "$OUT/MtInject.class"
"$ROOT/.local/platform-tools/adb" push "$OUT/classes.dex" /data/local/tmp/mtinject.dex
