from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest

from familyos.auth import SessionService
from familyos.db import Database
from familyos.households import HouseholdService, ValidationError
from familyos.life import LifeService


class LifeCoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.database = Database(str(Path(self.temp.name) / "familyos.db"))
        self.database.migrate()
        households = HouseholdService(self.database)
        households.initialize({"household_name": "林家", "member_name": "小林", "username": "xiaolin", "timezone": "Asia/Shanghai"})
        token, _ = SessionService(self.database).create("xiaolin")
        self.member = SessionService(self.database).resolve(token)
        self.life = LifeService(self.database)

    def tearDown(self):
        self.temp.cleanup()

    def test_today_contains_created_task_event_and_habit(self):
        task = self.life.create_task(self.member, {"title": "交电费", "visibility": "private"})
        self.life.create_event(self.member, {
            "title": "家庭晚餐", "starts_at": "2026-09-03T10:00:00+00:00",
            "ends_at": "2026-09-03T11:00:00+00:00", "visibility": "household"
        })
        habit = self.life.create_habit(self.member, {"title": "散步", "target_per_week": 5})
        self.life.check_in_habit(self.member, habit["id"], {"occurred_on": "2026-09-03"})
        today = self.life.today(self.member, now=datetime(2026, 9, 3, 12, tzinfo=timezone.utc))
        self.assertEqual(today["tasks"][0]["id"], task["id"])
        self.assertEqual(today["events"][0]["title"], "家庭晚餐")
        self.assertTrue(today["habits"][0]["checked_today"])

    def test_completion_is_audited_and_duplicate_checkin_rejected(self):
        task = self.life.create_task(self.member, {"title": "倒垃圾"})
        self.life.complete_task(self.member, task["id"])
        habit = self.life.create_habit(self.member, {"title": "喝水", "target_per_week": 7})
        self.life.check_in_habit(self.member, habit["id"], {"occurred_on": "2026-09-03"})
        with self.assertRaisesRegex(ValidationError, "已经打卡"):
            self.life.check_in_habit(self.member, habit["id"], {"occurred_on": "2026-09-03"})
        with self.database.session() as connection:
            events = [row[0] for row in connection.execute("SELECT event_type FROM household_events ORDER BY occurred_at")]
        self.assertIn("task.completed", events)
        self.assertIn("habit.checked_in", events)

    def test_event_end_must_follow_start(self):
        with self.assertRaisesRegex(ValidationError, "晚于"):
            self.life.create_event(self.member, {
                "title": "错误日程", "starts_at": "2026-09-03T12:00:00Z", "ends_at": "2026-09-03T11:00:00Z"
            })

    def test_butler_tasks_feed_today_schedule_and_calendar_without_becoming_writable(self):
        class ButlerFixture:
            def tasks(_, member):
                return {'configured': True, 'available': True, 'admin_only': True, 'items': [{
                    'id': 'butler:ddl-1', 'title': 'Butler 截止事项', 'due_at': '2026-09-03T04:00:00+00:00',
                    'status': 'open', 'visibility': 'private', 'source': 'butler', 'read_only': True,
                }]}

        life = LifeService(self.database, butler=ButlerFixture())
        today = life.today(self.member, now=datetime(2026, 9, 3, 2, tzinfo=timezone.utc))
        self.assertTrue(any(row['source'] == 'butler' for row in today['tasks']))
        self.assertTrue(any(row.get('deadline') and row['source'] == 'butler' for row in today['events']))
        calendar = life.calendar(self.member, '2026-09')
        imported = next(row for row in calendar['tasks'] if row['source'] == 'butler')
        self.assertTrue(imported['read_only'])
        self.assertTrue(calendar['sources']['butler']['available'])


if __name__ == "__main__":
    unittest.main()

