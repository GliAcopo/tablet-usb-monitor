#!/usr/bin/env python3
"""Compare the system-memory and GPU-memory capture paths under real motion.

The frame-rate question this project keeps running into cannot be answered from
an idle desktop: KWin emits a screencast frame only when something is damaged,
so an idle session measures how much the desktop happened to change.  This
harness runs each capture path against continuous motion, for the same duration,
and prints the three boundaries side by side: what PipeWire delivered, what the
encoder produced, and what the tablet acknowledged.

It automates everything except the KDE consent dialogs, which must be answered
by hand once per run (two dialogs per capture mode).  It prompts for them and
waits; nothing here clicks them.

    scripts/capture_ab.py --seconds 30
    scripts/capture_ab.py --modes gl --seconds 60

Only aggregate timing metadata is read: the host's own telemetry lines from the
journal, plus the service's CPU accounting.  No screen content is touched.
"""
import argparse
import fcntl
import json
from pathlib import Path
import statistics
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
LOCK = ROOT / '.local/state/host.lock'
UNIT = 'tab-s9-usb-display.service'
# Telemetry arrives every five seconds; discard the first window of each run.
# A cold encoder run has measured almost twice the per-frame cost of a warm one.
WARMUP_SECONDS = 6.0


def unit_active():
    return subprocess.run(['systemctl', '--user', '-q', 'is-active', UNIT]).returncode == 0


def start_host(mode, extra, env=()):
    subprocess.run(['systemd-run', '--user', '--quiet', '--collect', f'--unit={UNIT}',
                    '--property=Type=exec', '--property=TimeoutStopSec=10',
                    '--property=KillMode=mixed', f'--working-directory={ROOT}',
                    *(f'--setenv={item}' for item in env),
                    '/usr/bin/python3', '-u', str(ROOT / 'src/host.py'),
                    '--capture-memory', mode, *extra], check=True)


def host_gone():
    """True once no host process holds the single-instance lock.

    `systemctl is-active` can already report inactive while the unit is still
    deactivating, and host.py refuses to start while the previous process still
    holds .local/state/host.lock.  Waiting on the lock itself is what makes a
    back-to-back restart -- one capture path after the other -- reliable.
    """
    if unit_active():
        return False
    if not LOCK.exists():
        return True
    try:
        with LOCK.open('r') as handle:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(handle, fcntl.LOCK_UN)
        return True
    except (BlockingIOError, OSError):
        return False


def stop_host():
    if unit_active():
        subprocess.run(['systemctl', '--user', 'stop', UNIT], check=False)
    for _ in range(75):
        if host_gone():
            return True
        time.sleep(0.2)
    print('The previous host process is still holding its lock.', flush=True)
    return False


def cpu_seconds():
    result = subprocess.run(['systemctl', '--user', 'show', '-p', 'CPUUsageNSec', '--value', UNIT],
                            capture_output=True, text=True)
    value = result.stdout.strip()
    return int(value) / 1e9 if value.isdigit() else None


def journal_since(since):
    result = subprocess.run(['journalctl', '--user', '-u', UNIT, '--no-pager', '-o', 'cat',
                             '--since', f'@{int(since)}'], capture_output=True, text=True)
    return result.stdout.splitlines()


def wait_for(since, needle, timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for line in journal_since(since):
            if needle in line:
                return line
        if not unit_active():
            return None
        time.sleep(0.5)
    return None


def reports(lines):
    out = []
    for line in lines:
        line = line.strip()
        if line.startswith('{') and 'capture_fps' in line:
            try:
                out.append(json.loads(line))
            except ValueError:
                pass
    return out


def median(values):
    # bool is a subclass of int: a stray True would be averaged in as 1 fps.
    values = [v for v in values
              if isinstance(v, (int, float)) and not isinstance(v, bool)]
    return round(statistics.median(values), 1) if values else None


def measure(mode, seconds, extra, env=()):
    print(f'\n=== capture path: {mode} ===', flush=True)
    if not stop_host():
        return None
    started = time.time()
    start_host(mode, extra, env)
    print('Waiting for capture (stored consent tokens make this dialog-free).', flush=True)
    if wait_for(started, 'Capture authorized', 300) is None:
        print(f'Capture never started for {mode}; skipping this path.', flush=True)
        stop_host()
        return None
    actual = wait_for(started, 'Memory path:', 5) or ''
    if mode != 'system' and mode not in actual.split('Memory path:')[-1]:
        print('Note: the GL path fell back to system memory.', flush=True)
    fallback = bool(wait_for(started, 'falling back to the system-memory', 1))

    motion = subprocess.Popen([sys.executable, str(ROOT / 'scripts/gpu-motion-test.py'),
                               '--seconds', str(seconds + WARMUP_SECONDS + 2)])
    time.sleep(WARMUP_SECONDS)
    window_start = time.time()
    time.sleep(seconds)
    window = reports(journal_since(window_start))
    cpu = cpu_seconds()
    motion.terminate()
    try:
        motion.wait(timeout=10)
    except subprocess.TimeoutExpired:
        motion.kill()
    stop_host()

    if not window:
        print(f'No telemetry captured for {mode}.', flush=True)
        return None
    return {
        'mode': mode,
        'fell_back': fallback,
        'windows': len(window),
        'capture_fps': median([r.get('capture_fps') for r in window]),
        'encoded_fps': median([r.get('encoded_fps') for r in window]),
        'tablet_ack_fps': median([r.get('tablet_ack_fps') for r in window]),
        'capture_interval_ms_p50': median([r.get('capture_interval_ms_p50') for r in window]),
        'capture_interval_ms_p90': median([r.get('capture_interval_ms_p90') for r in window]),
        'capture_interval_ms_max': max((r.get('capture_interval_ms_max') for r in window
                                        if isinstance(r.get('capture_interval_ms_max'), (int, float))), default=None),
        'capture_pts_interval_ms_p50': median([r.get('capture_pts_interval_ms_p50') for r in window]),
        'encode_to_render_ms_p50': median([r.get('encode_to_render_ms_p50') for r in window]),
        'tablet_decoder_fps': median([r.get('tablet', {}).get('decoder_fps') for r in window]),
        'input_rejected_delta': max((r.get('tablet_input_rejected', 0) for r in window), default=0) -
                                min((r.get('tablet_input_rejected', 0) for r in window), default=0),
        'host_cpu_percent_one_core': round(cpu / (seconds + WARMUP_SECONDS) * 100, 1) if cpu is not None else None,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--seconds', type=float, default=30.0,
                        help='measured window per capture path, after warm-up')
    parser.add_argument('--modes', nargs='+', default=['va', 'system'],
                        choices=['system', 'gl', 'va'])
    parser.add_argument('--runs', type=int, default=3, help='separate runs per candidate')
    parser.add_argument('--json', type=Path, help='also write every run as one JSON document')
    parser.add_argument('--env', action='append', default=[], metavar='KEY=VALUE',
                        help='environment for the host (e.g. TABS9_DEBUG_TAIL=...); repeatable')
    parser.add_argument('--label', default='', help='name prefix for the runs in the comparison')
    parser.add_argument('rest', nargs='*', metavar='-- HOST ARGS',
                        help='extra host arguments; the -- separator is required, '
                             'e.g. bench-capture --seconds 30 -- --fps 60')
    args = parser.parse_args()
    if not 5 <= args.seconds <= 600:
        parser.error('--seconds must be between 5 and 600')
    if not 1 <= args.runs <= 10:
        parser.error('--runs must be between 1 and 10')
    if unit_active():
        parser.error('The USB display host is already running; stop it first.')

    results = []
    try:
        for mode in args.modes:
            for run in range(1, args.runs + 1):
                print(f'\nRun {run}/{args.runs}', flush=True)
                result = measure(mode, args.seconds, args.rest, args.env)
                if result:
                    result['mode'] = f'{args.label or mode}-{run}'
                    results.append(result)
    finally:
        stop_host()

    if args.json and results:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps({'seconds': args.seconds, 'host_args': args.rest, 'env': args.env,
                                         'runs': results}, indent=1) + '\n')
    if not results:
        print('\nNo comparable measurement was collected.', flush=True)
        return 1
    fields = [key for key in results[0] if key != 'mode']
    width = max(len(f) for f in fields) + 2
    print('\n=== comparison ===')
    print('field'.ljust(width) + ''.join(r['mode'].rjust(14) for r in results))
    for field in fields:
        print(field.ljust(width) + ''.join(str(r.get(field)).rjust(14) for r in results))
    print('\nCapture fps is the discriminator: if both paths sit near the same '
          'value, KWin\'s screencast scheduling is the ceiling and the memory '
          'path only changes cost, not cadence.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
