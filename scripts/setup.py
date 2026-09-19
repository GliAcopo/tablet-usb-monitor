#!/usr/bin/env python3
"""Guided setup and doctor for the tablet USB monitor.

One command takes a fresh machine and a fresh tablet to a working display,
one step at a time, and says at every step what it found, what it is about
to do and what only the user can do (tap something on the tablet, plug a
cable, log out).  The same steps run read-only as the doctor.

    ./tabs9 setup             check every step, fix what can be fixed, wait
                              for the tablet where a hand is needed
    ./tabs9 setup --yes       accept every fix without asking (sudo may
                              still ask for the password)
    ./tabs9 setup --no-sudo   never call sudo: print the commands instead
    ./tabs9 setup --start     when everything is ready, start the display
    ./tabs9 doctor            report only: the same checks, each with its
                              diagnosis and the exact fix, changes nothing
    ./tabs9 doctor --check    the quiet gate `tabs9 start` runs first

What it changes, and only with a yes: distro packages through the package
manager (sudo), a udev rule when the tablet's USB device is not readable
(sudo), checksum-pinned downloads under .local/ (ADB, headers for the
native helper, the client APK from the GitHub release), the app on the
tablet through ADB.  The one thing it taps on the tablet is the package
installer's own confirm button (Huawei shows one for every ADB install);
it never sends KEYCODE_POWER, never answers the USB-debugging prompt (it
cannot: ADB is not trusted yet) and never prints a serial number, a token
or screen content.
"""
from __future__ import annotations

import argparse
import ctypes
import ctypes.util
import glob
import hashlib
import importlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parents[1]
LOCAL = ROOT / '.local'
sys.path.insert(0, str(ROOT / 'src'))
from tablets import Tablet, TabletChoice, choose, list_tablets  # noqa: E402

# `adb` arguments addressing the tablet the tablet steps are working on.
TARGET = ['-d']
ADB = LOCAL / 'platform-tools/adb'
APK = LOCAL / 'artifacts/tab-s9-usb-display-debug.apk'
APK_NAME = 'tab-s9-usb-display-debug.apk'
PACKAGE = 'local.tabs9.usbdisplay'
STATE = LOCAL / 'state'
HELPER = ROOT / 'native/tabs9-capture'
RELEASES_API = 'https://api.github.com/repos/GliAcopo/tablet-usb-monitor/releases/latest'
UDEV_RULE = Path('/etc/udev/rules.d/51-tablet-usb-monitor.rules')

# USB vendor ids that ship Android tablets: a device from one of these on the
# bus without an ADB interface is almost always a tablet with USB debugging
# off (or a charge-only USB mode), which is worth a precise hint.
ANDROID_VENDORS = {
    '04e8': 'Samsung', '12d1': 'Huawei', '18d1': 'Google', '2717': 'Xiaomi',
    '17ef': 'Lenovo', '22b8': 'Motorola', '0bb4': 'HTC', '2a70': 'OnePlus',
    '1004': 'LG', '0fce': 'Sony', '2207': 'Rockchip (Boox and other e-readers)',
    '0e8d': 'MediaTek', '1f3a': 'Allwinner', '1bbb': 'TCL / Alcatel',
    '2ae5': 'Fairphone', '0b05': 'ASUS', '413c': 'Dell', '05c6': 'Qualcomm',
    '2e04': 'Nothing', '19d2': 'ZTE', '1ebf': 'Oppo / Realme', '2d95': 'Vivo',
}

# Host requirements: (label, probe, Debian/Ubuntu packages, essential).
# Probes are by presence, not by dpkg, so other distributions get a truthful
# report and only the fix text is Debian-shaped.
def _has_module(name):
    def probe():
        try:
            importlib.import_module(name)
            return True
        except Exception:
            return False
    return probe

def _has_gi(namespace):
    def probe():
        try:
            import gi
            gi.require_version(namespace, '1.0')
            importlib.import_module(f'gi.repository.{namespace}')
            return True
        except Exception:
            return False
    return probe

def _has_typelib(name):
    # Either installed system-wide or unpacked by scripts/setup-native.sh.
    def probe():
        for base in glob.glob('/usr/lib/*/girepository-1.0') + glob.glob('/usr/lib64/girepository-1.0') + [
                str(LOCAL / 'sysroot/usr/lib/x86_64-linux-gnu/girepository-1.0')]:
            if (Path(base) / f'{name}-1.0.typelib').is_file():
                return True
        return False
    return probe

def _has_gst_element(name):
    def probe():
        tool = shutil.which('gst-inspect-1.0')
        if not tool:
            return False
        env = dict(os.environ, GST_DEBUG='0')
        return subprocess.run([tool, name], capture_output=True, env=env, timeout=30).returncode == 0
    return probe

def _has_library(soname):
    def probe():
        try:
            ctypes.CDLL(soname)
            return True
        except OSError:
            return False
    return probe

def _has_websockets_13():
    try:
        import websockets
        importlib.import_module('websockets.asyncio.server')
        return int(str(websockets.__version__).split('.')[0]) >= 13
    except Exception:
        return False

def _has_file(*patterns):
    return lambda: any(glob.glob(p) for p in patterns)

def _has_command(name):
    return lambda: bool(shutil.which(name))

def _user_unit_active(unit):
    return lambda: subprocess.run(['systemctl', '--user', 'is-active', '--quiet', unit],
                                  capture_output=True).returncode == 0

REQUIREMENTS = [
    ('Python GObject bindings (gi)', _has_module('gi'), ['python3-gi'], True),
    ('GStreamer typelib (Gst)', _has_gi('Gst'), ['gir1.2-gstreamer-1.0'], True),
    ('GStreamer video typelib (GstVideo)', _has_typelib('GstVideo'),
     ['gir1.2-gst-plugins-base-1.0'], 'sysroot'),
    ('Python D-Bus (dbus)', _has_module('dbus'), ['python3-dbus'], True),
    ('Python websockets >= 13', _has_websockets_13, ['python3-websockets'], True),
    ('gst-inspect-1.0 / gst-launch-1.0', _has_command('gst-inspect-1.0'), ['gstreamer1.0-tools'], True),
    ('GStreamer PipeWire source (pipewiresrc)', _has_gst_element('pipewiresrc'),
     ['gstreamer1.0-pipewire'], True),
    ('GStreamer VA-API HEVC encoder (vah265enc)', _has_gst_element('vah265enc'),
     ['gstreamer1.0-plugins-bad', 'gstreamer1.0-plugins-extra', 'gstreamer1.0-vaapi'], True),
    ('libei (touch and pen into KWin)', _has_library('libei.so.1'), ['libei1'], True),
    ('kscreen-doctor (output placement)', _has_command('kscreen-doctor'),
     ['libkscreen-bin', 'kscreen'], True),
    ('KDE portal backend (xdg-desktop-portal-kde)',
     _has_file('/usr/lib/*/libexec/xdg-desktop-portal-kde', '/usr/libexec/xdg-desktop-portal-kde',
               '/usr/lib/libexec/xdg-desktop-portal-kde'),
     ['xdg-desktop-portal-kde'], True),
    ('PipeWire user service running', _user_unit_active('pipewire.service'), ['pipewire'], True),
    ('systemd-run (supervised start)', _has_command('systemd-run'), ['systemd'], True),
    ('Intel VA-API driver (iHD)', _has_file('/usr/lib/*/dri/iHD_drv_video.so', '/usr/lib64/dri/iHD_drv_video.so'),
     ['intel-media-va-driver-non-free', 'intel-media-va-driver'], False),
    ('C compiler and make (native capture helper)',
     lambda: bool(shutil.which('cc') or shutil.which('gcc')) and bool(shutil.which('make')),
     ['gcc', 'make'], False),
    ('notify-send (desktop notifications)', _has_command('notify-send'), ['libnotify-bin'], False),
]


class Console:
    """Plain, greppable output: one line per finding, a block per step."""
    def __init__(self, interactive, assume_yes):
        self.interactive = interactive
        self.assume_yes = assume_yes
        self.problems = []      # (step, what, fix) that block a start
        self.warnings = []      # (step, what, fix) that reduce the result
        self.step_no = 0
        self.tty = sys.stdin.isatty() and sys.stdout.isatty()

    def step(self, title):
        self.step_no += 1
        print(f'\n[{self.step_no}] {title}')
        print('-' * (len(title) + 6))

    def ok(self, text):
        print(f'  ok    {text}')

    def info(self, text):
        print(f'        {text}')

    def warn(self, step, what, fix=None):
        print(f'  WARN  {what}')
        if fix:
            for line in fix.splitlines():
                print(f'        -> {line}')
        self.warnings.append((step, what, fix))

    def fail(self, step, what, fix=None):
        print(f'  FAIL  {what}')
        if fix:
            for line in fix.splitlines():
                print(f'        -> {line}')
        self.problems.append((step, what, fix))

    def guide(self, lines):
        print()
        for line in lines:
            print(f'   {line}')
        print()

    def ask(self, question, default=True):
        """Yes/no; in report mode or without a terminal the answer is 'no'."""
        if not self.interactive:
            return False
        if self.assume_yes:
            print(f'  {question} [auto-yes]')
            return True
        if not self.tty:
            return False
        suffix = ' [Y/n] ' if default else ' [y/N] '
        try:
            answer = input(f'  {question}{suffix}').strip().lower()
        except EOFError:
            return False
        if not answer:
            return default
        return answer in ('y', 'yes', 's', 'si', 'sì', 'j', 'ja', 'o', 'oui')

    def wait_for(self, what, probe, guidance, timeout=600, interval=2.0):
        """Poll `probe` until it is truthy; the user is doing `guidance`.

        Returns the probe's value, or None when not interactive, on
        timeout, or on Ctrl-C.  Nothing is sent to the tablet meanwhile.
        """
        value = probe()
        if value or not self.interactive:
            return value or None
        self.guide(guidance)
        print(f'  waiting for: {what}  (Ctrl-C skips this step)')
        started = time.monotonic()
        last_note = started
        try:
            while time.monotonic() - started < timeout:
                time.sleep(interval)
                value = probe()
                if value:
                    print(f'  ok    {what}')
                    return value
                if time.monotonic() - last_note >= 15:
                    last_note = time.monotonic()
                    print(f'        still waiting ({int(last_note - started)} s)')
        except KeyboardInterrupt:
            print('\n        skipped')
            return None
        print(f'        gave up after {timeout} s')
        return None


def run(cmd, timeout=60, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, **kw)


def sudo_available():
    return bool(shutil.which('sudo'))


def apt_available():
    return bool(shutil.which('apt-get')) and bool(shutil.which('apt-cache'))


def existing_apt_packages(names):
    """Filter package names to those this distribution actually offers."""
    found = []
    for name in names:
        if run(['apt-cache', 'show', name], timeout=30).returncode == 0:
            found.append(name)
    return found


def sha256_of(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b''):
            digest.update(chunk)
    return digest.hexdigest()


def download_pinned(url, destination, sha256):
    if destination.exists() and sha256_of(destination) == sha256:
        return
    temporary = destination.with_suffix(destination.suffix + '.partial')
    urllib.request.urlretrieve(url, temporary)
    if sha256_of(temporary) != sha256:
        temporary.unlink()
        raise RuntimeError('Downloaded file did not match its pinned checksum')
    temporary.replace(destination)


# --- steps ------------------------------------------------------------------

def step_session(c):
    c.step('Desktop session')
    session = os.environ.get('XDG_SESSION_TYPE', '')
    desktop = os.environ.get('XDG_CURRENT_DESKTOP', '')
    if session == 'wayland' and 'KDE' in desktop:
        c.ok(f'KDE Plasma on Wayland ({desktop})')
    else:
        c.fail('session', f'this is {desktop or "an unknown desktop"} on {session or "an unknown session type"}, '
               'not KDE Plasma on Wayland',
               'Log out. On the login screen pick the session "Plasma (Wayland)", log in again and rerun.\n'
               'GNOME, Sway, Hyprland and X11 sessions cannot run this: the host needs KDE\'s\n'
               'virtual-output, screencast and remote-desktop portals.')
    version = run(['plasmashell', '--version'], timeout=20).stdout.strip() if shutil.which('plasmashell') else ''
    if version:
        c.info(version)
        match = re.search(r'(\d+)\.(\d+)', version)
        if match and (int(match.group(1)), int(match.group(2))) < (6, 1):
            c.warn('session', f'{version} is older than the Plasma 6.6 this was written against',
                   'The portal calls the host relies on may be missing; expect the doctor and logs to say so.')


def step_packages(c, use_sudo):
    c.step('Host packages')
    missing_essential, missing_optional = [], []
    for label, probe, packages, essential in REQUIREMENTS:
        if probe():
            c.ok(label)
        elif essential == 'sysroot' and shutil.which('apt-get') and shutil.which('dpkg-deb'):
            # Unpacked under .local/sysroot by the native-helper step, no sudo.
            c.info(f'{label}: not installed; the native helper step unpacks it under .local/sysroot')
        elif essential:
            c.fail('packages', f'{label}: missing', f'package(s): {" or ".join(packages)}')
            missing_essential.append((label, packages))
        else:
            c.warn('packages', f'{label}: missing (optional)', f'package(s): {" or ".join(packages)}')
            missing_optional.append((label, packages))
    missing = missing_essential + missing_optional
    if not missing:
        return
    if not apt_available():
        c.info('This is not a Debian/Ubuntu system: install the equivalents with your package manager and rerun.')
        return
    wanted = []
    for _, packages in missing:
        candidates = existing_apt_packages(packages)
        if not candidates:
            c.info(f'none of {packages} exists in this distribution\'s package index')
            continue
        # Several names cover the same thing across releases (vah265enc moved
        # between -bad, -extra and -vaapi): install every one that exists.
        wanted.extend(p for p in candidates if p not in wanted)
    if not wanted:
        return
    command = ['apt-get', 'install', '-y', *wanted]
    printed = 'sudo ' + ' '.join(command)
    c.info(f'The fix is one command: {printed}')
    if not use_sudo or not sudo_available():
        c.info('Run it yourself, then rerun ./tabs9 setup.')
        return
    if c.ask('Install these packages now with sudo (asks for your password)?'):
        print()
        result = subprocess.run(['sudo', *command])
        print()
        if result.returncode == 0:
            c.ok('packages installed')
            # Re-probe so the summary reflects the new state.
            c.problems = [p for p in c.problems if p[0] != 'packages']
            c.warnings = [w for w in c.warnings if w[0] != 'packages']
            for label, probe, packages, essential in REQUIREMENTS:
                if not probe():
                    (c.fail if essential else c.warn)('packages', f'{label}: still missing after the install',
                                                      f'package(s): {" or ".join(packages)}')
        else:
            c.fail('packages', 'apt-get did not finish', f'Run by hand: {printed}')


def intel_render_nodes():
    nodes = []
    for node in sorted(glob.glob('/dev/dri/renderD*')):
        try:
            vendor = Path(f'/sys/class/drm/{Path(node).name}/device/vendor').read_text().strip()
        except OSError:
            vendor = ''
        nodes.append((node, vendor))
    return nodes


def step_gpu(c, use_sudo):
    c.step('GPU access')
    nodes = intel_render_nodes()
    if not nodes:
        c.fail('gpu', 'no /dev/dri/renderD* device: no GPU driver is loaded',
               'Without a render node there is no capture and no encoder.')
        return
    intel = [n for n, v in nodes if v == '0x8086']
    for node, vendor in nodes:
        name = {'0x8086': 'Intel', '0x10de': 'NVIDIA', '0x1002': 'AMD'}.get(vendor, vendor or 'unknown vendor')
        access = os.access(node, os.R_OK | os.W_OK)
        if access:
            c.ok(f'{node}: {name}, readable and writable')
        else:
            c.fail('gpu', f'{node}: {name}, NOT accessible by this user')
            groups = run(['id', '-nG']).stdout.split()
            fix = ('Your user cannot open the GPU. Usually the "render" group is missing:\n'
                   f'  sudo usermod -aG render {os.environ.get("USER", "$USER")}\n'
                   'then log out and back in (group changes need a new session).')
            if use_sudo and 'render' not in groups and c.ask('Add your user to the "render" group now (sudo)?'):
                subprocess.run(['sudo', 'usermod', '-aG', 'render', os.environ.get('USER', '')])
                c.info('Added. Log out and back in, then rerun ./tabs9 setup.')
            else:
                c.info(fix)
    if not intel:
        c.warn('gpu', 'no Intel GPU: the zero-copy VA-API path is Intel-only',
               'Only the slow reference path exists (--capture-memory system, CPU readback, ~30 fps).\n'
               'The picture still works; the numbers in the README do not apply.')
    vainfo = shutil.which('vainfo')
    if intel and vainfo:
        env = dict(os.environ, LIBVA_MESSAGING_LEVEL='0')
        result = run([vainfo, '--display', 'drm', '--device', intel[0]], env=env, timeout=30)
        if 'VAProfileHEVCMain' in result.stdout and 'EncSlice' in result.stdout:
            c.ok('VA-API reports an HEVC encoder on the Intel GPU')
        elif result.returncode == 0:
            c.warn('gpu', 'VA-API works but lists no HEVC encoder',
                   'Install the non-free Intel media driver (intel-media-va-driver-non-free) and rerun.')
        else:
            c.warn('gpu', 'vainfo could not open the Intel GPU',
                   'Check the iHD driver package; the host prints the libva error at start.')


def step_adb(c):
    c.step('ADB (local copy, nothing system-wide)')
    manifest = json.loads((ROOT / 'dependencies.json').read_text())['adb']
    if ADB.is_file() and run([str(ADB), 'version'], timeout=20).returncode == 0:
        c.ok(f'{ADB.relative_to(ROOT)} works')
        return True
    if not c.interactive:
        c.fail('adb', 'the pinned ADB is not downloaded', 'Run ./tabs9 setup (downloads platform-tools '
               f'{manifest["version"]} into .local/ and checks its SHA-256).')
        return False
    c.info(f'downloading platform-tools {manifest["version"]} (checksum-pinned)')
    LOCAL.mkdir(exist_ok=True)
    archive = LOCAL / 'platform-tools.zip'
    try:
        download_pinned(manifest['url'], archive, manifest['sha256'])
        with zipfile.ZipFile(archive) as zipped:
            for name in zipped.namelist():
                if not (LOCAL / name).resolve().is_relative_to(LOCAL.resolve()):
                    raise RuntimeError('Invalid archive path')
            zipped.extractall(LOCAL)
        ADB.chmod(0o755)
    except Exception as error:
        c.fail('adb', f'download failed: {type(error).__name__}: {error}',
               'Check the network, or place platform-tools under .local/platform-tools yourself.')
        return False
    c.ok('ADB is ready in .local/platform-tools')
    return True


def step_native_helper(c):
    c.step('Native capture helper (the path that reaches 60 fps)')
    source = ROOT / 'native/tabs9-capture.c'
    if HELPER.is_file() and HELPER.stat().st_mtime >= source.stat().st_mtime:
        c.ok(f'{HELPER.relative_to(ROOT)} is built')
        return
    what = 'not built' if not HELPER.is_file() else 'older than its source'
    if not (shutil.which('apt-get') and shutil.which('dpkg-deb')):
        c.warn('native', f'helper {what}; this is not Debian/Ubuntu',
               'Build it against your own headers: make -C native SYSROOT=/usr/include\n'
               'Without it the host uses the GStreamer va path (works, but can fall to half rate).')
        return
    if not (shutil.which('cc') or shutil.which('gcc')) or not shutil.which('make'):
        c.warn('native', f'helper {what}; no compiler', 'Install gcc and make (step 2), then rerun.')
        return
    if not c.interactive:
        c.warn('native', f'helper {what}', 'Run ./tabs9 setup (or scripts/setup-native.sh) to build it.')
        return
    if not c.ask('Build it now? (unpacks a few -dev packages under .local/sysroot, no system change)'):
        c.warn('native', f'helper {what}', 'scripts/setup-native.sh builds it whenever you want.')
        return
    result = subprocess.run([str(ROOT / 'scripts/setup-native.sh')])
    if result.returncode == 0 and HELPER.is_file():
        c.ok('native helper built')
    else:
        c.warn('native', 'the build did not finish', 'See the compiler output above; the host still works on the va path.')


# --- the tablet ---------------------------------------------------------------

def usb_devices(sysfs=Path('/sys/bus/usb/devices')):
    """Non-hub USB devices on the bus, with the interface classes they expose."""
    devices = []
    for directory in sorted(Path(sysfs).glob('*')):
        try:
            if not (directory / 'idVendor').is_file() or (directory / 'bDeviceClass').read_text().strip() == '09':
                continue
            entry = {
                'vendor': (directory / 'idVendor').read_text().strip(),
                'product': (directory / 'idProduct').read_text().strip(),
                'manufacturer': (directory / 'manufacturer').read_text().strip() if (directory / 'manufacturer').is_file() else '',
                'name': (directory / 'product').read_text().strip() if (directory / 'product').is_file() else '',
                'speed': (directory / 'speed').read_text().strip(),
                'busnum': int((directory / 'busnum').read_text()),
                'devnum': int((directory / 'devnum').read_text()),
                'interfaces': [],
            }
        except (OSError, ValueError):
            continue
        for iface in directory.glob(f'{directory.name}:*'):
            try:
                entry['interfaces'].append((
                    (iface / 'bInterfaceClass').read_text().strip(),
                    (iface / 'bInterfaceSubClass').read_text().strip(),
                    (iface / 'bInterfaceProtocol').read_text().strip()))
            except OSError:
                pass
        entry['adb'] = ('ff', '42', '01') in entry['interfaces']
        entry['mtp'] = ('06', '01', '01') in entry['interfaces'] or ('ff', 'ff', '00') in entry['interfaces']
        entry['android'] = entry['adb'] or entry['vendor'] in ANDROID_VENDORS
        devices.append(entry)
    return devices


def adb_states():
    """[(state, label)] for every USB device ADB knows; no serials."""
    return [(t.state, t.label) for t in list_tablets(ADB) if t.usb]


def adb_shell(*args, timeout=30, any_status=False):
    """stdout of a shell command on the tablet ('' on failure unless any_status)."""
    result = run([str(ADB), *TARGET, 'shell', *args], timeout=timeout)
    return result.stdout.strip() if any_status or result.returncode == 0 else ''


def tablet_facts():
    """What the connected tablet says about itself (nothing identifying)."""
    facts = {}
    props = adb_shell('getprop ro.product.manufacturer; getprop ro.product.model; '
                      'getprop ro.build.version.release; getprop ro.build.version.sdk; '
                      'wm size; wm density').splitlines()
    if len(props) >= 4:
        facts['manufacturer'], facts['model'], facts['android'] = props[0], props[1], props[2]
        try:
            facts['sdk'] = int(props[3])
        except ValueError:
            facts['sdk'] = 0
    for line in props[4:]:
        size = re.search(r'Physical size:\s*(\d+)x(\d+)', line)
        if size:
            w, h = int(size.group(1)), int(size.group(2))
            facts['panel'] = (max(w, h), min(w, h))       # landscape, as the app reports it
        density = re.search(r'Physical density:\s*(\d+)', line)
        if density:
            facts['density'] = int(density.group(1))
    # grep exits 2 when one of the globs matches nothing; the output still counts.
    codecs = adb_shell('grep -h -o "name=\\"[^\\"]*hevc[^\\"]*\\"" /vendor/etc/media_codecs*.xml '
                       '/system/etc/media_codecs*.xml /odm/etc/media_codecs*.xml 2>/dev/null', any_status=True)
    names = sorted(set(re.findall(r'name="([^"]+)"', codecs)))
    facts['hevc_decoders'] = [n for n in names if 'decoder' in n and 'secure' not in n]
    facts['hevc_hw'] = [n for n in facts['hevc_decoders'] if not n.startswith(('OMX.google', 'c2.android'))]
    rates = re.findall(r'(?:refreshRate|fps)[=:]\s*([\d.]+)', adb_shell('dumpsys display', timeout=30))
    try:
        facts['refresh'] = max(float(r) for r in rates) if rates else None
    except ValueError:
        facts['refresh'] = None
    return facts


DEVELOPER_OPTIONS = [
    'Turn USB debugging on, on the tablet:',
    '  1. Settings -> About tablet (About device / About phone) -> tap "Build number" 7 times',
    '     ("You are now a developer"; on Huawei/Honor it is under About -> Build number,',
    '     on Samsung under About -> Software information -> Build number).',
    '  2. Settings -> System (or System & updates) -> Developer options -> USB debugging: ON.',
    '  3. Pull down the notification shade, tap the USB notification and choose',
    '     "Transfer files" / "File transfer (MTP)". "Charge only" hides the ADB interface,',
    '     so the computer sees a charger, not a tablet.',
    '  4. Keep the tablet unlocked while it connects.',
]

AUTHORIZE = [
    'Look at the tablet: it is asking "Allow USB debugging?" with this computer\'s key.',
    '  Tick "Always allow from this computer" and tap Allow / OK.',
    '  If there is no prompt: unplug and replug the cable, or in Developer options',
    '  tap "Revoke USB debugging authorizations" and replug.',
]


def step_tablet(c, use_sudo, selector=None):
    """Bus-level checks, then authorization and facts for every attached tablet.

    Returns [(Tablet, facts)] for the tablets that are ready for the app step.
    """
    c.step('Tablets on USB')
    if not ADB.is_file():
        c.fail('tablet', 'no ADB yet (step above)', 'Rerun after ADB is in place.')
        return []

    def bus_summary():
        return [d for d in usb_devices() if d['android']]

    # 1. Is there a tablet on the bus at all?
    tablets_on_bus = bus_summary() or c.wait_for('a tablet on the USB bus', lambda: bus_summary() or None, [
        'No tablet is connected.',
        '  - Use a USB *data* cable straight into the computer (no hub for the first try).',
        '    A charging-only cable shows nothing at all here.',
        '  - Unlock the tablet.',
        '  - If the tablet is connected and still not listed, try another port or cable.',
    ])
    if not tablets_on_bus:
        c.fail('tablet', 'no tablet on the USB bus', 'Data cable, direct port, tablet unlocked; then rerun.')
        return []
    for t in tablets_on_bus:
        vendor = ANDROID_VENDORS.get(t['vendor'], t['manufacturer'] or f'vendor {t["vendor"]}')
        c.ok(f'{vendor} device "{t["name"] or t["product"]}" at {t["speed"]} Mbit/s, '
             f'{"exposes ADB" if t["adb"] else "no ADB interface"}'
             f'{", MTP/file transfer" if t["mtp"] else ""}')
        speed = int(float(t['speed'] or 0))
        if 0 < speed < 480:
            c.warn('tablet', f'USB link is {t["speed"]} Mbit/s (USB 1.1)', 'Another cable or port; video needs 480 or more.')
        elif speed == 480:
            c.info('480 Mbit/s (USB 2): enough for the 30 fps / 15 Mbit profiles; a USB 3 cable and port give 5000.')

    # 2. USB debugging on (ADB interface present) on at least one of them?
    def probe_adb_iface():
        return [d for d in bus_summary() if d['adb']] or None
    if not probe_adb_iface() and not c.wait_for('USB debugging enabled (ADB interface on the bus)',
                                                probe_adb_iface, DEVELOPER_OPTIONS):
        c.fail('tablet', 'no tablet exposes ADB: USB debugging is off or the USB mode is charge-only',
               '\n'.join(DEVELOPER_OPTIONS))
        return []
    without = [d for d in bus_summary() if not d['adb']]
    if without:
        c.warn('tablet', f'{len(without)} Android device(s) on the bus without an ADB interface',
               'USB debugging off or charge-only mode on that one; the others are set up now.')
    c.ok('USB debugging is on (ADB interface present)')

    # 3. Does ADB see them, with permission?
    tablets = [t for t in list_tablets(ADB) if t.usb]
    if not tablets:
        # The kernel re-enumerated the device (suspend/resume, replug) and the
        # server missed it: a restart is enough, and costs nothing.
        run([str(ADB), 'kill-server'], timeout=30)
        run([str(ADB), 'start-server'], timeout=60)
        time.sleep(1)
        tablets = [t for t in list_tablets(ADB) if t.usb]
    if any(t.state == 'no permissions' for t in tablets):
        vendors = sorted({d['vendor'] for d in bus_summary() if d['adb']})
        rule = '\n'.join(f'SUBSYSTEM=="usb", ATTR{{idVendor}}=="{v}", MODE="0660", TAG+="uaccess"' for v in vendors)
        fix = (f'Your user may not open the tablet\'s USB device. Add a udev rule:\n'
               f'  sudo tee {UDEV_RULE} <<\'EOF\'\n{rule}\nEOF\n'
               '  sudo udevadm control --reload && sudo udevadm trigger\n'
               'then unplug and replug the tablet.')
        if use_sudo and c.interactive and sudo_available() and c.ask('Write that udev rule now (sudo)?'):
            subprocess.run(['sudo', 'tee', str(UDEV_RULE)], input=rule + '\n', text=True, capture_output=True)
            subprocess.run(['sudo', 'udevadm', 'control', '--reload'])
            subprocess.run(['sudo', 'udevadm', 'trigger'])
            run([str(ADB), 'kill-server'], timeout=30)
            c.info('Rule written. Unplug and replug the tablet.')
            tablets = c.wait_for('ADB permission',
                                 lambda: [t for t in list_tablets(ADB) if t.usb and t.state != 'no permissions'] or None,
                                 ['Unplug and replug the tablet.']) or []
        else:
            c.fail('tablet', 'ADB has no permission to open the USB device', fix)
            return []
    if selector:
        try:
            tablets = [choose(selector, tablets)]
        except TabletChoice as error:
            c.fail('tablet', str(error), 'Check --tablet against the models listed.')
            return []
    if not tablets:
        c.fail('tablet', 'ADB sees no device although the bus does', 'Unplug and replug the cable, then rerun.')
        return []
    c.ok(f'{len(tablets)} tablet(s) attached: ' + ', '.join(t.label for t in tablets))

    # 4. Each one: authorized, then what it is.
    ready = []
    for tablet in tablets:
        print(f'\n  -- {tablet.label} --')
        def probe_authorized(serial=tablet.serial):
            return next((t for t in list_tablets(ADB) if t.serial == serial and t.state == 'device'), None)
        current = probe_authorized()
        if current is None:
            state = next((t.state for t in list_tablets(ADB) if t.serial == tablet.serial), 'absent')
            guidance = AUTHORIZE if state == 'unauthorized' else [
                f'ADB reports {tablet.label} as "{state}".',
                '  offline: unplug and replug the cable; if it stays offline, toggle USB debugging off and on.',
                '  absent: the ADB server sees no device although the bus does; replug the cable.',
            ]
            current = c.wait_for(f'{tablet.label} authorized for USB debugging', probe_authorized, guidance)
            if current is None:
                c.fail('tablet', f'{tablet.label}: ADB state is "{state}", not "device"', '\n'.join(guidance))
                continue
        c.ok(f'{tablet.label}: authorized for USB debugging')
        TARGET[:] = tablet.target()
        facts = tablet_facts()
        if not facts.get('model'):
            c.warn('tablet', f'{tablet.label} answered ADB but not `getprop`', 'Odd; try replugging. The host may still work.')
            ready.append((tablet, facts))
            continue
        c.ok(f'{facts["manufacturer"]} {facts["model"]}, Android {facts["android"]} (API {facts["sdk"]})')
        if facts.get('sdk', 0) and facts['sdk'] < 27:
            c.fail('tablet', f'{tablet.label}: Android 8.1 (API 27) or newer is required by the app',
                   'This tablet is too old for the client.')
        if facts.get('panel'):
            w, h = facts['panel']
            c.ok(f'panel {w}x{h} (landscape), density {facts.get("density", "?")} dpi'
                 + (f', reports up to {facts["refresh"]:.0f} Hz' if facts.get('refresh') else ''))
        else:
            c.warn('tablet', '`wm size` gave no panel size', 'Pass --resolution WIDTHxHEIGHT to ./tabs9 start yourself.')
        if facts['hevc_hw']:
            c.ok(f'hardware HEVC decoder: {", ".join(facts["hevc_hw"])}')
        elif facts['hevc_decoders']:
            c.warn('tablet', f'only software HEVC decoders listed ({", ".join(facts["hevc_decoders"])})',
                   'Expect a low frame rate and a warm tablet; the picture should still appear.')
        else:
            c.warn('tablet', 'no HEVC decoder found in the tablet\'s media_codecs*.xml',
                   'The app needs one; if the picture never appears, this is why.')
        ready.append((tablet, facts))
    return ready


# --- the app -----------------------------------------------------------------

def latest_release_apk():
    """(url, sha256) of the APK in the latest GitHub release, from its notes."""
    with urllib.request.urlopen(RELEASES_API, timeout=30) as response:
        release = json.load(response)
    asset = next((a for a in release.get('assets', []) if a.get('name', '').endswith('.apk')), None)
    match = re.search(r'\b([0-9a-f]{64})\b', release.get('body', ''))
    if not asset or not match:
        raise RuntimeError('the latest release has no APK with a published SHA-256')
    return asset['browser_download_url'], match.group(1), release.get('tag_name', '')


def installed_apk_sha():
    path = adb_shell('pm', 'path', PACKAGE)
    if not path.startswith('package:'):
        return None
    out = adb_shell('sha256sum', path.split(':', 1)[1])
    return out.split()[0] if out and len(out.split()[0]) == 64 else ''


def installer_in_front():
    focus = adb_shell('dumpsys', 'window')
    match = re.search(r'mCurrentFocus=.*', focus)
    return bool(match and 'packageinstaller' in match.group(0))


def installer_confirm_button(dump):
    """(label, x, y) of the installer's positive button in a uiautomator dump."""
    for node in re.finditer(r'<node [^>]*>', dump):
        attrs = dict(re.findall(r'([\w-]+)="([^"]*)"', node.group(0)))
        if attrs.get('resource-id') != 'android:id/button1' or 'packageinstaller' not in attrs.get('package', ''):
            continue
        bounds = re.match(r'\[(\d+),(\d+)\]\[(\d+),(\d+)\]', attrs.get('bounds', ''))
        if not bounds:
            continue
        x1, y1, x2, y2 = (int(v) for v in bounds.groups())
        return attrs.get('text') or 'button1', (x1 + x2) // 2, (y1 + y2) // 2
    return None


def confirm_installer_on_tablet():
    """Tap the package installer's own confirm button, and nothing else.

    Huawei/EMUI holds every `adb install` behind an "unknown sources"
    warning on the tablet; the user asked for this to be answered for them.
    The tap goes only to the positive button (android:id/button1) of a
    window that belongs to the package installer, found through the
    accessibility tree, so no other dialog can ever receive it.  Returns the
    button's label when it tapped, None otherwise.
    """
    if not installer_in_front():
        return None
    dump = adb_shell('uiautomator dump /sdcard/.tabs9-ui.xml >/dev/null 2>&1 && '
                     'cat /sdcard/.tabs9-ui.xml; rm -f /sdcard/.tabs9-ui.xml', timeout=60)
    button = installer_confirm_button(dump)
    if button is None:
        return None
    label, x, y = button
    adb_shell('input', 'tap', str(x), str(y))
    return label


def install_apk(c, local_sha):
    """`adb install -r` with the tablet's own prompts answered meanwhile.

    Huawei/EMUI puts an "unknown sources" warning and then its own install
    screen in front of every ADB install; depending on the firmware `adb`
    either blocks until they are answered or answers "Success" at once and
    changes nothing until they are. So the install runs in the background
    and the positive button is tapped whenever the installer is in front,
    until the package on the tablet is this file.
    """
    process = subprocess.Popen([str(ADB), *TARGET, 'install', '-r', str(APK)],
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    deadline = time.monotonic() + 300
    tapped = []
    while time.monotonic() < deadline:
        if process.poll() is not None and installed_apk_sha() == local_sha:
            break
        label = confirm_installer_on_tablet()
        if label:
            tapped.append(label)
            c.info(f'answered the installer prompt on the tablet ("{label}")')
            time.sleep(3)
        elif process.poll() is not None and not tapped:
            # adb finished, nothing to tap, package unchanged: give the
            # package manager a few seconds, then treat it as a failure.
            time.sleep(2)
            if installed_apk_sha() == local_sha:
                break
            if time.monotonic() > deadline - 280:
                break
        else:
            time.sleep(1)
    if process.poll() is None:
        process.kill()
    output = (process.communicate()[0] or '').strip()
    if installed_apk_sha() == local_sha:
        return True
    hint = ('The tablet did not take the install. Vendor toggles that cause this, all under Developer options:\n'
            '  Xiaomi: "Install via USB" (needs a Mi account) and "USB debugging (Security settings)"\n'
            '  Huawei/Honor: answer the prompt on the tablet; "Allow ADB debugging in charge only mode"\n'
            '  Others: "Verify apps over USB" off, or free space.')
    last = output.splitlines()[-1] if output else 'no output'
    c.fail('app', f'install did not land on the tablet ({last})', hint)
    return False


def step_app(c, ready):
    c.step('Client app on the tablet(s)')
    if APK.is_file():
        c.ok(f'{APK.relative_to(ROOT)} ({sha256_of(APK)[:12]}...)')
    elif not c.interactive:
        c.fail('app', 'no client APK under .local/artifacts',
               'Run ./tabs9 setup (downloads the release APK and checks its SHA-256), or scripts/build-android.sh.')
    else:
        c.info('No local APK. The GitHub release ships one with its SHA-256 in the notes;')
        c.info('scripts/build-android.sh builds the same thing from source (downloads a JDK and the SDK, ~1 GB).')
        if c.ask('Download the release APK now?'):
            try:
                url, sha, tag = latest_release_apk()
                APK.parent.mkdir(parents=True, exist_ok=True)
                download_pinned(url, APK, sha)
                c.ok(f'downloaded {tag} APK, SHA-256 verified')
            except Exception as error:
                c.fail('app', f'download failed: {error}', 'Check the network, or build with scripts/build-android.sh.')
        else:
            c.fail('app', 'no client APK', 'scripts/build-android.sh, or download the release APK into .local/artifacts/.')
    if not APK.is_file() or not ready:
        if not ready:
            c.info('no tablet ready: install skipped')
        return
    local_sha = sha256_of(APK)
    for tablet, _ in ready:
        TARGET[:] = tablet.target()
        install_on(c, tablet, local_sha)


def install_on(c, tablet, local_sha):
    on_device = installed_apk_sha()
    if on_device == local_sha:
        c.ok(f'{tablet.label} runs exactly this APK')
        return
    what = 'not installed' if on_device is None else 'a different build is installed'
    fix = f'./tabs9 setup --tablet {tablet.model or tablet.label} installs it (and answers the tablet\'s prompts).'
    if not c.interactive:
        c.fail('app', f'{tablet.label}: client {what}', fix)
        return
    if not c.ask(f'{tablet.label}: client {what}. Install it over ADB now?'):
        c.fail('app', f'{tablet.label}: client {what}', fix)
        return
    if not install_apk(c, local_sha):
        return
    c.ok(f'{tablet.label}: client installed')


def step_consent(c, ready):
    c.step('KDE consent (the two portal dialogs)')
    explained = False
    for tablet, _ in ready or [(None, None)]:
        slug = tablet.slug if tablet else None
        tokens = {}
        try:
            tokens = json.loads((STATE / f'portal_tokens-{slug}.json').read_text()) if slug else {}
        except (OSError, ValueError):
            pass
        have = {k for k in ('screencast_create', 'remotedesktop_capture') if isinstance(tokens.get(k), str) and tokens[k]}
        if have == {'screencast_create', 'remotedesktop_capture'}:
            c.ok(f'{tablet.label}: restore tokens stored, starts are silent (no dialogs)')
            continue
        if not explained:
            explained = True
            c.info('The first start of a tablet shows two KDE dialogs on this computer, one after the other:')
            c.guide([
                '1. "Share virtual screen"  -> click Share.',
                '2. Remote-control approval  -> click Allow. There is no screen to pick: KDE',
                '   shares every screen and the host selects the virtual one; the laptop\'s',
                '   own screen is never sent.',
                'Leave "Allow restoring on future sessions" ticked in both: the tokens go to',
                '.local/state/portal_tokens-<model>.json (mode 0600) and every later start is silent.',
            ])
        c.warn('consent', f'{tablet.label if tablet else "tablet"}: {"no" if not have else "one"} restore token stored yet',
               'Answer the dialogs once at the first start.')


def suggested_command(facts, tablet_label=None):
    """The ./tabs9 start line for this tablet, from what it told us."""
    parts = ['./tabs9', 'start']
    if tablet_label:
        parts += ['--tablet', tablet_label.split()[0]]
    refresh = facts.get('refresh') or 0
    if refresh and refresh < 55:
        parts += ['--profile', 'light']           # 30 fps: e-ink and 40 Hz panels
    else:
        parts += ['--profile', 'balanced']        # 60 fps: the measured usable mode
    if facts.get('panel'):
        w, h = facts['panel']
        parts += ['--resolution', f'{w}x{h}']
    if (facts.get('manufacturer') or '').lower() != 'samsung':
        parts += ['--pen-button', 'off']          # the S Pen SDK is Samsung-only
    return ' '.join(parts)


def summary(c, ready, start):
    print('\n' + '=' * 64)
    if c.problems:
        print(f'NOT READY: {len(c.problems)} problem(s) block a start')
        for step, what, fix in c.problems:
            print(f'  - [{step}] {what}')
            if fix:
                print('      ' + fix.replace('\n', '\n      '))
        print('\nFix the items above and run ./tabs9 setup again (./tabs9 doctor re-checks without changing anything).')
        return 1
    print('READY' + (f' with {len(c.warnings)} warning(s)' if c.warnings else ''))
    for step, what, _ in c.warnings:
        print(f'  - [{step}] {what}')
    attached = len([t for t in list_tablets(ADB) if t.usb]) if ADB.is_file() else 0
    commands = [suggested_command(facts, tablet.label if attached > 1 else None) for tablet, facts in ready] \
        or [suggested_command({})]
    print('\nStart the display with:\n')
    for tablet_command in commands:
        print(f'    {tablet_command}')
    print('\nThen ./tabs9 status, ./tabs9 logs, ./tabs9 stop. The app opens on the tablet by itself;')
    print('./tabs9 ui opens a control panel in your browser, where these settings are remembered per tablet.')
    if start and c.interactive:
        print()
        return subprocess.run(commands[0].split(), cwd=ROOT).returncode
    return 0


def quick_check():
    """The gate `tabs9 start` runs: essential host pieces and one authorized tablet.

    Same wording as before (`name: ready|missing / unavailable`), no
    downloads, no waiting.
    """
    failed = []
    def check(name, okay):
        print(f'{name}: {"ready" if okay else "missing / unavailable"}')
        if not okay:
            failed.append(name)
    check('KDE Wayland session', os.environ.get('XDG_SESSION_TYPE') == 'wayland' and
          'KDE' in os.environ.get('XDG_CURRENT_DESKTOP', ''))
    for label, probe, _, essential in REQUIREMENTS:
        if essential:
            check(label, probe())
    check('Local ADB', ADB.is_file())
    if ADB.is_file():
        states = adb_states()
        authorized = [s for s in states if s[0] == 'device']
        check('An authorized USB tablet' + (f' ({len(authorized)} attached: pick one with --tablet)'
                                             if len(authorized) > 1 else ''), len(authorized) >= 1)
    wl_copy = shutil.which('wl-copy') or LOCAL / 'sysroot/usr/bin/wl-copy'
    print(f'wl-copy (tablet clipboard to PC, optional): '
          f'{"ready" if Path(wl_copy).is_file() else "missing: run scripts/setup-native.sh"}')
    print(f'native capture helper (optional, recommended): {"ready" if HELPER.is_file() else "missing: run ./tabs9 setup"}')
    if failed:
        print('Resolve the unavailable checks before starting: ./tabs9 setup walks through them.')
        return 1
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--doctor', action='store_true', help='report only; change nothing, wait for nothing')
    parser.add_argument('--check', action='store_true', help='quiet gate used by `tabs9 start`')
    parser.add_argument('--yes', '-y', action='store_true', help='accept every fix without asking')
    parser.add_argument('--no-sudo', action='store_true', help='never call sudo; print the commands instead')
    parser.add_argument('--start', action='store_true', help='start the display when everything is ready')
    parser.add_argument('--tablet', default=None, metavar='MODEL', help='only this attached tablet (model or part of it)')
    args = parser.parse_args(argv)
    if args.check:
        return quick_check()
    interactive = not args.doctor
    c = Console(interactive=interactive, assume_yes=args.yes)
    use_sudo = not args.no_sudo
    sys.stdout.reconfigure(line_buffering=True)   # keep child output (apt, make) in order when piped
    print('tabs9: ' + ('guided setup' if interactive else 'doctor (report only)'))
    print(f'Project: {ROOT}')
    if interactive and not c.tty and not args.yes:
        print('No terminal: questions default to "no". Use --yes to accept the fixes.')
    step_session(c)
    step_packages(c, use_sudo)
    step_gpu(c, use_sudo)
    have_adb = step_adb(c)
    step_native_helper(c)
    ready = step_tablet(c, use_sudo, args.tablet) if have_adb else []
    step_app(c, ready)
    step_consent(c, ready)
    return summary(c, ready, args.start)


if __name__ == '__main__':
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print('\ninterrupted')
        sys.exit(130)
