from pathlib import Path
import tempfile
import unittest

from familyos.agent_jobs import AgentJobService
from familyos.auth import SessionService
from familyos.db import Database
from familyos.households import HouseholdService, ValidationError
from familyos.companion_actions import CompanionActionService
from familyos.life import LifeService


class FakeCompanion:
    def chat(self, member, payload):
        return {
            'text': f"已处理：{payload['message']}",
            'conversation_id': None,
        }


class AgentJobTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Database(str(Path(self.temp.name) / 'db.sqlite'))
        self.assertEqual(self.db.migrate(), 24)
        households = HouseholdService(self.db)
        households.initialize({
            'household_name': 'Test', 'member_name': 'Lin',
            'username': 'lin', 'timezone': 'Asia/Shanghai',
        })
        _, self.member = SessionService(self.db).create('lin')
        households.add_member(self.member, {'display_name': 'Yan', 'username': 'yan', 'role': 'adult'})
        _, self.other = SessionService(self.db).create('yan')
        self.service = AgentJobService(self.db, FakeCompanion())

    def tearDown(self):
        self.temp.cleanup()

    def test_job_runs_with_real_events_and_member_isolation(self):
        job = self.service.create(self.member, {'title': '整理今天', 'instructions': '总结今天的任务'})
        self.assertEqual(job['status'], 'queued')
        with self.assertRaises(ValidationError):
            self.service.get(self.other, job['id'])
        claimed = self.service.claim_next()
        self.assertEqual(claimed['id'], job['id'])
        self.service.execute(claimed)
        completed = self.service.get(self.member, job['id'])
        self.assertEqual(completed['status'], 'completed')
        self.assertIn('总结今天的任务', completed['result_text'])
        self.assertEqual(completed['events'][0]['event_type'], 'completed')
        self.assertEqual(self.service.list(self.member)['agent']['status'], 'idle')

    def test_webhook_delivers_once_and_failure_is_not_replayed(self):
        class Receiver:
            ready = True
            calls = 0
            def send(self, member, job):
                self.calls += 1
                return {'text': 'receipt-123', 'conversation_id': None}
        receiver = Receiver()
        service = AgentJobService(self.db, FakeCompanion(), webhook=receiver)
        job = service.create(self.member, {'instructions': 'notification', 'execution_kind': 'webhook'})
        claimed = service.claim_next()
        service.execute(claimed)
        service.execute(claimed)
        self.assertEqual(receiver.calls, 1)
        self.assertEqual(service.get(self.member, job['id'])['status'], 'completed')

    def test_webhook_failure_does_not_claim_success(self):
        class Receiver:
            ready = True
            def send(self, member, job):
                raise TimeoutError('private endpoint must not enter result')
        service = AgentJobService(self.db, FakeCompanion(), webhook=Receiver())
        job = service.create(self.member, {'instructions': 'notification', 'execution_kind': 'webhook'})
        service.execute(service.claim_next())
        result = service.get(self.member, job['id'])
        self.assertEqual(result['error_code'], 'delivery_unknown')
        self.assertNotIn('private endpoint', str(result))

    def test_only_one_active_job_and_queued_cancel_or_takeover(self):
        first = self.service.create(self.member, {'instructions': '第一项'})
        with self.assertRaises(ValidationError):
            self.service.create(self.member, {'instructions': '第二项'})
        cancelled = self.service.cancel(self.member, first['id'])
        self.assertEqual(cancelled['status'], 'cancelled')
        second = self.service.create(self.member, {'instructions': '第三项'})
        takeover = self.service.takeover(self.member, second['id'])
        self.assertEqual(takeover['status'], 'needs_attention')

    def test_payload_is_bounded(self):
        for payload in ({}, {'instructions': 'x' * 2001}, {'instructions': 'ok', 'unknown': True}):
            with self.assertRaises(ValidationError):
                self.service.create(self.member, payload)

    def proposing_job(self):
        actions = CompanionActionService(self.db, LifeService(self.db))
        proposal = actions.from_explicit_message(self.member, '记录任务：买牛奶')
        self.service.companion.chat = lambda member, payload: {'text': '等待确认', 'proposal': proposal}
        job = self.service.create(self.member, {'instructions': '记录任务：买牛奶'})
        self.service.execute(self.service.claim_next())
        return actions, proposal, job

    def test_proposal_must_be_confirmed_before_job_completes(self):
        actions, proposal, job = self.proposing_job()
        self.assertEqual(self.service.get(self.member, job['id'])['stage'], 'awaiting_confirmation')
        with self.assertRaises(ValidationError):
            actions.confirm(self.other, proposal['id'])
        actions.confirm(self.member, proposal['id'])
        self.assertEqual(self.service.get(self.member, job['id'])['status'], 'completed')
        self.assertEqual(self.service.cancel(self.member, job['id'])['status'], 'completed')

    def test_cancelling_waiting_job_rejects_proposal(self):
        actions, proposal, job = self.proposing_job()
        result = self.service.cancel(self.member, job['id'])
        self.assertEqual(result['status'], 'cancelled')
        self.assertEqual(result['proposal']['status'], 'rejected')
        with self.assertRaises(ValidationError):
            actions.confirm(self.member, proposal['id'])

    def test_restart_requires_attention_without_replay(self):
        job = self.service.create(self.member, {'instructions': 'Interrupted'})
        self.service.claim_next()
        self.service.recover_interrupted()
        result = self.service.get(self.member, job['id'])
        self.assertEqual(result['stage'], 'interrupted')
        self.assertIsNone(self.service.claim_next())


if __name__ == '__main__':
    unittest.main()
