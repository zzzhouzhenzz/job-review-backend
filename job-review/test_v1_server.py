"""HTTP contract tests. Every server uses an isolated temporary database."""
import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from server import MAX_BODY, PUBLIC_TASK_FIELDS, ReviewServer
from store import ReviewError, digest
from test_store import catalog


class VersionedServerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.server = ReviewServer(("127.0.0.1", 0), self.root / "state.sqlite3", self.root)
        self.server.store.import_catalog(catalog(), "v1 HTTP tests")
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_port

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.temp.cleanup()

    def request(self, method, path, value=None, *, raw=None, headers=None):
        standard = {"Content-Type": "application/json",
                    "Origin": f"http://127.0.0.1:{self.port}",
                    "X-Review-Token": self.server.token}
        standard.update(headers or {})
        body = raw if raw is not None else (json.dumps(value) if value is not None else None)
        client = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            client.request(method, path, body, standard)
            response = client.getresponse()
            return response.status, json.loads(response.read())
        finally:
            client.close()

    def assert_error(self, result, status, code, commit_status="not_committed"):
        actual_status, body = result
        self.assertEqual(actual_status, status)
        self.assertEqual(body["schema_version"], 1)
        self.assertEqual(body["error"]["code"], code)
        self.assertEqual(body["error"]["commit_status"], commit_status)
        self.assertIsInstance(body["error"]["message"], str)

    def test_reads_are_complete_independent_snapshots_with_content_revision(self):
        for resource in ("capabilities", "feed", "processes", "questions", "tasks"):
            with self.subTest(resource=resource):
                status, body = self.request("GET", f"/api/v1/{resource}")
                self.assertEqual(status, 200)
                self.assertEqual(body["schema_version"], 1)
                self.assertTrue(body["complete"])
                self.assertIsNone(body["next_cursor"])
                self.assertEqual(body["token"], self.server.token)
                data = {key: value for key, value in body.items()
                        if key not in {"schema_version", "revision", "complete", "next_cursor", "token"}}
                self.assertEqual(body["revision"], digest(data))
                self.assertIn("cards" if resource == "feed" else resource, data)

    def test_capability_discovery_exposes_disabled_worker(self):
        status, body = self.request("GET", "/api/v1/capabilities")
        self.assertEqual(status, 200)
        self.assertEqual(body["capabilities"]["api_version"], 1)
        self.assertFalse(body["capabilities"]["worker_enabled"])
        self.assertEqual(body["capabilities"]["worker_status"], "not_configured")
        self.assertEqual(self.request("GET", "/api/v1/capabilities")[1], body)

    def test_auth_failure_uses_v1_error_and_never_invokes_command(self):
        operation = Mock()
        self.server.store.receive_decision = operation
        for headers in ({"Host": "evil.example"}, {"Origin": "https://evil.example"},
                        {"X-Review-Token": "wrong"}, {"X-Review-Token": "é"}):
            with self.subTest(headers=headers):
                self.assert_error(self.request("POST", "/api/v1/decisions", {}, headers=headers),
                                  403, "forbidden")
        operation.assert_not_called()
        self.assert_error(self.request("GET", "/api/v1/feed", headers={"Host": "evil.example"}),
                          403, "forbidden")

    def test_commands_return_ack_without_reading_mutable_state(self):
        acknowledgement = {"request_id": "test-request", "accepted": True, "task_ids": ["task-1"]}
        payload = {"request_id": "test-request"}
        self.server.store.state = Mock(side_effect=AssertionError("No post-commit state read"))
        self.server.store.feed = Mock(side_effect=AssertionError("No post-commit feed read"))
        for path, method in (("/api/v1/decisions", "receive_decision"),
                             ("/api/v1/answers", "answer_questions"),
                             ("/api/v1/tasks/cancel", "cancel_task_command")):
            operation = Mock(return_value=acknowledgement)
            setattr(self.server.store, method, operation)
            expected = {"schema_version": 1, "token": self.server.token, **acknowledgement}
            self.assertEqual(self.request("POST", path, payload), (200, expected))
            self.assertEqual(self.request("POST", path, payload), (200, expected))
            self.assertEqual(operation.call_count, 2)
            operation.assert_called_with(payload)

    def test_invalid_body_and_headers_fail_before_command(self):
        operation = Mock()
        self.server.store.receive_decision = operation
        for raw, headers in (("[]", {}), ("{", {}), ('{"score": NaN}', {}),
                             (b"\xff", {}), ("{}", {"Content-Length": "invalid"}),
                             ("{}", {"Content-Length": str(MAX_BODY + 1)}),
                             ("{}", {"Transfer-Encoding": "chunked"}),
                             ("{}", {"Content-Type": "text/plain"})):
            with self.subTest(raw=raw, headers=headers):
                self.assert_error(self.request("POST", "/api/v1/decisions", raw=raw, headers=headers),
                                  400, "invalid_request")
        operation.assert_not_called()

    def test_duplicate_content_length_is_rejected(self):
        operation = Mock()
        self.server.store.receive_decision = operation
        client = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            client.putrequest("POST", "/api/v1/decisions")
            client.putheader("Content-Type", "application/json")
            client.putheader("Origin", f"http://127.0.0.1:{self.port}")
            client.putheader("X-Review-Token", self.server.token)
            client.putheader("Content-Length", "2")
            client.putheader("Content-Length", "2")
            client.endheaders(b"{}")
            response = client.getresponse()
            self.assert_error((response.status, json.loads(response.read())), 400, "invalid_request")
        finally:
            client.close()
        operation.assert_not_called()

    def test_validation_conflict_is_definitive_but_unexpected_failure_is_unknown(self):
        for status, code in ((400, "invalid_request"), (404, "not_found"), (409, "conflict")):
            self.server.store.receive_decision = Mock(side_effect=ReviewError("Invalid command", status))
            self.assert_error(self.request("POST", "/api/v1/decisions", {}), status, code)
        for error in (ValueError("Failure after command started"), TypeError("Unexpected field handling")):
            self.server.store.receive_decision = Mock(side_effect=error)
            with self.assertLogs("server", level="ERROR"):
                self.assert_error(self.request("POST", "/api/v1/decisions", {}),
                                  500, "internal_error", "unknown")

    def test_unknown_routes_do_not_create_legacy_apply_endpoint(self):
        self.assert_error(self.request("GET", "/api/v1/missing"), 404, "not_found")
        self.assert_error(self.request("POST", "/api/v1/missing", {}), 404, "not_found")
        status, body = self.request("POST", "/api/apply", {})
        self.assertEqual(status, 404)
        self.assertIsInstance(body["error"], str)
        self.assertNotIn("schema_version", body)

    def decision(self, action="apply", company_id="a"):
        status, feed = self.request("GET", "/api/v1/feed")
        self.assertEqual(status, 200)
        card = next(card for card in feed["cards"] if card["company"]["id"] == company_id)
        return {"request_id": f"http-{action}", "action": action, "card_id": card["id"],
                "card_revision": card["revision"],
                "selected": [card["jobs"][0]["key"]] if action == "apply" else []}

    def test_apply_and_replay_survive_store_restart_without_duplicate_work(self):
        payload = self.decision()
        status, receipt = self.request("POST", "/api/v1/decisions", payload)
        self.assertEqual(status, 200)
        self.assertTrue(receipt["accepted"])
        self.assertEqual(len(receipt["process_ids"]), 1)
        self.assertEqual(len(receipt["task_ids"]), 1)
        self.server.store = type(self.server.store)(self.root / "state.sqlite3")
        self.assertEqual(self.request("POST", "/api/v1/decisions", payload), (200, receipt))
        self.assert_error(self.request("POST", "/api/v1/decisions", {**payload, "note": "different"}),
                          409, "conflict")
        status, body = self.request("GET", "/api/v1/processes")
        self.assertEqual(status, 200)
        self.assertEqual(len(body["processes"]), 1)
        self.assertEqual(body["processes"][0]["submission_status"], "not_started")
        self.assertEqual(body["processes"][0]["task_status"], "queued")
        status, legacy = self.request("GET", "/api/state")
        self.assertEqual(status, 200)
        self.assertEqual(legacy["queue"][0]["status"], "queued")
        job = next(job for job in legacy["jobs"] if job["key"] == payload["selected"][0])
        self.assertEqual(self.request("POST", "/api/queue", {
            "request_id": "legacy-duplicate", "jobs": [{"key": job["key"], "revision": job["revision"]}]})[0], 409)

    def test_find_more_and_cancel_return_durable_receipts(self):
        payload = self.decision("find_more", "b")
        payload["exclude_job_keys"] = ["b:b1"]
        status, receipt = self.request("POST", "/api/v1/decisions", payload)
        self.assertEqual(status, 200)
        self.assertEqual(receipt["process_ids"], [])
        self.assertEqual(len(receipt["task_ids"]), 1)
        self.assertEqual(self.request("POST", "/api/v1/decisions", payload), (200, receipt))
        status, body = self.request("GET", "/api/v1/tasks")
        self.assertEqual(status, 200)
        task = next(task for task in body["tasks"] if task["id"] == receipt["task_ids"][0])
        self.assertEqual(task["kind"], "explore")
        self.assertEqual(set(task), set(PUBLIC_TASK_FIELDS))
        cancel = {"request_id": "cancel-research", "task_id": task["id"]}
        status, cancelled = self.request("POST", "/api/v1/tasks/cancel", cancel)
        self.assertEqual(status, 200)
        self.assertEqual(self.request("POST", "/api/v1/tasks/cancel", cancel), (200, cancelled))
        self.assertEqual(self.request("GET", "/api/v1/tasks")[1]["tasks"][0]["status"], "cancelled")

    def test_conflict_reasons_distinguish_replay_identity_from_stale_card(self):
        payload = self.decision()
        self.assertEqual(200, self.request("POST", "/api/v1/decisions", payload)[0])
        for body, reason in (({**payload, "note": "different"}, "request_id_conflict"),
                             ({**payload, "request_id": "new-intent"}, "stale_revision")):
            result = self.request("POST", "/api/v1/decisions", body)
            self.assert_error(result, 409, "conflict")
            self.assertEqual(reason, result[1]["error"]["reason"])
        task = self.server.store.lease_task("test-worker", kinds=["apply"])
        result = self.request("POST", "/api/v1/tasks/cancel", {"request_id": "cannot-cancel", "task_id": task["id"]})
        self.assert_error(result, 409, "conflict")
        self.assertEqual("operation_rejected", result[1]["error"]["reason"])
        result = self.request("POST", "/api/v1/decisions", {})
        self.assertIsNone(result[1]["error"]["reason"])

    def test_task_reads_never_expose_execution_authority_or_internal_payload(self):
        task = {field: None for field in PUBLIC_TASK_FIELDS}
        task.update(id="task-1", kind="apply", status="running", lease_token="secret-lease",
                    lease_owner="worker-session", payload={"token": "private"},
                    checkpoint={"browser_session": "private"})
        self.server.store.tasks = Mock(return_value=[task])
        status, body = self.request("GET", "/api/v1/tasks")
        self.assertEqual(status, 200)
        self.assertEqual(set(body["tasks"][0]), set(PUBLIC_TASK_FIELDS))
        self.assertNotIn("secret-lease", json.dumps(body))
        self.assertNotIn("private", json.dumps(body))

    def test_questions_and_answers_resume_the_same_task_and_replay(self):
        status, receipt = self.request("POST", "/api/v1/decisions", self.decision())
        self.assertEqual(status, 200)
        process_id = receipt["process_ids"][0]
        task = self.server.store.lease_task("http-test-worker", kinds=["apply"])
        self.server.store.start_application(process_id, task["lease_token"])
        self.server.store.ask_questions(process_id, task["lease_token"], "test-form-v1", [{
            "id": "test-question", "text": "Which work location do you prefer?", "type": "single_choice",
            "choices": [{"id": "sf", "label": "San Francisco"}, {"id": "ny", "label": "New York"}],
            "required": True, "scope": "application", "reason": "The employer requires one choice."}])
        status, questions = self.request("GET", "/api/v1/questions")
        self.assertEqual(status, 200)
        self.assertEqual(len(questions["questions"]), 1)
        question = questions["questions"][0]
        process = self.request("GET", "/api/v1/processes")[1]["processes"][0]
        self.assertEqual(process["task_status"], "waiting_for_user")
        answer = {"request_id": "http-answer", "process_id": process_id,
                  "process_revision": process["revision"], "form_version": "test-form-v1",
                  "answers": [{"question_id": question["id"], "question_revision": question["revision"], "value": "sf"}]}
        stale = self.request("POST", "/api/v1/answers", {**answer, "process_revision": process["revision"] + 1})
        self.assertEqual("stale_revision", stale[1]["error"]["reason"])
        status, acknowledgement = self.request("POST", "/api/v1/answers", answer)
        self.assertEqual(status, 200)
        self.assertEqual(acknowledgement["task_ids"], receipt["task_ids"])
        self.assertEqual(self.request("POST", "/api/v1/answers", answer), (200, acknowledgement))
        reused = self.request("POST", "/api/v1/answers", {**answer, "form_version": "changed"})
        self.assertEqual("request_id_conflict", reused[1]["error"]["reason"])
        process = self.request("GET", "/api/v1/processes")[1]["processes"][0]
        self.assertEqual(process["task_status"], "queued")
        self.assertEqual(self.request("GET", "/api/v1/questions")[1]["questions"][0]["answer"], "sf")

    def test_scheduler_is_throttled_and_reports_errors(self):
        self.server.orchestrator.tick = Mock()
        self.server._last_tick = 100
        with patch("server.time.monotonic", return_value=104):
            self.server.service_actions()
        self.server.orchestrator.tick.assert_not_called()
        with patch("server.time.monotonic", return_value=105):
            self.server.service_actions()
        self.server.orchestrator.tick.assert_called_once_with()
        self.server.orchestrator.tick.side_effect = RuntimeError("Scheduling failed")
        with patch("server.time.monotonic", return_value=110), self.assertLogs("server", level="ERROR"):
            self.server.service_actions()


if __name__ == "__main__":
    unittest.main()
