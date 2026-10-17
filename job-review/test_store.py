import copy
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from store import CARD_HISTORY_MIGRATION, ReviewError, Store, canonical_url


def catalog():
    return {"version": "legacy-v1", "companies": [
        {"id": "a", "name": "Alpha", "summary": "Backend work", "jobs": [
            {"job_id": str(n), "title": f"Engineer {n}", "url": f"https://jobs.example/a/{n}",
             "location": "San Francisco", "arrangement": "Hybrid", "salary": "$100–200",
             "level": "Senior", "gaps": ["Kubernetes is unconfirmed."], "rank": n}
            for n in (1, 2, 3)]},
        {"id": "b", "name": "Beta", "jobs": [
            {"job_id": "b1", "title": "Backend Engineer", "url": "https://jobs.example/b/1", "location": "Remote"}]}]}


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "review.sqlite3"
        self.store = Store(self.path)
        self.store.import_catalog(catalog(), "initial")

    def tearDown(self):
        self.temp.cleanup()

    def job(self, key="a:1"):
        return next(j for j in self.store.state()["jobs"] if j["key"] == key)

    def company(self, cid="a"):
        return next(c for c in self.store.state()["companies"] if c["id"] == cid)

    def decide(self, key="a:1", decision="approve", note=""):
        job = self.job(key)
        return self.store.edit_job({"key": key, "revision": job["revision"], "decision": decision, "note": note})

    def queue(self, keys=("a:1",), request_id="request-1"):
        return self.store.queue_jobs({"request_id": request_id, "jobs": [{"key": key, "revision": self.job(key)["revision"]} for key in keys]})

    def exclude(self, excluded=True):
        c = self.company()
        return self.store.edit_company({"id": "a", "revision": c["revision"], "excluded": excluded, "note": "Choice"})

    def research(self):
        c = self.company()
        return self.store.request_research({"id": "a", "revision": c["revision"], "note": "Find backend roles."})

    def assert_conflict(self, call):
        with self.assertRaises(ReviewError) as caught:
            call()
        self.assertEqual(caught.exception.status, 409)

    def test_partial_queue_exact_snapshot_and_restart(self):
        self.decide(note="Use approved resume.")
        self.decide("a:2")
        result = self.queue()
        self.assertEqual(len(result["queue"]), 1)
        entry = result["queue"][0]
        self.assertEqual(entry["job_key"], "a:1")
        self.assertEqual(entry["snapshot"]["title"], "Engineer 1")
        self.assertEqual(entry["snapshot"]["url"], "https://jobs.example/a/1")
        self.assertEqual(entry["snapshot"]["gaps"], ["Kubernetes is unconfirmed."])
        self.assertEqual(entry["note"], "Use approved resume.")
        self.assertEqual(result["counts"]["ready"], 1)
        self.assertEqual(result["counts"]["inbox"], 2)
        self.assertEqual(Store(self.path).state(), result)

    def test_idempotency_retry_and_mismatch(self):
        self.decide()
        payload = {"request_id": "retry", "jobs": [{"key": "a:1", "revision": self.job()["revision"]}]}
        first = self.store.queue_jobs(payload)
        self.assertEqual(first, self.store.queue_jobs(payload))
        other = copy.deepcopy(payload)
        other["jobs"][0]["key"] = "a:2"
        self.assert_conflict(lambda: self.store.queue_jobs(other))
        self.assertEqual(len(self.store.state()["queue"]), 1)

    def test_atomic_queue_rejects_one_invalid_member(self):
        self.decide()
        self.assert_conflict(lambda: self.queue(("a:1", "a:2")))
        self.assertFalse(self.store.state()["queue"])
        self.assert_conflict(lambda: self.store.queue_jobs({"request_id": "bad", "jobs": [{"key": "a:1", "revision": 1}]}))
        self.assertFalse(self.store.state()["queue"])

    def test_no_duplicate_or_decision_change_for_active_or_applied(self):
        self.decide()
        qid = self.queue()["queue"][0]["id"]
        self.assert_conflict(lambda: self.queue(request_id="another"))
        self.assert_conflict(lambda: self.decide(decision="later"))
        self.store.update_status(qid, "applied", "Official confirmation page says application received, reference 42.")
        self.assert_conflict(lambda: self.queue(request_id="third"))
        self.assert_conflict(lambda: self.decide(decision="pass"))

    def test_cancel_resets_approval_and_preserves_history(self):
        self.decide()
        old = self.queue()["queue"][0]
        self.store.cancel({"id": old["id"]})
        self.assertEqual(self.job()["decision"], "pending")
        self.decide()
        result = self.queue(request_id="second")
        self.assertEqual(len(result["queue"]), 2)
        self.assertEqual({q["status"] for q in result["queue"]}, {"queued", "cancelled"})
        self.assertEqual(next(q for q in result["queue"] if q["id"] == old["id"])["snapshot"], old["snapshot"])

    def test_additive_import_fourth_job_and_new_company(self):
        self.decide(decision="later", note="Review Friday.")
        self.decide("a:2", "approve")
        changed = catalog()
        changed["companies"][0]["jobs"].append({"job_id": "4", "title": "New role", "url": "https://jobs.example/a/4"})
        changed["companies"].append({"id": "c", "name": "Gamma", "jobs": [{"job_id": "9", "title": "Infra", "url": "https://jobs.example/c/9"}]})
        self.store.import_catalog(changed, "additional")
        state = self.store.state()
        self.assertEqual(len(state["companies"]), 3)
        self.assertEqual(len(state["jobs"]), 6)
        self.assertEqual(self.job()["decision"], "later")
        self.assertEqual(self.job()["note"], "Review Friday.")
        self.assertEqual(self.job("a:2")["decision"], "approve")
        self.store.import_catalog({"companies": [changed["companies"][2]]}, "one company")
        self.assertEqual(len(self.store.state()["jobs"]), 6)

    def test_identical_import_is_idempotent_and_copy_edits_keep_approval(self):
        self.decide()
        initial = self.store.state()
        self.store.import_catalog(catalog(), "again")
        self.assertEqual(self.store.state(), initial)
        self.queue()
        updated = catalog()
        updated["companies"][0]["jobs"][0].update(rank=3, brief={"work": "Build APIs.", "fit": "Backend work matches.", "gap": "Kubernetes unconfirmed."})
        self.store.import_catalog(updated, "copy")
        self.assertEqual(self.job()["decision"], "approve")
        self.assertFalse(self.job()["changed"])
        self.assertEqual(self.store.state()["queue"][0]["status"], "queued")
        self.store.update_status(self.store.state()["queue"][0]["id"], "in_progress")

    def test_material_change_revokes_approval_and_stales_snapshot(self):
        self.decide()
        old = self.queue()["queue"][0]
        updated = catalog()
        updated["companies"][0]["jobs"][0]["location"] = "New York only"
        self.store.import_catalog(updated, "changed posting")
        current = self.job()
        entry = self.store.state()["queue"][0]
        self.assertEqual(current["decision"], "pending")
        self.assertTrue(current["changed"])
        self.assertEqual(entry["status"], "stale")
        self.assertEqual(entry["snapshot"], old["snapshot"])
        self.assert_conflict(lambda: self.store.update_status(entry["id"], "applied", "Some evidence"))
        self.decide()
        self.assertFalse(self.job()["changed"])
        self.assertEqual(len(self.queue(request_id="revised")["queue"]), 2)

    def test_started_history_remains_but_material_change_prevents_applied(self):
        self.decide()
        entry = self.queue()["queue"][0]
        self.store.update_status(entry["id"], "in_progress")
        updated = catalog()
        updated["companies"][0]["jobs"][0]["gaps"].append("Work authorization is not confirmed.")
        self.store.import_catalog(updated, "requirements change")
        self.assertEqual(self.store.state()["queue"][0]["status"], "in_progress")
        self.assert_conflict(lambda: self.store.update_status(entry["id"], "applied", "Cannot override stale posting."))
        self.store.update_status(entry["id"], "failed", "Stopped because the requirements changed.")
        self.assertEqual(self.store.state()["queue"][0]["status"], "failed")

    def test_exclusion_cancels_queued_work_survives_import_and_restore(self):
        self.decide()
        self.decide("a:2")
        self.queue()
        self.research()
        self.exclude()
        self.assertTrue(self.company()["excluded"])
        self.assertEqual(self.company()["research_status"], "none")
        self.assertTrue(all(q["status"] == "cancelled" for q in self.store.state()["queue"]))
        self.assertEqual(self.job()["decision"], "pending")
        self.assertEqual(self.job("a:2")["decision"], "pending")
        self.store.import_catalog(catalog(), "reimport")
        self.assertTrue(self.company()["excluded"])
        self.assert_conflict(lambda: self.decide())
        self.assert_conflict(self.research)
        self.exclude(False)
        self.assertEqual(self.job()["decision"], "pending")
        self.assertTrue(all(q["status"] == "cancelled" for q in self.store.state()["queue"]))

    def test_exclusion_blocks_started_application_or_research(self):
        self.decide()
        qid = self.queue()["queue"][0]["id"]
        self.store.update_status(qid, "needs_input", "Need the missing legal answer.")
        self.assert_conflict(self.exclude)
        self.store.update_status(qid, "failed", "User did not approve the required condition.")
        self.research()
        research = next(q for q in self.store.state()["queue"] if q["kind"] == "research")
        self.store.update_status(research["id"], "in_progress")
        self.assert_conflict(self.exclude)

    def test_research_independent_of_job_approval_and_deduplicated(self):
        self.decide()
        self.research()
        self.research()
        self.assertEqual(len(self.store.state()["queue"]), 1)
        self.assertEqual(self.job()["decision"], "approve")
        self.queue()
        self.assertEqual(len(self.store.state()["queue"]), 2)

    def test_alias_url_dedup_and_cross_company_conflict(self):
        self.decide()
        alias = catalog()
        alias["companies"][0]["jobs"][0]["job_id"] = "alternate-id"
        alias["companies"][0]["jobs"][0]["url"] += "/?utm_source=linkedin#description"
        self.store.import_catalog(alias, "alias")
        self.assertEqual(len(self.store.state()["jobs"]), 4)
        self.assertEqual(self.job()["job_id"], "1")
        self.assertEqual(self.job()["decision"], "approve")
        bad = {"companies": [{"id": "other-alpha", "name": "Alpha duplicate", "jobs": [copy.deepcopy(alias["companies"][0]["jobs"][0])]}]}
        initial = self.store.state()
        self.assert_conflict(lambda: self.store.import_catalog(bad, "bad company"))
        self.assertEqual(self.store.state(), initial)
        conflicting = catalog()
        conflicting["companies"][0]["jobs"][0]["url"] = "https://jobs.example/b/1"
        with self.assertRaises(ReviewError):
            self.store.import_catalog(conflicting, "conflict")
        self.assertEqual(self.store.state(), initial)

    def test_whole_import_validation_before_writes(self):
        bad = catalog()
        bad["companies"][0]["name"] = "Changed before invalid field"
        bad["companies"][1]["jobs"][0]["url"] = "http://not-https.example/job"
        before = self.store.state()
        with self.assertRaises(ReviewError):
            self.store.import_catalog(bad, "invalid")
        self.assertEqual(self.store.state(), before)

    def test_stale_edits_and_concurrent_queue(self):
        old = self.job()
        self.decide()
        self.assert_conflict(lambda: self.store.edit_job({"key": old["key"], "revision": old["revision"], "decision": "later"}))
        def enqueue(i):
            try:
                self.queue(request_id=f"parallel-{i}")
                return "saved"
            except ReviewError as error:
                return error.status
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(enqueue, range(2)))
        self.assertCountEqual(results, ["saved", 409])
        self.assertEqual(len(self.store.state()["queue"]), 1)

    def test_status_requires_evidence_and_never_calls_failed_applied(self):
        self.decide()
        qid = self.queue()["queue"][0]["id"]
        with self.assertRaises(ReviewError):
            self.store.update_status(qid, "applied", " ")
        self.store.update_status(qid, "failed", "Official form returned an error; no confirmation received.")
        self.assertEqual(self.store.state()["counts"]["applied"], 0)
        self.assertEqual(self.store.state()["queue"][0]["status"], "failed")

    def test_failed_application_and_research_cannot_be_reopened_after_retry(self):
        self.decide()
        first = self.queue()["queue"][0]["id"]
        self.store.update_status(first, "failed", "The form was closed.")
        self.decide()
        self.queue(request_id="retry")
        self.assert_conflict(lambda: self.store.update_status(first, "in_progress"))
        self.assert_conflict(lambda: self.store.update_status(first, "applied", "Evidence cannot reopen old failure."))
        self.research()
        research = next(q for q in self.store.state()["queue"] if q["kind"] == "research")
        self.store.update_status(research["id"], "failed", "The official board is unavailable.")
        self.research()
        self.assert_conflict(lambda: self.store.update_status(research["id"], "in_progress"))
        self.assertEqual(sum(q["kind"] == "application" and q["status"] == "queued" for q in self.store.state()["queue"]), 1)

    def test_normalization_keeps_identity_query_parameters(self):
        self.assertEqual(canonical_url("https://JOBS.example/role/?utm_campaign=x&job=123#top"), "https://jobs.example/role?job=123")
        self.assertNotEqual(canonical_url("https://jobs.example/role?job=123"), canonical_url("https://jobs.example/role?job=456"))


class FlashcardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "cards.sqlite3"
        self.store = Store(self.path)
        self.store.import_catalog(catalog(), "initial")

    def tearDown(self):
        self.temp.cleanup()

    def card(self, cid="a"):
        return next(c for c in self.store.state()["flashcards"] if c["company_id"] == cid)

    def payload(self, action="approve", selected=None, request_id="card-1", cid="a"):
        card = self.card(cid)
        return {"request_id": request_id, "company_id": cid, "revision": card["revision"],
                "jobs": [{"key": j["key"], "revision": j["revision"]} for j in card["jobs"]],
                "action": action, "selected": selected if selected is not None else [card["jobs"][0]["key"]] if action == "approve" else []}

    def assert_conflict(self, call):
        with self.assertRaises(ReviewError) as caught:
            call()
        self.assertEqual(caught.exception.status, 409)

    def test_subset_approval_records_whole_card_and_survives_restart(self):
        self.assertEqual([j["key"] for j in self.card()["jobs"]], ["a:1", "a:2", "a:3"])
        result = self.store.review_card(self.payload())
        jobs = {j["key"]: j for j in result["jobs"]}
        self.assertEqual(jobs["a:1"]["decision"], "approve")
        self.assertEqual(jobs["a:2"]["decision"], "pending")
        self.assertEqual(jobs["a:3"]["decision"], "pending")
        self.assertEqual(jobs["a:2"]["revision"], 1)
        self.assertEqual([c["company_id"] for c in result["flashcards"]], ["b"])
        self.assertFalse(result["queue"])
        self.assertEqual(result["counts"]["reviewed_cards"], 1)
        review = result["card_reviews"][0]
        self.assertEqual(review["selected"], ["a:1"])
        self.assertEqual(len(review["jobs"]), 3)
        self.assertEqual(Store(self.path).state(), result)

    def test_exact_retry_is_idempotent_but_reused_id_and_second_click_conflict(self):
        payload = self.payload()
        first = self.store.review_card(payload)
        self.assertEqual(first, self.store.review_card(payload))
        changed = {**payload, "note": "Different choice"}
        self.assert_conflict(lambda: self.store.review_card(changed))
        self.assert_conflict(lambda: self.store.review_card({**payload, "request_id": "second-click"}))
        self.assertEqual(first, self.store.state())

    def test_stale_or_partial_cohort_and_wrong_company_roll_back(self):
        payload = self.payload()
        variants = [
            {**payload, "revision": 0},
            {**payload, "jobs": payload["jobs"][:1]},
            {**payload, "company_id": "b"},
            {**payload, "jobs": [{**job, "revision": 0} for job in payload["jobs"]]},
        ]
        before = self.store.state()
        for variant in variants:
            self.assert_conflict(lambda: self.store.review_card(variant))
            self.assertEqual(before, self.store.state())
        self.store.edit_job({"key": "a:3", "revision": 1, "decision": "pass"})
        before = self.store.state()
        self.assert_conflict(lambda: self.store.review_card(payload))
        self.assertEqual(before, self.store.state())

    def test_invalid_selection_never_marks_card_reviewed(self):
        payload = self.payload()
        variants = [{**payload, "selected": []}, {**payload, "selected": ["a:1", "a:1"]},
                    {**payload, "selected": ["b:b1"]}, {**payload, "selected": [42]},
                    {**payload, "action": "research"}, {**payload, "jobs": []},
                    {**payload, "action": []}, {**payload, "note": 42}]
        before = self.store.state()
        for variant in variants:
            with self.assertRaises(ReviewError) as error:
                self.store.review_card(variant)
            self.assertEqual(error.exception.status, 400)
            self.assertEqual(before, self.store.state())

    def test_existing_choices_and_active_application_are_not_cards(self):
        for key, decision in [("a:1", "approve"), ("a:2", "later"), ("a:3", "pass")]:
            self.store.edit_job({"key": key, "revision": 1, "decision": decision})
        self.store.queue_jobs({"request_id": "application", "jobs": [{"key": "a:1", "revision": 2}]})
        qid = self.store.state()["queue"][0]["id"]
        self.store.update_status(qid, "in_progress")
        changed = catalog()
        changed["companies"][0]["jobs"][0]["location"] = "Remote"
        self.store.import_catalog(changed, "posting changed")
        # A material edit sets approval to pending, but cannot expose started work for another decision.
        self.assertEqual([c["company_id"] for c in self.store.state()["flashcards"]], ["b"])
        self.assertEqual(self.store.state()["counts"]["reviewed_cards"], 0)

    def test_research_is_deduplicated_and_preserves_previous_notes(self):
        company = next(c for c in self.store.state()["companies"] if c["id"] == "a")
        self.store.edit_company({"id": "a", "revision": company["revision"], "excluded": False, "note": "Former team."})
        company = next(c for c in self.store.state()["companies"] if c["id"] == "a")
        self.store.request_research({"id": "a", "revision": company["revision"], "note": "Find more roles."})
        state = self.store.review_card({**self.payload("research"), "note": "Prefer backend work."})
        self.assertEqual(len(state["queue"]), 1)
        self.assertIn("Find more roles.", state["queue"][0]["note"])
        self.assertIn("Prefer backend work.", state["queue"][0]["note"])
        company = next(c for c in state["companies"] if c["id"] == "a")
        self.assertEqual(company["note"], "Former team.\nPrefer backend work.")
        self.assertFalse(any(c["company_id"] == "a" for c in state["flashcards"]))

    def test_new_research_request_records_card_once(self):
        payload = self.payload("research")
        first = self.store.review_card(payload)
        self.assertEqual(len(first["queue"]), 1)
        self.assertEqual(first["queue"][0]["kind"], "research")
        self.assertEqual(first, self.store.review_card(payload))

    def test_exclusion_cancels_queued_work_and_survives_import(self):
        self.store.edit_job({"key": "a:1", "revision": 1, "decision": "approve"})
        self.store.queue_jobs({"request_id": "application", "jobs": [{"key": "a:1", "revision": 2}]})
        state = self.store.review_card(self.payload("exclude"))
        self.assertEqual(state["queue"][0]["status"], "cancelled")
        self.assertTrue(next(c for c in state["companies"] if c["id"] == "a")["excluded"])
        new = {"companies": [{"id": "a", "name": "Alpha", "jobs": [{"job_id": "new", "title": "New role", "url": "https://jobs.example/a/new"}]}]}
        self.store.import_catalog(new, "new role")
        self.assertFalse(any(c["company_id"] == "a" for c in self.store.state()["flashcards"]))

    def test_exclusion_rejects_started_work_without_recording_review(self):
        self.store.edit_job({"key": "a:1", "revision": 1, "decision": "approve"})
        self.store.queue_jobs({"request_id": "application", "jobs": [{"key": "a:1", "revision": 2}]})
        self.store.update_status(self.store.state()["queue"][0]["id"], "in_progress")
        before = self.store.state()
        self.assert_conflict(lambda: self.store.review_card(self.payload("exclude")))
        self.assertEqual(before, self.store.state())

    def test_new_import_groups_and_more_than_three_jobs_remain_reviewable(self):
        incoming = {"companies": [{"id": "a", "name": "Alpha", "jobs": [
            {"job_id": str(n), "title": f"Engineer {n}", "rank": n, "url": f"https://jobs.example/a/{n}"}
            for n in (7, 5, 4, 6)]}]}
        self.store.import_catalog(incoming, "new group")
        self.assertEqual([j["key"] for j in self.card()["jobs"]], ["a:4", "a:5", "a:6"])
        self.store.review_card(self.payload("research", request_id="new-first"))
        self.assertEqual([j["key"] for j in self.card()["jobs"]], ["a:7"])
        self.store.review_card(self.payload("research", request_id="new-last"))
        self.assertEqual([j["key"] for j in self.card()["jobs"]], ["a:1", "a:2", "a:3"])
        self.store.review_card(self.payload("research", request_id="original"))
        self.assertFalse(any(c["company_id"] == "a" for c in self.store.state()["flashcards"]))
        self.assertEqual(len(self.store.state()["queue"]), 1)

    def test_copy_and_rank_changes_stay_reviewed_material_change_returns(self):
        self.store.review_card(self.payload("research"))
        copy_edit = catalog()
        copy_edit["companies"][0]["jobs"][1].update(rank=0, brief={"work": "Build services."})
        self.store.import_catalog(copy_edit, "copy")
        self.assertFalse(any(c["company_id"] == "a" for c in self.store.state()["flashcards"]))
        copy_edit["companies"][0]["jobs"][1]["location"] = "Remote US"
        self.store.import_catalog(copy_edit, "material")
        self.assertEqual([j["key"] for j in self.card()["jobs"]], ["a:2"])
        self.store.review_card(self.payload("research", request_id="material-review"))
        self.assertFalse(any(c["company_id"] == "a" for c in self.store.state()["flashcards"]))
        self.assertEqual(len(self.store.state()["card_reviews"]), 2)

    def test_concurrent_choices_save_only_one_complete_review(self):
        payload = self.payload()
        def choose(index):
            try:
                self.store.review_card({**payload, "request_id": f"parallel-{index}"})
                return "saved"
            except ReviewError as error:
                return error.status
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(choose, range(2)))
        self.assertCountEqual(results, ["saved", 409])
        self.assertEqual(len(self.store.state()["card_reviews"]), 1)

    def test_upgrade_marks_prior_research_group_but_not_new_jobs(self):
        self.store.request_research({"id": "a", "revision": 1, "note": "Find another team."})
        prior_queue = self.store.state()["queue"]
        self.store.import_catalog({"companies": [{"id": "a", "name": "Alpha", "jobs": [
            {"job_id": str(n), "title": f"Engineer {n}", "rank": n, "url": f"https://jobs.example/a/{n}"}
            for n in (4, 5, 6)]}]}, "new research")
        # Simulate a database from before card history existed.
        with self.store.transaction(True) as db:
            db.execute("DELETE FROM migrations WHERE source=?", (CARD_HISTORY_MIGRATION,))
        upgraded = Store(self.path).state()
        card = next(c for c in upgraded["flashcards"] if c["company_id"] == "a")
        self.assertEqual([j["key"] for j in card["jobs"]], ["a:4", "a:5", "a:6"])
        self.assertEqual(card["remaining_jobs"], 3)
        self.assertEqual([j["key"] for j in upgraded["card_reviews"][0]["jobs"]], ["a:1", "a:2", "a:3"])
        self.assertEqual(upgraded["queue"], prior_queue)
        self.assertTrue(all(j["decision"] == "pending" for j in upgraded["jobs"]))
        self.assertEqual(Store(self.path).state(), upgraded)

    def test_upgrade_does_not_assume_edited_rows_were_previously_reviewed(self):
        self.store.request_research({"id": "a", "revision": 1, "note": "Find other teams."})
        updated = catalog()
        updated["companies"][0]["jobs"][0]["location"] = "Remote US"
        updated["companies"][0]["jobs"][1]["rank"] = 4
        self.store.import_catalog(updated, "edited after research request")
        with self.store.transaction(True) as db:
            db.execute("DELETE FROM migrations WHERE source=?", (CARD_HISTORY_MIGRATION,))
        state = Store(self.path).state()
        card = next(c for c in state["flashcards"] if c["company_id"] == "a")
        self.assertEqual([j["key"] for j in card["jobs"]], ["a:1", "a:2"])
        self.assertEqual([j["key"] for j in state["card_reviews"][0]["jobs"]], ["a:3"])


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / "migration.sqlite3")
        self.data = catalog()
        self.draft = {"dataset_version": "legacy-v1", "saved_at": "2026-10-08T21:00:00+00:00", "decisions": {
            "a": {"action": "approve", "approved_job_ids": ["1"], "note": "Only this job."},
            "b": {"action": "exclude", "approved_job_ids": [], "note": "Not interested."}}}

    def tearDown(self):
        self.temp.cleanup()

    def test_draft_migration_once_preserves_unqueued_choices(self):
        self.store.migrate(self.data, "legacy:/original/data.json", self.draft)
        state = self.store.state()
        self.assertEqual(state["jobs"][0]["decision"], "approve")
        self.assertFalse(state["queue"])
        self.assertTrue(state["companies"][1]["excluded"])
        self.store.edit_job({"key": "a:1", "revision": state["jobs"][0]["revision"], "decision": "pass"})
        result = self.store.migrate(self.data, "legacy:/original/data.json", self.draft)
        self.assertTrue(result["already_migrated"])
        self.assertEqual(self.store.state()["jobs"][0]["decision"], "pass")

    def submitted(self):
        job = self.data["companies"][0]["jobs"][0]
        return {**copy.deepcopy(self.draft), "review_id": "old-review", "submitted_at": "2026-10-08T20:00:00+00:00",
                "approved_jobs": [{"company_id": "a", "company": "Alpha", "note": "Only this job.", **{k: job[k] for k in ("title", "job_id", "url", "location")}}]}

    def test_submitted_migration_keeps_original_snapshot_and_history(self):
        submitted = self.submitted()
        draft = copy.deepcopy(self.draft)
        draft["decisions"]["a"]["approved_job_ids"].append("2")
        self.store.migrate(self.data, "legacy:/original/data.json", draft, submitted)
        state = self.store.state()
        self.assertEqual(len(state["queue"]), 1)
        queued = state["queue"][0]
        self.assertEqual(queued["status"], "queued")
        self.assertEqual(queued["snapshot"]["legacy_review_id"], "old-review")
        self.assertEqual(queued["snapshot"]["title"], submitted["approved_jobs"][0]["title"])
        self.assertEqual(queued["created_at"], submitted["submitted_at"])
        self.assertEqual(state["counts"]["ready"], 1)
        self.store.migrate(self.data, "legacy:/original/data.json", draft, submitted)
        self.assertEqual(self.store.state(), state)

    def test_version_mismatch_is_stale_not_an_approval_for_current_job(self):
        submitted = self.submitted()
        submitted["dataset_version"] = "older-version"
        submitted["approved_jobs"][0]["title"] = "Old exact title"
        self.store.migrate(self.data, "legacy:/old/data.json", submitted=submitted)
        state = self.store.state()
        self.assertEqual(state["queue"][0]["status"], "stale")
        self.assertEqual(state["queue"][0]["snapshot"]["title"], "Old exact title")
        self.assertEqual(state["jobs"][0]["decision"], "pending")

    def test_malformed_submission_rolls_back_whole_migration(self):
        submitted = self.submitted()
        submitted["approved_jobs"] = []
        with self.assertRaises(ReviewError):
            self.store.migrate(self.data, "legacy:/original/data.json", submitted=submitted)
        self.assertFalse(self.store.state()["companies"])

    def test_newer_draft_exclusion_cancels_submitted_queue_without_erasing_snapshot(self):
        submitted = self.submitted()
        draft = copy.deepcopy(self.draft)
        draft["decisions"]["a"] = {"action": "exclude", "approved_job_ids": [], "note": "Changed my mind."}
        self.store.migrate(self.data, "legacy:/original/data.json", draft, submitted)
        state = self.store.state()
        self.assertTrue(state["companies"][0]["excluded"])
        self.assertEqual(state["companies"][0]["note"], "Changed my mind.")
        self.assertEqual(state["queue"][0]["status"], "cancelled")
        self.assertEqual(state["queue"][0]["snapshot"]["title"], "Engineer 1")
        self.assertEqual(state["jobs"][0]["decision"], "pending")

    def test_newer_draft_skip_and_research_preserve_all_unqueued_choices(self):
        submitted = self.submitted()
        draft = copy.deepcopy(self.draft)
        draft["decisions"]["a"] = {"action": "skip", "approved_job_ids": [], "note": "Other roles later."}
        draft["decisions"]["b"] = {"action": "research", "approved_job_ids": [], "note": "Find remote roles."}
        self.store.migrate(self.data, "legacy:/original/data.json", draft, submitted)
        state = self.store.state()
        jobs = {j["key"]: j for j in state["jobs"]}
        self.assertEqual(jobs["a:1"]["decision"], "approve")
        self.assertEqual(jobs["a:2"]["decision"], "later")
        self.assertEqual(jobs["a:3"]["decision"], "later")
        self.assertFalse(state["companies"][1]["excluded"])
        self.assertEqual(state["companies"][1]["research_status"], "requested")
        research = next(q for q in state["queue"] if q["kind"] == "research")
        self.assertEqual(research["note"], "Find remote roles.")

    def test_older_draft_does_not_cancel_later_submitted_approval(self):
        submitted = self.submitted()
        draft = copy.deepcopy(self.draft)
        draft["saved_at"] = "2026-10-08T19:00:00Z"
        draft["decisions"]["a"] = {"action": "exclude", "approved_job_ids": [], "note": "Earlier choice."}
        self.store.migrate(self.data, "legacy:/original/data.json", draft, submitted)
        state = self.store.state()
        self.assertFalse(state["companies"][0]["excluded"])
        self.assertEqual(state["queue"][0]["status"], "queued")
        self.assertEqual(state["jobs"][0]["decision"], "approve")

    def test_unknown_legacy_timestamp_fails_without_overriding_choices(self):
        submitted = self.submitted()
        draft = copy.deepcopy(self.draft)
        draft.pop("saved_at")
        with self.assertRaisesRegex(ReviewError, "Cannot order"):
            self.store.migrate(self.data, "legacy:/original/data.json", draft, submitted)
        self.assertFalse(self.store.state()["companies"])


if __name__ == "__main__":
    unittest.main()
