"""Automated CLI tests for `tabs9`, using a fake `systemctl` on PATH and a
crafted status file so no real systemd unit, portal, or GUI is touched.

Covers the liveness/pid/freshness cross-check (`status_summary`) as seen
through the actual shell entry point, not just the Python unit underneath it.
"""

import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import time
import unittest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from status import StatusWriter  # noqa: E402


FAKE_SYSTEMCTL = """#!/usr/bin/env bash
# Minimal fake of the two `systemctl --user ...` invocations tabs9 makes.
if [[ "$*" == *"is-active"* ]]; then
  if [[ "${FAKE_UNIT_ACTIVE:-0}" == "1" ]]; then
    exit 0
  else
    exit 3
  fi
fi
if [[ "$*" == *"MainPID"* ]]; then
  if [[ "${FAKE_UNIT_ACTIVE:-0}" == "1" ]]; then
    echo "${FAKE_MAIN_PID:-0}"
  else
    echo "0"
  fi
  exit 0
fi
exit 0
"""


class TabsCliStatusTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.bin_dir = Path(self.tmpdir) / "bin"
        self.bin_dir.mkdir()
        fake_systemctl = self.bin_dir / "systemctl"
        fake_systemctl.write_text(FAKE_SYSTEMCTL)
        fake_systemctl.chmod(fake_systemctl.stat().st_mode | stat.S_IEXEC)

        self.status_file = Path(self.tmpdir) / "host.status.json"
        self.env = dict(os.environ)
        self.env["PATH"] = str(self.bin_dir) + os.pathsep + self.env["PATH"]
        self.env["TABS9_UNIT"] = "fake-unit.service"
        self.env["TABS9_STATUS_FILE"] = str(self.status_file)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def run_tabs9(self, *args, active, main_pid=None):
        env = dict(self.env)
        env["FAKE_UNIT_ACTIVE"] = "1" if active else "0"
        if main_pid is not None:
            env["FAKE_MAIN_PID"] = str(main_pid)
        return subprocess.run([str(REPO_ROOT / "tabs9"), *args], env=env,
                               capture_output=True, text=True, timeout=10)

    def write_status(self, phase, message="", pid=None, timestamp=None):
        writer = StatusWriter(self.status_file)
        writer.write(phase, message)
        if pid is not None or timestamp is not None:
            data = json.loads(self.status_file.read_text())
            if pid is not None:
                data["pid"] = pid
            if timestamp is not None:
                data["timestamp"] = timestamp
            self.status_file.write_text(json.dumps(data))

    def test_active_unit_matching_pid_reports_streaming(self):
        self.write_status("streaming", pid=4242)
        result = self.run_tabs9("status", active=True, main_pid=4242)
        self.assertIn("running (phase: streaming)", result.stdout)

    def test_active_unit_mismatched_pid_does_not_report_streaming(self):
        """A status file left by another process (e.g. bench-capture running
        outside systemd) must not be trusted just because *some* unit with
        this name happens to be active."""
        self.write_status("streaming", pid=4242)
        result = self.run_tabs9("status", active=True, main_pid=9999)
        self.assertIn("does not match this instance", result.stdout)
        self.assertNotIn("phase: streaming", result.stdout)

    def test_inactive_unit_with_streaming_status_is_reported_stale_not_streaming(self):
        """The exact contradiction from finding #1: the host died after
        reaching 'streaming' without writing 'stopped'. The unit is gone, so
        this must never be reported as a healthy streaming instance."""
        self.write_status("streaming", pid=4242)
        result = self.run_tabs9("status", active=False)
        self.assertIn("stale", result.stdout)
        self.assertNotIn("phase: streaming", result.stdout)

    def test_inactive_unit_with_failed_status_reports_last_failure(self):
        self.write_status("failed", "Video pipeline failed.", pid=4242)
        result = self.run_tabs9("status", active=False)
        self.assertIn("last failure: Video pipeline failed.", result.stdout)

    def test_inactive_unit_with_clean_stop_reports_stopped(self):
        self.write_status("stopped", pid=4242)
        result = self.run_tabs9("status", active=False)
        self.assertIn("stopped", result.stdout)
        self.assertNotIn("stale", result.stdout)

    def test_no_status_file_and_inactive_unit_reports_stopped(self):
        result = self.run_tabs9("status", active=False)
        self.assertIn("stopped", result.stdout)

    def test_old_leftover_streaming_status_with_no_active_unit_is_stale(self):
        old_timestamp = time.time() - 999999
        self.write_status("streaming", pid=4242, timestamp=old_timestamp)
        result = self.run_tabs9("status", active=False)
        self.assertIn("stale", result.stdout)


if __name__ == "__main__":
    unittest.main()
