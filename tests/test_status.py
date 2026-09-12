"""Tests for the status protocol module."""

import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from status import (  # noqa: E402
    CONSENT_PHASES,
    EXITED_PHASES,
    NON_TERMINAL_MAX_AGE,
    PHASES,
    StatusWriter,
    TERMINAL_PHASES,
    read_status,
    status_summary,
)


class StatusWriterTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.status_file = Path(self.tmpdir) / 'status.json'
        self.writer = StatusWriter(self.status_file)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_write_creates_valid_json(self):
        self.writer.write('starting')
        data = json.loads(self.status_file.read_text())
        self.assertEqual(data['phase'], 'starting')
        self.assertIn('timestamp', data)
        self.assertIn('pid', data)
        self.assertEqual(data['pid'], os.getpid())

    def test_write_includes_message(self):
        self.writer.write('failed', 'Portal rejected')
        data = json.loads(self.status_file.read_text())
        self.assertEqual(data['message'], 'Portal rejected')

    def test_write_truncates_long_messages(self):
        long_msg = 'x' * 1000
        self.writer.write('failed', long_msg)
        data = json.loads(self.status_file.read_text())
        self.assertEqual(len(data['message']), 500)

    def test_write_rejects_unknown_phase(self):
        with self.assertRaises(ValueError):
            self.writer.write('unknown_phase')

    def test_file_permissions(self):
        self.writer.write('starting')
        mode = os.stat(self.status_file).st_mode
        self.assertEqual(stat.S_IMODE(mode), 0o600)

    def test_overwrite_previous_status(self):
        self.writer.write('starting')
        self.writer.write('streaming')
        data = json.loads(self.status_file.read_text())
        self.assertEqual(data['phase'], 'streaming')

    def test_creates_parent_directory(self):
        nested = Path(self.tmpdir) / 'sub' / 'dir' / 'status.json'
        writer = StatusWriter(nested)
        writer.write('starting')
        self.assertTrue(nested.exists())

    def test_write_failure_closes_fd_exactly_once(self):
        """If rename fails after the fd was already closed, the error path
        must not attempt to close it again (the fd number may have been
        reused by another thread by the time the except block runs)."""
        self.writer.write('starting')  # baseline file exists
        real_close = os.close
        close_calls = []

        def counting_close(fd):
            close_calls.append(fd)
            real_close(fd)

        with patch('status.os.close', side_effect=counting_close), \
             patch('status.os.rename', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                self.writer.write('streaming')

        # Exactly one close: the successful one before the failing rename.
        # The old fstat-probe implementation could double-close or, worse,
        # close an unrelated fd that reused the same number.
        self.assertEqual(len(close_calls), 1)
        # The original file must be untouched by the failed write.
        data = json.loads(self.status_file.read_text())
        self.assertEqual(data['phase'], 'starting')

    def test_write_failure_before_close_still_closes_fd(self):
        """If the write() syscall itself fails (fd never closed), the error
        path must still close it."""
        real_close = os.close
        close_calls = []

        def counting_close(fd):
            close_calls.append(fd)
            real_close(fd)

        with patch('status.os.close', side_effect=counting_close), \
             patch('status.os.write', side_effect=OSError('no space left')):
            with self.assertRaises(OSError):
                self.writer.write('starting')

        self.assertEqual(len(close_calls), 1)


class StatusSummaryTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.status_file = Path(self.tmpdir) / 'status.json'
        self.writer = StatusWriter(self.status_file)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_missing_file_is_not_stale(self):
        summary = status_summary(self.status_file, unit_pid=1234)
        self.assertIsNone(summary['phase'])
        self.assertFalse(summary['stale'])

    def test_matching_pid_streaming_is_not_stale(self):
        self.writer.write('streaming')
        pid = json.loads(self.status_file.read_text())['pid']
        summary = status_summary(self.status_file, unit_pid=pid)
        self.assertEqual(summary['phase'], 'streaming')
        self.assertFalse(summary['stale'])

    def test_mismatched_pid_is_stale(self):
        self.writer.write('streaming')
        real_pid = json.loads(self.status_file.read_text())['pid']
        summary = status_summary(self.status_file, unit_pid=real_pid + 1)
        self.assertEqual(summary['phase'], 'streaming')
        self.assertTrue(summary['stale'])

    def test_non_terminal_phase_with_no_live_unit_is_stale(self):
        """A crash between reaching 'streaming' and writing 'stopped' must
        not be reported as still streaming once the unit is gone."""
        self.writer.write('streaming')
        summary = status_summary(self.status_file, unit_pid=None)
        self.assertEqual(summary['phase'], 'streaming')
        self.assertTrue(summary['stale'])

    def test_terminal_phase_with_no_live_unit_is_not_stale(self):
        self.writer.write('stopped')
        summary = status_summary(self.status_file, unit_pid=None)
        self.assertEqual(summary['phase'], 'stopped')
        self.assertFalse(summary['stale'])

        self.writer.write('failed', 'boom')
        summary = status_summary(self.status_file, unit_pid=None)
        self.assertEqual(summary['phase'], 'failed')
        self.assertFalse(summary['stale'])

    def test_aged_out_non_terminal_phase_is_stale_even_with_matching_pid(self):
        self.writer.write('streaming')
        pid = json.loads(self.status_file.read_text())['pid']
        future = time.time() + NON_TERMINAL_MAX_AGE + 1
        summary = status_summary(self.status_file, unit_pid=pid, now=future)
        self.assertTrue(summary['stale'])

    def test_recent_non_terminal_phase_with_matching_pid_is_not_aged_out(self):
        self.writer.write('streaming')
        pid = json.loads(self.status_file.read_text())['pid']
        soon = time.time() + 5
        summary = status_summary(self.status_file, unit_pid=pid, now=soon)
        self.assertFalse(summary['stale'])

    def test_old_terminal_phase_is_never_aged_out(self):
        self.writer.write('stopped')
        pid = json.loads(self.status_file.read_text())['pid']
        far_future = time.time() + NON_TERMINAL_MAX_AGE * 10
        summary = status_summary(self.status_file, unit_pid=pid, now=far_future)
        self.assertFalse(summary['stale'])


class ReadStatusTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.status_file = Path(self.tmpdir) / 'status.json'

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_read_valid_status(self):
        data = {'phase': 'streaming', 'message': '', 'timestamp': time.time(), 'pid': 123}
        self.status_file.write_text(json.dumps(data))
        result = read_status(self.status_file)
        self.assertEqual(result['phase'], 'streaming')

    def test_read_missing_file(self):
        result = read_status(Path(self.tmpdir) / 'nonexistent.json')
        self.assertIsNone(result)

    def test_read_corrupt_file(self):
        self.status_file.write_text('not json {{')
        result = read_status(self.status_file)
        self.assertIsNone(result)

    def test_read_invalid_phase(self):
        self.status_file.write_text(json.dumps({'phase': 'bogus'}))
        result = read_status(self.status_file)
        self.assertIsNone(result)


class PhaseConstantsTests(unittest.TestCase):
    def test_terminal_phases_are_valid(self):
        for phase in TERMINAL_PHASES:
            self.assertIn(phase, PHASES)

    def test_consent_phases_are_valid(self):
        for phase in CONSENT_PHASES:
            self.assertIn(phase, PHASES)

    def test_exited_phases_are_valid_and_a_subset_of_terminal(self):
        for phase in EXITED_PHASES:
            self.assertIn(phase, PHASES)
        # Every exited phase is also a wait-loop terminal phase, but not the
        # reverse: 'streaming' is wait-terminal while the process keeps running.
        self.assertTrue(set(EXITED_PHASES).issubset(set(TERMINAL_PHASES)))
        self.assertIn('streaming', set(TERMINAL_PHASES) - set(EXITED_PHASES))

    def test_all_phases_accounted(self):
        # Ensure no phase is left uncategorized in any protocol contract
        self.assertTrue(set(TERMINAL_PHASES).issubset(set(PHASES)))
        self.assertTrue(set(CONSENT_PHASES).issubset(set(PHASES)))


if __name__ == "__main__":
    unittest.main()
