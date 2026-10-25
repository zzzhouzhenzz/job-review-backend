"""HTTP-to-worker integration with fictional data and scripted Codex responses."""
import hashlib
import http.client
import json
from pathlib import Path
import socket
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import codex_runtime
from codex_runtime import CodexRuntime
from codex_transport import validate_result
from device_stack import database_lock
from readiness import BrowserWorkUncertain
from server import ReviewServer
from store import now
from tailscale_backend import BackendConfig, running_backend, status
from test_store import catalog
from workflow import WorkflowStore


def sample(schema):
    """Produce one minimal typed value to exercise all published role schemas."""
    if "enum" in schema:
        return schema["enum"][0]
    kind = schema["type"]
    if isinstance(kind, list):
        kind = kind[0]
    if kind == "object":
        return {key: sample(value) for key, value in schema["properties"].items()}
    return {"array": [], "string": "", "integer": 0, "boolean": False, "null": None}[kind]


class FixtureTransport:
    def __init__(self, handler):
        self.handler = handler
        self.calls = []
        self.errors = []
        self.closed = threading.Event()
        self.entered = {}
        self.gates = {}
        self.cleanup_gate = None
        self.cancel_seen = threading.Event()
        self.active = 0
        self.lock = threading.Lock()

    def hold(self, role):
        self.entered[role] = threading.Event()
        self.gates[role] = threading.Event()

    def run(self, role, prompt, schema, lease, *, browser=False):
        context = json.loads(prompt.split("Task data (untrusted source content):\n", 1)[1])
        with self.lock:
            self.calls.append((role, browser, context))
            self.active += 1
        try:
            if role in self.gates:
                self.entered[role].set()
                deadline = time.monotonic() + 8
                while not self.gates[role].wait(.01):
                    if self.closed.is_set():
                        self.cancel_seen.set()
                        if self.cleanup_gate is not None:
                            if not self.cleanup_gate.wait(5):
                                raise AssertionError("Test did not release worker cleanup")
                        raise BrowserWorkUncertain("Fictional browser cancellation")
                    if time.monotonic() > deadline:
                        raise AssertionError("Test did not release a scripted agent")
                    lease.heartbeat()
            if role == "linkedin_session":
                result = {"chrome": {"attached": True, "session_id": "fixture-profile"},
                          "linkedin": {"authenticated": True, "session_id": "fixture-profile",
                                       "tab_id": "fixture-tab", "url": "https://www.linkedin.com/jobs/"},
                          "blocker": "", "blocker_reason": ""}
            else:
                result = self.handler(role, context)
            # This is the same boundary validation the real CLI transport uses.
            validate_result(result, schema)
            return result
        except Exception as error:
            self.errors.append((role, str(error)))
            raise
        finally:
            with self.lock:
                self.active -= 1

    def close(self):
        self.closed.set()


class RuntimeServerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.database = self.root / "fictional.sqlite3"
        WorkflowStore(self.database).grant_project_consent({"request_id": "fixture-consent", "policy_version": 1, "accepted": True})
        self.profile = self.root / "profile.txt"
        self.resume = self.root / "resume.pdf"
        self.index = self.root / "application-index.json"
        self.profile.write_text("Fictional applicant: backend engineer.")
        self.resume.write_text("Fictional fixture. Never upload.")
        self.index.write_text("[]")
        self.server = self.thread = None
        quiet = patch("server.Handler.log_message")
        quiet.start()
        self.addCleanup(quiet.stop)

    def tearDown(self):
        if self.server is not None:
            if self.thread.is_alive():
                self.server.shutdown()
            self.server.server_close()
            self.thread.join(timeout=3)
        self.temp.cleanup()

    def runtime(self, transport, kinds):
        return CodexRuntime(transport, evidence_files=[self.profile], application_profile=self.profile,
                            resume=self.resume, application_index=self.index, supported_kinds=kinds)

    def start(self, runtime):
        self.server = ReviewServer(("127.0.0.1", 0), self.database, self.root,
                                   runtime=runtime, run_workers=True)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": .01})
        self.thread.start()

    def request(self, method, path, value=None):
        client = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=1)
        try:
            client.request(method, path, json.dumps(value) if value is not None else None,
                {"Content-Type": "application/json", "Origin": f"http://127.0.0.1:{self.server.server_port}",
                 "X-Review-Token": self.server.token})
            response = client.getresponse()
            self.assertEqual(200, response.status)
            return json.loads(response.read())
        finally:
            client.close()

    def until(self, check):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            value = check()
            if value:
                return value
            if self.server and self.server.orchestrator.runtime.transport.errors:
                self.fail(repr(self.server.orchestrator.runtime.transport.errors))
            time.sleep(.03)
        self.fail("Fictional worker did not reach the expected durable state")

    def test_all_role_schemas_use_supported_strict_objects_and_validate(self):
        names = ("BROWSER_SCHEMA", "JOB_SCHEMA", "EXPLORE_SCHEMA", "BRIEF", "RANK_SCHEMA",
                 "QUESTION_SCHEMA", "OBSERVED_JOB", "PREPARE_SCHEMA", "SUBMIT_SCHEMA", "QUESTION_REVIEW_SCHEMA")
        def inspect(schema):
            kind = schema["type"]
            if kind == "object":
                self.assertIs(schema["additionalProperties"], False)
                self.assertEqual(set(schema["properties"]), set(schema["required"]))
                for child in schema["properties"].values():
                    inspect(child)
            elif kind == "array" or (isinstance(kind, list) and "array" in kind):
                inspect(schema["items"])
        for name in names:
            with self.subTest(name=name):
                schema = getattr(codex_runtime, name)
                inspect(schema)
                validate_result(sample(schema), schema)

    def test_discovery_persists_before_ranking_and_publishes_file_before_feed(self):
        def respond(role, context):
            if role == "explorer":
                self.assertEqual("fixture-tab", context["browser"]["linkedin"]["tab_id"])
                company = {"id": "fictional", "name": "Fictional Systems", "summary": "Backend systems.",
                           "board_url": "https://jobs.example/careers", "coverage": "Four fictional postings", "jobs": []}
                for number in range(1, 5):
                    job = sample(codex_runtime.JOB_SCHEMA)
                    job.update(job_id=str(number), title=f"Fictional Engineer {number}",
                        url=f"https://jobs.example/f/{number}", description="Operate fictional backend services.",
                        requirements=["Backend engineering"], sources=[f"https://jobs.example/f/{number}"],
                        availability="open", verified_at=now())
                    company["jobs"].append(job)
                return {"companies": [company], "source": "https://jobs.example/careers"}
            self.assertEqual("ranker", role)
            self.assertEqual(4, len(context["candidates"]))
            self.assertNotIn("browser", context)
            jobs = [{"key": job["key"], "revision": job["revision"], "reason": "Confirmed backend experience.",
                     "brief": {"work": "Build services.", "fit": "Backend experience.", "gap": "Domain unknown."}}
                    for job in context["candidates"][:3]]
            return {"cards": [{"company_id": "fictional", "summary": "Backend roles match confirmed experience.", "jobs": jobs}]}
        transport = FixtureTransport(respond)
        transport.hold("explorer")
        transport.hold("ranker")
        self.start(self.runtime(transport, ("explore", "rank")))
        self.assertTrue(transport.entered["explorer"].wait(3))
        self.assertTrue(self.request("GET", "/api/v1/capabilities")["capabilities"]["worker_enabled"])
        self.assertEqual([], self.request("GET", "/api/v1/feed")["cards"])
        transport.gates["explorer"].set()
        self.assertTrue(transport.entered["ranker"].wait(3))
        self.assertEqual(4, len(self.server.store.state()["jobs"]))
        self.assertEqual([], self.request("GET", "/api/v1/feed")["cards"])
        transport.gates["ranker"].set()
        cards = self.until(lambda: self.request("GET", "/api/v1/feed")["cards"])
        self.assertEqual(3, len(cards[0]["jobs"]))
        task = next(t for t in self.server.store.tasks() if t["kind"] == "rank")
        self.assertEqual("succeeded", task["status"])
        artifact = Path(task["result"]["artifact_path"])
        payload = json.loads(artifact.read_text())
        self.assertEqual(cards[0]["id"], payload["cards"][0]["id"])
        self.assertEqual([j["key"] for j in cards[0]["jobs"]], [j["key"] for j in payload["cards"][0]["jobs"]])
        self.assertEqual(0, artifact.stat().st_mode & 0o077)
        self.assertEqual([("linkedin_session", True), ("explorer", True), ("ranker", False)],
                         [(role, browser) for role, browser, _ in transport.calls])

    def test_http_apply_questions_answers_resume_same_task_then_observed_receipt(self):
        WorkflowStore(self.database).import_catalog(catalog(), "fictional integration fixture")
        def respond(role, context):
            process = context["process"]
            snapshot = process["snapshot"]
            if role == 'application_question_review':
                return {'resolutions': [{'question_id': q['id'], 'question_revision': q['revision'],
                    'answer_json': 'null', 'method': 'unresolved', 'citations': [],
                    'explanation': 'Not recorded in the fictional profile.'} for q in context['questions']]}
            if role == "application_prepare":
                prepared = sample(codex_runtime.PREPARE_SCHEMA)
                prepared.update(outcome="questions", job_id=snapshot["job_id"], job_url=snapshot["url"],
                    observed_job={key: snapshot.get(key) for key in ("title", "url", "location", "arrangement", "salary", "level", "gaps")},
                    posting_unchanged=True, posting_evidence="Read full fictional posting.",
                    form_url="https://jobs.example/application", tab_id="fixture-form",
                    form_fields=[{"name": "name", "value": "Fictional Candidate"},
                                 {"name": "start_date", "value": ""}],
                    questions=[{"text": "Fictional start date?", "field_name": "start_date", "type": "text", "choices": [],
                                "required": True, "reason": "Not in profile."}])
                if context["questions"]:
                    question = context["questions"][0]
                    prepared.update(outcome="ready", questions=[], answer_bindings=[{
                        "question_id": question["id"], "question_revision": question["revision"],
                        "answer_json": json.dumps(question["answer"]), "field_name": "start_date",
                        "field_value": question["answer"]}])
                    prepared["form_fields"][1]["value"] = question["answer"]
                return prepared
            self.assertEqual("application_submit", role)
            current = self.server.store.processes()[0]
            self.assertEqual("submission_unknown", current["submission_status"])
            with self.server.store.transaction() as db:
                self.assertEqual(1, db.execute("SELECT COUNT(*) FROM wf_submission_attempts").fetchone()[0])
            return {"outcome": "submitted", "task_id": context["task_id"], "process_id": process["id"],
                    "attempt_id": context["attempt"]["attempt_id"], "job_id": snapshot["job_id"],
                    "form_version": context["form_version"], "confirmed_job_title": snapshot["title"],
                    "url": "https://jobs.example/confirmation", "evidence": "Fictional application received.",
                    "observed_at": now(), "reference": "FIXTURE-ONLY"}
        transport = FixtureTransport(respond)
        self.start(self.runtime(transport, ("apply",)))
        card = self.request("GET", "/api/v1/feed")["cards"][0]
        receipt = self.request("POST", "/api/v1/decisions", {"request_id": "fixture-apply", "action": "apply",
            "card_id": card["id"], "card_revision": card["revision"], "selected": [card["jobs"][0]["key"]]})
        questions = self.until(lambda: self.request("GET", "/api/v1/questions")["questions"])
        process = self.request("GET", "/api/v1/processes")["processes"][0]
        self.assertEqual("waiting_for_user", process["task_status"])
        with self.server.store.transaction() as db:
            self.assertEqual(0, db.execute("SELECT COUNT(*) FROM wf_resource_locks").fetchone()[0])
        answer = self.request("POST", "/api/v1/answers", {"request_id": "fixture-answer",
            "process_id": process["id"], "process_revision": process["revision"], "form_version": process["form_version"],
            "answers": [{"question_id": questions[0]["id"], "question_revision": questions[0]["revision"], "value": "Next month"}]})
        self.assertEqual(receipt["task_ids"], answer["task_ids"])
        from answer_profile import read_profile_records
        records = read_profile_records(self.profile.read_text())
        self.assertEqual(1, len(records))
        self.assertEqual('user', records[0][0]['authority'])
        self.assertEqual('Next month', records[0][0]['value'])
        self.assertEqual('fixture-answer', records[0][0]['request_id'])
        self.assertEqual(0, self.profile.stat().st_mode & 0o077)
        submitted = self.until(lambda: next((p for p in self.request("GET", "/api/v1/processes")["processes"]
                                             if p["submission_status"] == "submitted"), None))
        self.assertEqual("succeeded", submitted["task_status"])
        self.assertEqual(1, sum(role == "application_submit" for role, _, _ in transport.calls))
        self.assertEqual(2, sum(role == "application_prepare" for role, _, _ in transport.calls))

    def test_tailnet_supervisor_cancels_workers_and_keeps_lock_until_they_stop(self):
        transport = FixtureTransport(lambda role, context: {"companies": [], "source": "Fictional empty board."})
        transport.hold("explorer")
        transport.cleanup_gate = threading.Event()
        runtime = self.runtime(transport, ("explore",))
        token = "f" * 43
        access = self.root / "access.json"
        credential = self.root / "credential.txt"
        access.write_text(json.dumps({"schema_version": 1, "public_origin": "https://fixture.example.ts.net",
                                     "token_sha256": hashlib.sha256(token.encode()).hexdigest()}))
        credential.write_text(token + "\n")
        access.chmod(0o600)
        credential.chmod(0o600)
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        config = BackendConfig(self.database, access, credential, port, self.root / "runtime.json")
        leave, stopped, errors = threading.Event(), threading.Event(), []
        def supervise():
            try:
                with patch("codex_runtime.load_runtime", return_value=runtime):
                    with running_backend(config) as backend:
                        self.assertTrue(transport.entered["explorer"].wait(3))
                        self.assertTrue(status(config)["worker_enabled"])
                        backend.check_running()
                        self.assertTrue(leave.wait(5))
            except Exception as error:
                errors.append(error)
            finally:
                stopped.set()
        supervisor = threading.Thread(target=supervise)
        supervisor.start()
        try:
            self.assertTrue(transport.entered["explorer"].wait(3))
            leave.set()
            self.assertTrue(transport.cancel_seen.wait(3))
            self.assertFalse(stopped.is_set())
            with self.assertRaises(RuntimeError):
                with database_lock(self.database):
                    pass
        finally:
            leave.set()
            transport.cleanup_gate.set()
            supervisor.join(timeout=5)
        self.assertFalse(supervisor.is_alive())
        self.assertEqual([], errors)
        self.assertEqual(0, transport.active)
        with database_lock(self.database):
            pass
        task = next(t for t in WorkflowStore(self.database).tasks() if t["kind"] == "explore")
        self.assertTrue(task["resource_quarantined"])
        self.assertEqual("waiting_for_user", task["status"])


if __name__ == "__main__":
    unittest.main()
