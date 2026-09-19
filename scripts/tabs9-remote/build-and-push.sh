#!/bin/bash
# Compile the tablet-side input receiver (see Remote.java) with the
# project's Android toolchain and push it to the tablet. The host pushes
# the copy kept in this directory (tabs9-remote.dex) by itself the first
# time remote control is used on a tablet, so this script is only needed
# after changing the receiver. To run it by hand on the tablet:
#   .local/platform-tools/adb shell "CLASSPATH=/data/local/tmp/tabs9-remote.dex app_process / Remote"
# Usage: build-and-push.sh [--no-push] [--tablet MODEL]
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
T="$ROOT/.local/android-toolchain"
OUT="$ROOT/.local/build/tabs9-remote"
DEX="$ROOT/scripts/tabs9-remote/tabs9-remote.dex"
push=1; selector=''
while [[ $# -gt 0 ]]; do
    case "$1" in
        --no-push) push=0 ;;
        --tablet) selector=$2; shift ;;
        --tablet=*) selector=${1#--tablet=} ;;
        *) echo "unknown option $1" >&2; exit 2 ;;
    esac
    shift
done
if [[ ! -d "$T/sdk" ]]; then
    echo "The Android toolchain is not installed (.local/android-toolchain); run scripts/build-android.sh once." >&2
    exit 1
fi
mkdir -p "$OUT"
"$T/jdk-17.0.16+8/bin/javac" -source 11 -target 11 -cp "$T/sdk/platforms/android-34/android.jar" -d "$OUT" "$ROOT/scripts/tabs9-remote/Remote.java" "$ROOT/scripts/tabs9-remote/Uhid.java"
JAVA_HOME="$T/jdk-17.0.16+8" "$T/sdk/build-tools/34.0.0/d8" --output "$OUT" "$OUT"/*.class
cp "$OUT/classes.dex" "$DEX"
sha256sum "$DEX"
if (( push )); then
    # One tablet: the one named, or the only one attached (src/tablets.py
    # picks it and addresses adb; the serial stays out of the output).
    python3 - "$DEX" "$selector" <<'PY'
import os, subprocess, sys
sys.path.insert(0, os.path.join(os.path.dirname(sys.argv[1]), '../../src'))
from tablets import ADB, TabletChoice, choose, list_tablets
try:
    tablet = choose(sys.argv[2] or None, list_tablets(ADB))
except TabletChoice as error:
    sys.exit(str(error))
result = subprocess.run([str(ADB), *tablet.target(), 'push', sys.argv[1], '/data/local/tmp/tabs9-remote.dex'],
                        capture_output=True, text=True)
print(f'{tablet.label}: ' + ('receiver pushed' if result.returncode == 0 else 'push failed: ' + result.stderr.strip()))
sys.exit(result.returncode)
PY
fi
