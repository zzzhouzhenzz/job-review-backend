"""Regression checks for bounded discovery and unchanged rejected postings."""
import tempfile
import unittest
from pathlib import Path

from workflow import WorkflowStore


class DiscoveryRegressions(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = WorkflowStore(Path(self.tmp.name) / "regressions.sqlite3")

    def tearDown(self):
        self.tmp.cleanup()

    def find_more(self, count):
        jobs = [{"job_id": str(i), "title": f"Engineer {i}",
                 "url": f"https://example.com/jobs/{i}", "rank": i}
                for i in range(count)]
        self.store.import_catalog({"companies": [{"id": "a", "name": "Acme", "jobs": jobs}]}, "fictional")
        card = self.store.feed()["cards"][0]
        self.store.receive_decision({"request_id": "more", "card_id": card["id"],
                                     "card_revision": card["revision"], "action": "find_more"})
        task = self.store.lease_task("ranker", kinds=["rank"])
        self.store.discovery_context(task["id"], task["lease_token"])
        return task

    def test_empty_exploration_does_not_rerank_unchanged_rejections(self):
        rank = self.find_more(4)
        ranked = self.store.accept_ranking(rank["id"], rank["lease_token"], {"cards": []})
        self.assertIsNotNone(ranked["next_task_id"])
        explore = self.store.lease_task("explorer", kinds=["explore"])
        result = self.store.accept_exploration(explore["id"], explore["lease_token"],
                                               {"companies": [], "source": "No new evidence found"})
        self.assertIsNone(result["next_task_id"], "An empty source result must not rerank the same rejected revision.")

    def test_overflow_gets_ranked_before_unchanged_rejected_batch(self):
        rank = self.find_more(204)
        first_batch = {job["key"] for job in rank["payload"]["candidates"]}
        self.assertEqual(200, len(first_batch))
        result = self.store.accept_ranking(rank["id"], rank["lease_token"], {"cards": []})
        # An implementation can dispatch the next known batch directly or crawl
        # first. In either case it must not repeat the rejected first batch.
        following = self.store.lease_task("next-worker", kinds=["rank", "explore"])
        self.assertIsNotNone(following)
        if following["kind"] == "explore":
            self.store.accept_exploration(following["id"], following["lease_token"],
                                          {"companies": [], "source": "No new evidence found"})
            following = self.store.lease_task("next-ranker", kinds=["rank"])
        self.assertIsNotNone(following, "One known posting remains outside the first ranking batch.")
        next_batch = {job["key"] for job in following["payload"]["candidates"]}
        self.assertTrue(next_batch - first_batch, "The overflow posting must reach a ranker.")
        self.assertFalse(next_batch & first_batch, "Rejected revisions need new evidence before ranking again.")


if __name__ == "__main__":
    unittest.main()
