"""Versioned workflow: user decisions are authoritative; agent results are proposals."""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone

from application_flow import APPLICATION_SCHEMA, ApplicationMixin
from application_pool import ApplicationPoolMixin
from task_store import TASK_SCHEMA, TaskMixin
from ranking import validate_ranked_cards
from ranked_output import RANKED_OUTPUT_SCHEMA, RankedOutputMixin
from project_consent import CONSENT_SCHEMA, ProjectConsentMixin
from question_recovery import QUESTION_RECOVERY_SCHEMA
from unblock_instructions import UNBLOCK_SCHEMA, UnblockInstructionsMixin
from store import Store, ReviewError, digest, encode, material_hash, now, text, validate_catalog


WORKFLOW_SCHEMA = """
CREATE TABLE IF NOT EXISTS wf_commands (
 id TEXT PRIMARY KEY, payload_hash TEXT NOT NULL, request_body TEXT NOT NULL,
 receipt TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS wf_cards (
 id TEXT PRIMARY KEY, company_id TEXT NOT NULL REFERENCES companies(id),
 revision TEXT NOT NULL, company_revision INTEGER NOT NULL, jobs TEXT NOT NULL,
 status TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS wf_ready_company ON wf_cards(company_id) WHERE status='ready';
CREATE TABLE IF NOT EXISTS wf_card_content (
 card_id TEXT PRIMARY KEY REFERENCES wf_cards(id), content TEXT NOT NULL,
 company_fingerprint TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS wf_selection (
 job_key TEXT PRIMARY KEY REFERENCES jobs(key), revision INTEGER NOT NULL,
 selected INTEGER NOT NULL, reason TEXT NOT NULL, evaluated INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS wf_rank_pending (
 task_id TEXT NOT NULL REFERENCES wf_tasks(id), job_key TEXT NOT NULL REFERENCES jobs(key),
 revision INTEGER NOT NULL, PRIMARY KEY(task_id,job_key)
);
CREATE TABLE IF NOT EXISTS wf_observations (
 job_key TEXT PRIMARY KEY REFERENCES jobs(key), availability TEXT NOT NULL,
 source TEXT NOT NULL, verified_at TEXT, requires_rank INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS wf_reviewed_jobs (
 job_key TEXT PRIMARY KEY REFERENCES jobs(key), material_hash TEXT NOT NULL, decision_id TEXT NOT NULL
);
"""


def capabilities():
    return {"api_version": 1, "worker_enabled": False, "worker_status": "not_configured",
            "explicit_apply": True, "questions": True, "unblock_instructions": True, "feed_delivery": "poll",
            "question_scopes": ["application"], "remote_access": False}


class WorkflowStore(ApplicationPoolMixin, UnblockInstructionsMixin, ProjectConsentMixin, RankedOutputMixin, ApplicationMixin, TaskMixin, Store):
    """Additive schema; legacy saved approvals never become executable tasks."""

    def __init__(self, path, *, low_watermark=10, target_cards=30, refill_cooldown=300):
        if not 0 <= low_watermark < target_cards or refill_cooldown < 0:
            raise ValueError("Require 0 <= low_watermark < target_cards and nonnegative cooldown.")
        super().__init__(path)
        self.low_watermark, self.target_cards = low_watermark, target_cards
        self.refill_cooldown = refill_cooldown
        with self.connect() as db:
            db.executescript(TASK_SCHEMA + APPLICATION_SCHEMA + WORKFLOW_SCHEMA + RANKED_OUTPUT_SCHEMA + CONSENT_SCHEMA + QUESTION_RECOVERY_SCHEMA + UNBLOCK_SCHEMA)
        self.initialize_application_pool()

    @staticmethod
    def _keys(value, label="Excluded job keys"):
        if not isinstance(value, list) or len(value) > 1000:
            raise ReviewError(f"{label} must be a list of at most 1000 keys.")
        keys = [text(key, label, 1000, required=True) for key in value]
        if len(keys) != len(set(keys)):
            raise ReviewError(f"{label} must not contain duplicates.")
        return keys

    @staticmethod
    def _replay(db, request_id, payload):
        previous = db.execute("SELECT * FROM wf_commands WHERE id=?", (request_id,)).fetchone()
        if previous:
            if previous["payload_hash"] != digest(payload):
                raise ReviewError("This request ID already belongs to another command.", 409, reason="request_id_conflict")
            return json.loads(previous["receipt"])
        return None

    @staticmethod
    def _save_receipt(db, request_id, payload, receipt):
        db.execute("INSERT INTO wf_commands VALUES (?,?,?,?,?)",
                   (request_id, digest(payload), encode(payload), encode(receipt), now()))
        return receipt

    def _eligible(self, db, *, company_id=None, exclude=(), ranked=True, include_reserved=True, include_pending=False):
        """One filter used by feed, discovery, and rank result validation."""
        excluded = set(exclude)
        reviewed = {(r["job_key"], r["material_hash"]) for r in db.execute("SELECT * FROM card_review_jobs")}
        reviewed.update((r["job_key"], r["material_hash"]) for r in db.execute("SELECT * FROM wf_reviewed_jobs"))
        blocked = {r[0] for r in db.execute("SELECT job_key FROM queue WHERE kind='application' AND status IN ('queued','in_progress','needs_input','applied')")}
        blocked.update(r[0] for r in db.execute("SELECT job_key FROM wf_processes WHERE submission_status IN ('submission_unknown','submitted')"))
        observations = {r["job_key"]: r for r in db.execute("SELECT * FROM wf_observations")}
        selection = {r["job_key"]: r for r in db.execute("SELECT * FROM wf_selection")}
        pending = {(r[0], r[1]) for r in db.execute("SELECT p.job_key,p.revision FROM wf_rank_pending p JOIN wf_tasks t ON t.id=p.task_id WHERE t.status IN ('queued','running','retry_wait','waiting_for_user')")}
        if not include_reserved:
            for card in db.execute("SELECT jobs FROM wf_cards WHERE status IN ('ready','prepared')"):
                excluded.update(job["key"] for job in json.loads(card["jobs"]))
        result = []
        for row in db.execute("SELECT j.* FROM jobs j JOIN companies c ON c.id=j.company_id WHERE c.excluded=0 ORDER BY j.first_seen,j.key"):
            job = self.job(row)
            if (company_id is not None and job["company_id"] != company_id) or job["key"] in excluded:
                continue
            if job["decision"] != "pending" or job["key"] in blocked:
                continue
            if (job["key"], material_hash(job["data"])) in reviewed:
                continue
            observation = observations.get(job["key"])
            if observation and observation["availability"] == "closed":
                continue
            if not include_pending and (job["key"], job["revision"]) in pending:
                continue
            chosen = selection.get(job["key"])
            if ranked:
                if chosen and chosen["revision"] == job["revision"] and not chosen["selected"]:
                    continue
                if observation and observation["requires_rank"] and (
                        not chosen or chosen["revision"] != job["revision"] or not chosen["selected"]):
                    continue
            result.append(job)
        return result

    def _flashcards(self, db, companies, jobs, queue):
        eligible = {j["key"] for j in self._eligible(db)}
        return Store._flashcards(db, companies, [j for j in jobs if j["key"] in eligible], queue)

    @staticmethod
    def _company_fingerprint(company):
        # Review counters/revisions can change while a next card waits. Source
        # facts and user notes still belong to the context behind its prose.
        return digest({field: company[field] for field in ("name", "data", "note")})

    def _stale_card(self, db, card, eligible):
        db.execute("UPDATE wf_cards SET status='stale' WHERE id=?", (card["id"],))
        jobs = json.loads(card["jobs"])
        ranked = (db.execute("SELECT 1 FROM wf_card_content WHERE card_id=?", (card["id"],)).fetchone()
                  or any(db.execute("SELECT 1 FROM wf_selection WHERE job_key=?", (j["key"],)).fetchone() for j in jobs))
        if not ranked:
            return
        # A summary describes an exact cohort. Re-rank survivors instead of
        # reusing this prose on a smaller card or falling back to source copy.
        for job in jobs:
            db.execute("UPDATE wf_selection SET selected=0,evaluated=0,reason='Card context changed; ranking required.' WHERE job_key=? AND revision=? AND selected=1",
                       (job["key"], job["revision"]))
            eligible.pop(job["key"], None)

    def _publish_cards(self, db):
        eligible = {j["key"]: j for j in self._eligible(db)}
        companies = {r["id"]: self.company(r) for r in db.execute("SELECT * FROM companies")}
        pending_outputs = self._pending_rank_card_ids(db)
        reserved = set()
        for card in db.execute("SELECT * FROM wf_cards WHERE status='ready' ORDER BY created_at,id").fetchall():
            jobs = json.loads(card["jobs"])
            company = companies[card["company_id"]]
            valid = company["revision"] == card["company_revision"] and all(
                j["key"] in eligible and j["revision"] == eligible[j["key"]]["revision"] for j in jobs)
            if valid:
                reserved.add(card["company_id"])
            else:
                self._stale_card(db, card, eligible)
        for card in db.execute("SELECT c.*,x.content,x.company_fingerprint FROM wf_cards c JOIN wf_card_content x ON x.card_id=c.id WHERE c.status='prepared' ORDER BY c.created_at,c.id").fetchall():
            if card["id"] in pending_outputs:
                continue
            jobs = json.loads(card["jobs"])
            company = companies[card["company_id"]]
            valid = (not company["excluded"] and self._company_fingerprint(company) == card["company_fingerprint"]
                     and all(j["key"] in eligible and eligible[j["key"]]["revision"] == j["revision"] for j in jobs))
            if not valid:
                self._stale_card(db, card, eligible)
                continue
            if card["company_id"] in reserved:
                continue
            revision = digest({"company_revision": company["revision"], "jobs": jobs,
                               "content": json.loads(card["content"])})
            db.execute("UPDATE wf_cards SET status='ready',company_revision=?,revision=?,created_at=? WHERE id=?",
                       (company["revision"], revision, now(), card["id"]))
            reserved.add(card["company_id"])
        ranked_keys = {r[0] for r in db.execute("SELECT job_key FROM wf_selection")}
        grouped = {}
        for job in eligible.values():
            if job["company_id"] not in reserved and job["key"] not in ranked_keys:
                grouped.setdefault(job["company_id"], []).append(job)
        for cid, jobs in grouped.items():
            # Existing cards keep their identity and order when new research arrives.
            def order(job):
                rank = job["data"].get("rank")
                return (rank if type(rank) in (int, float) else float("inf"), job["first_seen"], job["key"])
            jobs = sorted(jobs, key=order)[:3]
            revision = digest({"company_revision": companies[cid]["revision"], "jobs": jobs})
            db.execute("INSERT INTO wf_cards VALUES (?,?,?,?,?,'ready',?)",
                       (str(uuid.uuid4()), cid, revision, companies[cid]["revision"], encode(jobs), now()))

    def _feed(self, db):
        cards = []
        for row in db.execute("SELECT * FROM wf_cards WHERE status='ready' ORDER BY created_at,id"):
            company = self.company(self._row(db, "companies", "id", row["company_id"]))
            content = db.execute("SELECT content FROM wf_card_content WHERE card_id=?", (row["id"],)).fetchone()
            cards.append({"id": row["id"], "revision": row["revision"], "company": company,
                          "jobs": json.loads(row["jobs"]), "content": json.loads(content[0]) if content else None,
                          "created_at": row["created_at"]})
        refill = db.execute("SELECT value FROM wf_settings WHERE key='refill_after'").fetchone()
        return {"cards": cards, "counts": {"ready_cards": len(cards), "ready_jobs": sum(len(c["jobs"]) for c in cards)},
                "refill": {"low_watermark": self.low_watermark, "target_cards": self.target_cards,
                           "next_allowed_at": refill[0] if refill else None}, "capabilities": capabilities()}

    def feed(self):
        # Publication is durable; polling does not consume cards or mark jobs viewed.
        with self.transaction(True) as db:
            self._publish_cards(db)
            return self._feed(db)

    def _request_discovery(self, db, company_id=None, exclude=(), *, refill=False, note=""):
        dedupe = "refill" if refill else f"company:{company_id}"
        existing = db.execute("SELECT id FROM wf_tasks WHERE dedupe_key=? AND status IN ('queued','running','waiting_for_user','retry_wait')", (dedupe,)).fetchone()
        if existing:
            if refill:
                return existing[0]
            raise ReviewError("Research for this company is already active.", 409)
        candidates = self._eligible(db, company_id=company_id, exclude=exclude, ranked=False, include_reserved=False)
        # Previously rejected ranks need new evidence or a new user request to run again.
        if refill:
            already = {(r["job_key"], r["revision"]) for r in db.execute("SELECT * FROM wf_selection WHERE evaluated=1")}
            candidates = [j for j in candidates if (j["key"], j["revision"]) not in already]
        # All remaining candidates await ranking, including the next batch.
        # Pending is distinct from a completed rejection, so overflow and failed
        # batches remain discoverable on a later scheduling pass.
        for job in candidates:
            db.execute("INSERT INTO wf_selection VALUES (?,?,0,?,0) ON CONFLICT(job_key) DO UPDATE SET revision=excluded.revision,selected=0,reason=excluded.reason,evaluated=0",
                       (job["key"], job["revision"], "Waiting for ranking."))
        candidates = candidates[:200]
        payload = {"company_id": company_id, "exclude_job_keys": list(exclude),
                   "excluded_company_ids": [r[0] for r in db.execute("SELECT id FROM companies WHERE excluded=1")],
                   "target_cards": self.target_cards, "note": note, "refill": refill,
                   "candidates": candidates[:200]}
        kind = "rank" if candidates else "explore"
        # Request exclusions also apply to publication of this posting version.
        for key in exclude:
            job = self._row(db, "jobs", "key", key)
            db.execute("INSERT INTO wf_selection VALUES (?,?,0,?,1) ON CONFLICT(job_key) DO UPDATE SET revision=excluded.revision,selected=0,reason=excluded.reason,evaluated=1",
                       (key, job["revision"], "Excluded from this research request."))
        task_id = self._enqueue_task(db, kind, payload, dedupe,
                                     resource="browser" if kind == "explore" else None)
        if kind == "rank":
            self._hold_rank(db, task_id, candidates)
        return task_id

    @staticmethod
    def _hold_rank(db, task_id, candidates):
        for job in candidates:
            db.execute("INSERT INTO wf_rank_pending VALUES (?,?,?)", (task_id, job["key"], job["revision"]))

    def receive_decision(self, payload):
        request_id = text(payload.get("request_id"), "Request ID", 200, required=True)
        card_id = text(payload.get("card_id"), "Card ID", 200, required=True)
        action = payload.get("action")
        if not isinstance(action, str) or action not in {"apply", "find_more", "exclude", "pass", "later"}:
            raise ReviewError("Choose apply, find_more, exclude, pass, or later.")
        selected = self._keys(payload.get("selected", []), "Selected job keys")
        exclude = self._keys(payload.get("exclude_job_keys", []))
        note = text(payload.get("note", ""), "Decision note")
        if action in {"apply", "pass", "later"} and not selected:
            raise ReviewError("Select at least one job for this action.")
        if action in {"find_more", "exclude"} and selected:
            raise ReviewError("This action does not accept a job selection.")
        if action != "find_more" and exclude:
            raise ReviewError("Only find_more accepts research exclusions.")
        with self.transaction(True) as db:
            previous = self._replay(db, request_id, payload)
            if previous:
                return previous
            self._publish_cards(db)
            card = self._row(db, "wf_cards", "id", card_id)
            if card["status"] != "ready" or card["revision"] != payload.get("card_revision"):
                raise ReviewError("This card changed or was reviewed. Refresh before making a new decision.", 409, reason="stale_revision")
            jobs = json.loads(card["jobs"])
            if not set(selected) <= {j["key"] for j in jobs}:
                raise ReviewError("Select only jobs from this card.")
            cid = card["company_id"]
            company = self._row(db, "companies", "id", cid)
            for key in exclude:
                if self._row(db, "jobs", "key", key)["company_id"] != cid:
                    raise ReviewError("Research exclusions must belong to this company.")
            task_ids, process_ids = [], []
            stamp = now()
            # Store the original cohort before changes. This also keeps legacy card views in sync.
            history = [{"key": j["key"], "revision": j["revision"], "title": j["data"]["title"],
                        "material_hash": material_hash(j["data"])} for j in jobs]
            for job in history:
                db.execute("INSERT INTO wf_reviewed_jobs VALUES (?,?,?) ON CONFLICT(job_key) DO UPDATE SET material_hash=excluded.material_hash,decision_id=excluded.decision_id",
                           (job["key"], job["material_hash"], request_id))
            if action == "exclude":
                self._exclude(db, cid)
                db.execute("UPDATE companies SET excluded=1 WHERE id=?", (cid,))
            else:
                for key in selected:
                    db.execute("UPDATE jobs SET decision=?,changed=0,revision=revision+1,updated_at=? WHERE key=?",
                               ("approve" if action == "apply" else action, stamp, key))
                    if action == "apply":
                        pid = self._create_application(db, self._row(db, "jobs", "key", key), request_id)
                        process_ids.append(pid)
                        task_ids.append(self._row(db, "wf_processes", "id", pid)["task_id"])
            db.execute("UPDATE wf_cards SET status='reviewed' WHERE id=?", (card_id,))
            combined = "\n".join(filter(None, (company["note"], note)))
            db.execute("UPDATE companies SET note=?,revision=revision+1,updated_at=? WHERE id=?", (combined, stamp, cid))
            if action == "find_more":
                task_ids.append(self._request_discovery(db, cid, exclude, note=note))
            self._publish_cards(db)
            receipt = {"request_id": request_id, "accepted": True, "decision_id": request_id,
                       "card_id": card_id, "action": action, "process_ids": process_ids, "task_ids": task_ids}
            return self._save_receipt(db, request_id, payload, receipt)

    def cancel_task_command(self, payload):
        request_id = text(payload.get("request_id"), "Request ID", 200, required=True)
        task_id = text(payload.get("task_id"), "Task ID", 200, required=True)
        with self.transaction(True) as db:
            previous = self._replay(db, request_id, payload)
            if previous:
                return previous
            task = self.task(self._task(db, task_id))
            self._cancel_task(db, task_id)
            if task["kind"] == "apply":
                self._cancel_process(db, task)
            receipt = {"request_id": request_id, "accepted": True, "process_ids": [], "task_ids": [task_id]}
            return self._save_receipt(db, request_id, payload, receipt)

    def _exclude(self, db, cid):
        # Both API versions enforce the same execution boundary.
        tasks = [self.task(self._task(db, r[0])) for r in db.execute("SELECT id FROM wf_tasks WHERE status IN ('queued','retry_wait','running','waiting_for_user')")]
        relevant = []
        for task in tasks:
            task_cid = task["payload"].get("company_id")
            if task["kind"] == "apply":
                process = self._row(db, "wf_processes", "id", task["payload"]["process_id"])
                task_cid = self._row(db, "jobs", "key", process["job_key"])["company_id"]
            if task_cid == cid:
                if task["status"] not in {"queued", "retry_wait"}:
                    raise ReviewError("Work at this company has started. Resolve it before exclusion.", 409)
                relevant.append(task)
        for task in relevant:
            self._cancel_task(db, task["id"])
            if task["kind"] == "apply":
                self._cancel_process(db, task)
        super()._exclude(db, cid)

    def _cancel(self, db, row, note):
        process = db.execute("SELECT task_id FROM wf_processes WHERE id=?", (row["id"],)).fetchone()
        if process:
            task = self.task(self._task(db, process[0]))
            self._cancel_task(db, task["id"])
            self._cancel_process(db, task)
        else:
            super()._cancel(db, row, note)

    def update_status(self, qid, status, note=""):
        with self.transaction() as db:
            if db.execute("SELECT 1 FROM wf_processes WHERE id=?", (qid,)).fetchone():
                raise ReviewError("Use the versioned process lifecycle for this application.", 409)
        return super().update_status(qid, status, note)

    def schedule_refill(self):
        with self.transaction(True) as db:
            self._publish_cards(db)
            if self._feed(db)["counts"]["ready_cards"] >= self.low_watermark:
                return None
            setting = db.execute("SELECT value FROM wf_settings WHERE key='refill_after'").fetchone()
            if setting and setting[0] > now():
                return None
            task_id = self._request_discovery(db, refill=True)
            deadline = (datetime.now(timezone.utc) + timedelta(seconds=self.refill_cooldown)).isoformat(timespec="microseconds")
            db.execute("INSERT INTO wf_settings VALUES ('refill_after',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (deadline,))
            return task_id

    def discovery_context(self, task_id, token):
        """Refresh exclusions at dispatch; stored task inputs are historical snapshots."""
        with self.transaction(True) as db:
            task = self.task(self._leased_task(db, task_id, token))
            payload = task["payload"]
            cid = payload.get("company_id")
            if cid and self._row(db, "companies", "id", cid)["excluded"]:
                raise ReviewError("The company is excluded.", 409)
            candidates = self._eligible(db, company_id=cid, exclude=payload.get("exclude_job_keys", []),
                                        ranked=False, include_reserved=False, include_pending=True)
            keys = {j["key"]: j["revision"] for j in payload.get("candidates", [])}
            candidates = [j for j in candidates if keys.get(j["key"]) == j["revision"]]
            company_ids = {j["company_id"] for j in candidates}
            if cid:
                company_ids.add(cid)
            companies = [self.company(self._row(db, "companies", "id", key)) for key in sorted(company_ids)]
            if task["kind"] == "rank":
                fingerprints = {c["id"]: self._company_fingerprint(c) for c in companies}
                checkpoint = task["checkpoint"]
                if checkpoint.get("rank_input_lease") == token and checkpoint.get("rank_company_fingerprints") != fingerprints:
                    raise ReviewError("Company context changed during ranking. Load a new ranking task.", 409, reason="stale_revision")
                checkpoint.update(rank_input_lease=token, rank_company_fingerprints=fingerprints)
                db.execute("UPDATE wf_tasks SET checkpoint=?,updated_at=? WHERE id=?", (encode(checkpoint), now(), task_id))
            return {**payload, "candidates": candidates, "companies": companies,
                    "excluded_company_ids": [r[0] for r in db.execute("SELECT id FROM companies WHERE excluded=1")]}

    def accept_exploration(self, task_id, token, result):
        """Accept a bounded source batch; no scraping occurs in this module."""
        if not isinstance(result, dict) or not isinstance(result.get("companies"), list):
            raise ReviewError("Explorer result must contain a companies list (empty is valid).")
        catalog = validate_catalog(result) if result["companies"] else result
        source = text(result.get("source"), "Source evidence", 10000, required=True)
        if len(catalog["companies"]) > 50 or sum(len(c["jobs"]) for c in catalog["companies"]) > 200:
            raise ReviewError("Explorer batch exceeds 50 companies or 200 jobs.")
        with self.transaction(True) as db:
            task = self.task(self._leased_task(db, task_id, token))
            if task["kind"] != "explore":
                raise ReviewError("This task is not exploration.", 409)
            payload = task["payload"]
            cid = payload.get("company_id")
            if cid and any(c["id"] != cid for c in catalog["companies"]):
                raise ReviewError("Company research returned a different company.")
            for company in catalog["companies"]:
                saved = db.execute("SELECT excluded FROM companies WHERE id=?", (company["id"],)).fetchone()
                if saved and saved[0]:
                    raise ReviewError("Explorer returned an excluded company.", 409)
            if catalog["companies"]:
                self._import(db, catalog, source)
                for company in catalog["companies"]:
                    for job in company["jobs"]:
                        key = f"{company['id']}:{job['job_id']}"
                        alias = db.execute("SELECT job_key FROM aliases WHERE alias=?", (key,)).fetchone()
                        key = alias[0] if alias else key
                        availability = job.get("availability", "unknown")
                        if availability not in {"open", "closed", "unknown"}:
                            raise ReviewError("Availability must be open, closed, or unknown.")
                        verified_at = job.get("verified_at")
                        if verified_at is not None:
                            try:
                                verified = datetime.fromisoformat(text(verified_at, "Verification time", 100, required=True).replace("Z", "+00:00"))
                                if verified.tzinfo is None or verified > datetime.now(timezone.utc):
                                    raise ValueError()
                            except ValueError:
                                raise ReviewError("Verification time must be a past timestamp with a time zone.") from None
                        db.execute("INSERT INTO wf_observations VALUES (?,?,?,?,1) ON CONFLICT(job_key) DO UPDATE SET availability=excluded.availability,source=excluded.source,verified_at=excluded.verified_at",
                                   (key, availability, source, verified_at))
            self._release_task(db, task_id, "succeeded", result={"companies": len(catalog["companies"])})
            candidates = self._eligible(db, company_id=cid, exclude=payload.get("exclude_job_keys", []), ranked=False, include_reserved=False)
            evaluated = {(r["job_key"], r["revision"]) for r in db.execute("SELECT * FROM wf_selection WHERE evaluated=1")}
            candidates = [j for j in candidates if (j["key"], j["revision"]) not in evaluated]
            next_id = None
            if candidates:
                next_id = self._enqueue_task(db, "rank", {**payload, "candidates": candidates[:200], "after_explore": True}, task["dedupe_key"])
                self._hold_rank(db, next_id, candidates[:200])
            return {"task_id": task_id, "next_task_id": next_id}

    def accept_ranking(self, task_id, token, result):
        with self.transaction(True) as db:
            task = self.task(self._leased_task(db, task_id, token))
            if task["kind"] != "rank":
                raise ReviewError("This task is not ranking.", 409)
            checkpoint = task["checkpoint"]
            if checkpoint.get("rank_input_lease") != token or "rank_company_fingerprints" not in checkpoint:
                raise ReviewError("Load this task's discovery context before returning ranker output.", 409)
            for cid, fingerprint in checkpoint["rank_company_fingerprints"].items():
                company = self.company(self._row(db, "companies", "id", cid))
                if self._company_fingerprint(company) != fingerprint:
                    raise ReviewError("Company context changed while ranking. Rank its current facts.", 409, reason="stale_revision")
            payload = task["payload"]
            candidates = {j["key"]: j for j in payload.get("candidates", [])}
            eligible = {j["key"]: j for j in self._eligible(db, company_id=payload.get("company_id"), exclude=payload.get("exclude_job_keys", []), ranked=False, include_reserved=False, include_pending=True)}
            cards = validate_ranked_cards(result, candidates, eligible)
            selected = {j["key"]: j["reason"] for card in cards for j in card["content"]["jobs"]}
            for key, candidate in candidates.items():
                if key in eligible and eligible[key]["revision"] == candidate["revision"]:
                    db.execute("INSERT INTO wf_selection VALUES (?,?,?,?,1) ON CONFLICT(job_key) DO UPDATE SET revision=excluded.revision,selected=excluded.selected,reason=excluded.reason,evaluated=1",
                               (key, candidate["revision"], int(key in selected), selected.get(key, "Not selected by this ranking.")))
            card_ids, output_cards = [], []
            for card in cards:
                cid = card["company_id"]
                company = self.company(self._row(db, "companies", "id", cid))
                card_id = str(uuid.uuid4())
                revision = digest({"company_revision": company["revision"], "jobs": card["jobs"], "content": card["content"]})
                db.execute("INSERT INTO wf_cards VALUES (?,?,?,?,?,'prepared',?)",
                           (card_id, cid, revision, company["revision"], encode(card["jobs"]), now()))
                db.execute("INSERT INTO wf_card_content VALUES (?,?,?)",
                           (card_id, encode(card["content"]), self._company_fingerprint(company)))
                card_ids.append(card_id)
                output_cards.append({"id": card_id, "revision": revision, "company": company,
                                     "jobs": card["jobs"], "content": card["content"]})
            receipt = {"task_id": task_id, "next_task_id": None, "selected_jobs": len(selected),
                       "card_ids": card_ids, "artifact_status": "pending"}
            self._queue_ranked_output(db, task, output_cards, receipt)
        return self.repair_ranked_outputs(task_id)[0]
