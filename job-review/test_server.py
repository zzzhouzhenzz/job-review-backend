import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path

from server import ReviewServer
from test_store import catalog


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "index.html").write_text("<!doctype html><title>Test review</title>")
        self.server = ReviewServer(("127.0.0.1", 0), self.root / "state.sqlite3", self.root)
        self.server.store.import_catalog(catalog(), "test")
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_port

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.temp.cleanup()

    def request(self, method, path, value=None, headers=None):
        client = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        standard = {"Content-Type": "application/json", "Origin": f"http://127.0.0.1:{self.port}", "X-Review-Token": self.server.token}
        standard.update(headers or {})
        try:
            client.request(method, path, body=json.dumps(value) if value is not None else None, headers=standard)
            response = client.getresponse()
            body = response.read()
            return response.status, dict(response.getheaders()), body
        finally:
            client.close()

    def test_state_and_export_no_cache_and_no_token_in_export(self):
        status, headers, raw = self.request("GET", "/api/state")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Cache-Control"], "no-store")
        state = json.loads(raw)
        self.assertEqual(state["token"], self.server.token)
        self.assertEqual(len(state["jobs"]), 4)
        status, headers, raw = self.request("GET", "/api/export")
        self.assertEqual(status, 200)
        self.assertNotIn("token", json.loads(raw))
        self.assertIn("attachment", headers["Content-Disposition"])

    def test_loopback_host_origin_and_token_required(self):
        self.assertEqual(self.request("GET", "/api/state", headers={"Host": "evil.example"})[0], 403)
        payload = {"key": "a:1", "revision": 1, "decision": "approve"}
        for headers in ({"Origin": "https://evil.example"}, {"X-Review-Token": "wrong"}, {"Host": "evil.example"}):
            self.assertEqual(self.request("POST", "/api/job", payload, headers)[0], 403)

    def test_mutations_return_full_state_and_conflicts(self):
        job = next(j for j in self.server.store.state()["jobs"] if j["key"] == "a:1")
        payload = {"key": "a:1", "revision": job["revision"], "decision": "approve", "note": "Selected"}
        status, _, raw = self.request("POST", "/api/job", payload)
        self.assertEqual(status, 200)
        state = json.loads(raw)
        self.assertTrue({"token", "companies", "jobs", "queue", "imports", "counts"} <= set(state))
        status, _, raw = self.request("POST", "/api/job", payload)
        self.assertEqual(status, 409)
        self.assertIn("Refresh", json.loads(raw)["error"])

    def test_live_import_visible_without_restart_and_no_database_file_served(self):
        self.server.store.import_catalog({"companies": [{"id": "new", "name": "New", "jobs": [{"job_id": "n1", "title": "New engineer", "url": "https://jobs.example/new/1"}]}]}, "live import")
        status, _, raw = self.request("GET", "/api/state")
        self.assertEqual(status, 200)
        self.assertIn("new:n1", {j["key"] for j in json.loads(raw)["jobs"]})
        self.assertEqual(self.request("GET", "/state.sqlite3")[0], 404)
        self.assertEqual(self.request("GET", "/../state.sqlite3")[0], 404)

    def test_card_route_saves_full_state_and_retries_without_duplicates(self):
        card = next(c for c in self.server.store.state()["flashcards"] if c["company_id"] == "a")
        payload = {"request_id": "http-card", "company_id": "a", "revision": card["revision"],
                   "jobs": [{"key": j["key"], "revision": j["revision"]} for j in card["jobs"]],
                   "action": "approve", "selected": ["a:2"]}
        self.assertEqual(self.request("POST", "/api/card", payload, {"Origin": "https://evil.example"})[0], 403)
        status, _, raw = self.request("POST", "/api/card", payload)
        self.assertEqual(status, 200)
        state = json.loads(raw)
        self.assertEqual(state["token"], self.server.token)
        self.assertEqual(state["card_reviews"][0]["selected"], ["a:2"])
        self.assertEqual(len(state["flashcards"]), 1)
        status, _, raw = self.request("POST", "/api/card", payload)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw), state)
        status, _, raw = self.request("POST", "/api/card", {**payload, "request_id": "second-click"})
        self.assertEqual(status, 409)
        self.assertIn("Refresh", json.loads(raw)["error"])

    def test_invalid_fields_return_json_error_without_mutation(self):
        for value in ([], {"key": "a:2", "revision": 1, "decision": []}, {"key": "a:2", "revision": 1, "decision": "approve", "note": 42}):
            status, _, raw = self.request("POST", "/api/job", value)
            self.assertEqual(status, 400)
            self.assertIn("error", json.loads(raw))
        self.assertEqual(self.request("POST", "/api/apply", {})[0], 404)


if __name__ == "__main__":
    unittest.main()
