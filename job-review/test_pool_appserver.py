"""Session-only native protocol checks against a fictional subprocess."""
import unittest

import test_codex_appserver as fixtures
from codex_transport import RANK_DISABLED_PLUGINS


class SessionOnlyAppServerTests(unittest.TestCase):
    setUp = fixtures.AppServerTests.setUp
    heartbeat = fixtures.AppServerTests.heartbeat
    approve = fixtures.AppServerTests.approve
    run_fixture = fixtures.AppServerTests.run_fixture
    received = fixtures.AppServerTests.received
    assert_failure = fixtures.AppServerTests.assert_failure

    def test_tool_free_existing_thread_can_complete_without_browser_receipt(self):
        result = self.run_fixture('no-browser', thread_id=fixtures.THREAD, require_browser=False)
        self.assertEqual(fixtures.THREAD, result.thread_id)
        self.assertTrue(result.turn_completed)
        self.assertFalse(result.browser_verified)
        self.assertEqual((), result.tools)
        resume = next(v for v in self.received() if v['method'] == 'thread/resume')
        for plugin in RANK_DISABLED_PLUGINS:
            self.assertIs(False, resume['params']['config'][f'plugins.{plugin}@openai-bundled.enabled'])
        # CUA is supplied by the disabled plugin. Creating an incomplete
        # standalone MCP entry rejects the real native resume configuration.
        self.assertNotIn('mcp_servers.cua_repl.enabled',resume['params']['config'])
        self.assertIn('tool-free', resume['params']['developerInstructions'])

    def test_unexpected_browser_tool_fails_closed(self):
        error = self.assert_failure('success', require_browser=False)
        self.assertTrue(error.uncertain)
        self.assertIn('tool or external action', str(error))

    def test_approval_callback_not_used_in_session_only_mode(self):
        error = self.assert_failure('elicitation', require_browser=False)
        self.assertEqual([], self.approvals)
        self.assertTrue(error.uncertain)

    def test_browser_mode_still_requires_real_browser_completion(self):
        self.assert_failure('no-browser')

    def test_historical_child_activity_fails_closed(self):
        error = self.assert_failure('preload-child-events', thread_id=fixtures.THREAD, require_browser=False)
        self.assertTrue(error.uncertain)
        self.assertIn('child activity', str(error))


if __name__ == '__main__':
    unittest.main()
