"""Tests for token persistence module."""

import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tokens import (  # noqa: E402
    load_tokens,
    save_token,
    discard_token,
    _validate_tokens,
    TOKEN_KEYS,
    MAX_TOKEN_LENGTH,
)


class TokenKeysTests(unittest.TestCase):
    def test_token_keys_are_the_two_defined_purposes(self):
        """This module stays a generic two-key store even though host.py
        currently only ever populates 'remotedesktop_capture' (KDE 6.6.6 has
        no persistence for virtual-output creation). A silent third key
        would be a sign the constant drifted from what save_token() and
        load_tokens() actually validate against."""
        self.assertEqual(TOKEN_KEYS, frozenset({'screencast_create', 'remotedesktop_capture'}))


class ValidateTokensTests(unittest.TestCase):
    def test_accepts_valid_tokens(self):
        data = {
            'screencast_create': 'abc123',
            'remotedesktop_capture': 'xyz789',
        }
        result = _validate_tokens(data)
        self.assertEqual(result, data)

    def test_rejects_non_dict(self):
        self.assertEqual(_validate_tokens("string"), {})
        self.assertEqual(_validate_tokens([1, 2]), {})
        self.assertEqual(_validate_tokens(None), {})

    def test_ignores_unknown_keys(self):
        data = {'unknown_key': 'value', 'screencast_create': 'valid'}
        result = _validate_tokens(data)
        self.assertEqual(result, {'screencast_create': 'valid'})

    def test_rejects_empty_tokens(self):
        result = _validate_tokens({'screencast_create': ''})
        self.assertEqual(result, {})

    def test_rejects_oversized_tokens(self):
        result = _validate_tokens({'screencast_create': 'x' * (MAX_TOKEN_LENGTH + 1)})
        self.assertEqual(result, {})

    def test_rejects_boolean_values(self):
        result = _validate_tokens({'screencast_create': True})
        self.assertEqual(result, {})

    def test_rejects_non_string_values(self):
        result = _validate_tokens({'screencast_create': 42})
        self.assertEqual(result, {})


class TokenPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.token_file = Path(self.tmpdir) / 'tokens.json'

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_save_and_load_roundtrip(self):
        save_token(self.token_file, 'screencast_create', 'token_a')
        result = load_tokens(self.token_file)
        self.assertEqual(result, {'screencast_create': 'token_a'})

    def test_save_preserves_other_keys(self):
        save_token(self.token_file, 'screencast_create', 'token_a')
        save_token(self.token_file, 'remotedesktop_capture', 'token_b')
        result = load_tokens(self.token_file)
        self.assertEqual(result, {
            'screencast_create': 'token_a',
            'remotedesktop_capture': 'token_b',
        })

    def test_save_overwrites_same_key(self):
        save_token(self.token_file, 'screencast_create', 'old')
        save_token(self.token_file, 'screencast_create', 'new')
        result = load_tokens(self.token_file)
        self.assertEqual(result['screencast_create'], 'new')

    def test_file_permissions_are_restrictive(self):
        save_token(self.token_file, 'screencast_create', 'secret')
        mode = os.stat(self.token_file).st_mode
        # Only owner should have read/write
        self.assertEqual(stat.S_IMODE(mode), 0o600)

    def test_load_returns_empty_on_missing_file(self):
        result = load_tokens(Path(self.tmpdir) / 'nonexistent.json')
        self.assertEqual(result, {})

    def test_load_returns_empty_on_corrupt_file(self):
        self.token_file.write_text('not valid json{{{')
        result = load_tokens(self.token_file)
        self.assertEqual(result, {})

    def test_load_returns_empty_on_invalid_structure(self):
        self.token_file.write_text('"just a string"')
        result = load_tokens(self.token_file)
        self.assertEqual(result, {})

    def test_discard_removes_key(self):
        save_token(self.token_file, 'screencast_create', 'token_a')
        save_token(self.token_file, 'remotedesktop_capture', 'token_b')
        discard_token(self.token_file, 'screencast_create')
        result = load_tokens(self.token_file)
        self.assertEqual(result, {'remotedesktop_capture': 'token_b'})

    def test_discard_last_key_removes_file(self):
        save_token(self.token_file, 'screencast_create', 'token_a')
        discard_token(self.token_file, 'screencast_create')
        self.assertFalse(self.token_file.exists())

    def test_discard_nonexistent_key_is_noop(self):
        save_token(self.token_file, 'screencast_create', 'token_a')
        discard_token(self.token_file, 'remotedesktop_capture')
        result = load_tokens(self.token_file)
        self.assertEqual(result, {'screencast_create': 'token_a'})

    def test_discard_unknown_key_is_noop(self):
        # Unknown keys are silently ignored
        discard_token(self.token_file, 'bogus_key')

    def test_save_rejects_invalid_key(self):
        with self.assertRaises(ValueError):
            save_token(self.token_file, 'invalid_key', 'value')

    def test_save_rejects_empty_token(self):
        with self.assertRaises(ValueError):
            save_token(self.token_file, 'screencast_create', '')

    def test_save_rejects_oversized_token(self):
        with self.assertRaises(ValueError):
            save_token(self.token_file, 'screencast_create', 'x' * (MAX_TOKEN_LENGTH + 1))

    def test_atomic_write_does_not_corrupt_on_parent_missing(self):
        nested = Path(self.tmpdir) / 'sub' / 'dir' / 'tokens.json'
        save_token(nested, 'screencast_create', 'deep_token')
        result = load_tokens(nested)
        self.assertEqual(result['screencast_create'], 'deep_token')

    def test_write_failure_closes_fd_exactly_once(self):
        """If rename fails after the fd was already closed, the error path
        must not attempt to close it again (the fd number may have been
        reused elsewhere by the time the except block runs)."""
        save_token(self.token_file, 'screencast_create', 'token_a')
        real_close = os.close
        close_calls = []

        def counting_close(fd):
            close_calls.append(fd)
            real_close(fd)

        with patch('tokens.os.close', side_effect=counting_close), \
             patch('tokens.os.rename', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                save_token(self.token_file, 'screencast_create', 'token_b')

        self.assertEqual(len(close_calls), 1)
        # The original file must be untouched by the failed write.
        result = load_tokens(self.token_file)
        self.assertEqual(result, {'screencast_create': 'token_a'})

    def test_write_failure_before_close_still_closes_fd(self):
        """If the write() syscall itself fails (fd never closed), the error
        path must still close it."""
        real_close = os.close
        close_calls = []

        def counting_close(fd):
            close_calls.append(fd)
            real_close(fd)

        with patch('tokens.os.close', side_effect=counting_close), \
             patch('tokens.os.write', side_effect=OSError('no space left')):
            with self.assertRaises(OSError):
                save_token(self.token_file, 'screencast_create', 'token_a')

        self.assertEqual(len(close_calls), 1)


if __name__ == "__main__":
    unittest.main()
