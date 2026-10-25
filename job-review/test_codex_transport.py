"""Exercise the CLI boundary with a local fictional executable, never Codex."""
import concurrent.futures
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
import uuid
from unittest.mock import patch

from codex_transport import CodexTransport, CodexTransportError, CodexWorkerStopped, validate_result
from readiness import BrowserRecoverable, BrowserWorkUncertain, WorkerCapacityUnavailable


SCHEMA = {"type": "object", "properties": {"ok": {"type": "boolean"}},
          "required": ["ok"], "additionalProperties": False}


class Lease:
    def __init__(self, fail_after=None):
        self.calls = 0
        self.fail_after = fail_after
        self.task_id = str(uuid.uuid4())
        self.token = str(uuid.uuid4())
        self.checkpoints = []

    def heartbeat(self):
        self.calls += 1
        if self.fail_after is not None and self.calls > self.fail_after:
            raise RuntimeError("private lease token must never escape")

    def checkpoint(self, value):
        self.checkpoints.append(value)
        return True


class TransportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.binary = self.root / "codex-fixture"
        self.binary.write_text("#!" + sys.executable + "\n" + '''
import json,pathlib,subprocess,sys,time
args=sys.argv[1:]
output=pathlib.Path(args[args.index("--output-last-message")+1])
prompt=sys.stdin.read()
(output.parent/"fixture-argv.json").write_text(json.dumps(args))
print(json.dumps({"type":"turn.started"}),flush=True)
thread_id=args[-2] if "resume" in args else "11111111-1111-4111-8111-111111111111"
if "resume" not in args and output.parent.name.startswith("linkedin_session-"):
    thread_id="22222222-2222-4222-8222-222222222222"
if prompt.startswith("wrong-thread"): thread_id="33333333-3333-4333-8333-333333333333"
if not prompt.startswith("no-thread"):
    print(json.dumps({"type":"thread.started","thread_id":thread_id}),flush=True)
if prompt.startswith("double-thread"):
    print(json.dumps({"type":"thread.started","thread_id":thread_id}),flush=True)
if prompt.startswith("slow"):
    time.sleep(5)
if prompt.startswith("child"):
    marker=str(output.parent/"child-marker")
    subprocess.Popen([sys.executable,"-c","import pathlib,time; time.sleep(.6); pathlib.Path("+repr(marker)+").write_text('should not happen')"])
    time.sleep(5)
if prompt.startswith("error"):
    print("private secret from Codex",file=sys.stderr)
    sys.exit(2)
if prompt.startswith("config-error"):
    print("Error loading config.toml: invalid private configuration",file=sys.stderr)
    sys.exit(1)
if prompt.startswith("bad-stream"):
    print("private secret not valid JSON",flush=True)
    time.sleep(5)
if prompt.startswith("turn-error"):
    print(json.dumps({"type":"turn.failed","error":{"message":"Invalid schema with private candidate answer", "code":"invalid_json_schema"}}),flush=True)
    sys.exit(1)
if prompt.startswith("capacity"):
    if prompt == "capacity-action":
        print(json.dumps({"type":"item.completed","item":{"type":"command_execution"}}),flush=True)
    if prompt == "capacity-mixed":
        print(json.dumps({"type":"error","message":"Permission denied"}),flush=True)
    if prompt == "capacity-stderr":
        print("Error loading config.toml: private configuration",file=sys.stderr)
    if prompt == "capacity-completed":
        print(json.dumps({"type":"turn.completed"}),flush=True)
    message="Selected model is at capacity. Please try a different model."
    if prompt == "capacity-unknown": message="A private model report mentions at capacity"
    print(json.dumps({"type":"error" if prompt == "capacity-error-only" else "turn.failed","error":{"message":message}}),flush=True)
    if prompt == "capacity-truncated": print('{"type":',end="",flush=True)
    sys.exit(1)
result={"ok":True}
if prompt.startswith("extra"): result["secret"]="private result"
if prompt.startswith("duplicate"):
    output.write_text('{"ok": true, "ok": false}')
else:
    output.write_text(json.dumps(result))
if not prompt.startswith("fabricated"):
    print(json.dumps({"type":"item.completed","item":{"type":"mcp_tool_call","server":"cua_repl","tool":"js","status":"completed","error":None,"arguments":{"secret":"must not be logged"},"result":{"secret":"must not be logged", "isError":prompt.startswith("tool-error")}}}),flush=True)
if not prompt.startswith("incomplete"):
    print(json.dumps({"type":"turn.completed"}),flush=True)
''')
        self.binary.chmod(0o700)
        self.run_dir = self.root / "runs"
        self.transports = []

    def tearDown(self):
        for transport in self.transports:
            transport.close()
        self.temp.cleanup()

    def transport(self, **kwargs):
        value = CodexTransport(self.binary, self.run_dir, heartbeat_interval=kwargs.pop("heartbeat_interval", .05),
                               cwd=self.root, **kwargs)
        self.transports.append(value)
        return value

    def wait_for_fixture_start(self):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if list(self.run_dir.glob("*/fixture-argv.json")):
                return
            time.sleep(.01)
        self.fail("Fictional worker did not start")

    def runs(self):
        return list(self.run_dir.glob("*-*"))

    def independent_browser_case(self):
        # An uncertain native turn deliberately keeps its Chrome gate blocked.
        # A separate failure scenario must not reuse that unresolved owner.
        self.run_dir = self.root / ('runs-' + str(uuid.uuid4()))

    def test_result_is_validated_and_inputs_are_private_and_not_in_argv(self):
        lease = Lease()
        lease.task_id = "22222222-2222-4222-8222-222222222222"
        result = self.transport().run("rank", "private candidate answer", SCHEMA, lease)
        self.assertEqual({"ok": True}, result)
        self.assertGreaterEqual(lease.calls, 2)
        run, = self.runs()
        for path in (run, run / "prompt.txt", run / "schema.json", run / "result.json", run / "receipt.json"):
            self.assertEqual(0, path.stat().st_mode & 0o077)
        argv = json.loads((run / "fixture-argv.json").read_text())
        self.assertNotIn("private candidate answer", " ".join(argv))
        self.assertEqual("read-only", argv[argv.index("--sandbox") + 1])
        self.assertIn("--no-daemon", argv)
        self.assertNotIn("--model", argv)
        self.assertIn('plugins.unified-computer-use@openai-bundled.enabled=false', argv)
        self.assertIn('mcp_servers.node_repl.enabled=false', argv)
        receipt = (run / "receipt.json").read_text()
        self.assertNotIn("secret", receipt)
        self.assertEqual([{"server": "cua_repl", "tool": "js"}], json.loads(receipt)["tools"])
        self.assertEqual(lease.task_id, json.loads(receipt)["task_id"])
        self.assertEqual("11111111-1111-4111-8111-111111111111", json.loads(receipt)["thread_id"])
        self.assertFalse((run / "stderr.log").exists())

    def test_schema_failure_duplicate_keys_and_incomplete_turn_are_not_success(self):
        for prompt in ("extra", "duplicate", "incomplete", "error"):
            with self.subTest(prompt=prompt):
                with self.assertRaises(CodexTransportError) as error:
                    self.transport().run("rank", prompt, SCHEMA, Lease())
                self.assertNotIn("private", str(error.exception))

    def test_rechecks_lease_after_completion_before_import(self):
        with self.assertRaisesRegex(CodexTransportError, "lease renewal failed"):
            self.transport().run("rank", "ok", SCHEMA, Lease(fail_after=1))

    def test_timeout_stops_child_process_group_and_is_safe(self):
        with self.assertRaisesRegex(CodexTransportError, "timed out"):
            self.transport(timeout=.15).run("rank", "child", SCHEMA, Lease())
        time.sleep(.65)
        self.assertEqual([], list(self.run_dir.glob("*/child-marker")))

    def test_long_call_renews_lease_and_stops_when_fence_is_lost(self):
        lease = Lease(fail_after=2)
        with self.assertRaisesRegex(CodexTransportError, "lease renewal failed"):
            self.transport().run("rank", "slow", SCHEMA, lease)
        self.assertEqual(3, lease.calls)

    def test_close_cancels_an_active_invocation(self):
        transport = self.transport()
        with concurrent.futures.ThreadPoolExecutor() as pool:
            future = pool.submit(transport.run, "rank", "child", SCHEMA, Lease())
            self.wait_for_fixture_start()
            transport.close()
            with self.assertRaisesRegex(CodexWorkerStopped, "cancelled"):
                future.result(timeout=3)
        run, = self.runs()
        receipt = json.loads((run / "receipt.json").read_text())
        self.assertEqual("stopped", receipt["status"])
        self.assertIsNotNone(receipt["exit_code"])
        time.sleep(.65)
        self.assertFalse((run / "child-marker").exists())
        with self.assertRaisesRegex(CodexWorkerStopped, "closed"):
            transport.run("rank", "ok", SCHEMA, Lease())

    def test_closed_or_cancelled_before_spawn_is_safe_for_browser_role(self):
        transport = self.transport()
        transport.close()
        with patch("codex_transport.subprocess.Popen") as spawn:
            with self.assertRaises(CodexWorkerStopped):
                transport.run("explore", "ok", SCHEMA, Lease(), browser=True)
            spawn.assert_not_called()
        transport = self.transport()
        class StopDuringHeartbeat:
            def heartbeat(self):
                transport.close()
            task_id = str(uuid.uuid4())
            token = str(uuid.uuid4())
            checkpoint = lambda self, value: True
        with patch("codex_transport.subprocess.Popen") as spawn:
            with self.assertRaises(CodexWorkerStopped):
                transport.run("explore", "ok", SCHEMA, StopDuringHeartbeat(), browser=True)
            spawn.assert_not_called()
        self.assertEqual([], self.runs())
        self.assertEqual('free', transport.chrome_access_gate.snapshot()['state'])
        self.assertEqual([], transport.chrome_access_gate.snapshot()['queue'])

    def test_cancelling_started_browser_stays_uncertain(self):
        from readiness import BrowserWorkUncertain
        transport = self.transport()
        with concurrent.futures.ThreadPoolExecutor() as pool:
            future = pool.submit(transport.run, "explore", "slow", SCHEMA, Lease(), browser=True)
            self.wait_for_fixture_start()
            transport.close()
            with self.assertRaises(BrowserWorkUncertain):
                future.result(timeout=3)
        run, = self.runs()
        self.assertEqual("failed", json.loads((run / "receipt.json").read_text())["status"])

    def test_unconfirmed_local_termination_is_not_a_safe_stop(self):
        transport = self.transport()
        processes = []
        def leave_running(process):
            processes.append(process)
        try:
            with patch.object(transport, "_stop", side_effect=leave_running), concurrent.futures.ThreadPoolExecutor() as pool:
                future = pool.submit(transport.run, "rank", "slow", SCHEMA, Lease())
                self.wait_for_fixture_start()
                transport.close()
                with self.assertRaisesRegex(CodexTransportError, "termination could not be confirmed") as caught:
                    future.result(timeout=3)
                self.assertNotIsInstance(caught.exception, CodexWorkerStopped)
        finally:
            for process in processes:
                CodexTransport._stop(process)

    def test_browser_timeout_lease_loss_and_broken_stream_remain_uncertain(self):
        from readiness import BrowserWorkUncertain
        for prompt, lease in (("slow", Lease()), ("slow", Lease(2)), ("bad-stream", Lease())):
            with self.subTest(prompt=prompt, fail_after=lease.fail_after):
                self.independent_browser_case()
                with self.assertRaises(BrowserWorkUncertain):
                    self.transport(timeout=.15).run("explore", prompt, SCHEMA, lease, browser=True)

    def test_unstarted_browser_worker_does_not_claim_uncertain_external_actions(self):
        with self.assertRaises(CodexTransportError):
            self.transport().run("explore", "ok", SCHEMA, Lease(0), browser=True)
        self.assertEqual([], self.runs())

    def test_browser_success_requires_a_successful_plugin_call(self):
        from readiness import BrowserWorkUncertain
        self.assertEqual({"ok": True}, self.transport().run("explore", "ok", SCHEMA, Lease(), browser=True))
        with self.assertRaises(CodexTransportError):
            self.transport().run("explore", "fabricated", SCHEMA, Lease(), browser=True)
        with self.assertRaises(BrowserWorkUncertain):
            self.transport().run("explore", "tool-error", SCHEMA, Lease(), browser=True)

    def test_pretool_turn_failure_has_fixed_safe_diagnostic_category(self):
        with self.assertRaisesRegex(CodexTransportError, "invalid_json_schema") as error:
            self.transport().run("explore", "turn-error", SCHEMA, Lease(), browser=True)
        self.assertNotIn("private", str(error.exception))
        run, = self.runs()
        receipt = json.loads((run / "receipt.json").read_text())
        self.assertEqual(["invalid_json_schema"], receipt["failure_categories"])
        self.assertFalse(receipt["browser_verified"])
        self.assertEqual(1, receipt["exit_code"])

    def test_pretool_configuration_error_is_sanitized_without_stderr_log(self):
        with self.assertRaisesRegex(CodexTransportError, "invalid_configuration") as error:
            self.transport().run("explore", "config-error", SCHEMA, Lease(), browser=True)
        self.assertNotIn("private", str(error.exception))
        run, = self.runs()
        receipt = json.loads((run / "receipt.json").read_text())
        self.assertIn("invalid_configuration", receipt["failure_categories"])
        self.assertGreater(receipt["stderr_bytes"], 0)

    def test_rejects_public_or_symlink_artifact_directory(self):
        self.run_dir.mkdir(mode=0o755)
        with self.assertRaises(ValueError):
            self.transport()
        link = self.root / "link"
        link.symlink_to(self.run_dir, target_is_directory=True)
        with self.assertRaises(ValueError):
            CodexTransport(self.binary, link)

    def test_schema_rejects_booleans_as_integer_and_enforces_nested_bounds(self):
        schema = {"type": "array", "maxItems": 3, "items": {"type": "integer", "minimum": 1}}
        validate_result([1, 2, 3], schema)
        for value in ([True], [0], [1, 2, 3, 4]):
            with self.assertRaises(ValueError):
                validate_result(value, schema)

    def test_nullable_material_fields_and_multiple_types_are_validated(self):
        schema = {"type": ["string", "array", "null"], "items": {"type": "string"}}
        for value in (None, "reason", ["reason"]):
            validate_result(value, schema)
        for value in ([3], False, {}):
            with self.assertRaises(ValueError):
                validate_result(value, schema)

    def argv_for(self, role):
        run, = self.run_dir.glob(role + "-*")
        return json.loads((run / "fixture-argv.json").read_text())

    def session_record(self, lease):
        return self.run_dir / "_browser_sessions" / (lease.task_id + ".json")

    def test_browser_turns_resume_same_task_session_across_roles_and_restart(self):
        lease = Lease()
        first = self.transport()
        first.run("browser_fixture", "ok", SCHEMA, lease, browser=True)
        first.close()
        new_lease = Lease()
        new_lease.task_id = lease.task_id
        second = self.transport()
        second.run("application_prepare", "ok", SCHEMA, new_lease, browser=True)
        second.run("application_submit", "ok", SCHEMA, new_lease, browser=True)
        initial = self.argv_for("browser_fixture")
        self.assertNotIn("resume", initial)
        self.assertNotIn("--ephemeral", initial)
        for role in ("application_prepare", "application_submit"):
            argv = self.argv_for(role)
            self.assertIn("resume", argv)
            self.assertEqual("11111111-1111-4111-8111-111111111111", argv[-2])
            self.assertEqual("-", argv[-1])
            self.assertNotIn("--ephemeral", argv)
            self.assertNotIn("--last", argv)
            self.assertEqual("never", argv[argv.index("-a") + 1])
            self.assertEqual("read-only", argv[argv.index("--sandbox") + 1])
            self.assertLess(argv.index("--sandbox"), argv.index("resume"))
        record = json.loads(self.session_record(lease).read_text())
        self.assertEqual("ready", record["status"])
        self.assertTrue(record["receipt"].startswith("application_submit-"))
        self.assertEqual([], new_lease.checkpoints)
        self.assertEqual(0, self.session_record(lease).stat().st_mode & 0o077)
        self.assertNotIn(lease.token, json.dumps(record))

    def test_browser_different_tasks_and_nonbrowser_roles_never_share_session(self):
        lease, other = Lease(), Lease()
        transport = self.transport()
        transport.run("browser_fixture", "ok", SCHEMA, lease, browser=True)
        transport.run("explore", "ok", SCHEMA, other, browser=True)
        transport.run("rank", "ok", SCHEMA, lease)
        transport.run("application_question_review", "ok", SCHEMA, lease)
        self.assertNotIn("resume", self.argv_for("explore"))
        for role in ("rank", "application_question_review"):
            self.assertNotIn("resume", self.argv_for(role))
            self.assertIn("--ephemeral", self.argv_for(role))
        self.assertEqual([], lease.checkpoints)

    def test_no_tool_failure_thread_is_not_adopted(self):
        lease = Lease()
        transport = self.transport()
        with self.assertRaises(CodexTransportError):
            transport.run("browser_fixture", "turn-error", SCHEMA, lease, browser=True)
        self.assertFalse(self.session_record(lease).exists())
        transport.run("application_prepare", "ok", SCHEMA, lease, browser=True)
        self.assertNotIn("resume", self.argv_for("application_prepare"))

    def test_tool_free_completion_cannot_claim_browser_but_can_retry_safely(self):
        lease = Lease()
        transport = self.transport()
        with self.assertRaises(CodexTransportError):
            transport.run("browser_fixture", "fabricated", SCHEMA, lease, browser=True)
        self.assertFalse(self.session_record(lease).exists())
        transport.run("application_prepare", "ok", SCHEMA, lease, browser=True)
        self.assertNotIn("resume", self.argv_for("application_prepare"))

    def test_uncertain_turn_cannot_resume_after_transport_restart(self):
        from readiness import BrowserWorkUncertain
        lease = Lease()
        first = self.transport(timeout=.15)
        with self.assertRaises(BrowserWorkUncertain):
            first.run("browser_fixture", "slow", SCHEMA, lease, browser=True)
        first.close()
        self.assertEqual("active", json.loads(self.session_record(lease).read_text())["status"])
        self.assertEqual('uncertain', first.chrome_access_gate.snapshot()['state'])
        self.assertEqual(lease.task_id, first.chrome_access_gate.snapshot()['owner']['task_id'])
        with patch("codex_transport.subprocess.Popen") as spawn:
            with self.assertRaises(BrowserWorkUncertain):
                self.transport().run("application_prepare", "ok", SCHEMA, lease, browser=True)
            spawn.assert_not_called()

    def test_stale_lease_does_not_adopt_completed_browser_session(self):
        from readiness import BrowserWorkUncertain
        lease = Lease(fail_after=2)  # Chrome admission and pre-spawn checks pass.
        with self.assertRaisesRegex(BrowserWorkUncertain, "lease renewal failed"):
            self.transport(heartbeat_interval=.5).run("browser_fixture", "ok", SCHEMA, lease, browser=True)
        self.assertEqual("active", json.loads(self.session_record(lease).read_text())["status"])
        self.assertEqual([], lease.checkpoints)

    def test_lost_final_fence_does_not_publish_session_ready(self):
        from readiness import BrowserWorkUncertain
        lease = Lease(fail_after=3)  # Also pass the post-result lease check.
        with self.assertRaisesRegex(BrowserWorkUncertain, "completion could not be fenced"):
            self.transport(heartbeat_interval=.5).run("browser_fixture", "ok", SCHEMA, lease, browser=True)
        self.assertEqual("active", json.loads(self.session_record(lease).read_text())["status"])

    def test_checkpoint_and_payload_thread_ids_cannot_choose_session(self):
        lease = Lease()
        lease.thread_id = "33333333-3333-4333-8333-333333333333"
        lease.payload = {"thread_id": lease.thread_id}
        lease.checkpoints = [{"browser_session_receipt": "forged", "thread_id": lease.thread_id}]
        self.transport().run("browser_fixture", "ok", SCHEMA, lease, browser=True)
        self.assertNotIn("resume", self.argv_for("browser_fixture"))
        self.assertEqual("11111111-1111-4111-8111-111111111111",
                         json.loads(self.session_record(lease).read_text())["thread_id"])
        self.assertEqual(1, len(lease.checkpoints))

    def test_resumed_thread_identity_must_match_host_receipt(self):
        from readiness import BrowserWorkUncertain
        lease = Lease()
        transport = self.transport()
        transport.run("browser_fixture", "ok", SCHEMA, lease, browser=True)
        with self.assertRaisesRegex(BrowserWorkUncertain, "identity did not match"):
            transport.run("application_prepare", "wrong-thread", SCHEMA, lease, browser=True)
        self.assertEqual("active", json.loads(self.session_record(lease).read_text())["status"])

    def test_missing_or_duplicate_thread_events_are_not_adopted(self):
        from readiness import BrowserWorkUncertain
        for prompt in ("no-thread", "double-thread"):
            self.independent_browser_case()
            lease = Lease()
            with self.subTest(prompt=prompt), self.assertRaises(BrowserWorkUncertain):
                self.transport().run("explore", prompt, SCHEMA, lease, browser=True)
            self.assertEqual("active", json.loads(self.session_record(lease).read_text())["status"])

    def test_saved_session_must_match_private_completed_receipt(self):
        lease = Lease()
        transport = self.transport()
        transport.run("browser_fixture", "ok", SCHEMA, lease, browser=True)
        path = self.session_record(lease)
        record = json.loads(path.read_text())
        record["thread_id"] = "33333333-3333-4333-8333-333333333333"
        path.write_text(json.dumps(record))
        with patch("codex_transport.subprocess.Popen") as spawn:
            with self.assertRaisesRegex(CodexTransportError, "record could not be verified"):
                transport.run("application_prepare", "ok", SCHEMA, lease, browser=True)
            spawn.assert_not_called()

    def test_public_session_record_or_symlink_receipt_is_not_resumed(self):
        lease = Lease()
        transport = self.transport()
        transport.run("browser_fixture", "ok", SCHEMA, lease, browser=True)
        path = self.session_record(lease)
        path.chmod(0o644)
        with patch("codex_transport.subprocess.Popen") as spawn:
            with self.assertRaises(CodexTransportError):
                transport.run("application_prepare", "ok", SCHEMA, lease, browser=True)
            spawn.assert_not_called()
        path.chmod(0o600)
        receipt = self.run_dir / json.loads(path.read_text())["receipt"] / "receipt.json"
        other = receipt.with_name("other.json")
        receipt.rename(other)
        receipt.symlink_to(other)
        with patch("codex_transport.subprocess.Popen") as spawn:
            with self.assertRaises(CodexTransportError):
                transport.run("application_prepare", "ok", SCHEMA, lease, browser=True)
            spawn.assert_not_called()

    def test_same_task_cannot_start_two_simultaneous_browser_workers(self):
        from readiness import BrowserWorkUncertain
        lease = Lease()
        first = self.transport(timeout=.4)
        second = self.transport()
        with concurrent.futures.ThreadPoolExecutor() as pool:
            future = pool.submit(first.run, "browser_fixture", "slow", SCHEMA, lease, browser=True)
            self.wait_for_fixture_start()
            with self.assertRaises(BrowserRecoverable) as blocked:
                second.run("application_prepare", "ok", SCHEMA, lease, browser=True)
            self.assertEqual('browser_session_unavailable', blocked.exception.reason)
            with self.assertRaises(BrowserWorkUncertain):
                future.result(timeout=3)

    def test_no_tool_failure_during_resume_preserves_previous_safe_session(self):
        lease = Lease()
        transport = self.transport()
        transport.run("browser_fixture", "ok", SCHEMA, lease, browser=True)
        previous = self.session_record(lease).read_text()
        with self.assertRaises(CodexTransportError):
            transport.run("application_prepare", "config-error", SCHEMA, lease, browser=True)
        self.assertEqual(previous, self.session_record(lease).read_text())
        transport.run("explore", "ok", SCHEMA, lease, browser=True)
        self.assertIn("resume", self.argv_for("explore"))

    def test_capacity_before_tools_preserves_browser_session_and_private_diagnostics(self):
        lease = Lease()
        transport = self.transport()
        transport.run('browser_fixture', 'ok', SCHEMA, lease, browser=True)
        previous = self.session_record(lease).read_text()
        with self.assertRaises(WorkerCapacityUnavailable):
            transport.run('application_prepare', 'capacity', SCHEMA, lease, browser=True)
        self.assertEqual(previous, self.session_record(lease).read_text())
        receipt = json.loads(next(self.run_dir.glob('application_prepare-*/receipt.json')).read_text())
        self.assertEqual(['model_at_capacity'], receipt['failure_categories'])
        self.assertEqual([], receipt['tools'])
        self.assertEqual(1, receipt['exit_code'])
        self.assertFalse(receipt['turn_completed'])
        self.assertNotIn('Please try', json.dumps(receipt))
        transport.run('explore', 'ok', SCHEMA, lease, browser=True)
        self.assertIn('resume', self.argv_for('explore'))

    def test_capacity_requires_exact_complete_pretool_failure(self):
        for suffix in ('action', 'mixed', 'stderr', 'completed', 'unknown', 'truncated', 'error-only'):
            with self.subTest(suffix=suffix):
                self.independent_browser_case()
                expected = BrowserWorkUncertain if suffix in ('action', 'truncated') else CodexTransportError
                with self.assertRaises(expected):
                    self.transport().run('explore', 'capacity-' + suffix, SCHEMA, Lease(), browser=True)
        with self.assertRaises(WorkerCapacityUnavailable):
            self.transport().run('rank', 'capacity', SCHEMA, Lease())

    def test_readiness_never_resumes_or_overwrites_application_session(self):
        lease = Lease()
        transport = self.transport()
        transport.run('application_prepare', 'ok', SCHEMA, lease, browser=True)
        application = self.session_record(lease).read_text()
        transport.run('linkedin_session', 'ok', SCHEMA, lease, browser=True)
        self.assertNotIn('resume', self.argv_for('linkedin_session'))
        self.assertEqual(application, self.session_record(lease).read_text())
        readiness = self.session_record(lease).with_suffix('.readiness.json')
        self.assertEqual('22222222-2222-4222-8222-222222222222', json.loads(readiness.read_text())['thread_id'])
        transport.close()
        restarted = self.transport()
        restarted.run('application_submit', 'ok', SCHEMA, lease, browser=True)
        self.assertEqual('11111111-1111-4111-8111-111111111111', self.argv_for('application_submit')[-2])
        with self.assertRaises(WorkerCapacityUnavailable):
            restarted.run('linkedin_session', 'capacity', SCHEMA, lease, browser=True)
        self.assertEqual('22222222-2222-4222-8222-222222222222', json.loads(readiness.read_text())['thread_id'])
        self.assertEqual('11111111-1111-4111-8111-111111111111', json.loads(self.session_record(lease).read_text())['thread_id'])

    def test_readiness_and_application_share_lock_and_uncertainty_fence(self):
        for first_role, second_role in (('linkedin_session', 'application_prepare'),
                                         ('application_prepare', 'linkedin_session')):
            with self.subTest(first_role=first_role):
                self.independent_browser_case()
                lease = Lease()
                first = self.transport(timeout=.4)
                second = self.transport()
                with concurrent.futures.ThreadPoolExecutor() as pool:
                    future = pool.submit(first.run, first_role, 'slow', SCHEMA, lease, browser=True)
                    deadline = time.monotonic() + 3
                    record = self.session_record(lease)
                    if first_role == 'linkedin_session':
                        record = record.with_suffix('.readiness.json')
                    while not record.exists() and time.monotonic() < deadline:
                        time.sleep(.01)
                    self.assertTrue(record.exists())
                    with self.assertRaises(BrowserRecoverable) as blocked:
                        second.run(second_role, 'ok', SCHEMA, lease, browser=True)
                    self.assertEqual('browser_session_unavailable', blocked.exception.reason)
                    with self.assertRaises(BrowserWorkUncertain):
                        future.result(timeout=3)
                with patch('codex_transport.subprocess.Popen') as spawn:
                    self.assertEqual('uncertain', first.chrome_access_gate.snapshot()['state'])
                    with self.assertRaises(BrowserWorkUncertain):
                        self.transport().run(second_role, 'ok', SCHEMA, lease, browser=True)
                    spawn.assert_not_called()


if __name__ == "__main__":
    unittest.main()
