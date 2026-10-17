"""Durable local review state. This module never contacts an employer."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

ACTIVE = ("queued", "in_progress", "needs_input")
BLOCKING = (*ACTIVE, "applied")
MATERIAL = ("title", "url", "location", "arrangement", "salary", "level", "gaps")
DECISIONS = {"pending", "later", "approve", "pass"}
CARD_HISTORY_MIGRATION = "flashcard-research-history-v1"


class ReviewError(Exception):
    def __init__(self, message, status=400, *, reason=None):
        super().__init__(message)
        self.status = status
        self.reason = reason


def now():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def encode(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(encode(value).encode()).hexdigest()


def text(value, label, limit=2500, *, required=False):
    if not isinstance(value, str) or len(value) > limit or (required and not value.strip()):
        raise ReviewError(f"{label} must be {'nonempty ' if required else ''}text under {limit + 1} characters.")
    return value


def canonical_url(value):
    text(value, "Job URL", 10000, required=True)
    try:
        parts = urlsplit(value)
        if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
            raise ValueError()
        port = parts.port
    except ValueError:
        raise ReviewError("Each job must have an HTTPS URL without account credentials.") from None
    host = parts.hostname.lower()
    if port and port != 443:
        host += f":{port}"
    tracking = {"gh_src", "source", "src", "ref", "referral", "trackingid"}
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
             if not k.lower().startswith("utm_") and k.lower() not in tracking]
    return urlunsplit(("https", host, parts.path.rstrip("/") or "/", urlencode(sorted(query)), ""))


def material_hash(data):
    fields = {key: data.get(key) for key in MATERIAL}
    fields["url"] = canonical_url(data["url"])
    return digest(fields)


def validate_catalog(catalog):
    if not isinstance(catalog, dict) or not isinstance(catalog.get("companies"), list) or not catalog["companies"]:
        raise ReviewError("The catalog must contain a nonempty companies list.")
    try:
        cleaned = json.loads(encode(catalog))
    except (ValueError, TypeError):
        raise ReviewError("The catalog must contain valid JSON values.") from None
    company_ids, keys, urls = set(), set(), {}
    for company in cleaned["companies"]:
        if not isinstance(company, dict):
            raise ReviewError("Each company must be an object.")
        cid = text(company.get("id"), "Company ID", 200, required=True)
        if ":" in cid or cid != cid.strip() or cid in company_ids:
            raise ReviewError("Company IDs must be unique and cannot contain a colon or outer spaces.")
        company_ids.add(cid)
        text(company.get("name"), "Company name", 500, required=True)
        jobs = company.get("jobs")
        if not isinstance(jobs, list) or not jobs:
            raise ReviewError(f"{cid} must contain at least one job.")
        for job in jobs:
            if not isinstance(job, dict):
                raise ReviewError("Each job must be an object.")
            jid = text(job.get("job_id"), "Job ID", 500, required=True)
            if jid != jid.strip():
                raise ReviewError("Job IDs cannot contain outer spaces.")
            key = f"{cid}:{jid}"
            if key in keys:
                raise ReviewError(f"Duplicate job ID in this catalog: {key}.")
            keys.add(key)
            text(job.get("title"), "Job title", 2000, required=True)
            url = canonical_url(job.get("url"))
            if url in urls:
                raise ReviewError(f"Duplicate job URL in this catalog: {url}.")
            urls[url] = cid
    return cleaned


SCHEMA = """
CREATE TABLE IF NOT EXISTS companies (
 id TEXT PRIMARY KEY, name TEXT NOT NULL, data TEXT NOT NULL,
 excluded INTEGER NOT NULL DEFAULT 0, research_status TEXT NOT NULL DEFAULT 'none',
 note TEXT NOT NULL DEFAULT '', revision INTEGER NOT NULL DEFAULT 1,
 first_seen TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS jobs (
 key TEXT PRIMARY KEY, company_id TEXT NOT NULL REFERENCES companies(id),
 job_id TEXT NOT NULL, canonical_url TEXT NOT NULL UNIQUE, data TEXT NOT NULL,
 revision INTEGER NOT NULL DEFAULT 1, decision TEXT NOT NULL DEFAULT 'pending',
 note TEXT NOT NULL DEFAULT '', first_seen TEXT NOT NULL, updated_at TEXT NOT NULL,
 changed INTEGER NOT NULL DEFAULT 0, UNIQUE(company_id,job_id)
);
CREATE TABLE IF NOT EXISTS aliases (
 alias TEXT PRIMARY KEY, job_key TEXT NOT NULL REFERENCES jobs(key)
);
CREATE TABLE IF NOT EXISTS queue (
 id TEXT PRIMARY KEY, kind TEXT NOT NULL, company_id TEXT NOT NULL REFERENCES companies(id),
 job_key TEXT REFERENCES jobs(key), status TEXT NOT NULL, snapshot TEXT NOT NULL,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL, note TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS queue_job ON queue(job_key,status);
CREATE INDEX IF NOT EXISTS queue_company ON queue(company_id,kind,status);
CREATE TABLE IF NOT EXISTS requests (
 id TEXT PRIMARY KEY, payload_hash TEXT NOT NULL, queue_ids TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS imports (
 id TEXT PRIMARY KEY, source TEXT NOT NULL, fingerprint TEXT NOT NULL,
 created_at TEXT NOT NULL, added_companies INTEGER NOT NULL,
 added_jobs INTEGER NOT NULL, updated_jobs INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS migrations (source TEXT PRIMARY KEY, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS card_reviews (
 id TEXT PRIMARY KEY, company_id TEXT NOT NULL REFERENCES companies(id),
 payload_hash TEXT NOT NULL, action TEXT NOT NULL, jobs TEXT NOT NULL,
 selected TEXT NOT NULL, note TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS card_review_jobs (
 job_key TEXT PRIMARY KEY REFERENCES jobs(key), material_hash TEXT NOT NULL,
 review_id TEXT NOT NULL REFERENCES card_reviews(id)
);
"""


class Store:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript(SCHEMA)
        with self.transaction(True) as db:
            self._migrate_research_cards(db)

    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        return db

    @contextmanager
    def transaction(self, write=False):
        db = self.connect()
        try:
            db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def company(row):
        result = dict(row)
        result["data"] = json.loads(result["data"])
        result["excluded"] = bool(result["excluded"])
        return result

    @staticmethod
    def job(row):
        result = dict(row)
        result.pop("canonical_url", None)
        result["data"] = json.loads(result["data"])
        result["changed"] = bool(result["changed"])
        return result

    @staticmethod
    def entry(row):
        result = dict(row)
        result["snapshot"] = json.loads(result["snapshot"])
        return result

    @staticmethod
    def _migrate_research_cards(db):
        """Keep earlier research choices handled when the card view is first installed."""
        if db.execute("SELECT 1 FROM migrations WHERE source=?", (CARD_HISTORY_MIGRATION,)).fetchone():
            return

        def timestamp(value):
            try:
                result = datetime.fromisoformat(value.replace("Z", "+00:00"))
                if result.tzinfo is None:
                    raise ValueError()
                return result
            except (AttributeError, ValueError):
                raise ReviewError("A saved research or job timestamp is invalid. Check it before migrating card history.") from None

        for entry in db.execute("SELECT * FROM queue WHERE kind='research' ORDER BY created_at,rowid").fetchall():
            requested_at = timestamp(entry["created_at"])
            jobs = []
            for row in db.execute("SELECT * FROM jobs WHERE company_id=? ORDER BY first_seen,rowid", (entry["company_id"],)):
                # No old posting snapshot exists. An edited row may contain facts the user never reviewed.
                if timestamp(row["first_seen"]) > requested_at or timestamp(row["updated_at"]) > requested_at:
                    continue
                data = json.loads(row["data"])
                jobs.append({"key": row["key"], "revision": row["revision"], "title": data["title"],
                             "material_hash": material_hash(data)})
            if not jobs:
                continue
            review_id = f"legacy-research:{entry['id']}"
            fingerprint = digest({"legacy_research_request": entry["id"], "jobs": jobs})
            db.execute("INSERT INTO card_reviews VALUES (?,?,?,?,?,?,?,?)", (review_id, entry["company_id"], fingerprint,
                       "research", encode(jobs), "[]", entry["note"], entry["created_at"]))
            for job in jobs:
                db.execute("INSERT INTO card_review_jobs VALUES (?,?,?) ON CONFLICT(job_key) DO UPDATE SET material_hash=excluded.material_hash,review_id=excluded.review_id", (job["key"], job["material_hash"], review_id))
        db.execute("INSERT INTO migrations VALUES (?,?)", (CARD_HISTORY_MIGRATION, now()))

    @staticmethod
    def _flashcards(db, companies, jobs, queue):
        reviewed = {r["job_key"]: r["material_hash"] for r in db.execute("SELECT * FROM card_review_jobs")}
        blocked = {q["job_key"] for q in queue if q["kind"] == "application" and q["status"] in BLOCKING}
        eligible = {}
        for job in jobs:
            if (job["decision"] == "pending" and job["key"] not in blocked
                    and reviewed.get(job["key"]) != material_hash(job["data"])):
                eligible.setdefault(job["company_id"], []).append(job)

        def job_order(job):
            rank = job["data"].get("rank")
            # Missing or invalid ranks follow ranked jobs. Original insertion order breaks ties.
            return (rank if type(rank) in (int, float) else float("inf"), job["first_seen"])

        cards = []
        for company in companies:
            remaining = eligible.get(company["id"], [])
            if company["excluded"] or not remaining:
                continue
            newest = max(j["first_seen"] for j in remaining)
            ordered = sorted((j for j in remaining if j["first_seen"] == newest), key=job_order)
            cards.append({"company_id": company["id"], "revision": company["revision"],
                          "jobs": ordered[:3], "remaining_jobs": len(remaining),
                          "newest_job_at": newest})
        return sorted(cards, key=lambda card: card["newest_job_at"], reverse=True)

    def _state(self, db):
        companies = [self.company(r) for r in db.execute("SELECT * FROM companies ORDER BY first_seen,rowid")]
        jobs = [self.job(r) for r in db.execute("SELECT * FROM jobs ORDER BY first_seen,rowid")]
        queue = [self.entry(r) for r in db.execute("SELECT * FROM queue ORDER BY created_at DESC,rowid DESC")]
        imports = [{k: r[k] for k in ("id", "source", "created_at", "added_companies", "added_jobs", "updated_jobs")}
                   for r in db.execute("SELECT * FROM imports ORDER BY created_at DESC,rowid DESC")]
        excluded = {c["id"] for c in companies if c["excluded"]}
        blocked = {q["job_key"] for q in queue if q["kind"] == "application" and q["status"] in BLOCKING}
        visible = [j for j in jobs if j["company_id"] not in excluded and j["key"] not in blocked]
        cards = self._flashcards(db, companies, jobs, queue)
        reviews = []
        for row in db.execute("SELECT * FROM card_reviews ORDER BY created_at DESC,rowid DESC"):
            review = dict(row)
            review.pop("payload_hash")
            review["jobs"] = json.loads(review["jobs"])
            review["selected"] = json.loads(review["selected"])
            reviews.append(review)
        counts = {"companies": len(companies), "jobs": len(jobs), "all": len(jobs),
                  "inbox": sum(j["decision"] == "pending" for j in visible),
                  "later": sum(j["decision"] == "later" for j in visible),
                  "ready": sum(j["decision"] == "approve" for j in visible),
                  "queue": sum(q["status"] in ACTIVE for q in queue),
                  "excluded": len(excluded), "applied": sum(q["status"] == "applied" for q in queue),
                  "flashcards": len(cards), "card_jobs": sum(c["remaining_jobs"] for c in cards),
                  "reviewed_cards": len(reviews)}
        return {"companies": companies, "jobs": jobs, "queue": queue, "imports": imports, "counts": counts,
                "flashcards": cards, "card_reviews": reviews}

    def state(self):
        with self.transaction() as db:
            return self._state(db)

    @staticmethod
    def _row(db, table, key, value):
        # table/key are internal constants, never client strings.
        row = db.execute(f"SELECT * FROM {table} WHERE {key}=?", (value,)).fetchone()
        if row is None:
            raise ReviewError("The requested item does not exist.", 404)
        return row

    @staticmethod
    def _revision(row, expected):
        if type(expected) is not int or row["revision"] != expected:
            raise ReviewError("This item changed. Refresh the feed before editing it.", 409, reason="stale_revision")

    @staticmethod
    def _blocking(db, job_key):
        return db.execute("SELECT 1 FROM queue WHERE job_key=? AND kind='application' AND status IN (?,?,?,?)",
                          (job_key, *BLOCKING)).fetchone()

    def edit_job(self, payload):
        key = text(payload.get("key"), "Job key", 1000, required=True)
        decision = payload.get("decision")
        if not isinstance(decision, str) or decision not in DECISIONS:
            raise ReviewError("Choose pending, later, approve, or pass.")
        note = text(payload.get("note", ""), "Job note")
        with self.transaction(True) as db:
            row = self._row(db, "jobs", "key", key)
            self._revision(row, payload.get("revision"))
            if self._blocking(db, key):
                raise ReviewError("This job has an active or completed application. Cancel a queued item before changing the decision.", 409)
            company = self._row(db, "companies", "id", row["company_id"])
            if decision == "approve" and company["excluded"]:
                raise ReviewError("Restore this company before approving a job.", 409)
            db.execute("UPDATE jobs SET decision=?,note=?,changed=0,revision=revision+1,updated_at=? WHERE key=?",
                       (decision, note, now(), key))
            return self._state(db)

    def edit_company(self, payload):
        cid = text(payload.get("id"), "Company ID", 200, required=True)
        if type(payload.get("excluded")) is not bool:
            raise ReviewError("The excluded field must be true or false.")
        note = text(payload.get("note", ""), "Company note")
        with self.transaction(True) as db:
            company = self._row(db, "companies", "id", cid)
            self._revision(company, payload.get("revision"))
            if payload["excluded"]:
                self._exclude(db, cid)
            db.execute("UPDATE companies SET excluded=?,note=?,revision=revision+1,updated_at=? WHERE id=?",
                       (int(payload["excluded"]), note, now(), cid))
            return self._state(db)

    def _exclude(self, db, cid):
        active = db.execute("SELECT kind FROM queue WHERE company_id=? AND status IN ('in_progress','needs_input')", (cid,)).fetchone()
        if active:
            raise ReviewError(f"{active['kind'].capitalize()} work at this company is in progress or needs input. Resolve it before excluding the company.", 409)
        stamp = now()
        db.execute("UPDATE queue SET status='cancelled',updated_at=?,note=CASE WHEN note='' THEN 'Company excluded.' ELSE note || char(10) || 'Company excluded.' END WHERE company_id=? AND status='queued'", (stamp, cid))
        db.execute("UPDATE companies SET research_status='none' WHERE id=?", (cid,))
        db.execute("UPDATE jobs SET decision='pending',revision=revision+1,updated_at=? WHERE company_id=? AND decision='approve' AND NOT EXISTS (SELECT 1 FROM queue WHERE queue.job_key=jobs.key AND queue.kind='application' AND queue.status='applied')", (stamp, cid))

    def request_research(self, payload):
        cid = text(payload.get("id"), "Company ID", 200, required=True)
        note = text(payload.get("note", ""), "Research note")
        with self.transaction(True) as db:
            company = self._row(db, "companies", "id", cid)
            self._revision(company, payload.get("revision"))
            if company["excluded"]:
                raise ReviewError("Restore this company before requesting research.", 409)
            existing = db.execute("SELECT id FROM queue WHERE company_id=? AND kind='research' AND status IN (?,?,?)", (cid, *ACTIVE)).fetchone()
            if not existing:
                stamp = now()
                snapshot = {**json.loads(company["data"]), "id": cid, "name": company["name"], "company_revision": company["revision"]}
                db.execute("INSERT INTO queue VALUES (?,?,?,?,?,?,?,?,?)", (str(uuid.uuid4()), "research", cid, None, "queued", encode(snapshot), stamp, stamp, note))
                db.execute("UPDATE companies SET research_status='requested',revision=revision+1,updated_at=? WHERE id=?", (stamp, cid))
            return self._state(db)

    def review_card(self, payload):
        """Save one complete card in one transaction. This does not queue applications."""
        request_id = text(payload.get("request_id"), "Request ID", 200, required=True)
        cid = text(payload.get("company_id"), "Company ID", 200, required=True)
        action = payload.get("action")
        if not isinstance(action, str) or action not in {"approve", "research", "exclude"}:
            raise ReviewError("Choose approve, research, or exclude.")
        note = text(payload.get("note", ""), "Card note")
        supplied, selected = payload.get("jobs"), payload.get("selected", [])
        if not isinstance(supplied, list) or not 1 <= len(supplied) <= 3:
            raise ReviewError("Send every job on the card, up to three jobs.")
        cohort, seen = [], set()
        for item in supplied:
            if not isinstance(item, dict):
                raise ReviewError("Each card job must contain its key and revision.")
            key = text(item.get("key"), "Job key", 1000, required=True)
            if key in seen or type(item.get("revision")) is not int:
                raise ReviewError("Send each card job once, with its current revision.")
            cohort.append({"key": key, "revision": item["revision"]})
            seen.add(key)
        if (not isinstance(selected, list) or any(not isinstance(key, str) for key in selected)
                or len(set(selected)) != len(selected) or not set(selected) <= seen):
            raise ReviewError("Select only jobs on this card, once each.")
        if action == "approve" and not selected:
            raise ReviewError("Select at least one job to approve.")
        if action != "approve" and selected:
            raise ReviewError("Research and exclusion cannot approve selected jobs.")
        try:
            fingerprint = digest(payload)
        except (ValueError, TypeError):
            raise ReviewError("The card request must contain valid JSON values.") from None
        with self.transaction(True) as db:
            previous = db.execute("SELECT payload_hash FROM card_reviews WHERE id=?", (request_id,)).fetchone()
            if previous:
                if previous["payload_hash"] != fingerprint:
                    raise ReviewError("This request ID was already used for another card choice.", 409)
                return self._state(db)
            company = self._row(db, "companies", "id", cid)
            self._revision(company, payload.get("revision"))
            state = self._state(db)
            card = next((card for card in state["flashcards"] if card["company_id"] == cid), None)
            expected = [] if card is None else [{"key": j["key"], "revision": j["revision"]} for j in card["jobs"]]
            if sorted(cohort, key=lambda j: j["key"]) != sorted(expected, key=lambda j: j["key"]):
                raise ReviewError("This card changed or was already reviewed. Refresh the feed before editing it.", 409)

            stamp = now()
            jobs = [{"key": j["key"], "revision": j["revision"], "title": j["data"]["title"],
                     "material_hash": material_hash(j["data"])} for j in card["jobs"]]
            if action == "exclude":
                self._exclude(db, cid)
                db.execute("UPDATE companies SET excluded=1 WHERE id=?", (cid,))
            elif action == "approve":
                for key in selected:
                    db.execute("UPDATE jobs SET decision='approve',changed=0,revision=revision+1,updated_at=? WHERE key=?", (stamp, key))
            else:
                existing = db.execute("SELECT id FROM queue WHERE company_id=? AND kind='research' AND status IN (?,?,?)", (cid, *ACTIVE)).fetchone()
                if not existing:
                    snapshot = {**json.loads(company["data"]), "id": cid, "name": company["name"], "company_revision": company["revision"]}
                    db.execute("INSERT INTO queue VALUES (?,?,?,?,?,?,?,?,?)", (str(uuid.uuid4()), "research", cid, None, "queued", encode(snapshot), stamp, stamp, note))
                    db.execute("UPDATE companies SET research_status='requested' WHERE id=?", (cid,))
                elif note.strip():
                    db.execute("UPDATE queue SET note=CASE WHEN note='' THEN ? ELSE note || char(10) || ? END,updated_at=? WHERE id=?", (note, note, stamp, existing["id"]))
            # Card notes add context without erasing earlier company notes.
            combined_note = "\n".join(filter(None, (company["note"], note))) if note.strip() else company["note"]
            db.execute("UPDATE companies SET note=?,revision=revision+1,updated_at=? WHERE id=?", (combined_note, stamp, cid))
            db.execute("INSERT INTO card_reviews VALUES (?,?,?,?,?,?,?,?)", (request_id, cid, fingerprint, action, encode(jobs), encode(selected), note, stamp))
            for job in jobs:
                db.execute("INSERT INTO card_review_jobs VALUES (?,?,?) ON CONFLICT(job_key) DO UPDATE SET material_hash=excluded.material_hash,review_id=excluded.review_id", (job["key"], job["material_hash"], request_id))
            return self._state(db)

    def queue_jobs(self, payload):
        request_id = text(payload.get("request_id"), "Request ID", 200, required=True)
        selected = payload.get("jobs")
        if not isinstance(selected, list) or not selected:
            raise ReviewError("Select at least one approved job.")
        entries, seen = [], set()
        for item in selected:
            if not isinstance(item, dict):
                raise ReviewError("Each selected job must contain its key and revision.")
            key = text(item.get("key"), "Job key", 1000, required=True)
            if key in seen or type(item.get("revision")) is not int:
                raise ReviewError("Select each job once, with its current revision.")
            seen.add(key)
            entries.append({"key": key, "revision": item["revision"]})
        fingerprint = digest(sorted(entries, key=lambda i: i["key"]))
        with self.transaction(True) as db:
            previous = db.execute("SELECT * FROM requests WHERE id=?", (request_id,)).fetchone()
            if previous:
                if previous["payload_hash"] != fingerprint:
                    raise ReviewError("This request ID was already used for another selection.", 409)
                return self._state(db)
            rows = []
            for item in entries:
                job = self._row(db, "jobs", "key", item["key"])
                self._revision(job, item["revision"])
                company = self._row(db, "companies", "id", job["company_id"])
                if company["excluded"] or job["decision"] != "approve" or job["changed"]:
                    raise ReviewError("Each selected job must have a current approval at an included company.", 409)
                if self._blocking(db, job["key"]):
                    raise ReviewError("A selected job already has an active or completed application.", 409)
                rows.append((job, company))
            stamp, ids = now(), []
            for job, company in rows:
                data = json.loads(job["data"])
                snapshot = {**data, "company_id": job["company_id"], "company_name": company["name"],
                            "job_key": job["key"], "job_revision": job["revision"], "material_hash": material_hash(data)}
                qid = str(uuid.uuid4())
                ids.append(qid)
                db.execute("INSERT INTO queue VALUES (?,?,?,?,?,?,?,?,?)", (qid, "application", job["company_id"], job["key"], "queued", encode(snapshot), stamp, stamp, job["note"]))
            db.execute("INSERT INTO requests VALUES (?,?,?,?)", (request_id, fingerprint, encode(ids), stamp))
            return self._state(db)

    def cancel(self, payload):
        qid = text(payload.get("id"), "Queue ID", 200, required=True)
        with self.transaction(True) as db:
            row = self._row(db, "queue", "id", qid)
            if row["status"] != "queued":
                raise ReviewError("Only queued work can be cancelled here.", 409)
            self._cancel(db, row, "Cancelled by the user.")
            return self._state(db)

    def _cancel(self, db, row, note):
        stamp = now()
        combined = "\n".join(filter(None, (row["note"], note)))
        db.execute("UPDATE queue SET status='cancelled',note=?,updated_at=? WHERE id=?", (combined, stamp, row["id"]))
        if row["kind"] == "application":
            db.execute("UPDATE jobs SET decision='pending',revision=revision+1,updated_at=? WHERE key=?", (stamp, row["job_key"]))
        else:
            db.execute("UPDATE companies SET research_status='none',revision=revision+1,updated_at=? WHERE id=?", (stamp, row["company_id"]))

    def import_catalog(self, catalog, source):
        catalog = validate_catalog(catalog)
        source = text(str(source), "Import source", 10000, required=True)
        with self.transaction(True) as db:
            result = self._import(db, catalog, source)
            return result

    def _import(self, db, catalog, source):
        stamp = now()
        added_companies = added_jobs = updated_jobs = changed_companies = 0
        for company in catalog["companies"]:
            cid, name = company["id"], company["name"]
            metadata = {k: v for k, v in company.items() if k != "jobs"}
            old_company = db.execute("SELECT * FROM companies WHERE id=?", (cid,)).fetchone()
            if old_company is None:
                db.execute("INSERT INTO companies(id,name,data,first_seen,updated_at) VALUES (?,?,?,?,?)", (cid, name, encode(metadata), stamp, stamp))
                added_companies += 1
            elif old_company["data"] != encode(metadata) or old_company["name"] != name:
                db.execute("UPDATE companies SET name=?,data=?,revision=revision+1,updated_at=? WHERE id=?", (name, encode(metadata), stamp, cid))
                changed_companies += 1
            for incoming in company["jobs"]:
                incoming_key = f"{cid}:{incoming['job_id']}"
                url = canonical_url(incoming["url"])
                old = db.execute("SELECT jobs.* FROM jobs LEFT JOIN aliases ON aliases.job_key=jobs.key WHERE jobs.key=? OR aliases.alias=?", (incoming_key, incoming_key)).fetchone()
                by_url = db.execute("SELECT * FROM jobs WHERE canonical_url=?", (url,)).fetchone()
                if old and by_url and old["key"] != by_url["key"]:
                    raise ReviewError(f"The job ID and URL refer to different saved jobs: {incoming_key}.", 409)
                if by_url and by_url["company_id"] != cid:
                    raise ReviewError(f"This URL belongs to a different saved company: {url}.", 409)
                old = old or by_url
                if old is None:
                    db.execute("INSERT INTO jobs(key,company_id,job_id,canonical_url,data,first_seen,updated_at) VALUES (?,?,?,?,?,?,?)", (incoming_key, cid, incoming["job_id"], url, encode(incoming), stamp, stamp))
                    added_jobs += 1
                    continue
                if old["company_id"] != cid:
                    raise ReviewError("An import cannot move a job to another company.", 409)
                if incoming_key != old["key"]:
                    existing_alias = db.execute("SELECT job_key FROM aliases WHERE alias=?", (incoming_key,)).fetchone()
                    if existing_alias and existing_alias["job_key"] != old["key"]:
                        raise ReviewError("The imported job ID conflicts with a saved alias.", 409)
                    db.execute("INSERT OR IGNORE INTO aliases VALUES (?,?)", (incoming_key, old["key"]))
                data = {**incoming, "job_id": old["job_id"]}
                if encode(data) == old["data"]:
                    continue
                material = material_hash(data) != material_hash(json.loads(old["data"]))
                decision = "pending" if material and old["decision"] == "approve" else old["decision"]
                db.execute("UPDATE jobs SET canonical_url=?,data=?,revision=revision+1,decision=?,changed=?,updated_at=? WHERE key=?", (url, encode(data), decision, int(material or old["changed"]), stamp, old["key"]))
                updated_jobs += 1
                if material:
                    db.execute("UPDATE queue SET status='stale',updated_at=?,note=CASE WHEN note='' THEN 'The posting changed. Review the current job before applying.' ELSE note || char(10) || 'The posting changed. Review the current job before applying.' END WHERE job_key=? AND kind='application' AND status='queued'", (stamp, old["key"]))
        fingerprint = digest(catalog)
        if not (added_companies or changed_companies or added_jobs or updated_jobs):
            previous = db.execute("SELECT * FROM imports WHERE fingerprint=? ORDER BY created_at DESC LIMIT 1", (fingerprint,)).fetchone()
            if previous:
                return {**dict(previous), "unchanged": True}
        result = {"id": str(uuid.uuid4()), "source": source, "fingerprint": fingerprint, "created_at": stamp,
                  "added_companies": added_companies, "added_jobs": added_jobs, "updated_jobs": updated_jobs}
        db.execute("INSERT INTO imports VALUES (:id,:source,:fingerprint,:created_at,:added_companies,:added_jobs,:updated_jobs)", result)
        return result

    def update_status(self, qid, status, note=""):
        text(qid, "Queue ID", 200, required=True)
        note = text(note, "Status note", 10000)
        with self.transaction(True) as db:
            entry = self._row(db, "queue", "id", qid)
            if entry["status"] in {"applied", "done", "cancelled", "stale", "failed"}:
                raise ReviewError("Completed, failed, cancelled, or stale history cannot be reopened. Create a new queue item for a retry.", 409)
            allowed = {"in_progress", "needs_input", "failed", "cancelled", "applied" if entry["kind"] == "application" else "done"}
            if not isinstance(status, str) or status not in allowed:
                raise ReviewError("Choose a valid status for this queue item.")
            if status in {"applied", "done", "needs_input", "failed"} and not note.strip():
                raise ReviewError("Add an evidence or outcome note for this status.")
            if status == "cancelled":
                if entry["status"] != "queued":
                    raise ReviewError("Only queued work can be cancelled. Record the outcome of started work.", 409)
                self._cancel(db, entry, note or "Cancelled by the operator.")
                return self._state(db)
            if entry["kind"] == "research" and status in {"in_progress", "done"}:
                company = self._row(db, "companies", "id", entry["company_id"])
                if company["excluded"]:
                    raise ReviewError("This company is excluded. Restore it before starting research.", 409)
            if entry["kind"] == "application" and status in {"in_progress", "applied"}:
                job = self._row(db, "jobs", "key", entry["job_key"])
                snapshot = json.loads(entry["snapshot"])
                company = self._row(db, "companies", "id", entry["company_id"])
                if company["excluded"] or snapshot.get("material_hash") != material_hash(json.loads(job["data"])):
                    raise ReviewError("The posting or company choice changed. Do not apply from this snapshot.", 409)
                if job["decision"] != "approve":
                    raise ReviewError("This job no longer has approval.", 409)
            stamp = now()
            combined = "\n".join(filter(None, (entry["note"], f"{stamp} [{status}] {note}")))
            db.execute("UPDATE queue SET status=?,note=?,updated_at=? WHERE id=?", (status, combined, stamp, qid))
            if entry["kind"] == "research":
                research_status = "done" if status == "done" else "none" if status == "failed" else "in_progress"
                db.execute("UPDATE companies SET research_status=?,revision=revision+1,updated_at=? WHERE id=?", (research_status, stamp, entry["company_id"]))
            return self._state(db)

    def migrate(self, catalog, source, draft=None, submitted=None):
        """Import one legacy batch. Callers must exclude QA decision directories."""
        catalog = validate_catalog(catalog)
        text(source, "Migration source", 10000, required=True)
        for value in (draft, submitted):
            if value is not None and (not isinstance(value, dict) or not isinstance(value.get("decisions"), dict)):
                raise ReviewError("A legacy decision file has an invalid format.")
        newer_draft = False
        if draft is not None and submitted is not None:
            try:
                draft_time = datetime.fromisoformat((draft.get("saved_at") or draft.get("updated_at") or "").replace("Z", "+00:00"))
                submitted_time = datetime.fromisoformat((submitted.get("submitted_at") or "").replace("Z", "+00:00"))
                if draft_time.tzinfo is None or submitted_time.tzinfo is None:
                    raise ValueError()
                newer_draft = draft_time > submitted_time
            except (ValueError, TypeError, AttributeError):
                raise ReviewError("Cannot order the legacy draft and submission. Check their saved_at and submitted_at timestamps before migration.") from None
        with self.transaction(True) as db:
            if db.execute("SELECT 1 FROM migrations WHERE source=?", (source,)).fetchone():
                return {"already_migrated": True, "source": source}
            # Migration is an initialization path, not a way to overwrite a live catalog.
            ids = [company["id"] for company in catalog["companies"]]
            if any(db.execute("SELECT 1 FROM companies WHERE id=?", (cid,)).fetchone() for cid in ids):
                raise ReviewError("These companies already exist. Import new research normally; migration cannot overwrite live choices.", 409)
            result = self._import(db, catalog, source)
            lookup = {c["id"]: c for c in catalog["companies"]}
            base = submitted or draft
            if base:
                self._migrate_choices(db, lookup, base["decisions"])
            if submitted:
                original_jobs = submitted.get("approved_jobs")
                if not isinstance(original_jobs, list):
                    raise ReviewError("The submitted review has no approved-job snapshot list.")
                selected = {(cid, jid) for cid, choice in submitted["decisions"].items()
                            for jid in choice.get("approved_job_ids", []) if choice.get("action") == "approve"}
                supplied = {(j.get("company_id"), j.get("job_id")) for j in original_jobs if isinstance(j, dict)}
                if supplied != selected or len(supplied) != len(original_jobs):
                    raise ReviewError("The legacy submitted choices and job snapshots do not agree.")
                for original in original_jobs:
                    cid, jid = original["company_id"], original["job_id"]
                    job = self._row(db, "jobs", "key", f"{cid}:{jid}")
                    current = json.loads(job["data"])
                    match = submitted.get("dataset_version") == catalog.get("version") and all(original.get(k) == current.get(k) for k in ("title", "url", "location"))
                    snapshot = {**(current if match else {}), **original, "job_key": job["key"],
                                "job_revision": job["revision"], "company_name": lookup[cid]["name"],
                                "legacy_review_id": submitted.get("review_id"), "legacy_dataset_version": submitted.get("dataset_version")}
                    if match:
                        snapshot["material_hash"] = material_hash(current)
                    stamp = submitted.get("submitted_at") or now()
                    db.execute("INSERT INTO queue VALUES (?,?,?,?,?,?,?,?,?)", (str(uuid.uuid4()), "application", cid, job["key"], "queued" if match else "stale", encode(snapshot), stamp, stamp, text(original.get("note", ""), "Legacy note")))
                    if not match:
                        db.execute("UPDATE jobs SET decision='pending',changed=1,revision=revision+1 WHERE key=?", (job["key"],))
                # A newer draft can add unqueued job choices. It cannot erase a submitted snapshot.
                if newer_draft:
                    self._migrate_draft_additions(db, lookup, draft["decisions"])
            db.execute("INSERT INTO migrations VALUES (?,?)", (source, now()))
            return {**result, "migrated": True}

    def _legacy_choice(self, lookup, cid, choice):
        if cid not in lookup or not isinstance(choice, dict) or choice.get("action") not in {"pending", "approve", "research", "exclude", "skip"}:
            raise ReviewError("The legacy review contains an unknown company or action.")
        ids = choice.get("approved_job_ids", [])
        allowed = {j["job_id"] for j in lookup[cid]["jobs"]}
        if not isinstance(ids, list) or any(not isinstance(j, str) for j in ids) or len(set(ids)) != len(ids) or not set(ids) <= allowed:
            raise ReviewError("A legacy approved job is not in its company catalog.")
        if (choice["action"] == "approve") != bool(ids):
            raise ReviewError("Legacy approvals do not match the company action.")
        return text(choice.get("note", ""), "Legacy note")

    def _migrate_choices(self, db, lookup, decisions):
        for cid, choice in decisions.items():
            note = self._legacy_choice(lookup, cid, choice)
            action = choice["action"]
            db.execute("UPDATE companies SET note=?,excluded=? WHERE id=?", (note, int(action == "exclude"), cid))
            if action == "skip":
                db.execute("UPDATE jobs SET decision='later' WHERE company_id=?", (cid,))
            if action == "approve":
                for jid in choice["approved_job_ids"]:
                    db.execute("UPDATE jobs SET decision='approve' WHERE key=?", (f"{cid}:{jid}",))
            if action == "research":
                company = self._row(db, "companies", "id", cid)
                stamp = now()
                db.execute("INSERT INTO queue VALUES (?,?,?,?,?,?,?,?,?)", (str(uuid.uuid4()), "research", cid, None, "queued", company["data"], stamp, stamp, note))
                db.execute("UPDATE companies SET research_status='requested' WHERE id=?", (cid,))

    def _migrate_draft_additions(self, db, lookup, decisions):
        for cid, choice in decisions.items():
            note = self._legacy_choice(lookup, cid, choice)
            action = choice["action"]
            stamp = now()
            if action == "exclude":
                # The latest explicit exclusion wins, without deleting the old snapshot.
                self._exclude(db, cid)
                db.execute("UPDATE companies SET excluded=1,note=?,revision=revision+1,updated_at=? WHERE id=?", (note, stamp, cid))
                continue
            db.execute("UPDATE companies SET excluded=0,note=?,revision=revision+1,updated_at=? WHERE id=?", (note, stamp, cid))
            selected = set(choice.get("approved_job_ids", []))
            for job in db.execute("SELECT * FROM jobs WHERE company_id=?", (cid,)).fetchall():
                if self._blocking(db, job["key"]):
                    continue
                decision = "later" if action == "skip" else "approve" if job["job_id"] in selected and not job["changed"] else "pending"
                db.execute("UPDATE jobs SET decision=?,revision=revision+1,updated_at=? WHERE key=?", (decision, stamp, job["key"]))
            if action == "research":
                active = db.execute("SELECT id FROM queue WHERE company_id=? AND kind='research' AND status IN (?,?,?)", (cid, *ACTIVE)).fetchone()
                if active:
                    db.execute("UPDATE queue SET note=?,updated_at=? WHERE id=?", (note, stamp, active["id"]))
                else:
                    company = self._row(db, "companies", "id", cid)
                    db.execute("INSERT INTO queue VALUES (?,?,?,?,?,?,?,?,?)", (str(uuid.uuid4()), "research", cid, None, "queued", company["data"], stamp, stamp, note))
                db.execute("UPDATE companies SET research_status='requested' WHERE id=?", (cid,))
