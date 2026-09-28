from datetime import datetime, timedelta, timezone
import unittest

from familyos.agent_schedule import AgentScheduleService
from familyos.households import ValidationError
import test_agent_jobs


class AgentScheduleTests(unittest.TestCase):
    tearDown = test_agent_jobs.AgentJobTests.tearDown
    def setUp(self):
        test_agent_jobs.AgentJobTests.setUp(self)
        self.schedules = AgentScheduleService(self.db)
        self.spec = dict(title='Morning', instructions='Summarize my tasks',
                         schedule_kind='daily', timezone='Asia/Shanghai', local_time='08:00')

    def test_ownership_and_optimistic_edit(self):
        saved = self.schedules.save(self.member, self.spec)['definitions'][0]
        self.assertEqual(self.schedules.list(self.other)['definitions'], [])
        with self.assertRaises(ValidationError):
            self.schedules.save(self.other, {**self.spec, 'version': 1}, saved['id'])
        self.schedules.save(self.member, {**self.spec, 'version': 1, 'enabled': False}, saved['id'])
        with self.assertRaises(ValidationError):
            self.schedules.save(self.member, {**self.spec, 'version': 1}, saved['id'])
        self.assertFalse(self.schedules.list(self.member)['definitions'][0]['enabled'])

    def test_dispatch_is_once_and_survives_service_recreation(self):
        saved = self.schedules.save(self.member, self.spec)['definitions'][0]
        due = datetime.fromisoformat(saved['next_run_at'])
        self.schedules.tick(due)
        AgentScheduleService(self.db).tick(due + timedelta(seconds=1))
        state = self.schedules.list(self.member)
        self.assertEqual(len(state['occurrences']), 1)
        self.assertEqual(state['occurrences'][0]['status'], 'dispatched')
        self.assertEqual(len(self.service.list(self.member)['jobs']), 1)
        self.assertNotEqual(state['definitions'][0]['next_run_at'], saved['next_run_at'])

    def test_webhook_execution_kind_survives_dispatch(self):
        saved = self.schedules.save(self.member, {**self.spec, 'execution_kind': 'webhook'})['definitions'][0]
        self.schedules.tick(datetime.fromisoformat(saved['next_run_at']))
        self.assertEqual(self.service.claim_next()['execution_kind'], 'webhook')

    def test_busy_queue_and_expired_occurrence(self):
        self.service.create(self.member, {'instructions': 'Busy'})
        saved = self.schedules.save(self.member, self.spec)['definitions'][0]
        due = datetime.fromisoformat(saved['next_run_at'])
        self.schedules.tick(due)
        self.assertEqual(self.schedules.list(self.member)['occurrences'][0]['status'], 'waiting')
        self.schedules.tick(due + timedelta(minutes=6))
        self.assertEqual(self.schedules.list(self.member)['occurrences'][0]['status'], 'missed')
        self.assertEqual(len(self.service.list(self.member)['jobs']), 1)

    def test_once_disables_after_dispatch(self):
        due = datetime.now(timezone.utc) + timedelta(hours=1)
        saved = self.schedules.save(self.member, {**self.spec, 'schedule_kind': 'once', 'one_time_at': due.isoformat()})
        self.schedules.tick(due)
        self.assertFalse(self.schedules.list(self.member)['definitions'][0]['enabled'])

    def test_dst_and_weekdays(self):
        spec = {**self.spec, 'timezone': 'America/New_York', 'local_time': '02:30', 'weekdays': []}
        result = self.schedules.next_times(spec, datetime(2026, 3, 8, tzinfo=timezone.utc), 1)
        self.assertEqual(result, ['2026-03-09T06:30:00+00:00'])
        spec['local_time'] = '01:30'
        result = self.schedules.next_times(spec, datetime(2026, 11, 1, tzinfo=timezone.utc), 2)
        self.assertEqual(result, ['2026-11-01T05:30:00+00:00', '2026-11-02T06:30:00+00:00'])
        spec.update(schedule_kind='weekly', weekdays=[0], timezone='Asia/Shanghai', local_time='08:00')
        result = self.schedules.next_times(spec, datetime(2026, 9, 24, tzinfo=timezone.utc), 1)
        self.assertEqual(result, ['2026-09-28T00:00:00+00:00'])


if __name__ == '__main__':
    unittest.main()
