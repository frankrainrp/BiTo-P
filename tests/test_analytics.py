from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest
from uuid import uuid4

from familyos.analytics import AnalyticsService, confidence_for
from familyos.auth import SessionService
from familyos.db import Database
from familyos.households import HouseholdService
from familyos.life import LifeService


class AnalyticsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.database = Database(str(Path(self.temp.name) / "familyos.db"))
        self.database.migrate()
        HouseholdService(self.database).initialize({
            "household_name": "林家", "member_name": "小林", "username": "xiaolin", "timezone": "Asia/Shanghai"
        })
        token, _ = SessionService(self.database).create("xiaolin")
        self.member = SessionService(self.database).resolve(token)
        self.analytics = AnalyticsService(self.database)
        self.as_of = datetime(2026, 9, 3, 12, tzinfo=timezone.utc)

    def tearDown(self):
        self.temp.cleanup()

    def insert_task(self, days_ago: int, completed: bool):
        due = (self.as_of - timedelta(days=days_ago)).isoformat()
        now = self.as_of.isoformat()
        with self.database.session() as connection:
            connection.execute(
                "INSERT INTO tasks(id,household_id,owner_member_id,title,due_at,status,visibility,completed_at,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (str(uuid4()), self.member["household_id"], self.member["id"], "测试任务", due,
                 "completed" if completed else "open", "private", now if completed else None, now, now),
            )

    def test_task_rate_has_exact_window_sample_and_confidence(self):
        for index in range(5):
            self.insert_task(index, completed=index < 4)
        self.insert_task(40, completed=True)
        snapshot = self.analytics.member_snapshot(self.member, as_of=self.as_of)
        task = snapshot["metrics"][0]
        self.assertEqual((task["numerator"], task["denominator"], task["value"]), (4, 5, 80.0))
        self.assertEqual(task["confidence"], "directional")

    def test_empty_data_never_claims_a_percentage(self):
        snapshot = self.analytics.member_snapshot(self.member, as_of=self.as_of)
        for metric in snapshot["metrics"]:
            self.assertIsNone(metric["value"])
            self.assertEqual(metric["confidence"], "insufficient_data")

    def test_confidence_thresholds_are_deterministic(self):
        self.assertEqual(confidence_for(4), "insufficient_data")
        self.assertEqual(confidence_for(5), "directional")
        self.assertEqual(confidence_for(19), "directional")
        self.assertEqual(confidence_for(20), "stable")

    def test_new_habit_has_one_opportunity_but_no_trend_claim(self):
        habit = LifeService(self.database).create_habit(self.member, {"title": "散步", "target_per_week": 3})
        with self.database.session() as connection:
            connection.execute("UPDATE habits SET created_at=? WHERE id=?", (self.as_of.isoformat(), habit["id"]))
        LifeService(self.database).check_in_habit(self.member, habit["id"], {"occurred_on": "2026-09-03"})
        metric = self.analytics.member_snapshot(self.member, as_of=self.as_of)["metrics"][1]
        self.assertEqual((metric["numerator"], metric["denominator"], metric["value"]), (1, 1, 100.0))
        self.assertEqual(metric["confidence"], "insufficient_data")

    def test_window_validation_rejects_misleading_extremes(self):
        with self.assertRaises(ValueError):
            self.analytics.member_snapshot(self.member, as_of=self.as_of, window_days=1)
        with self.assertRaises(ValueError):
            self.analytics.member_snapshot(self.member, as_of=self.as_of, window_days=365)


if __name__ == "__main__":
    unittest.main()
