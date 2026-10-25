"""Durable application state and question rounds. No browser or LLM calls."""
from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone

from store import ReviewError, canonical_url, digest, encode, material_hash, now, text
from application_pool import browser_resource
from answer_profile import (ANSWER_PROFILE_SCHEMA, backfill_profile_answers,
                            profile_answer_records, record_user_answers, sync_profile_answers)

LOGGER = logging.getLogger(__name__)


APPLICATION_SCHEMA = """
CREATE TABLE IF NOT EXISTS wf_processes (
 id TEXT PRIMARY KEY REFERENCES queue(id), job_key TEXT NOT NULL REFERENCES jobs(key),
 task_id TEXT NOT NULL, decision_id TEXT NOT NULL, snapshot TEXT NOT NULL,
 submission_status TEXT NOT NULL DEFAULT 'not_started',
 recruiting_status TEXT NOT NULL DEFAULT 'awaiting_response',
 revision INTEGER NOT NULL DEFAULT 1, receipt TEXT, block_reason TEXT,
 form_version TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS wf_process_job ON wf_processes(job_key);
CREATE TABLE IF NOT EXISTS wf_questions (
 id TEXT PRIMARY KEY, process_id TEXT NOT NULL REFERENCES wf_processes(id),
 form_version TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 1,
 text TEXT NOT NULL, type TEXT NOT NULL, choices TEXT NOT NULL,
 required INTEGER NOT NULL, scope TEXT NOT NULL, reason TEXT NOT NULL,
 answer TEXT, answered_at TEXT, superseded INTEGER NOT NULL DEFAULT 0,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS wf_question_process ON wf_questions(process_id,superseded);
CREATE TABLE IF NOT EXISTS wf_answer_requests (
 id TEXT PRIMARY KEY, payload_hash TEXT NOT NULL, request_body TEXT NOT NULL,
 receipt TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS wf_answer_evidence (
 request_id TEXT NOT NULL REFERENCES wf_answer_requests(id),
 question_id TEXT NOT NULL REFERENCES wf_questions(id), answer_revision INTEGER NOT NULL,
 source_snapshot_hash TEXT NOT NULL, field_name TEXT NOT NULL, method TEXT NOT NULL,
 citations TEXT NOT NULL, explanation TEXT NOT NULL, resolved_value TEXT NOT NULL,
 created_at TEXT NOT NULL, PRIMARY KEY(request_id,question_id)
);
CREATE TABLE IF NOT EXISTS wf_submission_attempts (
 id TEXT PRIMARY KEY, process_id TEXT NOT NULL REFERENCES wf_processes(id),
 task_id TEXT NOT NULL, form_version TEXT NOT NULL, started_at TEXT NOT NULL,
 confirmed_at TEXT, receipt TEXT
);
CREATE TABLE IF NOT EXISTS wf_process_events (
 id INTEGER PRIMARY KEY AUTOINCREMENT, process_id TEXT NOT NULL REFERENCES wf_processes(id),
 type TEXT NOT NULL, actor TEXT NOT NULL, data TEXT NOT NULL, created_at TEXT NOT NULL
);
""" + ANSWER_PROFILE_SCHEMA

RECRUITING_TRANSITIONS = {
    "awaiting_response": {"responded", "interviewing", "offer", "rejected", "withdrawn", "hired"},
    "responded": {"interviewing", "offer", "rejected", "withdrawn", "hired"},
    "interviewing": {"offer", "rejected", "withdrawn", "hired"},
    "offer": {"rejected", "withdrawn", "hired"},
    "rejected": set(), "withdrawn": set(), "hired": set(),
}


class ApplicationMixin:
    @staticmethod
    def process(row):
        result = dict(row)
        result["snapshot"] = json.loads(result["snapshot"])
        result["receipt"] = json.loads(result["receipt"]) if result["receipt"] else None
        return result

    @staticmethod
    def question(row):
        result = dict(row)
        result.pop("superseded", None)
        result["choices"] = json.loads(result["choices"])
        result["required"] = bool(result["required"])
        result["answer"] = json.loads(result["answer"]) if result["answer"] is not None else None
        return result

    @staticmethod
    def _process(db, process_id):
        row = db.execute("SELECT * FROM wf_processes WHERE id=?", (process_id,)).fetchone()
        if row is None:
            raise ReviewError("Application process not found.", 404)
        return row

    @staticmethod
    def _process_event(db, process_id, kind, data, actor="worker"):
        db.execute("INSERT INTO wf_process_events(process_id,type,actor,data,created_at) VALUES (?,?,?,?,?)",
                   (process_id, kind, actor, encode(data), now()))

    def _process_view(self, db, row):
        result = self.process(row)
        result["task_status"] = self._task(db, row["task_id"])["status"]
        result["events"] = [{**dict(event), "data": json.loads(event["data"])} for event in
                            db.execute("SELECT * FROM wf_process_events WHERE process_id=? ORDER BY id", (row["id"],))]
        return result

    def processes(self):
        """Return all processes in one database snapshot, newest first."""
        with self.transaction() as db:
            return [self._process_view(db, row) for row in
                    db.execute("SELECT * FROM wf_processes ORDER BY created_at DESC,id").fetchall()]

    def questions(self):
        """Return all questions in current rounds, including their saved answers."""
        with self.transaction() as db:
            result = [self.question(row) for row in db.execute(
                "SELECT * FROM wf_questions WHERE superseded=0 ORDER BY created_at,id")]
            if getattr(self, 'question_pool', None) is not None:
                from question_sync import overlay_questions
                revision = db.execute('SELECT revision FROM wf_qpool_state WHERE singleton=1').fetchone()[0]
                result = overlay_questions(db, result, revision)
            return result

    def _create_application(self, db, job_row, request_id):
        if db.execute("SELECT 1 FROM wf_processes p JOIN wf_tasks t ON t.id=p.task_id WHERE p.job_key=? AND t.status IN ('queued','running','waiting_for_user','retry_wait')", (job_row["key"],)).fetchone():
            raise ReviewError("This job already has an active application process.", 409)
        if db.execute("SELECT 1 FROM wf_processes WHERE job_key=? AND submission_status IN ('submission_unknown','submitted')",
                      (job_row["key"],)).fetchone():
            raise ReviewError("This job has a submitted application or an unresolved submission.", 409)
        if self._blocking(db, job_row["key"]):
            raise ReviewError("This job already has active application work.", 409)
        if job_row["decision"] != "approve":
            raise ReviewError("An explicit Apply decision is required.", 409)
        company = self._row(db, "companies", "id", job_row["company_id"])
        if company["excluded"]:
            raise ReviewError("This company is excluded.", 409)
        stamp, process_id = now(), str(uuid.uuid4())
        data = json.loads(job_row["data"])
        snapshot = {**data, "company_id": company["id"], "company_name": company["name"],
                    "job_key": job_row["key"], "job_revision": job_row["revision"],
                    "material_hash": material_hash(data)}
        task_id = self._enqueue_task(db, "apply", {"process_id": process_id},
                                     dedupe_key=f"apply:{job_row['key']}", resource="browser")
        if json.loads(self._task(db, task_id)["payload"]).get("process_id") != process_id:
            raise ReviewError("This job already has unresolved application work.", 409)
        db.execute("INSERT INTO queue(id,kind,company_id,job_key,status,snapshot,created_at,updated_at,note) VALUES (?,?,?,?,?,?,?,?,?)",
                   (process_id, "application", company["id"], job_row["key"], "queued", encode(snapshot), stamp, stamp,
                    "Explicit v1 Apply authorization. Worker execution is separate."))
        db.execute("INSERT INTO wf_processes(id,job_key,task_id,decision_id,snapshot,created_at,updated_at) VALUES (?,?,?,?,?,?,?)",
                   (process_id, job_row["key"], task_id, request_id, encode(snapshot), stamp, stamp))
        self._process_event(db, process_id, "authorized", {"decision_id": request_id, "job_revision": job_row["revision"]}, "user")
        return process_id

    def _application_lease(self, db, process_id, token):
        process = self._process(db, process_id)
        task = self._leased_task(db, process["task_id"], token)
        payload = json.loads(task["payload"])
        if task["kind"] != "apply" or payload.get("process_id") != process_id:
            raise ReviewError("The task does not own this application.", 409)
        return process, task

    def _check_application(self, db, process):
        job = self._row(db, "jobs", "key", process["job_key"])
        company = self._row(db, "companies", "id", job["company_id"])
        entry = self._row(db, "queue", "id", process["id"])
        snapshot = json.loads(process["snapshot"])
        if company["excluded"] or job["decision"] != "approve" or entry["status"] not in ("queued", "in_progress", "needs_input"):
            raise ReviewError("The application authorization is no longer active.", 409)
        if material_hash(json.loads(job["data"])) != snapshot["material_hash"]:
            raise ReviewError("The job changed after the Apply decision.", 409)
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='wf_observations'").fetchone():
            observation = db.execute("SELECT availability FROM wf_observations WHERE job_key=?", (job["key"],)).fetchone()
            if observation and observation["availability"] == "closed":
                raise ReviewError("The job is marked closed. Verify it before applying.", 409)

    def start_application(self, process_id, token):
        with self.transaction(True) as db:
            process, task = self._application_lease(db, process_id, token)
            if process["submission_status"] not in ("not_started", "preparing"):
                raise ReviewError("This application cannot be started again.", 409)
            self._check_application(db, process)
            stamp = now()
            db.execute("UPDATE wf_processes SET submission_status='preparing',block_reason=NULL,revision=revision+1,updated_at=? WHERE id=?",
                       (stamp, process_id))
            db.execute("UPDATE queue SET status='in_progress',updated_at=? WHERE id=?", (stamp, process_id))
            self._process_event(db, process_id, "preparing", {"task_id": task["id"]})
            return self._process_view(db, self._process(db, process_id))

    @staticmethod
    def _validate_questions(questions):
        if not isinstance(questions, list) or not 1 <= len(questions) <= 50:
            raise ReviewError("Provide between 1 and 50 questions.")
        cleaned, ids = [], set()
        for question in questions:
            if not isinstance(question, dict):
                raise ReviewError("Each question must be an object.")
            qid = text(question.get("id", str(uuid.uuid4())), "Question ID", 200, required=True)
            if qid in ids:
                raise ReviewError("Question IDs must be unique.")
            ids.add(qid)
            question_text = text(question.get("text"), "Question text", 10000, required=True)
            kind = question.get("type")
            choices = question.get("choices", [])
            if kind not in ("text", "single_choice", "multi_choice"):
                raise ReviewError("Unsupported question type.")
            if type(question.get("required")) is not bool or question.get("scope", "application") != "application":
                raise ReviewError("Questions need a required flag and application scope.")
            if not isinstance(choices, list) or len(choices) > 100:
                raise ReviewError("Question choices must be a list with at most 100 items.")
            checked_choices, choice_ids = [], set()
            for choice in choices:
                if not isinstance(choice, dict):
                    raise ReviewError("Each choice must contain an ID and label.")
                cid = text(choice.get("id"), "Choice ID", 200, required=True)
                label = text(choice.get("label"), "Choice label", 2000, required=True)
                if cid in choice_ids:
                    raise ReviewError("Choice IDs must be unique within each question.")
                choice_ids.add(cid)
                checked_choices.append({"id": cid, "label": label})
            if (kind == "text" and choices) or (kind != "text" and not choices):
                raise ReviewError("Text questions have no choices; choice questions need choices.")
            cleaned.append({"id": qid, "text": question_text, "type": kind, "choices": checked_choices,
                            "required": question["required"], "scope": "application",
                            "reason": text(question.get("reason", ""), "Question reason", 2500)})
        return cleaned

    def ask_questions(self, process_id, token, form_version, questions, *, preserve_answer_ids=(),
                      browser_blocker=None):
        form_version = text(form_version, "Form version", 200, required=True)
        questions = self._validate_questions(questions)
        if browser_blocker is not None:
            from application_blockers import blocker_operation
            from readiness import HUMAN_BROWSER_REASONS
            if (not isinstance(browser_blocker, dict)
                    or browser_blocker.get('reason') not in HUMAN_BROWSER_REASONS):
                raise ReviewError('Questions require a recognized browser blocker.')
            browser_blocker = {'reason': browser_blocker['reason'],
                'detail': text(browser_blocker.get('detail', ''), 'Blocker detail', 2500),
                'operation': blocker_operation(browser_blocker['reason'], browser_blocker.get('operation'))}
        if not isinstance(preserve_answer_ids, (list, tuple, set)):
            raise ReviewError("Preserved answer IDs must be a collection.")
        preserved = {text(qid, "Preserved question ID", 200, required=True) for qid in preserve_answer_ids}
        if len(preserved) != len(preserve_answer_ids):
            raise ReviewError("Preserved answer IDs must be unique.")
        with self.transaction(True) as db:
            process, task = self._application_lease(db, process_id, token)
            if process["submission_status"] != "preparing":
                raise ReviewError("Questions require an application in preparation.", 409)
            self._check_application(db, process)
            if browser_blocker and (not browser_resource(task['resource'])
                    or json.loads(task['checkpoint']).get('submission_started')
                    or db.execute('SELECT 1 FROM wf_submission_attempts WHERE process_id=?', (process_id,)).fetchone()):
                raise ReviewError('Blocked questions cannot change a submission attempt.', 409)
            answered = {row["id"] for row in db.execute(
                "SELECT id FROM wf_questions WHERE process_id=? AND superseded=0 AND answer IS NOT NULL", (process_id,))}
            if preserved != answered:
                raise ReviewError("A new question round must preserve every existing answer.", 409)
            stamp = now()
            for question in questions:
                if db.execute("SELECT 1 FROM wf_questions WHERE id=?", (question["id"],)).fetchone():
                    raise ReviewError("Question ID was already used; create a new question revision.", 409)
            db.execute("UPDATE wf_questions SET superseded=1,updated_at=? WHERE process_id=? AND superseded=0 AND answer IS NULL",
                       (stamp, process_id))
            # The value and its revision still identify the original authority.
            # The process and form revisions fence acceptance in the new round.
            db.execute("UPDATE wf_questions SET form_version=?,updated_at=? WHERE process_id=? AND superseded=0 AND answer IS NOT NULL",
                       (form_version, stamp, process_id))
            for question in questions:
                db.execute("INSERT INTO wf_questions(id,process_id,form_version,text,type,choices,required,scope,reason,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                           (question["id"], process_id, form_version, question["text"], question["type"],
                            encode(question["choices"]), int(question["required"]), "application", question["reason"], stamp, stamp))
            reason = browser_blocker['reason'] if browser_blocker else 'questions_pending'
            db.execute("UPDATE wf_processes SET form_version=?,block_reason=?,revision=revision+1,updated_at=? WHERE id=?",
                       (form_version, reason, stamp, process_id))
            db.execute("UPDATE queue SET status='needs_input',updated_at=? WHERE id=?", (stamp, process_id))
            self._release_task(db, task["id"], "waiting_for_user", error=reason)
            if browser_blocker:
                self._process_event(db, process_id, 'blocked', browser_blocker)
            self._process_event(db, process_id, "questions_requested", {
                "form_version": form_version, "question_ids": [q["id"] for q in questions],
                "preserved_answer_ids": sorted(preserved)})
            return self._process_view(db, self._process(db, process_id))

    def _waiting_browser_question_blocker(self, db, process, task):
        """Recognize a stopped application with both questions and a browser gate."""
        from readiness import HUMAN_BROWSER_REASONS
        reason = process['block_reason']
        if (reason not in HUMAN_BROWSER_REASONS or task['error'] != reason
                or task['status'] != 'waiting_for_user' or not browser_resource(task['resource'])
                or task['resource_quarantined'] or task['lease_token']
                or process['submission_status'] != 'preparing'
                or json.loads(task['checkpoint']).get('submission_started')
                or db.execute('SELECT 1 FROM wf_submission_attempts WHERE process_id=?', (process['id'],)).fetchone()
                or db.execute("SELECT 1 FROM wf_resource_locks WHERE task_id=? OR (resource='browser' AND quarantined=1)",
                              (task['id'],)).fetchone()):
            return False
        from application_blockers import latest_application_blocker
        event = latest_application_blocker(db, process['id'])
        return event.get('reason') == reason


    @staticmethod
    def _validate_answer(question, value):
        choices = {choice["id"] for choice in json.loads(question["choices"])}
        if question["type"] == "text":
            return text(value, "Answer", 20000, required=bool(question["required"]))
        if question["type"] == "single_choice":
            if not isinstance(value, str) or value not in choices:
                raise ReviewError("Answer must be a listed choice ID.")
            return value
        if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
            raise ReviewError("Answer must be a list of choice IDs.")
        if len(value) != len(set(value)) or set(value) - choices or (question["required"] and not value):
            raise ReviewError("Answer must contain unique listed choice IDs and satisfy required questions.")
        return value

    def answer_questions(self, payload):
        """Save revision-checked user answers; caller fields cannot grant authority."""
        if getattr(self, 'question_pool', None) is not None:
            from pool_application import answer_questions
            return answer_questions(self, payload)
        receipt = self._answer_questions(payload)
        # The request is already durable. A file error must not turn an accepted
        # answer into an apparent HTTP failure; the outbox retries the projection.
        self.sync_profile_answers()
        changed = getattr(self, 'on_answers_changed', None)
        if callable(changed):
            try:
                changed()
            except Exception as error:
                # Polling and the durable profile outbox remain the fallback.
                # A wake failure must not make an accepted answer look rejected.
                LOGGER.warning('Question recovery wake failed (error=%s).', type(error).__name__)
        return receipt

    def sync_profile_answers(self, *, strict=False):
        return sync_profile_answers(self, strict=strict)

    def backfill_profile_answers(self):
        return backfill_profile_answers(self)

    def profile_answer_records(self):
        return profile_answer_records(self)

    def _check_profile_projection(self, db):
        """Fence automatic actions against user facts committed but not exported.

        This check runs under the answer/submission write transaction. Files are
        synchronized by the caller outside that transaction.
        """
        configured = (getattr(self, "application_profile", None) is not None
                      or getattr(self, "application_profile_path", None) is not None)
        if configured and db.execute("SELECT 1 FROM wf_profile_outbox WHERE projected_at IS NULL LIMIT 1").fetchone():
            from applicant_evidence import EvidenceChanged
            raise EvidenceChanged()

    def answer_questions_from_evidence(self, payload, provenance, *, validate_sources=None):
        """Internal writer for source-validated answers. Never replace an answer.

        The caller validates citation contents, then provides a no-argument
        callback that checks the source snapshot again under the database lock.
        No HTTP route grants access to this operation.
        """
        if not callable(validate_sources):
            raise ReviewError("Evidence answers require a current source validation callback.")
        if not isinstance(provenance, dict) or not provenance:
            raise ReviewError("Evidence answers require question provenance.")
        return self._answer_questions(payload, provenance=provenance, validate_sources=validate_sources)

    @staticmethod
    def _validate_answer_provenance(provenance):
        """Check the persistence contract; source text checks belong to the caller."""
        def source_hash(value):
            if not isinstance(value, str) or len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
                raise ReviewError("Answer evidence requires a SHA-256 source hash.")
            return value

        result = {}
        for qid, item in provenance.items():
            text(qid, "Question ID", 200, required=True)
            if not isinstance(item, dict) or item.get("method") not in ("direct", "derived", "drafted"):
                raise ReviewError("Answer evidence requires a supported resolution method.")
            citations = item.get("citations")
            if not isinstance(citations, list) or not 1 <= len(citations) <= 20:
                raise ReviewError("Answer evidence requires bounded source citations.")
            checked = []
            for citation in citations:
                if not isinstance(citation, dict):
                    raise ReviewError("Each answer citation must be an object.")
                start, end = citation.get("start_line"), citation.get("end_line")
                if type(start) is not int or type(end) is not int or not 1 <= start <= end:
                    raise ReviewError("Answer evidence requires a valid source line range.")
                checked.append({
                    "source_id": text(citation.get("source_id"), "Source ID", 200, required=True),
                    "sha256": source_hash(citation.get("sha256")),
                    "start_line": start, "end_line": end,
                    "quote": text(citation.get("quote"), "Source quote", 262144, required=True),
                })
            result[qid] = {
                "field_name": text(item.get("field_name"), "Form field name", 2000, required=True),
                "method": item["method"], "source_snapshot_hash": source_hash(item.get("source_snapshot_hash")),
                "citations": checked,
                "explanation": text(item.get("explanation"), "Evidence explanation", 5000, required=True),
            }
        return result

    def _answer_questions(self, payload, *, provenance=None, validate_sources=None, mutation_fence=None,
                          shared_sources=None):
        if not isinstance(payload, dict):
            raise ReviewError("The answer command must be an object.")
        request_id = text(payload.get("request_id"), "Request ID", 200, required=True)
        evidence = provenance is not None
        shared = shared_sources is not None
        if shared and (evidence or mutation_fence is None or not isinstance(shared_sources,list)
                       or not shared_sources or any(not isinstance(source,dict) or
                           not source.get('question_id') for source in shared_sources)):
            raise ReviewError('Shared answers require fenced source provenance.',409)
        shared_ids={source['question_id'] for source in shared_sources} if shared else set()
        if evidence:
            provenance = self._validate_answer_provenance(provenance)
        body = ({"source": "evidence_resolver", "payload": payload, "provenance": provenance} if evidence else
                {"source":"question_pool","payload":payload,"sources":shared_sources} if shared else payload)
        fingerprint = digest(body)
        with self.transaction(True) as db:
            if mutation_fence is not None:
                mutation_fence(db)
            previous = db.execute("SELECT * FROM wf_answer_requests WHERE id=?", (request_id,)).fetchone()
            if previous:
                was_evidence = db.execute("SELECT 1 FROM wf_answer_evidence WHERE request_id=?", (request_id,)).fetchone() is not None
                if was_evidence != evidence or previous["payload_hash"] != fingerprint:
                    raise ReviewError("Request ID was reused with different answers.", 409, reason="request_id_conflict")
                if mutation_fence is not None:
                    mutation_fence(db)
                return json.loads(previous["receipt"])
            process_id = text(payload.get("process_id"), "Process ID", 200, required=True)
            process = self._process(db, process_id)
            self._revision(process, payload.get("process_revision"))
            self._check_application(db, process)
            task = self._task(db, process["task_id"])
            browser_blocked = self._waiting_browser_question_blocker(db, process, task)
            if (process["submission_status"] != "preparing" or task["status"] != "waiting_for_user"
                    or process["block_reason"] != "questions_pending" and not browser_blocked):
                raise ReviewError("This process is not waiting for these answers.", 409)
            if payload.get("form_version") != process["form_version"]:
                raise ReviewError("The form changed. Refresh its questions.", 409, reason="stale_revision")
            if evidence:
                checkpoint = json.loads(task["checkpoint"])
                if (task["kind"] != "apply" or json.loads(task["payload"]).get("process_id") != process_id
                        or checkpoint.get("submission_started") or task["error"] == "submission_unknown"):
                    raise ReviewError("Evidence answers cannot resume an uncertain application.", 409)
                if task["resource_quarantined"] or db.execute(
                        "SELECT 1 FROM wf_resource_locks WHERE resource=? AND quarantined=1", (task["resource"],)).fetchone():
                    raise ReviewError("Evidence answers cannot resume a quarantined browser resource.", 409)
                bindings = checkpoint.get("question_bindings", {})
                if not isinstance(bindings, dict):
                    raise ReviewError("The original question field bindings are missing.", 409)
            answers = payload.get("answers")
            if not isinstance(answers, list) or not 1 <= len(answers) <= 50:
                raise ReviewError("Provide between 1 and 50 answers.")
            seen, validated = set(), []
            for answer in answers:
                if not isinstance(answer, dict):
                    raise ReviewError("Each answer must be an object.")
                qid = text(answer.get("question_id"), "Question ID", 200, required=True)
                if qid in seen:
                    raise ReviewError("Each question can be answered once per command.")
                seen.add(qid)
                question = db.execute("SELECT * FROM wf_questions WHERE id=? AND process_id=? AND superseded=0", (qid, process_id)).fetchone()
                if question is None or question["form_version"] != process["form_version"]:
                    raise ReviewError("The question is no longer current.", 409, reason="stale_revision")
                self._revision(question, answer.get("question_revision"))
                if shared and (question['answer'] is not None or qid not in shared_ids):
                    raise ReviewError('Shared answers cannot replace an accepted answer or omit its source.',409)
                if evidence:
                    if question["answer"] is not None:
                        raise ReviewError("Evidence cannot replace an existing answer.", 409)
                    if qid not in provenance or bindings.get(qid) != provenance[qid]["field_name"]:
                        raise ReviewError("Answer evidence does not match the original form field.", 409)
                validated.append((qid, self._validate_answer(question, answer.get("value")), question["revision"] + 1))
            if evidence:
                if set(provenance) != seen:
                    raise ReviewError("Answer evidence must match the answered questions exactly.")
                # Last check before mutation: source changes abort the entire write.
                self._check_profile_projection(db)
                validate_sources()
            stamp = now()
            for qid, value, revision in validated:
                db.execute("UPDATE wf_questions SET answer=?,answered_at=?,revision=revision+1,updated_at=? WHERE id=?",
                           (encode(value), stamp, stamp, qid))
            missing = db.execute("SELECT 1 FROM wf_questions WHERE process_id=? AND superseded=0 AND required=1 AND answer IS NULL", (process_id,)).fetchone()
            if not missing and not browser_blocked:
                self._resume_task(db, task["id"])
                db.execute("UPDATE queue SET status='queued',updated_at=? WHERE id=?", (stamp, process_id))
            db.execute("UPDATE wf_processes SET block_reason=?,revision=revision+1,updated_at=? WHERE id=?",
                       (process['block_reason'] if browser_blocked else "questions_pending" if missing else None,
                        stamp, process_id))
            receipt = {"request_id": request_id, "accepted": True, "process_ids": [process_id], "task_ids": [task["id"]]}
            db.execute("INSERT INTO wf_answer_requests VALUES (?,?,?,?,?)", (request_id, fingerprint, encode(body), encode(receipt), stamp))
            if evidence:
                for qid, value, revision in validated:
                    item = provenance[qid]
                    db.execute("""INSERT INTO wf_answer_evidence
                        (request_id,question_id,answer_revision,source_snapshot_hash,field_name,method,
                         citations,explanation,resolved_value,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                        (request_id, qid, revision, item["source_snapshot_hash"], item["field_name"], item["method"],
                         encode(item["citations"]), item["explanation"], encode(value), stamp))
            elif not shared:
                record_user_answers(db, process, request_id, validated, stamp)
            self._process_event(db, process_id, "answers_resolved_from_evidence" if evidence else
                                "shared_answers_saved" if shared else "answers_saved",
                                {"request_id": request_id, "question_ids": list(seen),
                                 **({'sources':shared_sources} if shared else {}),
                                 "resumed": not bool(missing) and not browser_blocked},
                                "evidence_resolver" if evidence else "question_pool" if shared else "user")
            if mutation_fence is not None:
                mutation_fence(db)
            return receipt

    def refresh_answer_evidence(self, process_id, lease_token, process_revision,
                                provenance, expected_answers, *, validate_sources):
        """Refresh proof for unchanged automatic answers after a source append.

        An active worker must independently re-review the current evidence.
        This operation cannot create, replace, or promote a user answer.
        """
        if not callable(validate_sources) or not isinstance(provenance, dict) or not provenance:
            raise ReviewError("Evidence refresh requires validated current sources.")
        provenance = self._validate_answer_provenance(provenance)
        if not isinstance(expected_answers, dict) or set(expected_answers) != set(provenance):
            raise ReviewError("Evidence refresh must identify every unchanged answer.")
        with self.transaction(True) as db:
            process, task = self._application_lease(db, process_id, lease_token)
            self._revision(process, process_revision)
            self._check_application(db, process)
            checkpoint = json.loads(task["checkpoint"])
            if (process["submission_status"] != "preparing" or checkpoint.get("submission_started")
                    or task["resource_quarantined"] or task["error"] == "submission_unknown"
                    or db.execute("SELECT 1 FROM wf_resource_locks WHERE resource=? AND quarantined=1",
                                  (task["resource"],)).fetchone()):
                raise ReviewError("This application cannot refresh answer evidence.", 409)
            bindings = checkpoint.get("question_bindings", {})
            changes = []
            for qid, item in provenance.items():
                expected = expected_answers[qid]
                if not isinstance(expected, dict):
                    raise ReviewError("An evidence refresh requires an answer revision and value.")
                question = db.execute("SELECT * FROM wf_questions WHERE id=? AND process_id=? AND superseded=0",
                                      (qid, process_id)).fetchone()
                if question is None or question["form_version"] != process["form_version"] or question["answer"] is None:
                    raise ReviewError("The answered question is no longer current.", 409, reason="stale_revision")
                self._revision(question, expected.get("question_revision"))
                if encode(expected.get("value")) != question["answer"]:
                    raise ReviewError("An evidence refresh cannot change the answer.", 409)
                prior = db.execute("SELECT * FROM wf_answer_evidence WHERE question_id=? AND answer_revision=?",
                                   (qid, question["revision"])).fetchone()
                if prior is None:
                    raise ReviewError("Only automatic answers can refresh their evidence.", 409)
                if not isinstance(bindings, dict) or bindings.get(qid) != item["field_name"] or item["field_name"] != prior["field_name"]:
                    raise ReviewError("Answer evidence does not match the original form field.", 409)
                changes.append((qid, prior, item))
            self._check_profile_projection(db)
            validate_sources()
            for qid, prior, item in changes:
                db.execute("""UPDATE wf_answer_evidence SET source_snapshot_hash=?,method=?,citations=?,explanation=?
                    WHERE request_id=? AND question_id=?""",
                    (item["source_snapshot_hash"], item["method"], encode(item["citations"]), item["explanation"],
                     prior["request_id"], qid))
            self._process_event(db, process_id, "answer_evidence_refreshed", {
                "answers": [{"question_id": qid, "answer_revision": prior["answer_revision"],
                             "previous_source_snapshot_hash": prior["source_snapshot_hash"],
                             "source_snapshot_hash": item["source_snapshot_hash"]} for qid, prior, item in changes],
            }, "evidence_resolver")
            return {"refreshed": len(changes), "process_id": process_id}

    def before_submit(self, process_id, token, form_version, *, validate_sources=None):
        """Commit this checkpoint before any external action that can submit."""
        form_version = text(form_version, "Form version", 200, required=True)
        if validate_sources is not None and not callable(validate_sources):
            raise ReviewError("Submission source validation must be callable.")
        with self.transaction(True) as db:
            process, task = self._application_lease(db, process_id, token)
            if process["submission_status"] != "preparing":
                raise ReviewError("The application is not ready for a submission attempt.", 409)
            self._check_application(db, process)
            if process["form_version"] is not None and process["form_version"] != form_version:
                raise ReviewError("The form changed. Revalidate its answers before submission.", 409)
            if db.execute("SELECT 1 FROM wf_questions WHERE process_id=? AND superseded=0 AND required=1 AND answer IS NULL", (process_id,)).fetchone():
                raise ReviewError("Required questions still need answers.", 409)
            self._check_profile_projection(db)
            if validate_sources is not None:
                validate_sources()
            stamp, attempt_id = now(), str(uuid.uuid4())
            db.execute("INSERT INTO wf_submission_attempts(id,process_id,task_id,form_version,started_at) VALUES (?,?,?,?,?)",
                       (attempt_id, process_id, task["id"], form_version, stamp))
            checkpoint = json.loads(task["checkpoint"])
            checkpoint.update({"submission_started": True, "submission_attempt_id": attempt_id})
            db.execute("UPDATE wf_tasks SET checkpoint=?,updated_at=? WHERE id=?", (encode(checkpoint), stamp, task["id"]))
            db.execute("UPDATE wf_processes SET submission_status='submission_unknown',form_version=?,block_reason='submission_confirmation_pending',revision=revision+1,updated_at=? WHERE id=?",
                       (form_version, stamp, process_id))
            self._process_event(db, process_id, "submission_started", {"attempt_id": attempt_id, "form_version": form_version})
            return {"attempt_id": attempt_id, "process_id": process_id, "submission_status": "submission_unknown"}

    @staticmethod
    def _submission_receipt(receipt):
        if not isinstance(receipt, dict):
            raise ReviewError("A submission receipt must be an object.")
        result = {"url": canonical_url(receipt.get("url")),
                  "evidence": text(receipt.get("evidence"), "Submission evidence", 20000, required=True),
                  "observed_at": text(receipt.get("observed_at"), "Evidence timestamp", 100, required=True),
                  "reference": text(receipt.get("reference", ""), "Submission reference", 500)}
        try:
            observed = datetime.fromisoformat(result["observed_at"].replace("Z", "+00:00"))
            if observed.tzinfo is None or observed > datetime.now(timezone.utc):
                raise ValueError()
        except ValueError:
            raise ReviewError("Evidence timestamp must be an observed time with a time zone.") from None
        return result

    def confirm_submission(self, process_id, token, receipt):
        receipt = self._submission_receipt(receipt)
        with self.transaction(True) as db:
            process, task = self._application_lease(db, process_id, token)
            return self._confirm_submission(db, process, task, receipt, "worker")

    def _confirm_submission(self, db, process, task, receipt, actor):
        if process["submission_status"] != "submission_unknown":
            raise ReviewError("A submission attempt is required before recording confirmation.", 409)
        checkpoint = json.loads(task["checkpoint"])
        attempt = db.execute("SELECT * FROM wf_submission_attempts WHERE id=? AND process_id=? AND task_id=?",
                             (checkpoint.get("submission_attempt_id"), process["id"], task["id"])).fetchone()
        if not checkpoint.get("submission_started") or attempt is None:
            raise ReviewError("The task has no matching submission attempt.", 409)
        observed = datetime.fromisoformat(receipt["observed_at"].replace("Z", "+00:00"))
        if observed < datetime.fromisoformat(attempt["started_at"]):
            raise ReviewError("The receipt predates this submission attempt.", 409)
        # Confirmation is evidence of what happened. Changed preferences cannot erase it.
        stamp = now()
        db.execute("UPDATE wf_submission_attempts SET confirmed_at=?,receipt=? WHERE id=?", (stamp, encode(receipt), attempt["id"]))
        db.execute("UPDATE wf_processes SET submission_status='submitted',receipt=?,block_reason=NULL,revision=revision+1,updated_at=? WHERE id=?",
                   (encode(receipt), stamp, process["id"]))
        db.execute("UPDATE queue SET status='applied',updated_at=? WHERE id=?", (stamp, process["id"]))
        self._release_task(db, task["id"], "succeeded", result={"process_id": process["id"], "receipt": receipt})
        self._process_event(db, process["id"], "submitted", {"attempt_id": attempt["id"], "receipt": receipt}, actor)
        return self._process_view(db, self._process(db, process["id"]))

    def resolve_submission(self, process_id, revision, receipt):
        """Record later confirmation after the previous worker has stopped.

        This operator/adapter boundary cannot restart Apply or declare a negative
        result from missing evidence. Browser execution remains unimplemented.
        """
        receipt = self._submission_receipt(receipt)
        with self.transaction(True) as db:
            process = self._process(db, process_id)
            self._revision(process, revision)
            task = self._task(db, process["task_id"])
            if task["status"] not in ("waiting_for_user", "failed") or task["resource_quarantined"]:
                raise ReviewError("Confirm the previous worker stopped before resolving submission.", 409)
            return self._confirm_submission(db, process, task, receipt, "operator")

    def track_response(self, process_id, revision, status, evidence):
        evidence = text(evidence, "Recruiting evidence", 20000, required=True)
        if not isinstance(status, str) or status not in RECRUITING_TRANSITIONS:
            raise ReviewError("Unknown recruiting status.")
        with self.transaction(True) as db:
            process = self._process(db, process_id)
            self._revision(process, revision)
            if process["submission_status"] != "submitted":
                raise ReviewError("Recruiting status requires a confirmed submission.", 409)
            previous = process["recruiting_status"]
            if status not in RECRUITING_TRANSITIONS[previous]:
                raise ReviewError("This recruiting transition is not allowed.", 409)
            db.execute("UPDATE wf_processes SET recruiting_status=?,revision=revision+1,updated_at=? WHERE id=?", (status, now(), process_id))
            self._process_event(db, process_id, "recruiting_updated", {"from": previous, "to": status, "evidence": evidence}, "tracker")
            return self._process_view(db, self._process(db, process_id))

    def _cancel_process(self, db, task):
        """Synchronize a safely cancelled task; never erase uncertain submission."""
        payload = task["payload"] if isinstance(task["payload"], dict) else json.loads(task["payload"])
        if task["kind"] != "apply":
            return
        if task["status"] not in ("queued", "retry_wait", "cancelled"):
            raise ReviewError("Only application work without a running or blocked worker can be cancelled.", 409)
        process = self._process(db, payload["process_id"])
        if process["submission_status"] in ("submission_unknown", "submitted"):
            raise ReviewError("Submission has started. Reconcile its outcome before changing this process.", 409)
        stamp = now()
        db.execute("UPDATE queue SET status='cancelled',updated_at=? WHERE id=?", (stamp, process["id"]))
        db.execute("UPDATE wf_processes SET block_reason='task_cancelled',revision=revision+1,updated_at=? WHERE id=?", (stamp, process["id"]))
        db.execute("UPDATE jobs SET decision='pending',revision=revision+1,updated_at=? WHERE key=? AND decision='approve'", (stamp, process["job_key"]))
        db.execute("UPDATE wf_questions SET superseded=1,updated_at=? WHERE process_id=? AND superseded=0", (stamp, process["id"]))
        self._process_event(db, process["id"], "cancelled", {"task_id": task["id"]}, "orchestrator")

    def reconcile_processes(self):
        """Project task failures and invalidate stale work without releasing a live worker."""
        changed = []
        with self.transaction(True) as db:
            for process in db.execute("SELECT * FROM wf_processes WHERE submission_status!='submitted'").fetchall():
                task = self.task(self._task(db, process["task_id"]))
                reason = None
                try:
                    self._check_application(db, process)
                except ReviewError:
                    reason = "authorization_changed"
                # Repair a stale question wait from an interrupted/older writer.
                # Stored answers are authoritative; never infer an answer from
                # absence of questions or use this to clear a browser blocker.
                if (reason is None and process['submission_status'] == 'preparing'
                        and task['status'] == 'waiting_for_user' and task['error'] == 'questions_pending'
                        and process['block_reason'] in (None, 'questions_pending')
                        and not task.get('resource_quarantined')
                        and not db.execute('SELECT 1 FROM wf_resource_locks WHERE task_id=?', (task['id'],)).fetchone()
                        and not task['checkpoint'].get('submission_started')
                        and task['failure_count'] < task['max_attempts']
                        and db.execute('SELECT 1 FROM wf_questions WHERE process_id=? AND superseded=0 AND form_version=?',
                                       (process['id'], process['form_version'])).fetchone()
                        and not db.execute('SELECT 1 FROM wf_questions WHERE process_id=? AND superseded=0 AND required=1 AND answer IS NULL',
                                           (process['id'],)).fetchone()
                        and not db.execute('SELECT 1 FROM wf_submission_attempts WHERE process_id=?', (process['id'],)).fetchone()):
                    self._resume_task(db, task['id'])
                    stamp = now()
                    db.execute("UPDATE queue SET status='queued',updated_at=? WHERE id=?", (stamp, process['id']))
                    db.execute('UPDATE wf_processes SET block_reason=NULL,revision=revision+1,updated_at=? WHERE id=?',
                               (stamp, process['id']))
                    self._process_event(db, process['id'], 'answers_reconciled', {'task_id': task['id']}, 'health_monitor')
                    changed.append(process['id'])
                    continue
                if process["submission_status"] == "submission_unknown":
                    reason = "submission_confirmation_pending" if task["status"] == "running" else "submission_unknown"
                elif reason and task["status"] in ("queued", "retry_wait"):
                    self._cancel_task(db, task["id"])
                    self._cancel_process(db, task)
                    changed.append(process["id"])
                    continue
                elif task["status"] == "failed":
                    reason = "task_failed"
                    db.execute("UPDATE queue SET status='failed',updated_at=? WHERE id=? AND status!='failed'", (now(), process["id"]))
                elif task["status"] == "retry_wait":
                    reason = "task_retry_wait"
                    db.execute("UPDATE queue SET status='queued',updated_at=? WHERE id=? AND status!='queued'", (now(), process["id"]))
                elif task["status"] == "cancelled":
                    if process["block_reason"] != "task_cancelled":
                        self._cancel_process(db, task)
                        changed.append(process["id"])
                    continue
                elif task.get("resource_quarantined") or (task["status"] == "running" and task["lease_until"] and task["lease_until"] <= now()):
                    reason = "worker_lease_lost"
                elif task["status"] == "waiting_for_user":
                    reason = reason or process["block_reason"] or "worker_blocked"
                if reason and reason != process["block_reason"]:
                    db.execute("UPDATE wf_processes SET block_reason=?,revision=revision+1,updated_at=? WHERE id=?", (reason, now(), process["id"]))
                    self._process_event(db, process["id"], "blocked", {"reason": reason}, "orchestrator")
                    changed.append(process["id"])
        return changed
