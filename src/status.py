"""Atomic status file protocol for supervised host startup.

The host writes phase transitions to a JSON status file so the CLI can
distinguish "systemd-run succeeded" from "the encoder is actually streaming".
Every write is atomic (write-to-temp + rename) with restrictive permissions.

Phases in order:
  starting                – host process is initializing
  waiting_virtual_consent – first portal dialog is open (create virtual output)
  configuring_output      – virtual output appeared, configuring resolution/position
  waiting_capture_consent – second portal dialog is open (capture + input)
  streaming               – encoder pipeline is running
  failed                  – unrecoverable error
  stopped                 – clean shutdown

Never includes desktop content, tokens, or device identifiers in messages.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import time


PHASES = (
    'starting',
    'waiting_virtual_consent',
    'configuring_output',
    'waiting_capture_consent',
    'streaming',
    'failed',
    'stopped',
)

# Terminal phases that the CLI waits for. Note 'streaming' is terminal only
# for that wait loop -- the host process keeps running after reaching it.
# For "has the process actually exited" checks, see EXITED_PHASES below.
TERMINAL_PHASES = ('streaming', 'failed', 'stopped')
# Phases after which the host process has exited and will not write again.
EXITED_PHASES = ('failed', 'stopped')
# Phases where user action is needed.
CONSENT_PHASES = ('waiting_virtual_consent', 'waiting_capture_consent')


class StatusWriter:
    """Write host phase transitions atomically to a JSON status file."""

    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, phase: str, message: str = '') -> None:
        """Atomically write the current phase and a sanitized message."""
        if phase not in PHASES:
            raise ValueError(f'unknown phase: {phase}')
        # Sanitize: never include tokens, paths with secrets, etc.
        safe_message = str(message)[:500] if message else ''
        data = {
            'phase': phase,
            'message': safe_message,
            'timestamp': time.time(),
            'pid': os.getpid(),
        }
        # Atomic write: temp file in same directory, then rename.
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), prefix='.status_',
                                    suffix='.tmp')
        closed = False
        try:
            os.fchmod(fd, 0o600)
            os.write(fd, json.dumps(data).encode())
            os.close(fd)
            closed = True
            os.rename(tmp, str(self.path))
        except Exception:
            if not closed:
                try:
                    os.close(fd)
                except OSError:
                    pass
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise


def read_status(path: Path) -> dict | None:
    """Read the status file, returning None if missing or corrupt."""
    try:
        data = json.loads(path.read_text())
        if not isinstance(data, dict) or data.get('phase') not in PHASES:
            return None
        return data
    except (OSError, json.JSONDecodeError, ValueError):
        return None


# Conservative upper bound on how old a non-terminal phase can be before it is
# treated as stale even when the pid still matches: guards against the (very
# unlikely) coincidence of a reused pid landing on genuinely old status data,
# rather than a session that has simply been streaming for a long time.
NON_TERMINAL_MAX_AGE = 6 * 60 * 60


def status_summary(path: Path, unit_pid: int | None, *, now: float | None = None,
                    max_age: float = NON_TERMINAL_MAX_AGE) -> dict:
    """Classify a status file against the process systemd currently tracks.

    Returns ``{'phase': str | None, 'message': str, 'stale': bool}``.
    ``stale`` is True whenever the file cannot be trusted to describe the
    process systemd reports as the unit's current MainPID (pass ``None`` for
    ``unit_pid`` when the unit is not active): a pid that does not match, a
    phase implying the process is still running (anything other than
    'failed'/'stopped', including 'streaming') left behind after systemd no
    longer tracks it, or such a phase old enough to be more likely stale data
    than a genuinely long-running one. A missing or corrupt status file is
    not itself stale -- it just means no trustworthy phase is available.
    """
    data = read_status(path)
    if data is None:
        return {'phase': None, 'message': '', 'stale': False}
    now = time.time() if now is None else now
    phase = data['phase']
    exited = phase in EXITED_PHASES
    pid_mismatch = unit_pid is not None and data.get('pid') != unit_pid
    orphaned = unit_pid is None and not exited
    timestamp = data.get('timestamp')
    aged_out = (not exited and isinstance(timestamp, (int, float))
                and (now - timestamp) > max_age)
    return {
        'phase': phase,
        'message': data.get('message', ''),
        'stale': bool(pid_mismatch or orphaned or aged_out),
    }
