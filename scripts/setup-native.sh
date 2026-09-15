#!/bin/bash
# Unpack the headers and typelibs the native capture helper needs under
# .local/sysroot (no system packages are installed), then build it.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
mkdir -p "$ROOT/.local/debs" "$ROOT/.local/sysroot"
cd "$ROOT/.local/debs"
# wl-clipboard is a runtime piece, not a header: its wl-copy puts what the
# tablet sends on the desktop clipboard (host.py looks for it here when it
# is not on PATH).
for pkg in libpipewire-0.3-dev libspa-0.2-dev libva-dev libdrm-dev gir1.2-gst-plugins-base-1.0 wl-clipboard; do
    if ! ls "${pkg}_"*.deb >/dev/null 2>&1; then
        apt-get download "$pkg"
    fi
    dpkg-deb -x "${pkg}_"*.deb "$ROOT/.local/sysroot"
done
make -C "$ROOT/native"
echo "built $ROOT/native/tabs9-capture"
