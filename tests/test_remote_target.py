"""Which host answers the remote-control shortcuts when several run."""
import fcntl
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

import remote_target  # noqa: E402
from remote_target import Instances  # noqa: E402


class InstancesTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.instances = Instances(self.dir)
        self.locks = []

    def tearDown(self):
        for handle in self.locks:
            handle.close()

    def host(self, slug, slot, label, mode='screen'):
        """A running host: its lock held and its instance file published."""
        handle = (self.dir / f'host-{slug}.lock').open('w')
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.locks.append(handle)
        self.instances.publish(slug, slot=slot, label=label, mode=mode)
        return handle

    def test_nothing_running_means_no_target(self):
        self.assertEqual(self.instances.running(), [])
        self.assertIsNone(self.instances.target())
        self.assertEqual(self.instances.describe('sm_x910'), '')

    def test_a_single_host_is_the_target_without_being_told(self):
        self.host('sm_x910', 1, 'SM X910')
        self.assertTrue(self.instances.is_target('sm_x910'))
        self.assertEqual(self.instances.describe('sm_x910'), '')   # nothing to say with one

    def test_the_first_started_host_wins_by_default(self):
        self.host('hmw_w09', 2, 'HMW W09')
        self.host('sm_x910', 1, 'SM X910')
        self.assertEqual([i['slug'] for i in self.instances.running()], ['sm_x910', 'hmw_w09'])
        self.assertTrue(self.instances.is_target('sm_x910'))
        self.assertFalse(self.instances.is_target('hmw_w09'))
        self.assertIn('Shortcuts drive this tablet; HMW W09 ignores them',
                      self.instances.describe('sm_x910'))
        self.assertIn('Shortcuts drive SM X910, not this tablet',
                      self.instances.describe('hmw_w09'))

    def test_a_choice_overrides_the_default_and_survives_a_restart(self):
        self.host('sm_x910', 1, 'SM X910')
        self.host('hmw_w09', 2, 'HMW W09')
        self.instances.choose('hmw_w09', 'HMW W09')
        self.assertTrue(self.instances.is_target('hmw_w09'))
        # A fresh object reads the same file.
        self.assertTrue(Instances(self.dir).is_target('hmw_w09'))
        self.instances.forget_choice()
        self.assertTrue(self.instances.is_target('sm_x910'))

    def test_a_chosen_tablet_that_is_not_running_falls_back(self):
        self.host('sm_x910', 1, 'SM X910')
        self.instances.choose('hmw_w09', 'HMW W09')
        self.assertTrue(self.instances.is_target('sm_x910'))

    def test_a_host_whose_lock_is_gone_is_not_running(self):
        handle = self.host('sm_x910', 1, 'SM X910')
        self.host('hmw_w09', 2, 'HMW W09')
        handle.close()          # the process died without retiring its file
        self.assertEqual([i['slug'] for i in self.instances.running()], ['hmw_w09'])
        self.assertTrue(self.instances.is_target('hmw_w09'))

    def test_retire_removes_the_file(self):
        self.host('sm_x910', 1, 'SM X910')
        self.instances.retire('sm_x910')
        self.assertEqual(self.instances.running(), [])

    def test_publish_carries_the_mode(self):
        self.host('sm_x910', 1, 'SM X910', mode='desktop')
        self.assertEqual(self.instances.running()[0]['mode'], 'desktop')

    def test_no_serial_is_written(self):
        self.host('sm_x910', 1, 'SM X910')
        self.instances.choose('sm_x910', 'SM X910')
        for path in self.dir.glob('*.json'):
            self.assertNotIn('serial', path.read_text())


class CliTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())

    def run_cli(self, *args):
        result = subprocess.run([sys.executable, str(ROOT / 'src/remote_target.py'),
                                 '--state-dir', str(self.dir), *args],
                                capture_output=True, text=True, timeout=30)
        return result.returncode, result.stdout + result.stderr

    def test_reports_when_nothing_runs(self):
        code, out = self.run_cli()
        self.assertEqual(code, 0)
        self.assertIn('No tablet display is running', out)

    def test_naming_a_running_host_by_slug_works_without_the_tablet_attached(self):
        instances = Instances(self.dir)
        handle = (self.dir / 'host-hmw_w09.lock').open('w')
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        instances.publish('hmw_w09', slot=2, label='HMW W09')
        try:
            with patch.dict(os.environ, {'PATH': ''}):   # no adb to consult
                code, out = self.run_cli('hmw-w09')
        finally:
            handle.close()
        self.assertEqual(code, 0, out)
        self.assertEqual(instances.chosen().get('slug'), 'hmw_w09')
        self.assertIn('* HMW W09', out)

    def test_an_unknown_name_is_refused(self):
        code, out = self.run_cli('nonesuch')
        self.assertEqual(code, 1)
        self.assertIn('No tablet matches', out)


class HostRoutingTests(unittest.TestCase):
    """Host.on_shortcut: only the target acts, tablet-screen restores the rest."""

    def setUp(self):
        import host as host_module
        self.host_module = host_module
        self.dir = Path(tempfile.mkdtemp())

    def make_host(self, slug, mode='screen'):
        host = self.host_module.Host.__new__(self.host_module.Host)
        host.instances = Instances(self.dir)
        host.instance = slug
        host.tablet_mode = mode
        host.calls = []
        host.cycle_tablet_mode = lambda: host.calls.append('cycle')
        host.tablet_as_screen = lambda quiet=False: host.calls.append(('screen', quiet))
        return host

    def test_alone_every_press_is_handled(self):
        host = self.make_host('sm_x910')
        host.on_shortcut('remote-control')
        host.on_shortcut('tablet-screen')
        self.assertEqual(host.calls, ['cycle', ('screen', False)])

    def test_only_the_target_takes_the_remote_control_press(self):
        first = self.make_host('sm_x910')
        second = self.make_host('hmw_w09')
        first.instances.choose('hmw_w09', 'HMW W09')
        for slug, slot in (('sm_x910', 1), ('hmw_w09', 2)):
            handle = (self.dir / f'host-{slug}.lock').open('w')
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.addCleanup(handle.close)
            first.instances.publish(slug, slot=slot, label=slug.upper())
        first.on_shortcut('remote-control')
        second.on_shortcut('remote-control')
        self.assertEqual(first.calls, [])
        self.assertEqual(second.calls, ['cycle'])

    def test_tablet_screen_puts_a_non_target_back_quietly(self):
        first = self.make_host('sm_x910', mode='desktop')
        second = self.make_host('hmw_w09')
        for slug, slot in (('sm_x910', 1), ('hmw_w09', 2)):
            handle = (self.dir / f'host-{slug}.lock').open('w')
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.addCleanup(handle.close)
            first.instances.publish(slug, slot=slot, label=slug.upper())
        first.instances.choose('hmw_w09', 'HMW W09')
        first.on_shortcut('tablet-screen')
        second.on_shortcut('tablet-screen')
        self.assertEqual(first.calls, [('screen', True)])     # not the target, but not a screen
        self.assertEqual(second.calls, [('screen', False)])
        # Already a screen and not the target: nothing to do.
        first.tablet_mode = 'screen'
        first.calls.clear()
        first.on_shortcut('tablet-screen')
        self.assertEqual(first.calls, [])


if __name__ == '__main__':
    unittest.main()
