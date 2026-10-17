"""Ranker text, cohort identity, and publication tests on temporary databases."""
import copy
import json
import tempfile
import unittest
from pathlib import Path

from store import ReviewError
from test_store import catalog
from workflow import WorkflowStore


class RankedCardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "ranked-cards.sqlite3"
        self.store = WorkflowStore(self.path)
        self.catalog = catalog()
        jobs = self.catalog["companies"][0]["jobs"]
        for number in (4, 5, 6):
            job = copy.deepcopy(jobs[0])
            job.update(job_id=str(number), title=f"Engineer {number}",
                       url=f"https://jobs.example/a/{number}", rank=number)
            jobs.append(job)
        for job in jobs:
            job["brief"] = {"work": "Original source work.", "fit": "Original source fit.",
                            "gap": "Original source gap."}
        self.store.import_catalog(self.catalog, "Fictional source catalog")

    def tearDown(self):
        self.tmp.cleanup()

    def request_ranking(self, *, dispatch=True):
        with self.store.transaction(True) as db:
            task_id = self.store._request_discovery(db, "a")
        task = self.store.lease_task("fictional-ranker", kinds=["rank"])
        self.assertIsNotNone(task)
        self.assertEqual(task_id, task["id"])
        if dispatch:
            self.store.discovery_context(task["id"], task["lease_token"])
        return task

    @staticmethod
    def result(task, keys=("a:3", "a:1", "a:2"), summary="These roles use confirmed backend experience."):
        candidates = {job["key"]: job for job in task["payload"]["candidates"]}
        return {"cards": [{"company_id": "a", "summary": summary, "jobs": [
            {"key": key, "revision": candidates[key]["revision"],
             "reason": f"The confirmed experience supports {key}.",
             "brief": {"work": f"Build systems for {key}.",
                       "fit": f"Relevant experience for {key}.",
                       "gap": f"Confirm domain requirements for {key}."}}
            for key in keys]}]}

    def accept(self, task, result):
        return self.store.accept_ranking(task["id"], task["lease_token"], result)

    def cards_for_a(self):
        return [card for card in self.store.feed()["cards"] if card["company"]["id"] == "a"]

    def persisted_workflow(self):
        with self.store.transaction() as db:
            return {table: [tuple(row) for row in db.execute(f"SELECT * FROM {table} ORDER BY rowid")]
                    for table in ("wf_selection", "wf_cards", "wf_card_content", "wf_tasks")}

    def test_ranker_order_and_exact_content_survive_restart_without_source_edits(self):
        before = {job["key"]: job for job in self.store.state()["jobs"]}
        task = self.request_ranking()
        result = self.result(task)
        receipt = self.accept(task, result)
        card = self.cards_for_a()[0]
        self.assertEqual(["a:3", "a:1", "a:2"], [job["key"] for job in card["jobs"]])
        self.assertEqual([3, 1, 2], [job["data"]["rank"] for job in card["jobs"]])
        expected_content = {"schema_version": 1, "source": "ranker",
                            "summary": result["cards"][0]["summary"],
                            "jobs": result["cards"][0]["jobs"]}
        self.assertEqual(expected_content, card["content"])
        self.assertEqual(receipt["card_ids"], [card["id"]])
        self.assertEqual([(job["key"], job["revision"]) for job in card["jobs"]],
                         [(job["key"], job["revision"]) for job in card["content"]["jobs"]])
        after = {job["key"]: job for job in self.store.state()["jobs"]}
        self.assertEqual(before, after)
        for job in card["jobs"]:
            self.assertEqual("Original source work.", job["data"]["brief"]["work"])
        self.store = WorkflowStore(self.path)
        self.assertEqual(card, self.cards_for_a()[0])

    def test_invalid_rank_results_commit_no_partial_selection_card_or_task_success(self):
        task = self.request_ranking()
        valid = self.result(task)
        invalid = {}
        invalid["missing cards"] = {"selected": []}
        invalid["cards is not a list"] = {"cards": {}}
        invalid["card is not an object"] = {"cards": ["not a card"]}
        for field in ("company_id", "summary", "jobs"):
            value = copy.deepcopy(valid)
            del value["cards"][0][field]
            invalid[f"missing {field}"] = value
        for field in ("reason", "brief"):
            value = copy.deepcopy(valid)
            del value["cards"][0]["jobs"][1][field]
            invalid[f"missing {field}"] = value
        for field in ("work", "fit", "gap"):
            value = copy.deepcopy(valid)
            value["cards"][0]["jobs"][1]["brief"][field] = "   "
            invalid[f"empty {field}"] = value
        value = copy.deepcopy(valid)
        value["cards"][0]["summary"] = "x" * 1001
        invalid["oversized summary"] = value
        value = copy.deepcopy(valid)
        value["cards"][0]["jobs"][1]["reason"] = "x" * 5001
        invalid["oversized reason"] = value
        value = copy.deepcopy(valid)
        value["cards"][0]["jobs"][1]["brief"]["work"] = "x" * 601
        invalid["oversized brief"] = value
        value = copy.deepcopy(valid)
        value["cards"].append(copy.deepcopy(value["cards"][0]))
        invalid["duplicate company"] = value
        value = copy.deepcopy(valid)
        value["cards"][0]["jobs"][1] = copy.deepcopy(value["cards"][0]["jobs"][0])
        invalid["duplicate job"] = value
        value = copy.deepcopy(valid)
        value["cards"][0]["jobs"][1]["revision"] += 1
        invalid["wrong revision"] = value
        value = copy.deepcopy(valid)
        value["cards"][0]["company_id"] = "b"
        invalid["cross company"] = value
        value = copy.deepcopy(valid)
        value["cards"][0]["jobs"] = []
        invalid["empty cohort"] = value
        invalid["four jobs"] = self.result(task, ("a:1", "a:2", "a:3", "a:4"))
        before = self.persisted_workflow()
        for label, result in invalid.items():
            with self.subTest(label=label):
                with self.assertRaises(ReviewError):
                    self.accept(task, result)
                self.assertEqual(before, self.persisted_workflow())
                self.assertEqual("running", self.store.tasks()[0]["status"])
        self.accept(task, valid)
        self.assertEqual("succeeded", self.store.tasks()[0]["status"])

    def test_worker_checkpoint_preserves_bound_company_context(self):
        task = self.request_ranking()
        saved = self.store.checkpoint_task(task["id"], task["lease_token"], {"phase": "writing card text"})
        self.assertEqual(task["lease_token"], saved["checkpoint"]["rank_input_lease"])
        self.assertIn("a", saved["checkpoint"]["rank_company_fingerprints"])
        with self.assertRaises(ReviewError):
            self.store.checkpoint_task(task["id"], task["lease_token"], {"rank_company_fingerprints": {}})
        self.accept(task, self.result(task))
        self.assertEqual("ranker", self.cards_for_a()[0]["content"]["source"])

    def test_legacy_card_keeps_identity_and_revision_after_schema_upgrade(self):
        before = self.store.feed()["cards"]
        self.assertTrue(all(card["content"] is None for card in before))
        with self.store.transaction(True) as db:
            self.assertEqual(0, db.execute("SELECT COUNT(*) FROM wf_card_content").fetchone()[0])
            db.execute("DROP TABLE wf_card_content")
        self.store = WorkflowStore(self.path)
        self.assertEqual(before, self.store.feed()["cards"])

    def test_prepared_card_waits_for_current_card_and_survives_review_revision_bump(self):
        task = self.request_ranking()
        self.accept(task, self.result(task))
        current = self.cards_for_a()[0]
        second_task = self.request_ranking()
        result = self.result(second_task, ("a:6", "a:4", "a:5"), "The next roles fit a different part of the experience.")
        receipt = self.accept(second_task, result)
        self.assertEqual(current, self.cards_for_a()[0])
        with self.store.transaction() as db:
            prepared = dict(db.execute("SELECT * FROM wf_cards WHERE id=?", (receipt["card_ids"][0],)).fetchone())
            self.assertEqual("prepared", prepared["status"])
            candidates = self.store._eligible(db, company_id="a", ranked=False, include_reserved=False)
            self.assertEqual([], candidates)
        self.store = WorkflowStore(self.path)
        self.assertEqual(current, self.cards_for_a()[0])
        self.store.receive_decision({"request_id": "review-old-card", "card_id": current["id"],
            "card_revision": current["revision"], "action": "pass", "selected": [current["jobs"][0]["key"]]})
        promoted = self.cards_for_a()[0]
        self.assertEqual(prepared["id"], promoted["id"])
        self.assertGreater(promoted["company"]["revision"], current["company"]["revision"])
        self.assertNotEqual(prepared["revision"], promoted["revision"])
        self.assertEqual(["a:6", "a:4", "a:5"], [job["key"] for job in promoted["jobs"]])
        self.assertEqual(result["cards"][0]["summary"], promoted["content"]["summary"])
        self.assertEqual(result["cards"][0]["jobs"], promoted["content"]["jobs"])
        with self.store.transaction() as db:
            old_content = json.loads(db.execute("SELECT content FROM wf_card_content WHERE card_id=?", (current["id"],)).fetchone()[0])
        self.assertEqual(current["content"], old_content)

    def test_one_changed_job_stales_the_whole_cohort_and_survivors_can_be_ranked_again(self):
        task = self.request_ranking()
        self.accept(task, self.result(task))
        current = self.cards_for_a()[0]
        changed = copy.deepcopy(self.catalog)
        changed["companies"][0]["jobs"][0]["title"] = "Changed responsibilities"
        self.store.import_catalog(changed, "Changed fictional posting")
        self.assertEqual([], self.cards_for_a())
        with self.store.transaction() as db:
            self.assertEqual("stale", db.execute("SELECT status FROM wf_cards WHERE id=?", (current["id"],)).fetchone()[0])
            for key in ("a:2", "a:3"):
                selection = db.execute("SELECT selected,evaluated FROM wf_selection WHERE job_key=?", (key,)).fetchone()
                self.assertEqual((0, 0), tuple(selection))
        rerank = self.request_ranking()
        self.assertTrue({"a:2", "a:3"} <= {job["key"] for job in rerank["payload"]["candidates"]})
        result = self.result(rerank, ("a:2", "a:3"), "Updated context for the two remaining matches.")
        self.accept(rerank, result)
        new_card = self.cards_for_a()[0]
        self.assertNotEqual(current["id"], new_card["id"])
        self.assertEqual(result["cards"][0]["summary"], new_card["content"]["summary"])

    def test_changed_ranked_job_never_falls_back_to_a_card_with_null_content(self):
        task = self.request_ranking()
        self.accept(task, self.result(task, ("a:1",)))
        changed = copy.deepcopy(self.catalog)
        changed["companies"][0]["jobs"][0]["brief"]["work"] = "Updated source wording."
        self.store.import_catalog(changed, "Revised fictional source wording")
        self.assertEqual([], self.cards_for_a())
        self.store = WorkflowStore(self.path)
        self.assertEqual([], self.cards_for_a())
        rerank = self.request_ranking()
        self.assertIn("a:1", {job["key"] for job in rerank["payload"]["candidates"]})

    def test_empty_rank_result_is_valid_and_publishes_no_ranked_card(self):
        task = self.request_ranking()
        receipt = self.accept(task, {"cards": []})
        self.assertEqual([], receipt["card_ids"])
        self.assertEqual(0, receipt["selected_jobs"])
        self.assertEqual([], self.cards_for_a())
        finished = next(item for item in self.store.tasks() if item["id"] == task["id"])
        self.assertEqual("succeeded", finished["status"])

    def test_rank_result_rejects_company_context_changed_after_dispatch(self):
        task = self.request_ranking()
        company = next(item for item in self.store.state()["companies"] if item["id"] == "a")
        self.store.edit_company({"id": "a", "revision": company["revision"], "excluded": False,
                                 "note": "Only consider roles with confirmed remote work."})
        before = self.persisted_workflow()
        with self.assertRaises(ReviewError):
            self.accept(task, self.result(task))
        self.assertEqual(before, self.persisted_workflow())

    def test_rank_result_rejects_company_source_change_without_job_revision_change(self):
        task = self.request_ranking()
        jobs = self.store.state()["jobs"]
        changed = copy.deepcopy(self.catalog)
        changed["companies"][0]["summary"] = "The company now focuses on on-site hardware work."
        self.store.import_catalog(changed, "Changed fictional company context")
        self.assertEqual(jobs, self.store.state()["jobs"])
        before = self.persisted_workflow()
        with self.assertRaises(ReviewError) as caught:
            self.accept(task, self.result(task))
        self.assertEqual(409, caught.exception.status)
        self.assertEqual(before, self.persisted_workflow())

    def test_ranking_requires_dispatch_context_even_for_empty_result(self):
        task = self.request_ranking(dispatch=False)
        before = self.persisted_workflow()
        with self.assertRaises(ReviewError):
            self.accept(task, {"cards": []})
        self.assertEqual(before, self.persisted_workflow())
        self.store.discovery_context(task["id"], task["lease_token"])
        changed = copy.deepcopy(self.catalog)
        changed["companies"][0]["name"] = "Alpha with revised company context"
        self.store.import_catalog(changed, "Changed fictional company name")
        before = self.persisted_workflow()
        with self.assertRaises(ReviewError):
            self.accept(task, {"cards": []})
        self.assertEqual(before, self.persisted_workflow())

    def test_stale_pre_feature_ranked_card_keeps_survivors_eligible_for_refill(self):
        original = self.cards_for_a()[0]
        keys = {job["key"] for job in original["jobs"]}
        with self.store.transaction(True) as db:
            # Old rank results selected jobs before separate authored card content existed.
            for job in original["jobs"]:
                db.execute("INSERT INTO wf_selection VALUES (?,?,1,?,1)",
                           (job["key"], job["revision"], "Earlier rank result."))
        changed = copy.deepcopy(self.catalog)
        changed["companies"][0]["jobs"][0]["salary"] = "$150–250"
        self.store.import_catalog(changed, "Changed fictional compensation")
        self.store.feed()
        task_id = self.store.schedule_refill()
        task = next(item for item in self.store.tasks() if item["id"] == task_id)
        self.assertEqual("rank", task["kind"])
        self.assertTrue(keys <= {job["key"] for job in task["payload"]["candidates"]})

    def test_staling_prepared_card_preserves_explicit_research_exclusion(self):
        first = self.request_ranking()
        self.accept(first, self.result(first))
        current = self.cards_for_a()[0]
        second = self.request_ranking()
        self.accept(second, self.result(second, ("a:4", "a:5", "a:6")))
        self.store.receive_decision({"request_id": "find-more-with-exclusion", "card_id": current["id"],
            "card_revision": current["revision"], "action": "find_more", "selected": [],
            "exclude_job_keys": ["a:6"]})
        self.assertEqual([], self.cards_for_a())
        with self.store.transaction() as db:
            excluded = db.execute("SELECT selected,evaluated,reason FROM wf_selection WHERE job_key='a:6'").fetchone()
            self.assertEqual((0, 1), tuple(excluded)[:2])
            self.assertEqual("Excluded from this research request.", excluded["reason"])
        task_id = self.store.schedule_refill()
        task = next(item for item in self.store.tasks() if item["id"] == task_id)
        self.assertEqual({"a:4", "a:5"}, {job["key"] for job in task["payload"]["candidates"]})


if __name__ == "__main__":
    unittest.main()
