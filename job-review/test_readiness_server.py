"""Startup health HTTP tests: isolated ports, temporary DBs, no real adapters."""
import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path

from server import ReviewServer
from store import digest
from test_readiness import ReadyRuntime, healthy_probe
from test_store import catalog


class ReadinessServerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.server = None
        self.release = threading.Event()

    def tearDown(self):
        self.release.set()
        if self.server is not None:
            self.server.orchestrator.readiness.wait(2)
            self.server.shutdown()
            self.server.server_close()
            self.thread.join(2)
        self.temp.cleanup()

    def start(self, runtime=None):
        self.server = ReviewServer(("127.0.0.1", 0), Path(self.temp.name) / "test.sqlite3", runtime=runtime)
        self.server.store.import_catalog(catalog(), "fictional health HTTP tests")
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
        self.server.orchestrator.readiness.wait(2)
        health = self.request("/api/v1/health")["health"]
        self.assertEqual("not_configured", health["status"])
        self.assertFalse(health["ready"])
        self.assertEqual("passed", health["checks"][0]["status"])
        caps = self.request("/api/v1/capabilities")["capabilities"]
        self.assertEqual(caps, self.request("/api/v1/feed")["capabilities"])

    def test_hung_preflight_does_not_block_api_decisions_or_change_capability_truth(self):
        entered, release = threading.Event(), self.release

        class WaitingRuntime(ReadyRuntime):
            def preflight(self, probe_id, lease):
                entered.set()
                if not release.wait(5):
                    raise RuntimeError("Fictional probe was not released")
                return healthy_probe(probe_id)

        self.start(WaitingRuntime())
        self.assertTrue(entered.wait(2))
        health = self.request("/api/v1/health")["health"]
        self.assertFalse(health["ready"])
        self.assertEqual("checking", health["status"])
        feed = self.request("/api/v1/feed")
        self.assertTrue(feed["capabilities"]["worker_enabled"])
        self.assertEqual("checking", feed["capabilities"]["worker_status"])
        card = feed["cards"][0]
        receipt = self.request("/api/v1/decisions", {
            "request_id": "fictional-health-decision", "action": "apply", "card_id": card["id"],
            "card_revision": card["revision"], "selected": [card["jobs"][0]["key"]]})
        self.assertTrue(receipt["accepted"])
        self.assertEqual("checking", self.server.orchestrator.run_once()["worker_status"])
        self.assertEqual("queued", self.request("/api/v1/processes")["processes"][0]["task_status"])
        release.set()
        self.assertTrue(self.server.orchestrator.readiness.wait(2)["ready"])
        self.assertTrue(self.request("/api/v1/health")["health"]["ready"])
        caps = self.request("/api/v1/capabilities")["capabilities"]
        self.assertEqual("ready", caps["worker_status"])
        self.assertEqual(caps, self.request("/api/v1/feed")["capabilities"])
        # Completing preflight does not execute the user's application.
        self.assertEqual("queued", self.request("/api/v1/processes")["processes"][0]["task_status"])


if __name__ == "__main__":
    unittest.main()
