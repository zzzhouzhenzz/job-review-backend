"""On-demand browser checks do not block the HTTP decision API."""
import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from server import ReviewServer
from store import digest
from test_readiness import ReadyRuntime, healthy_browser
from test_store import catalog


class ReadinessServerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.server = None
        self.release = threading.Event()
        self.dispatch = None

    def tearDown(self):
        self.release.set()
        if self.dispatch is not None:
            self.dispatch.join(2)
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
            self.thread.join(2)
        self.temp.cleanup()

    def start(self, runtime=None):
        self.server = ReviewServer(("127.0.0.1", 0), Path(self.temp.name) / "test.sqlite3", runtime=runtime)
        self.server.store.grant_project_consent({"request_id": "fixture-consent", "policy_version": 1, "accepted": True})
        self.server.store.low_watermark = 0
        self.server.store.import_catalog(catalog(), "fictional health HTTP tests")
        self.server.orchestrator.readiness.wait(2)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def request(self, path, payload=None):
        port = self.server.server_port
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
        try:
            conn.request("GET" if payload is None else "POST", path,
                         None if payload is None else json.dumps(payload),
                         {"Content-Type": "application/json", "Origin": f"http://127.0.0.1:{port}",
                          "X-Review-Token": self.server.token})
            response = conn.getresponse()
            self.assertEqual(200, response.status)
            body = json.loads(response.read())
            if payload is None:
                data = {key: value for key, value in body.items()
                        if key not in {"schema_version", "revision", "complete", "next_cursor", "token"}}
                self.assertEqual(digest(data), body["revision"])
                self.assertTrue(body["complete"])
            return body
        finally:
            conn.close()

    def test_default_boot_reports_unconfigured_with_healthy_local_storage(self):
        self.start()
        health = self.request("/api/v1/health")["health"]
        self.assertEqual("not_configured", health["status"])
        self.assertFalse(health["ready"])
        self.assertEqual("passed", health["checks"][0]["status"])
        caps = self.request("/api/v1/capabilities")["capabilities"]
        self.assertEqual(caps, self.request("/api/v1/feed")["capabilities"])

    def test_enabled_runtime_is_idle_at_boot_without_agent_or_browser_calls(self):
        runtime = ReadyRuntime()
        self.start(runtime)
        health = self.request("/api/v1/health")["health"]
        self.assertEqual("idle", health["status"])
        self.assertFalse(health["ready"])
        self.assertEqual(["passed", "on_demand", "on_demand"], [row["status"] for row in health["checks"]])
        self.assertEqual(0, runtime.probe_calls)
        self.assertEqual([], self.server.store.tasks())

    def test_workers_disabled_prevents_http_scheduler_from_starting_question_agents(self):
        calls = []
        class ReviewRuntime(ReadyRuntime):
            def reuse_pending_answers(self, store):
                calls.append(True)
        self.start(ReviewRuntime())
        self.assertFalse(self.server.orchestrator.schedule_answer_reviews)
        with patch.object(self.server.store, 'sync_profile_answers', return_value={'status': 'current'}):
            self.server._last_tick = 0
            self.server.service_actions()
        self.assertEqual([], calls)

    def test_hung_task_browser_check_does_not_block_api_decisions(self):
        entered, release = threading.Event(), self.release
        class WaitingRuntime(ReadyRuntime):
            def prepare_browser(self, task, lease):
                entered.set()
                if not release.wait(5):
                    raise TimeoutError("Fictional browser check was not released")
                return healthy_browser()
        runtime = WaitingRuntime()
        self.start(runtime)
        with self.server.store.transaction(True) as db:
            explore = self.server.store._enqueue_task(db, "explore", {}, "fictional-browser-check", resource="browser")
        self.dispatch = threading.Thread(target=self.server.orchestrator.run_once, kwargs={"kinds": ["explore"]})
        self.dispatch.start()
        self.assertTrue(entered.wait(2))
        health = self.request("/api/v1/health")["health"]
        self.assertFalse(health["ready"])
        self.assertEqual("checking", health["status"])
        feed = self.request("/api/v1/feed")
        self.assertTrue(feed["capabilities"]["worker_enabled"])
        card = feed["cards"][0]
        receipt = self.request("/api/v1/decisions", {
            "request_id": "fictional-health-decision", "action": "apply", "card_id": card["id"],
            "card_revision": card["revision"], "selected": [card["jobs"][0]["key"]]})
        self.assertTrue(receipt["accepted"])
        self.assertEqual("queued", self.request("/api/v1/processes")["processes"][0]["task_status"])
        self.assertEqual("idle", self.server.orchestrator.run_once(kinds=["apply"])["worker_status"])
        release.set()
        self.dispatch.join(2)
        self.assertFalse(self.dispatch.is_alive())
        self.assertTrue(self.request("/api/v1/health")["health"]["ready"])
        self.assertEqual("succeeded", next(t for t in self.server.store.tasks() if t["id"] == explore)["status"])
        self.assertEqual("queued", self.request("/api/v1/processes")["processes"][0]["task_status"])


if __name__ == "__main__":
    unittest.main()
