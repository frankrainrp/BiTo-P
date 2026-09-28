from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone
import base64
import binascii
import hashlib
import json
import re
import sqlite3
import struct
from uuid import uuid4
from zoneinfo import ZoneInfo

from .analytics import confidence_for
from .db import Database
from .households import ValidationError

CATEGORIES = {"work", "study", "communication", "entertainment", "system", "life", "sensitive", "other"}
METHODS = {"accessibility", "windows_ocr", "local_ocr", "local_vision", "manual"}
REPORT_METHODS = {"deterministic", "local_ollama", "manual"}
EVENT_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{8,80}$")
FORBIDDEN_IMAGE_FIELDS = {"image", "image_base64", "screenshot", "screenshot_base64", "frame", "pixels"}
REDACTIONS = (
    (re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{8,}"), "Bearer [REDACTED]"),
    (re.compile(r"\b(?:sk|ghp|glpat|xoxb)-?[A-Za-z0-9_-]{12,}\b"), "[REDACTED_TOKEN]"),
    (re.compile(r"(?i)\b(password|passwd|token|secret|api[_-]?key)\s*[:=]\s*\S+"), r"\1=[REDACTED]"),
    (re.compile(r"\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b"), "[REDACTED_EMAIL]"),
    (re.compile(r"\b(?:\d[ -]*?){13,19}\b"), "[REDACTED_NUMBER]"),
)


def redact_text(value: str) -> str:
    result = value
    for pattern, replacement in REDACTIONS:
        result = pattern.sub(replacement, result)
    return result


class ActivityService:
    """Stores redacted activity observations; raw screenshots are never accepted."""

    def __init__(self, database: Database, retention_days: int = 365):
        self.database = database
        self.retention_days = retention_days

    def ingest(self, member: dict, payload: dict, *, now: datetime | None = None) -> dict:
        if FORBIDDEN_IMAGE_FIELDS.intersection(payload):
            raise ValidationError("服务器不接收原始截图；请在设备本地 OCR 后只上传脱敏结构化记录")
        if payload.get("redacted") is not True:
            raise ValidationError("活动记录必须先在采集设备完成脱敏")

        client_event_id = self._text(payload.get("client_event_id"), "客户端事件编号", 80)
        if not EVENT_ID_RE.fullmatch(client_event_id):
            raise ValidationError("客户端事件编号格式不正确")
        source_device = self._text(payload.get("source_device"), "来源设备", 80)
        app_name = redact_text(self._text(payload.get("app_name"), "应用名称", 120))
        window_title = redact_text(self._optional_text(payload.get("window_title"), "窗口标题", 240))
        ocr_excerpt = redact_text(self._optional_text(payload.get("ocr_excerpt"), "OCR 摘要", 1200))
        summary = redact_text(self._optional_text(payload.get("activity_summary"), "活动摘要", 600))
        if not any((window_title, ocr_excerpt, summary)):
            raise ValidationError("窗口标题、OCR 摘要或活动摘要至少需要一项")

        started = self._timestamp(payload.get("started_at"), "开始时间")
        ended = self._timestamp(payload.get("ended_at"), "结束时间")
        duration = ended - started
        if duration <= timedelta(0) or duration > timedelta(hours=1):
            raise ValidationError("单条活动时长必须大于 0 且不超过 1 小时")
        current = now or datetime.now(timezone.utc)
        if ended > current + timedelta(minutes=5):
            raise ValidationError("活动结束时间不能位于未来")

        category = payload.get("category")
        if category not in CATEGORIES:
            raise ValidationError("活动分类不在允许范围内")
        method = payload.get("analysis_method")
        if method not in METHODS:
            raise ValidationError("分析方式不在允许范围内")
        try:
            analysis_confidence = float(payload.get("analysis_confidence"))
        except (TypeError, ValueError) as exc:
            raise ValidationError("分析置信度必须是 0–1 之间的数字") from exc
        if not 0 <= analysis_confidence <= 1:
            raise ValidationError("分析置信度必须是 0–1 之间的数字")

        record_id = str(uuid4())
        created_at = current.astimezone(timezone.utc).isoformat()
        expires_at = (current.astimezone(timezone.utc) + timedelta(days=self.retention_days)).isoformat()
        inserted = True
        with self.database.session() as connection:
            connection.execute("DELETE FROM activity_records WHERE expires_at<=?", (created_at,))
            try:
                connection.execute(
                    "INSERT INTO activity_records("
                    "id,client_event_id,household_id,member_id,source_device,started_at,ended_at,app_name,"
                    "window_title,ocr_excerpt,activity_summary,category,analysis_method,analysis_confidence,"
                    "redacted,created_at,expires_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,?,?)",
                    (
                        record_id, client_event_id, member["household_id"], member["id"], source_device,
                        started.astimezone(timezone.utc).isoformat(), ended.astimezone(timezone.utc).isoformat(),
                        app_name, window_title, ocr_excerpt, summary, category, method,
                        analysis_confidence, created_at, expires_at,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                if "UNIQUE constraint failed" not in str(exc):
                    raise
                inserted = False
                row = connection.execute(
                    "SELECT id FROM activity_records WHERE member_id=? AND source_device=? AND client_event_id=?",
                    (member["id"], source_device, client_event_id),
                ).fetchone()
                record_id = row["id"]
        return {"id": record_id, "accepted": inserted, "redacted": True, "expires_at": expires_at}

    def ingest_icon(self, member: dict, payload: dict, *, now: datetime | None = None) -> dict:
        """Store a small executable icon, never a captured screen image."""
        source_device = self._text(payload.get("source_device"), "来源设备", 80)
        app_name = redact_text(self._text(payload.get("app_name"), "应用名称", 120))
        encoded = payload.get("icon_png_base64")
        if not isinstance(encoded, str) or not encoded:
            raise ValidationError("请提供应用图标")
        if len(encoded) > 90_000:
            raise ValidationError("应用图标不能超过 64 KB")
        try:
            icon_png = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValidationError("应用图标不是有效的 Base64") from exc
        if (
            not 33 <= len(icon_png) <= 65_536
            or not icon_png.startswith(b"\x89PNG\r\n\x1a\n")
            or icon_png[12:16] != b"IHDR"
        ):
            raise ValidationError("应用图标必须是 64 KB 以内的 PNG")
        width, height = struct.unpack(">II", icon_png[16:24])
        if not 1 <= width <= 256 or not 1 <= height <= 256:
            raise ValidationError("应用图标尺寸必须在 1–256 像素之间")

        digest = hashlib.sha256(icon_png).hexdigest()
        current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()
        with self.database.session() as connection:
            existing = connection.execute(
                "SELECT id,icon_sha256 FROM activity_app_icons "
                "WHERE member_id=? AND source_device=? AND app_name=?",
                (member["id"], source_device, app_name),
            ).fetchone()
            if existing:
                changed = existing["icon_sha256"] != digest
                if changed:
                    connection.execute(
                        "UPDATE activity_app_icons SET icon_png=?,icon_sha256=?,updated_at=? WHERE id=?",
                        (icon_png, digest, current, existing["id"]),
                    )
                return {"id": existing["id"], "accepted": changed}
            icon_id = str(uuid4())
            connection.execute(
                "INSERT INTO activity_app_icons(id,household_id,member_id,source_device,app_name,"
                "icon_png,icon_sha256,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (icon_id, member["household_id"], member["id"], source_device, app_name,
                 icon_png, digest, current, current),
            )
        return {"id": icon_id, "accepted": True}

    def icon(self, member: dict, icon_id: str) -> bytes:
        with self.database.session() as connection:
            row = connection.execute(
                "SELECT icon_png FROM activity_app_icons WHERE id=? AND member_id=?",
                (icon_id, member["id"]),
            ).fetchone()
        if not row:
            raise ValidationError("没有找到这个应用图标")
        return bytes(row["icon_png"])

    def timeline(self, member: dict, day_value: object | None = None) -> dict:
        day, start_utc, end_utc = self._day_window(member, day_value)
        with self.database.session() as connection:
            rows = connection.execute(
                "SELECT activity_records.id,client_event_id,activity_records.source_device,started_at,ended_at,"
                "activity_records.app_name,window_title,ocr_excerpt,activity_summary,category,analysis_method,"
                "analysis_confidence,redacted,activity_app_icons.id AS app_icon_id "
                "FROM activity_records LEFT JOIN activity_app_icons ON "
                "activity_app_icons.member_id=activity_records.member_id AND "
                "activity_app_icons.source_device=activity_records.source_device AND "
                "activity_app_icons.app_name=activity_records.app_name "
                "WHERE activity_records.member_id=? AND started_at>=? AND started_at<? "
                "ORDER BY started_at LIMIT 5000",
                (member["id"], start_utc, end_utc),
            ).fetchall()
        return {"date": day.isoformat(), "timezone": member["household_timezone"], "records": [dict(row) for row in rows]}

    def ingest_report(self, member: dict, payload: dict, *, now: datetime | None = None) -> dict:
        if FORBIDDEN_IMAGE_FIELDS.intersection(payload):
            raise ValidationError("服务器不接收原始截图；日报只能包含本机脱敏后的文本和统计")
        if payload.get("redacted") is not True:
            raise ValidationError("电脑日报必须先在采集设备完成脱敏")

        client_report_id = self._text(payload.get("client_report_id"), "客户端日报编号", 80)
        if not EVENT_ID_RE.fullmatch(client_report_id):
            raise ValidationError("客户端日报编号格式不正确")
        source_device = self._text(payload.get("source_device"), "来源设备", 80)
        report_date = self._day(payload.get("report_date")).isoformat()
        title = redact_text(self._text(payload.get("title"), "日报标题", 160))
        summary = redact_text(self._text(payload.get("summary"), "日报总结", 12000))
        highlights = self._text_list(payload.get("highlights", []), "关键片段", 20, 500)
        next_actions = self._text_list(payload.get("next_actions", []), "后续建议", 20, 500)
        method = payload.get("analysis_method")
        if method not in REPORT_METHODS:
            raise ValidationError("日报分析方式不在允许范围内")
        model_name = self._optional_text(payload.get("model_name"), "模型名称", 160)
        try:
            sample_size = int(payload.get("sample_size"))
            observed_minutes = float(payload.get("observed_minutes"))
        except (TypeError, ValueError) as exc:
            raise ValidationError("日报样本量和观察分钟数格式不正确") from exc
        if sample_size < 0 or not 0 <= observed_minutes <= 1440:
            raise ValidationError("日报样本量或观察分钟数超出范围")
        generated = self._timestamp(payload.get("generated_at"), "生成时间")
        current = now or datetime.now(timezone.utc)
        if generated > current + timedelta(minutes=5):
            raise ValidationError("日报生成时间不能位于未来")

        report_id = str(uuid4())
        created_at = current.astimezone(timezone.utc).isoformat()
        expires_at = (current.astimezone(timezone.utc) + timedelta(days=self.retention_days)).isoformat()
        inserted = True
        with self.database.session() as connection:
            connection.execute("DELETE FROM activity_reports WHERE expires_at<=?", (created_at,))
            try:
                connection.execute(
                    "INSERT INTO activity_reports("
                    "id,client_report_id,household_id,member_id,source_device,report_date,title,summary,"
                    "highlights_json,next_actions_json,analysis_method,model_name,sample_size,observed_minutes,"
                    "generated_at,redacted,created_at,expires_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,?,?)",
                    (
                        report_id, client_report_id, member["household_id"], member["id"], source_device,
                        report_date, title, summary,
                        json.dumps(highlights, ensure_ascii=False), json.dumps(next_actions, ensure_ascii=False),
                        method, model_name, sample_size, observed_minutes,
                        generated.astimezone(timezone.utc).isoformat(), created_at, expires_at,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                if "UNIQUE constraint failed" not in str(exc):
                    raise
                inserted = False
                row = connection.execute(
                    "SELECT id FROM activity_reports WHERE member_id=? AND source_device=? AND client_report_id=?",
                    (member["id"], source_device, client_report_id),
                ).fetchone()
                report_id = row["id"]
        return {"id": report_id, "accepted": inserted, "redacted": True, "expires_at": expires_at}

    def reports(self, member: dict, day_value: object | None = None) -> dict:
        day = self._day(day_value) if day_value not in (None, "") else self._day_window(member, None)[0]
        with self.database.session() as connection:
            rows = connection.execute(
                "SELECT id,client_report_id,source_device,report_date,title,summary,highlights_json,"
                "next_actions_json,analysis_method,model_name,sample_size,observed_minutes,generated_at,redacted "
                "FROM activity_reports WHERE member_id=? AND report_date=? ORDER BY generated_at DESC LIMIT 10",
                (member["id"], day.isoformat()),
            ).fetchall()
        reports = []
        for row in rows:
            item = dict(row)
            item["highlights"] = json.loads(item.pop("highlights_json"))
            item["next_actions"] = json.loads(item.pop("next_actions_json"))
            reports.append(item)
        return {"date": day.isoformat(), "timezone": member["household_timezone"], "reports": reports}

    def daily_summary(self, member: dict, day_value: object | None = None) -> dict:
        timeline = self.timeline(member, day_value)
        return self._summarize_records(timeline["records"], timeline["date"], timeline["timezone"])

    def history(self, member: dict, range_value: object | None = None) -> dict:
        """Return member-only historical aggregates; never return screenshots or raw OCR."""
        ranges = {'7d': 7, '30d': 30, '90d': 90, '365d': 365, 'all': None}
        selected = range_value or '30d'
        if selected not in ranges:
            raise ValidationError('历史范围只能是 7d、30d、90d、365d 或 all')
        zone = ZoneInfo(member['household_timezone'])
        today = datetime.now(timezone.utc).astimezone(zone).date()
        with self.database.session() as connection:
            if ranges[selected] is None:
                rows = connection.execute(
                    "SELECT started_at,ended_at,app_name,activity_summary,category,analysis_confidence "
                    "FROM activity_records WHERE member_id=? ORDER BY started_at LIMIT 50000", (member['id'],)
                ).fetchall()
            else:
                start_day = today - timedelta(days=ranges[selected] - 1)
                start = datetime.combine(start_day, time.min, zone).astimezone(timezone.utc).isoformat()
                rows = connection.execute(
                    "SELECT started_at,ended_at,app_name,activity_summary,category,analysis_confidence "
                    "FROM activity_records WHERE member_id=? AND started_at>=? ORDER BY started_at LIMIT 50000",
                    (member['id'], start),
                ).fetchall()
            report_rows = connection.execute(
                "SELECT report_date,title,summary,highlights_json,observed_minutes FROM activity_reports "
                "WHERE member_id=? ORDER BY report_date DESC,generated_at DESC LIMIT 120", (member['id'],)
            ).fetchall()
        records = [dict(row) for row in rows]
        grouped: dict[str, list[dict]] = defaultdict(list)
        for row in records:
            grouped[datetime.fromisoformat(row['started_at']).astimezone(zone).date().isoformat()].append(row)
        days = [self._summarize_records(grouped[day], day, member['household_timezone']) for day in sorted(grouped)]
        all_summary = self._summarize_records(records, 'all', member['household_timezone'])
        reports = []
        for row in report_rows:
            if ranges[selected] is not None and row['report_date'] < (today - timedelta(days=ranges[selected] - 1)).isoformat():
                continue
            reports.append({
                'date': row['report_date'], 'title': row['title'], 'summary': row['summary'][:800],
                'highlights': json.loads(row['highlights_json'])[:6], 'observed_minutes': row['observed_minutes'],
            })
        return {
            'range': selected, 'timezone': member['household_timezone'], 'days': days,
            'totals': all_summary, 'reports': reports[:30],
            'progress_note': '“记录到的进展”来自本机日报和可确认活动摘要；它不是对总产出、效率或工作质量的评分。',
        }

    def _summarize_records(self, records: list[dict], day: str, timezone_name: str) -> dict:
        overall_intervals = []
        category_intervals: dict[str, list[tuple[datetime, datetime]]] = defaultdict(list)
        app_intervals: dict[str, list[tuple[datetime, datetime]]] = defaultdict(list)
        work_study_intervals = []
        confidences = []
        for record in records:
            interval = (datetime.fromisoformat(record["started_at"]), datetime.fromisoformat(record["ended_at"]))
            overall_intervals.append(interval)
            category_intervals[record["category"]].append(interval)
            app_intervals[record["app_name"]].append(interval)
            if record["category"] in {"work", "study"}:
                work_study_intervals.append(interval)
            confidences.append(float(record["analysis_confidence"]))
        sample_size = len(records)
        observed_minutes = self._union_minutes(overall_intervals)
        work_study_minutes = self._union_minutes(work_study_intervals)
        return {
            "date": day,
            "timezone": timezone_name,
            "observed_minutes": observed_minutes,
            "sample_size": sample_size,
            "confidence": confidence_for(sample_size),
            "average_analysis_confidence": None if not confidences else round(sum(confidences) / len(confidences), 3),
            "work_study_minutes": work_study_minutes,
            "work_study_share_percent": None if observed_minutes <= 0 else round(100 * work_study_minutes / observed_minutes, 1),
            "categories": self._breakdown(category_intervals, "category"),
            "applications": self._breakdown(app_intervals, "application")[:10],
            "calculation": "对同一成员同一天的活动区间取并集，避免重叠截图重复累计；分类与应用分别计算并集分钟数。",
            "interpretation": "工作/学习占比的分子是被归为工作或学习的观察分钟，分母是全部被观察分钟。它只描述屏幕投入结构，不等于产出、专注度、幸福感或工作质量，也不推断因果。",
        }

    def delete_day(self, member: dict, day_value: object) -> dict:
        day, start_utc, end_utc = self._day_window(member, day_value)
        with self.database.session() as connection:
            record_cursor = connection.execute(
                "DELETE FROM activity_records WHERE member_id=? AND started_at>=? AND started_at<?",
                (member["id"], start_utc, end_utc),
            )
            report_cursor = connection.execute(
                "DELETE FROM activity_reports WHERE member_id=? AND report_date=?",
                (member["id"], day.isoformat()),
            )
        return {"date": day.isoformat(), "deleted_records": record_cursor.rowcount, "deleted_reports": report_cursor.rowcount}

    @staticmethod
    def _breakdown(groups: dict[str, list[tuple[datetime, datetime]]], label: str) -> list[dict]:
        result = [{label: key, "minutes": ActivityService._union_minutes(value)} for key, value in groups.items()]
        return sorted(result, key=lambda item: (-item["minutes"], item[label].casefold()))

    @staticmethod
    def _union_minutes(intervals: list[tuple[datetime, datetime]]) -> float:
        if not intervals:
            return 0.0
        merged: list[list[datetime]] = []
        for start, end in sorted(intervals):
            if not merged or start > merged[-1][1]:
                merged.append([start, end])
            elif end > merged[-1][1]:
                merged[-1][1] = end
        seconds = sum((end - start).total_seconds() for start, end in merged)
        return round(seconds / 60, 1)

    @staticmethod
    def _text(value: object, label: str, limit: int) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValidationError(f"请填写{label}")
        text = value.strip()
        if len(text) > limit:
            raise ValidationError(f"{label}不能超过 {limit} 个字符")
        return text

    @staticmethod
    def _optional_text(value: object, label: str, limit: int) -> str:
        if value is None:
            return ""
        if not isinstance(value, str):
            raise ValidationError(f"{label}格式不正确")
        text = value.strip()
        if len(text) > limit:
            raise ValidationError(f"{label}不能超过 {limit} 个字符")
        return text

    @staticmethod
    def _text_list(value: object, label: str, item_limit: int, text_limit: int) -> list[str]:
        if not isinstance(value, list) or len(value) > item_limit:
            raise ValidationError(f"{label}必须是最多 {item_limit} 项的列表")
        result = []
        for item in value:
            if not isinstance(item, str) or len(item.strip()) > text_limit:
                raise ValidationError(f"{label}中每项必须是最多 {text_limit} 个字符的文本")
            if item.strip():
                result.append(redact_text(item.strip()))
        return result

    @staticmethod
    def _timestamp(value: object, label: str) -> datetime:
        if not isinstance(value, str):
            raise ValidationError(f"请填写{label}")
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValidationError(f"{label}格式不正确") from exc
        if parsed.tzinfo is None:
            raise ValidationError(f"{label}必须包含时区")
        return parsed.astimezone(timezone.utc)

    @staticmethod
    def _day_window(member: dict, value: object | None) -> tuple[date, str, str]:
        zone = ZoneInfo(member["household_timezone"])
        if value in (None, ""):
            day = datetime.now(timezone.utc).astimezone(zone).date()
        elif isinstance(value, str):
            try:
                day = date.fromisoformat(value)
            except ValueError as exc:
                raise ValidationError("日期格式必须为 YYYY-MM-DD") from exc
        else:
            raise ValidationError("日期格式必须为 YYYY-MM-DD")
        start = datetime.combine(day, time.min, zone).astimezone(timezone.utc).isoformat()
        end = datetime.combine(day + timedelta(days=1), time.min, zone).astimezone(timezone.utc).isoformat()
        return day, start, end

    @staticmethod
    def _day(value: object) -> date:
        if not isinstance(value, str):
            raise ValidationError("日期格式必须为 YYYY-MM-DD")
        try:
            return date.fromisoformat(value)
        except ValueError as exc:
            raise ValidationError("日期格式必须为 YYYY-MM-DD") from exc
