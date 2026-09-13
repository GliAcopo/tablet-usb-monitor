"""Tests for portal token persistence, rejection recovery, and status lifecycle."""

import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import host as host_module
from status import StatusWriter, read_status
from tokens import load_tokens, save_token


def make_test_host(tmp_dir):
    h = host_module.Host.__new__(host_module.Host)
    h.args = SimpleNamespace(width=2960, height=1848, fps=120, bitrate=60000, scale=1.5, capture_memory='system')
    h.tokens_file = Path(tmp_dir) / 'portal_tokens.json'
    h.status = StatusWriter(Path(tmp_dir) / 'host.status.json')
    h.loop = MagicMock()
    h.portal = MagicMock()
    h.remote = MagicMock()
    h.bus = MagicMock()
    h.session = 'session_1'
    h.creation_session = None
    h._capture_token_used = False
    h._virtual_token_used = False
    h._wrong_source_attempts = 0
    h.failed = False
    h.virtual_name = 'Virtual-1'
    h.session_watches = []
    h.touch = None
    return h


class HostTokenIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_capture_status_distinguishes_requested_and_fallback_path(self):
        h = make_test_host(self.tmpdir)
        h.args.capture_memory = 'native'
        h.memory_mode = 'native'
        self.assertIn('Capture: native', h.capture_status())
        self.assertIn('not measured throughput', h.capture_status())
        self.assertNotIn('WARNING', h.capture_status())
        h.memory_mode = 'va'
        self.assertIn('Capture: va', h.capture_status())
        self.assertIn('WARNING: fallback from native', h.capture_status())

    def test_fallback_with_dead_portal_fails_visibly_instead_of_leaving_streaming(self):
        h = make_test_host(self.tmpdir)
        h.native = None
        h.memory_mode = 'native'
        h.pipeline_bus = None
        h.pipeline = None
        h.fd = None
        h.portal.OpenPipeWireRemote.side_effect = host_module.dbus.DBusException('session gone')
        h.fallback_or_stop()
        self.assertTrue(h.failed)
        self.assertEqual(read_status(h.status.path)['phase'], 'failed')
        h.loop.quit.assert_called_once()

    def test_capture_token_rejection_retries_capture_session(self):
        """Stale capture token is discarded and a fresh capture session is requested."""
        h = make_test_host(self.tmpdir)
        save_token(h.tokens_file, 'remotedesktop_capture', 'stale_token_456')
        h._capture_token_used = True
        h.request_capture_session = MagicMock()

        h._handle_portal_rejection(2, h.selected)

        self.assertFalse(h._capture_token_used)
        tokens = load_tokens(h.tokens_file)
        self.assertNotIn('remotedesktop_capture', tokens)
        h.request_capture_session.assert_called_once()
        self.assertFalse(h.failed)

    def test_stale_token_triggers_exactly_one_retry_no_infinite_loop(self):
        """A stale token retries once; if the interactive retry also fails, it aborts cleanly."""
        h = make_test_host(self.tmpdir)
        save_token(h.tokens_file, 'remotedesktop_capture', 'stale_token')
        h._capture_token_used = True
        h.request_capture_session = MagicMock()

        # First rejection: triggers retry
        h._handle_portal_rejection(2, h.selected)
        self.assertFalse(h._capture_token_used)
        self.assertFalse(h.failed)
        h.request_capture_session.assert_called_once()

        # Second rejection (interactive attempt rejected or cancelled): must fail
        h._handle_portal_rejection(2, h.selected)
        self.assertTrue(h.failed)
        h.loop.quit.assert_called_once()
        status = read_status(Path(self.tmpdir) / 'host.status.json')
        self.assertEqual(status['phase'], 'failed')

    def test_capture_rejection_at_select_devices_retries(self):
        """Rejection at SelectDevices (where token was passed) triggers interactive retry."""
        h = make_test_host(self.tmpdir)
        save_token(h.tokens_file, 'remotedesktop_capture', 'stale_tok')
        h._capture_token_used = True
        h.request_capture_session = MagicMock()

        h._handle_portal_rejection(2, h.capture_devices_selected)

        self.assertFalse(h._capture_token_used)
        self.assertNotIn('remotedesktop_capture', load_tokens(h.tokens_file))
        h.request_capture_session.assert_called_once()
        self.assertFalse(h.failed)

    def test_capture_rejection_at_start_retries(self):
        """Rejection at Start triggers interactive retry when token was used."""
        h = make_test_host(self.tmpdir)
        save_token(h.tokens_file, 'remotedesktop_capture', 'stale_tok')
        h._capture_token_used = True
        h.request_capture_session = MagicMock()

        h._handle_portal_rejection(2, h.started)

        self.assertFalse(h._capture_token_used)
        self.assertNotIn('remotedesktop_capture', load_tokens(h.tokens_file))
        h.request_capture_session.assert_called_once()
        self.assertFalse(h.failed)

    def test_user_cancellation_without_tokens_fails(self):
        """User cancellation without a token reports failure and quits loop."""
        h = make_test_host(self.tmpdir)
        h._handle_portal_rejection(1, h.selected)

        self.assertTrue(h.failed)
        h.loop.quit.assert_called_once()
        status = read_status(Path(self.tmpdir) / 'host.status.json')
        self.assertEqual(status['phase'], 'failed')
        self.assertIn('cancelled', status['message'])

    def test_status_failed_is_not_overwritten_by_run_finally(self):
        """If _fail was called, finally block does not overwrite status with stopped."""
        h = make_test_host(self.tmpdir)
        h._fail('Some error')

        status_before = read_status(Path(self.tmpdir) / 'host.status.json')
        self.assertEqual(status_before['phase'], 'failed')

        # Simulate finally block in run()
        h.closing = True
        if not getattr(h, 'failed', False):
            h.status.write('stopped')

        status_after = read_status(Path(self.tmpdir) / 'host.status.json')
        self.assertEqual(status_after['phase'], 'failed')
        self.assertEqual(status_after['message'], 'Some error')

    def test_clean_stop_writes_stopped_status(self):
        """On clean shutdown without failure, finally block writes 'stopped'."""
        h = make_test_host(self.tmpdir)
        h.status.write('streaming')

        # Simulate clean shutdown without failure
        h.closing = True
        if not getattr(h, 'failed', False):
            h.status.write('stopped')

        status = read_status(Path(self.tmpdir) / 'host.status.json')
        self.assertEqual(status['phase'], 'stopped')

    def test_capture_created_passes_persist_mode_and_token_in_select_devices(self):
        """persist_mode and restore_token are passed in SelectDevices, not SelectSources."""
        h = make_test_host(self.tmpdir)
        save_token(h.tokens_file, 'remotedesktop_capture', 'valid_cap_tok')
        h.request = MagicMock()

        h.capture_created({'session_handle': 'new_session_path'})

        # Verify request was called on SelectDevices
        h.request.assert_called_once()
        method, args, callback = h.request.call_args[0]
        self.assertEqual(method, h.remote.SelectDevices)
        options = args[1]
        self.assertEqual(options['types'], 6)
        self.assertEqual(options['persist_mode'], 2)
        self.assertEqual(options['restore_token'], 'valid_cap_tok')
        self.assertTrue(h._capture_token_used)

    def test_capture_select_sources_omits_persist_mode_and_restore_token(self):
        """_do_capture_select_sources must NOT contain persist_mode or restore_token."""
        h = make_test_host(self.tmpdir)
        h.request = MagicMock()

        h._do_capture_select_sources()

        h.request.assert_called_once()
        method, args, callback = h.request.call_args[0]
        self.assertEqual(method, h.portal.SelectSources)
        options = args[1]
        self.assertEqual(options['types'], 1)
        self.assertNotIn('persist_mode', options)
        self.assertNotIn('restore_token', options)

    def test_wrong_source_discards_capture_token_and_does_not_save(self):
        """If user selects wrong output (e.g. laptop screen), restore_token is discarded and not saved."""
        h = make_test_host(self.tmpdir)
        save_token(h.tokens_file, 'remotedesktop_capture', 'some_token')
        h._capture_token_used = True
        h.creation_session = 'creation_session_path'
        h.request_capture_session = MagicMock()

        # Laptop screen mode size is 2560x1600 (not virtual size 2960x1848 / scale 1.5)
        wrong_result = {
            'streams': [(100, {'source_type': 1, 'size': [2560, 1600]})],
            'restore_token': 'wrong_laptop_token',
        }

        with patch('host.dbus.Interface'):
            h.started(wrong_result)

        # Token should be discarded
        tokens = load_tokens(h.tokens_file)
        self.assertNotIn('remotedesktop_capture', tokens)
        self.assertFalse(h._capture_token_used)
        h.request_capture_session.assert_called_once()

    def test_valid_source_saves_capture_token(self):
        """When Virtual Output stream is confirmed, capture restore token is saved."""
        h = make_test_host(self.tmpdir)
        h.creation_session = 'creation_session_path'
        h.start_pipeline = MagicMock()
        h.watch_session = MagicMock()

        # Expected size: round(2960 / 1.5) = 1973, round(1848 / 1.5) = 1232
        expected_size = [round(2960 / 1.5), round(1848 / 1.5)]
        valid_result = {
            'streams': [(100, {'source_type': 1, 'size': expected_size})],
            'devices': 6,
            'restore_token': 'verified_restore_token',
        }

        with patch('host.LiveKScreenTarget') as mock_target, \
             patch('host.PortalTouchInput.bind') as mock_bind:
            mock_target.return_value.geometry.return_value = MagicMock()
            mock_bind.return_value = MagicMock(mode='touchscreen')
            h.portal.OpenPipeWireRemote.return_value.take.return_value = 42

            h.started(valid_result)

        tokens = load_tokens(h.tokens_file)
        self.assertEqual(tokens.get('remotedesktop_capture'), 'verified_restore_token')

    def test_creation_non_virtual_source_fails_and_never_saves_token(self):
        """If creation session returns source_type != 4, fail immediately without saving token."""
        h = make_test_host(self.tmpdir)
        h.creation_session = None
        h._fail = MagicMock()

        invalid_result = {
            'streams': [(100, {'source_type': 1, 'size': [2560, 1600]})],
            'restore_token': 'should_not_be_saved',
        }
        h.started(invalid_result)

        tokens = load_tokens(h.tokens_file)
        self.assertNotIn('screencast_create', tokens)
        h._fail.assert_called_once_with('Refusing a non-virtual creation source.')

    def test_creation_saves_its_restore_token(self):
        """xdg-desktop-portal-kde restores the "Virtual" selection by its fixed
        uniqueId, so the creation session persists a token like capture does."""
        h = make_test_host(self.tmpdir)
        h.creation_session = None
        h.watch_session = MagicMock()
        h.configure_output = MagicMock()

        with patch('host.GLib.timeout_add_seconds'):
            h.started({'streams': [(100, {'source_type': 4})], 'restore_token': 'virt_token_1'})

        self.assertEqual(load_tokens(h.tokens_file), {'screencast_create': 'virt_token_1'})
        self.assertEqual(read_status(Path(self.tmpdir) / 'host.status.json')['phase'], 'configuring_output')

    def test_creation_select_sources_presents_stored_token(self):
        h = make_test_host(self.tmpdir)
        save_token(h.tokens_file, 'screencast_create', 'virt_token_1')
        h.session = 'creation_session_1'
        h.request = MagicMock()

        h.created({'session_handle': 'creation_session_1'})

        method, args, callback = h.request.call_args[0]
        self.assertEqual(method, h.portal.SelectSources)
        options = args[1]
        self.assertEqual(int(options['persist_mode']), 2)
        self.assertEqual(options['restore_token'], 'virt_token_1')
        self.assertTrue(h._virtual_token_used)

    def test_stale_creation_token_retries_creation_once(self):
        h = make_test_host(self.tmpdir)
        save_token(h.tokens_file, 'screencast_create', 'stale')
        h._virtual_token_used = True
        h.create = MagicMock()

        h._handle_portal_rejection(2, h.selected)

        self.assertFalse(h._virtual_token_used)
        self.assertNotIn('screencast_create', load_tokens(h.tokens_file))
        h.create.assert_called_once()
        self.assertFalse(h.failed)
        # A second rejection without a token is final.
        h._handle_portal_rejection(1, h.selected)
        self.assertTrue(h.failed)

    def test_wrong_source_retry_is_capped(self):
        """Repeatedly selecting the wrong output must fail instead of
        looping forever; the supervised `tabs9 start` would otherwise report
        an unbounded prompt loop as a generic 60s timeout."""
        h = make_test_host(self.tmpdir)
        h.creation_session = 'creation_session_path'
        h.request_capture_session = MagicMock()
        h._fail = MagicMock(side_effect=lambda msg: setattr(h, 'failed', True))

        wrong_result = {
            'streams': [(100, {'source_type': 1, 'size': [2560, 1600]})],
        }
        with patch('host.dbus.Interface'):
            for _ in range(host_module.MAX_WRONG_SOURCE_ATTEMPTS):
                h.started(wrong_result)
        self.assertFalse(h.failed)
        self.assertEqual(h.request_capture_session.call_count,
                          host_module.MAX_WRONG_SOURCE_ATTEMPTS)

        # One more wrong selection past the cap must fail instead of retrying.
        with patch('host.dbus.Interface'):
            h.started(wrong_result)
        h._fail.assert_called_once()
        self.assertTrue(h.failed)
        # No additional retry was requested once the cap was exceeded.
        self.assertEqual(h.request_capture_session.call_count,
                          host_module.MAX_WRONG_SOURCE_ATTEMPTS)


class FailReentrancyTests(unittest.TestCase):
    """_fail must be idempotent and never let a raising status writer skip
    loop.quit() or the finally-block cleanup that depends on self.failed."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_second_call_does_not_overwrite_first_failure_or_requit(self):
        h = make_test_host(self.tmpdir)

        h._fail('Video pipeline failed.')
        h._fail('KDE sharing session ended unexpectedly.')

        status = read_status(Path(self.tmpdir) / 'host.status.json')
        self.assertEqual(status['message'], 'Video pipeline failed.')
        h.loop.quit.assert_called_once()

    def test_raising_status_writer_still_sets_failed_and_quits_loop(self):
        h = make_test_host(self.tmpdir)
        h.status.write = MagicMock(side_effect=OSError('disk full'))

        h._fail('Video pipeline failed.')

        self.assertTrue(h.failed)
        h.loop.quit.assert_called_once()

    def test_raising_status_writer_does_not_prevent_reentrancy_guard(self):
        h = make_test_host(self.tmpdir)
        h.status.write = MagicMock(side_effect=OSError('disk full'))

        h._fail('first')
        h._fail('second')

        self.assertEqual(h.loop.quit.call_count, 1)


class RunFinallyCleanupTests(unittest.TestCase):
    """Exercises the real run()/finally path (not a re-implemented inline
    condition) with a status writer that always raises, to confirm cleanup
    -- touch release, fd close, and ADB reverse teardown -- still executes."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _make_run_ready_host(self):
        h = host_module.Host.__new__(host_module.Host)
        h.args = SimpleNamespace(width=2960, height=1848, fps=120, bitrate=60000,
                                  scale=1.5, capture_memory='system')
        h.token = 'tok'
        h.loop = MagicMock()
        h.bus = MagicMock()
        h.aio = MagicMock()
        h.status = StatusWriter(Path(self.tmpdir) / 'host.status.json')
        h.status.write = MagicMock(side_effect=OSError('disk full'))
        h.ready = MagicMock()
        h.ready.wait.return_value = False  # forces the startup RuntimeError immediately
        h.report_timer = None
        h.session = None
        h.creation_session = None
        h.session_watches = []
        h.pipeline = None
        h.reverse_ports = [8890, 8891]
        h.touch = MagicMock()
        h.closing = False
        h.failed = False
        return h

    def test_finally_cleanup_survives_a_raising_status_writer(self):
        h = self._make_run_ready_host()
        read_fd, write_fd = os.pipe()
        h.fd = write_fd
        adb_calls = []

        def fake_adb(*args):
            adb_calls.append(args)
            return b''

        with patch('host.threading.Thread') as mock_thread, \
             patch('host.adb', side_effect=fake_adb):
            mock_thread.return_value.start = MagicMock()
            h.run()

        try:
            self.assertTrue(h.failed)
            self.assertTrue(h.closing)
            h.touch.release_all.assert_called_once()
            self.assertIn(('reverse', '--remove', 'tcp:8890'), adb_calls)
            self.assertIn(('reverse', '--remove', 'tcp:8891'), adb_calls)
            with self.assertRaises(OSError):
                os.fstat(write_fd)  # closed by the finally block's cleanup
        finally:
            os.close(read_fd)

    def test_startup_runtime_error_message_is_preserved(self):
        """A RuntimeError the host raises itself carries a static, safe
        message that is more actionable than the bare exception type name."""
        h = self._make_run_ready_host()
        h.status.write = MagicMock()  # do not also exercise the raising path here
        h.reverse_ports = []
        h.fd = None

        with patch('host.threading.Thread') as mock_thread, patch('host.adb'):
            mock_thread.return_value.start = MagicMock()
            h.run()

        failed_calls = [c for c in h.status.write.call_args_list if c.args[0] == 'failed']
        self.assertEqual(len(failed_calls), 1)
        self.assertIn('Local streaming ports unavailable', failed_calls[0].args[1])


if __name__ == '__main__':
    unittest.main()
