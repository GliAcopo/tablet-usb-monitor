#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
TOOLCHAIN_ROOT="$PROJECT_ROOT/.local/android-toolchain"
DOWNLOAD_DIR="$TOOLCHAIN_ROOT/downloads"
JDK_DIR="$TOOLCHAIN_ROOT/jdk-17.0.16+8"
SDK_ROOT="$TOOLCHAIN_ROOT/sdk"
GRADLE_CACHE="$TOOLCHAIN_ROOT/gradle-home"
ANDROID_CACHE="$TOOLCHAIN_ROOT/android-user-home"

JDK_ARCHIVE="$DOWNLOAD_DIR/OpenJDK17U-jdk_x64_linux_hotspot_17.0.16_8.tar.gz"
JDK_URL="https://github.com/adoptium/temurin17-binaries/releases/download/jdk-17.0.16%2B8/OpenJDK17U-jdk_x64_linux_hotspot_17.0.16_8.tar.gz"
JDK_SHA256="166774efcf0f722f2ee18eba0039de2d685b350ee14d7b69e6f83437dafd2af1"

TOOLS_ARCHIVE="$DOWNLOAD_DIR/commandlinetools-linux-15859902_latest.zip"
TOOLS_URL="https://dl.google.com/android/repository/commandlinetools-linux-15859902_latest.zip"
TOOLS_SHA256="4e4c464f145a7512b57d088ac6c278c03c9eea610886b35a5e0804e74eedf583"

mkdir -p "$DOWNLOAD_DIR" "$SDK_ROOT/cmdline-tools" "$GRADLE_CACHE" "$ANDROID_CACHE"

download_checked() {
    local url="$1" destination="$2" expected="$3"
    if [[ -f "$destination" ]] && printf '%s  %s\n' "$expected" "$destination" | sha256sum --check --status; then
        return
    fi
    curl --fail --location --retry 3 --output "$destination.part" "$url"
    printf '%s  %s\n' "$expected" "$destination.part" | sha256sum --check --status
    mv "$destination.part" "$destination"
}

if [[ ! -x "$JDK_DIR/bin/javac" ]]; then
    download_checked "$JDK_URL" "$JDK_ARCHIVE" "$JDK_SHA256"
    jdk_stage="$(mktemp -d "$TOOLCHAIN_ROOT/jdk-stage.XXXXXX")"
    tar -xzf "$JDK_ARCHIVE" --strip-components=1 -C "$jdk_stage"
    mv "$jdk_stage" "$JDK_DIR"
fi

if [[ ! -x "$SDK_ROOT/cmdline-tools/latest/bin/sdkmanager" ]]; then
    download_checked "$TOOLS_URL" "$TOOLS_ARCHIVE" "$TOOLS_SHA256"
    tools_stage="$(mktemp -d "$TOOLCHAIN_ROOT/tools-stage.XXXXXX")"
    unzip -q "$TOOLS_ARCHIVE" -d "$tools_stage"
    mv "$tools_stage/cmdline-tools" "$SDK_ROOT/cmdline-tools/latest"
    rmdir "$tools_stage"
fi

export JAVA_HOME="$JDK_DIR"
export ANDROID_HOME="$SDK_ROOT"
export ANDROID_SDK_ROOT="$SDK_ROOT"
export ANDROID_USER_HOME="$ANDROID_CACHE"
export GRADLE_USER_HOME="$GRADLE_CACHE"
export PATH="$JAVA_HOME/bin:$SDK_ROOT/cmdline-tools/latest/bin:$PATH"

set +o pipefail
yes | sdkmanager --sdk_root="$SDK_ROOT" --licenses >/dev/null
license_status="${PIPESTATUS[1]}"
set -o pipefail
if (( license_status != 0 )); then
    printf 'Android SDK license setup failed with status %s\n' "$license_status" >&2
    exit "$license_status"
fi
sdkmanager --sdk_root="$SDK_ROOT" \
    "platforms;android-34" \
    "build-tools;34.0.0"

cd "$PROJECT_ROOT/android"
# JVM unit tests (packet framing etc.) run before the APK is assembled;
# TABS9_SKIP_ANDROID_TESTS=1 skips them.
gradle_tasks=(clean)
if [[ -z "${TABS9_SKIP_ANDROID_TESTS:-}" ]]; then
    gradle_tasks+=(:app:testDebugUnitTest)
fi
gradle_tasks+=(:app:assembleDebug)
./gradlew --no-daemon --console=plain "${gradle_tasks[@]}"

mkdir -p "$PROJECT_ROOT/.local/artifacts"
built_apk="$PROJECT_ROOT/android/app/build/outputs/apk/debug/app-debug.apk"
artifact="$PROJECT_ROOT/.local/artifacts/tab-s9-usb-display-debug.apk"
install -m 0644 "$built_apk" "$artifact"
sha256sum "$artifact"
