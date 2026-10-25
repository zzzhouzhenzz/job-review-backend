"""Canonical read and write fences, with fictional SQLite fixtures only."""
import copy
import json
import unittest

import test_question_pool as fixtures
from question_pool import SharedQuestionPool
from question_sync import mapped_answer, snapshot
from store import ReviewError


class QuestionSyncStoreTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.QuestionPoolTests(methodName='runTest')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.store = self.fixture.store
        self.pool = self.store.question_pool = self.fixture.pool
        with self.pool.hold('worker', 'fixture'):
            self.fixture.register()
            self.fixture.register(1)

    def command(self, index=0):
        command = self.fixture.payload(index, request=f'fixture-answer-{index}')
        command['expected_pool_revision'] = snapshot(self.store)['sync']['pool_revision']
        return command

    def test_revision_rechecked_inside_writer_lock_after_formatting(self):
        command = self.command(1)
        formatted = self.pool.formatting_spec(command)
        first = self.pool.formatting_spec(self.command())
        self.fixture.save(first)
        with self.pool.hold('answer_handler', 'delayed-device'):
            with self.assertRaises(ReviewError) as error:
                self.pool.save_answered_questions(formatted['append_text'])
        self.assertEqual(error.exception.status, 409)
        self.assertEqual(error.exception.reason, 'question_sync_stale')
        with self.store.transaction() as db:
            self.assertIsNone(db.execute("SELECT answer FROM wf_questions WHERE id='q1'").fetchone()[0])
            self.assertEqual(db.execute('SELECT COUNT(*) FROM wf_qpool_answers').fetchone()[0], 1)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM wf_qpool_batches').fetchone()[0], 1)

    def test_invalid_client_revision_rejected_before_formatting(self):
        for revision in (True, False, -1, 1.0, '1', None):
            with self.subTest(revision=revision):
                command = self.command()
                command['expected_pool_revision'] = revision
                with self.assertRaises(ReviewError) as error:
                    self.pool.formatting_spec(command)
                self.assertEqual(error.exception.status, 400)
        with self.store.transaction() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM wf_qpool_batches').fetchone()[0], 0)

    def test_expired_owner_and_unpublished_projection_are_pending(self):
        before = snapshot(self.store)
        with self.store.transaction(True) as db:
            db.execute("UPDATE wf_qpool_lock SET token='expired',generation=generation+1,expires_at='2000-01-01' WHERE singleton=1")
        pending = snapshot(self.store)
        self.assertEqual(pending['sync']['state'], 'pending')
        self.assertIsNone(pending['questions'])
        self.assertGreaterEqual(pending['sync']['revision'], before['sync']['revision'])
        with self.pool.hold('recovery', 'fixture-recovery'):
            pass
        repaired = snapshot(self.store)
        self.assertEqual(repaired['sync']['state'], 'ready')
        self.assertGreaterEqual(repaired['sync']['revision'], pending['sync']['revision'])
        with self.store.transaction(True) as db:
            self.pool._bump(db)
        self.assertEqual(snapshot(self.store)['sync']['state'], 'pending')
        with self.pool.hold('recovery', 'fixture-publication'):
            pass
        self.assertEqual(snapshot(self.store)['sync']['state'], 'ready')

    def test_read_does_not_increment_revision_or_acquire_writer_lock(self):
        before = snapshot(self.store)
        for _ in range(3):
            self.assertEqual(snapshot(self.store), before)
        with self.store.transaction() as db:
            self.assertIsNone(db.execute('SELECT token FROM wf_qpool_lock').fetchone()[0])

    def test_unbound_canonical_knowledge_remains_once_and_legacy_answer_is_audit_only(self):
        self.fixture.save(self.pool.formatting_spec(self.command()))
        with self.pool.hold('recovery', 'fictional-history'):
            with self.store.transaction(True) as db:
                db.execute("DELETE FROM wf_qpool_links WHERE question_id='q0'")
                self.pool._bump(db)
            self.pool.repair()
        result = snapshot(self.store)
        self.assertEqual(result['sync']['state'], 'ready')
        self.assertEqual(len(result['shared_questions']), 2)
        answered = next(q for q in result['shared_questions'] if q['status'] == 'answered')
        self.assertEqual(answered['binding_ids'], [])
        self.assertEqual(answered['answer'], 'yes-0')
        self.assertEqual(result['historical_unlinked_question_ids'], ['q0'])
        self.assertEqual([q['id'] for q in result['questions']], ['q1'])
        # The new read must not create a guessed link or alter history.
        with self.store.transaction() as db:
            self.assertIsNone(db.execute("SELECT * FROM wf_qpool_links WHERE question_id='q0'").fetchone())
            self.assertEqual(db.execute('SELECT COUNT(*) FROM wf_qpool_answers').fetchone()[0], 1)
            self.assertEqual(json.loads(db.execute("SELECT answer FROM wf_questions WHERE id='q0'").fetchone()[0]), 'yes-0')

    def test_unlinked_unanswered_submitted_history_does_not_block_ready(self):
        with self.pool.hold('recovery', 'fictional-history'):
            with self.store.transaction(True) as db:
                db.execute("DELETE FROM wf_qpool_links WHERE question_id='q0'")
                db.execute("UPDATE wf_processes SET submission_status='submitted' WHERE id=?", (self.fixture.processes[0][0],))
                self.pool._bump(db)
            self.pool.repair()
        result = snapshot(self.store)
        self.assertEqual(result['sync']['state'], 'ready')
        self.assertEqual(result['historical_unlinked_question_ids'], ['q0'])
        self.assertEqual(len(result['shared_questions']), 2)

    def test_multichoice_equivalent_order_uses_one_canonical_answer(self):
        with self.pool.hold('worker', 'fictional-multi'):
            with self.store.transaction(True) as db:
                db.execute("UPDATE wf_questions SET type='multi_choice' WHERE id='q0'")
                row = db.execute("SELECT p.* FROM wf_qpool_questions p JOIN wf_qpool_links l ON l.pool_id=p.id WHERE l.question_id='q0'").fetchone()
                record = json.loads(row['record']); record['type'] = 'multi_choice'
                db.execute('UPDATE wf_qpool_questions SET record=? WHERE id=?', (json.dumps(record), row['id']))
                self.pool._bump(db)
            self.pool.repair()
        first = self.command(); first['answers'][0]['value'] = ['yes-0', 'no-0']
        second = copy.deepcopy(first); second['request_id'] = 'equivalent-order'
        second['answers'][0]['value'].reverse()
        self.fixture.save(self.pool.formatting_spec(first))
        self.assertTrue(self.pool.formatting_spec(second)['already_answered'])
        with self.store.transaction() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM wf_qpool_answers').fetchone()[0], 1)

    def test_answer_mapping_text_multi_choice_and_ambiguous_labels(self):
        self.assertEqual(mapped_answer({'type': 'text'}, {'labels': json.dumps(['Exact user text.'])}),
                         'Exact user text.')
        record = {'type': 'multi_choice', 'choices': [{'id': 'local-b', 'label': 'Second'},
                                                    {'id': 'local-a', 'label': 'First'}]}
        self.assertEqual(mapped_answer(record, {'labels': json.dumps(['First', 'Second'])}),
                         ['local-a', 'local-b'])
        for invalid in (['Unknown'], ['First', 'First']):
            with self.assertRaises(ReviewError):
                mapped_answer(record, {'labels': json.dumps(invalid)})
        ambiguous = copy.deepcopy(record)
        ambiguous['choices'][1]['label'] = 'second'
        with self.assertRaises(ReviewError):
            mapped_answer(ambiguous, {'labels': json.dumps(['Second'])})

    def test_canonical_binding_overrides_divergent_legacy_read_and_blocks_stale_legacy_write(self):
        # Rebind this fictional second form to the same already registered pool.
        with self.pool.hold('worker', 'fixture-link'):
            with self.store.transaction(True) as db:
                target = db.execute("SELECT pool_id FROM wf_qpool_links WHERE question_id='q0'").fetchone()[0]
                db.execute("UPDATE wf_qpool_links SET pool_id=? WHERE question_id='q1'", (target,))
                self.pool._bump(db)
            self.pool.repair()
        old = self.fixture.payload(1, value='no-1', request='stale-legacy')
        self.fixture.save(self.pool.formatting_spec(self.command()))
        # Fan-out has not executed. The canonical API and legacy view must agree.
        views = {q['id']: q for q in self.store.questions()}
        self.assertEqual(views['q1']['answer'], 'yes-1')
        self.assertTrue(views['q1']['answered_at'])
        with self.assertRaises(ReviewError) as error:
            self.pool.formatting_spec(old)
        self.assertEqual(error.exception.reason, 'question_sync_stale')
        equivalent = copy.deepcopy(old)
        equivalent['answers'][0]['value'] = 'yes-1'
        self.assertTrue(self.pool.formatting_spec(equivalent)['already_answered'])


if __name__ == '__main__':
    unittest.main()
