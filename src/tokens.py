"""Persist and retrieve xdg-desktop-portal restore tokens.

Tokens are stored as JSON in a private repo-local runtime directory ignored
by git (.local/state/portal_tokens.json).  Every write is atomic (temp +
rename) with 0o600 permissions.

The file holds at most two tokens keyed by purpose:
  - "screencast_create": the ScreenCast session for virtual output creation
  - "remotedesktop_capture": the RemoteDesktop+ScreenCast session for capture

Tokens are validated on read: if the file is corrupt, missing, or contains
unexpected data, it is silently discarded and the caller falls through to
interactive consent.

Never stores desktop content, device identifiers, or session secrets beyond
the portal-issued opaque restore tokens.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile


# Valid token keys
TOKEN_KEYS = frozenset({'screencast_create', 'remotedesktop_capture'})

# Maximum reasonable token length (portal tokens are opaque strings)
MAX_TOKEN_LENGTH = 4096


def _validate_tokens(data: object) -> dict[str, str]:
    """Return only valid token entries from parsed JSON, or empty dict."""
    if not isinstance(data, dict):
        return {}
    result = {}
    for key in TOKEN_KEYS:
        value = data.get(key)
        if (isinstance(value, str) and 0 < len(value) <= MAX_TOKEN_LENGTH
                and not isinstance(value, bool)):
            result[key] = value
    return result


def load_tokens(path: Path) -> dict[str, str]:
    """Load validated tokens from disk; return empty dict on any failure."""
    try:
        raw = path.read_text(encoding='utf-8')
        data = json.loads(raw)
        return _validate_tokens(data)
    except (OSError, json.JSONDecodeError, ValueError, UnicodeDecodeError):
        return {}


def save_token(path: Path, key: str, token: str) -> None:
    """Atomically merge one token into the persisted file.

    Existing valid tokens for other keys are preserved.
    """
    if key not in TOKEN_KEYS:
        raise ValueError(f'unknown token key: {key}')
    if not isinstance(token, str) or not token or len(token) > MAX_TOKEN_LENGTH:
        raise ValueError('invalid token value')

    existing = load_tokens(path)
    existing[key] = token
    _atomic_write(path, existing)


def discard_token(path: Path, key: str) -> None:
    """Remove one token from the persisted file, if present."""
    if key not in TOKEN_KEYS:
        return
    existing = load_tokens(path)
    if key in existing:
        del existing[key]
        if existing:
            _atomic_write(path, existing)
        else:
            try:
                path.unlink()
            except OSError:
                pass


def _atomic_write(path: Path, data: dict[str, str]) -> None:
    """Write JSON atomically with restrictive permissions."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix='.tokens_',
                                suffix='.tmp')
    closed = False
    try:
        os.fchmod(fd, 0o600)
        os.write(fd, json.dumps(data, indent=2).encode())
        os.close(fd)
        closed = True
        os.rename(tmp, str(path))
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
