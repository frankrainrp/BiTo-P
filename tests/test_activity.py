from datetime import datetime, timezone
import base64
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from familyos.activity import ActivityService
from familyos.db import Database
from familyos.households import HouseholdService, ValidationError


class ActivityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.database = Database(str(Path(self.temp.name) / "familyos.db"))
        self.database.migrate()
        state = HouseholdService(self.database).initialize({
            "household_name": "验收家庭", "member_name": "甲", "username": "member-a", "timezone": "Asia/Shanghai"
        })
        self.member = state["members"][0]
        self.member["household_id"] = state["household"]["id"]
        self.member["household_timezone"] = "Asia/Shanghai"
        self.service = ActivityService(self.database)
        self.now = datetime(2026, 9, 3, 10, 0, tzinfo=timezone.utc)

    def tearDown(self):
        self.temp.cleanup()

    def payload(self, **changes):
        value = {
            "client_event_id": "event-0001",
            "source_device": "windows-main",
            "started_at": "2026-09-03T09:00:00+00:00",
            "ended_at": "2026-09-03T09:05:00+00:00",
            "app_name": "Visual Studio Code",
            "window_title": "FamilyOS — activity.py",
            "ocr_excerpt": "token=super-secret-value user@example.com",
            "activity_summary": "正在实现活动时间线",
            "category": "work",
            "analysis_method": "local_ocr",
            "analysis_confidence": 0.91,
            "redacted": True,
        }
        value.update(changes)
        return value

    def test_ingest_is_idempotent_and_redacts_common_secrets(self):
        first = self.service.ingest(self.member, self.payload(), now=self.now)
        second = self.service.ingest(self.member, self.payload(), now=self.now)
        self.assertTrue(first["accepted"])
        self.assertFalse(second["accepted"])
        record = self.service.timeline(self.member, "2026-09-03")["records"][0]
        self.assertNotIn("super-secret-value", record["ocr_excerpt"])
        self.assertNotIn("user@example.com", record["ocr_excerpt"])
        self.assertTrue(record["redacted"])

    def test_raw_screenshot_and_unredacted_payload_are_rejected(self):
        with self.assertRaises(ValidationError):
            self.service.ingest(self.member, self.payload(screenshot_base64="abc"), now=self.now)
        with self.assertRaises(ValidationError):
            self.service.ingest(self.member, self.payload(redacted=False), now=self.now)

    def test_daily_summary_unions_overlapping_intervals(self):
        self.service.ingest(self.member, self.payload(), now=self.now)
        self.service.ingest(self.member, self.payload(
            client_event_id="event-0002", started_at="2026-09-03T09:04:00+00:00",
            ended_at="2026-09-03T09:10:00+00:00", app_name="Terminal",
        ), now=self.now)
        summary = self.service.daily_summary(self.member, "2026-09-03")
        self.assertEqual(summary["observed_minutes"], 10.0)
        self.assertEqual(summary["sample_size"], 2)
        self.assertEqual(summary["confidence"], "insufficient_data")
        self.assertIn("不等于产出", summary["interpretation"])

    def test_history_keeps_member_data_and_labels_observation_limits(self):
        self.service.ingest(self.member, self.payload(), now=self.now)
        with patch("familyos.activity.datetime", wraps=datetime) as clock:
            clock.now.return_value = self.now
            result = self.service.history(self.member, "30d")
        self.assertEqual(result["range"], "30d")
        self.assertEqual(result["totals"]["sample_size"], 1)
        self.assertEqual(result["totals"]["observed_minutes"], 5.0)
        self.assertTrue(result["progress_note"])
        with self.assertRaises(ValidationError):
            self.service.history(self.member, "forever")

    def test_delete_day_removes_only_requested_day(self):
        self.service.ingest(self.member, self.payload(), now=self.now)
        result = self.service.delete_day(self.member, "2026-09-03")
        self.assertEqual(result["deleted_records"], 1)
        self.assertEqual(self.service.timeline(self.member, "2026-09-03")["records"], [])

    def test_app_icon_is_scoped_and_joined_to_timeline(self):
        icon = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M/wHwAF/gL+8oI2WQAAAABJRU5ErkJggg=="
        )
        payload = {
            "source_device": "windows-main",
            "app_name": "Visual Studio Code",
            "icon_png_base64": base64.b64encode(icon).decode("ascii"),
        }
        first = self.service.ingest_icon(self.member, payload, now=self.now)
        second = self.service.ingest_icon(self.member, payload, now=self.now)
        self.assertTrue(first["accepted"])
        self.assertFalse(second["accepted"])
        self.service.ingest(self.member, self.payload(), now=self.now)
        record = self.service.timeline(self.member, "2026-09-03")["records"][0]
        self.assertEqual(record["app_icon_id"], first["id"])
        self.assertEqual(self.service.icon(self.member, first["id"]), icon)

    def test_app_icon_rejects_non_png_payload(self):
        with self.assertRaises(ValidationError):
            self.service.ingest_icon(self.member, {
                "source_device": "windows-main",
                "app_name": "Broken",
                "icon_png_base64": base64.b64encode(b"not-an-image").decode("ascii"),
            }, now=self.now)

    def test_report_round_trip_is_idempotent_and_redacted(self):
        payload = {
            "client_report_id": "report-20260903",
            "source_device": "windows-main",
            "report_date": "2026-09-03",
            "title": "电脑日报",
            "summary": "完成 FamilyOS；token=very-secret-token",
            "highlights": ["完成活动采集接口"],
            "next_actions": ["补充界面测试"],
            "analysis_method": "local_ollama",
            "model_name": "local-model",
            "sample_size": 18,
            "observed_minutes": 95.5,
            "generated_at": "2026-09-03T09:59:00+00:00",
            "redacted": True,
        }
        first = self.service.ingest_report(self.member, payload, now=self.now)
        second = self.service.ingest_report(self.member, payload, now=self.now)
        self.assertTrue(first["accepted"])
        self.assertFalse(second["accepted"])
        report = self.service.reports(self.member, "2026-09-03")["reports"][0]
        self.assertNotIn("very-secret-token", report["summary"])
        self.assertEqual(report["highlights"], ["完成活动采集接口"])

    def test_report_rejects_images_and_invalid_metrics(self):
        payload = {
            "client_report_id": "report-20260903", "source_device": "windows-main",
            "report_date": "2026-09-03", "title": "日报", "summary": "摘要",
            "highlights": [], "next_actions": [], "analysis_method": "deterministic",
            "model_name": "", "sample_size": 1, "observed_minutes": 5,
            "generated_at": "2026-09-03T09:59:00+00:00", "redacted": True,
        }
        with self.assertRaises(ValidationError):
            self.service.ingest_report(self.member, {**payload, "image": "raw"}, now=self.now)
        with self.assertRaises(ValidationError):
            self.service.ingest_report(self.member, {**payload, "observed_minutes": 2000}, now=self.now)

    def test_timezone_is_required_and_future_records_are_rejected(self):
        with self.assertRaises(ValidationError):
            self.service.ingest(self.member, self.payload(started_at="2026-09-03T09:00:00"), now=self.now)
        with self.assertRaises(ValidationError):
            self.service.ingest(self.member, self.payload(
                started_at="2026-09-03T11:00:00+00:00", ended_at="2026-09-03T11:05:00+00:00"
            ), now=self.now)


if __name__ == "__main__":
    unittest.main()
