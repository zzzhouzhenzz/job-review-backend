import subprocess
import unittest
from unittest.mock import Mock, patch

from worker_power import prevent_idle_sleep


class WorkerPowerTests(unittest.TestCase):
    def test_assertion_lasts_only_for_active_call_even_when_call_fails(self):
        child = Mock()
        child.poll.return_value = None
        with patch('worker_power.sys.platform', 'darwin'), patch('worker_power.subprocess.Popen', return_value=child) as spawn:
            with self.assertRaises(RuntimeError):
                with prevent_idle_sleep(1234):
                    child.terminate.assert_not_called()
                    raise RuntimeError('Fictional worker failure')
            self.assertEqual(['/usr/bin/caffeinate', '-i', '-w', '1234'], spawn.call_args.args[0])
            child.terminate.assert_called_once()
            child.wait.assert_called_once_with(timeout=2)

    def test_non_mac_does_not_launch_a_guard(self):
        with patch('worker_power.sys.platform', 'linux'), patch('worker_power.subprocess.Popen') as spawn:
            with prevent_idle_sleep(1234):
                pass
            spawn.assert_not_called()

    def test_missing_guard_logs_safe_failure_and_work_continues(self):
        with patch('worker_power.sys.platform', 'darwin'), patch('worker_power.subprocess.Popen', side_effect=OSError('private')):
            with self.assertLogs('worker_power') as logs, prevent_idle_sleep(1234):
                pass
            self.assertIn('OSError', logs.output[0])
            self.assertNotIn('private', logs.output[0])

    def test_already_exited_guard_is_reaped_without_termination(self):
        child = Mock()
        child.poll.return_value = 0
        with patch('worker_power.sys.platform', 'darwin'), patch('worker_power.subprocess.Popen', return_value=child):
            with prevent_idle_sleep(1234):
                pass
            child.terminate.assert_not_called()
            child.wait.assert_called_once_with(timeout=2)

    def test_slow_guard_is_killed_and_reaped(self):
        child = Mock()
        child.poll.return_value = None
        child.wait.side_effect = [subprocess.TimeoutExpired('caffeinate', 2), 0]
        with patch('worker_power.sys.platform', 'darwin'), patch('worker_power.subprocess.Popen', return_value=child):
            with prevent_idle_sleep(1234):
                pass
            child.kill.assert_called_once()
            self.assertEqual(2, child.wait.call_count)


if __name__ == '__main__':
    unittest.main()
