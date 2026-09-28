from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
import json
import sqlite3
from uuid import uuid4
import re
from zoneinfo import ZoneInfo

from .db import Database
from .households import ValidationError


class LifeService:
    def __init__(self, database: Database, butler=None):
        self.database = database
        self.butler = butler

    def calendar(self, member: dict, month: str | None = None) -> dict:
        zone = ZoneInfo(member['household_timezone'])
        month = month or datetime.now(zone).strftime('%Y-%m')
        if not isinstance(month, str) or not re.fullmatch(r'\d{4}-\d{2}', month):
            raise ValidationError('月份请使用 YYYY-MM 格式')
        try:
            first = date.fromisoformat(month + '-01')
            following = date(first.year + (first.month == 12), first.month % 12 + 1, 1)
        except ValueError as exc:
            raise ValidationError('月份不正确') from exc
        start = datetime.combine(first, time.min, zone).astimezone(timezone.utc).isoformat()
        end = datetime.combine(following, time.min, zone).astimezone(timezone.utc).isoformat()
        with self.database.session() as connection:
            events = connection.execute(
                'SELECT id,title,starts_at,ends_at,visibility FROM calendar_events '
                'WHERE household_id=? AND ends_at>? AND starts_at<? '
                "AND (owner_member_id=? OR visibility='household') ORDER BY starts_at LIMIT 1000",
                (member['household_id'], start, end, member['id']),
            ).fetchall()
            tasks = connection.execute(
                'SELECT id,title,due_at,status,visibility FROM tasks '
                'WHERE household_id=? AND due_at>=? AND due_at<? '
                "AND (owner_member_id=? OR visibility='household') ORDER BY due_at LIMIT 1000",
                (member['household_id'], start, end, member['id']),
            ).fetchall()
        source = self.butler.tasks(member) if self.butler else {'configured': False, 'available': False, 'items': []}
        butler_tasks = [item for item in source['items'] if item['due_at'] and
                        datetime.fromisoformat(item['due_at']).astimezone(zone).strftime('%Y-%m') == month]
        merged_tasks = [{**dict(row), 'source': 'familyos', 'read_only': False} for row in tasks] + butler_tasks
        merged_tasks.sort(key=lambda item: (item['due_at'] or '', item['title'].casefold()))
        return {'month': month, 'timezone': member['household_timezone'],
                'events': [{**dict(row), 'source': 'familyos'} for row in events], 'tasks': merged_tasks[:1000],
                'sources': {'butler': {key: value for key, value in source.items() if key != 'items'}}}

    def today(self, member: dict, *, now: datetime | None = None) -> dict:
        zone = ZoneInfo(member["household_timezone"])
        current = (now or datetime.now(timezone.utc)).astimezone(zone)
        today = current.date()
        start = datetime.combine(today, time.min, zone).astimezone(timezone.utc).isoformat()
        end = datetime.combine(today + timedelta(days=1), time.min, zone).astimezone(timezone.utc).isoformat()
        with self.database.session() as connection:
            tasks = connection.execute(
                "SELECT id, title, due_at, status, visibility FROM tasks "
                "WHERE household_id=? AND status='open' AND (owner_member_id=? OR visibility='household') "
                "ORDER BY due_at IS NULL, due_at, created_at LIMIT 50",
                (member["household_id"], member["id"]),
            ).fetchall()
            events = connection.execute(
                "SELECT id, title, starts_at, ends_at, visibility FROM calendar_events "
                "WHERE household_id=? AND ends_at>? AND starts_at<? "
                "AND (owner_member_id=? OR visibility='household') ORDER BY starts_at LIMIT 50",
                (member["household_id"], start, end, member["id"]),
            ).fetchall()
            habits = connection.execute(
                "SELECT h.id, h.title, h.target_per_week, h.visibility, "
                "CASE WHEN c.id IS NULL THEN 0 ELSE 1 END AS checked_today "
                "FROM habits h LEFT JOIN habit_checkins c ON c.habit_id=h.id AND c.occurred_on=? "
                "WHERE h.household_id=? AND h.status='active' "
                "AND (h.owner_member_id=? OR h.visibility='household') ORDER BY h.created_at",
                (today.isoformat(), member["household_id"], member["id"]),
            ).fetchall()
        source = self.butler.tasks(member) if self.butler else {'configured': False, 'available': False, 'items': []}
        butler_tasks = [item for item in source['items'] if item['status'] == 'open']
        butler_events = [{
            'id': f"butler-event:{item['id']}", 'title': item['title'], 'starts_at': item['due_at'],
            'ends_at': item['due_at'], 'visibility': 'private', 'source': 'butler', 'deadline': True,
        } for item in butler_tasks if item['due_at'] and
            datetime.fromisoformat(item['due_at']).astimezone(zone).date() == today]
        merged_tasks = [{**dict(row), 'source': 'familyos', 'read_only': False} for row in tasks] + butler_tasks
        merged_tasks.sort(key=lambda item: (item['due_at'] is None, item['due_at'] or '', item['title'].casefold()))
        merged_events = [{**dict(row), 'source': 'familyos', 'deadline': False} for row in events] + butler_events
        merged_events.sort(key=lambda item: item['starts_at'])
        return {
            "date": today.isoformat(),
            "timezone": member["household_timezone"],
            "tasks": merged_tasks[:100],
            "events": merged_events[:100],
            "habits": [{**dict(row), "checked_today": bool(row["checked_today"])} for row in habits],
            "sources": {"butler": {key: value for key, value in source.items() if key != 'items'}},
        }

    def create_task(self, member: dict, payload: dict) -> dict:
        title = self._text(payload.get("title"), "任务名称", 160)
        due_at = self._optional_datetime(payload.get("due_at"))
        visibility = self._visibility(payload.get("visibility"))
        now = datetime.now(timezone.utc).isoformat()
        item_id = str(uuid4())
        with self.database.session() as connection:
            connection.execute(
                "INSERT INTO tasks(id,household_id,owner_member_id,title,due_at,status,visibility,created_at,updated_at) "
                "VALUES (?,?,?,?,?,'open',?,?,?)",
                (item_id, member["household_id"], member["id"], title, due_at, visibility, now, now),
            )
            self._audit(connection, member, "task.created", item_id, {"title": title, "due_at": due_at})
        return {"id": item_id, "title": title, "due_at": due_at, "status": "open", "visibility": visibility}

    def complete_task(self, member: dict, task_id: str) -> dict:
        now = datetime.now(timezone.utc).isoformat()
        with self.database.session() as connection:
            row = connection.execute(
                "SELECT owner_member_id FROM tasks WHERE id=? AND household_id=?",
                (task_id, member["household_id"]),
            ).fetchone()
            if not row or (row["owner_member_id"] != member["id"] and not member["is_admin"]):
                raise ValidationError("任务不存在或你无权完成")
            connection.execute(
                "UPDATE tasks SET status='completed',completed_at=?,updated_at=? WHERE id=?",
                (now, now, task_id),
            )
            self._audit(connection, member, "task.completed", task_id, {})
        return {"id": task_id, "status": "completed", "completed_at": now}

    def create_event(self, member: dict, payload: dict) -> dict:
        title = self._text(payload.get("title"), "日程名称", 160)
        starts_at = self._required_datetime(payload.get("starts_at"), "开始时间")
        ends_at = self._required_datetime(payload.get("ends_at"), "结束时间")
        if ends_at <= starts_at:
            raise ValidationError("结束时间必须晚于开始时间")
        visibility = self._visibility(payload.get("visibility"))
        item_id, now = str(uuid4()), datetime.now(timezone.utc).isoformat()
        with self.database.session() as connection:
            connection.execute(
                "INSERT INTO calendar_events(id,household_id,owner_member_id,title,starts_at,ends_at,visibility,source,created_at) "
                "VALUES (?,?,?,?,?,?,?,'manual',?)",
                (item_id, member["household_id"], member["id"], title, starts_at, ends_at, visibility, now),
            )
            self._audit(connection, member, "calendar_event.created", item_id, {"title": title})
        return {"id": item_id, "title": title, "starts_at": starts_at, "ends_at": ends_at, "visibility": visibility}

    def create_habit(self, member: dict, payload: dict) -> dict:
        title = self._text(payload.get("title"), "习惯名称", 120)
        try:
            target = int(payload.get("target_per_week"))
        except (TypeError, ValueError) as exc:
            raise ValidationError("每周目标必须是 1–7") from exc
        if target < 1 or target > 7:
            raise ValidationError("每周目标必须是 1–7")
        visibility = self._visibility(payload.get("visibility"))
        item_id, now = str(uuid4()), datetime.now(timezone.utc).isoformat()
        with self.database.session() as connection:
            connection.execute(
                "INSERT INTO habits(id,household_id,owner_member_id,title,target_per_week,visibility,status,created_at) "
                "VALUES (?,?,?,?,?,?,'active',?)",
                (item_id, member["household_id"], member["id"], title, target, visibility, now),
            )
            self._audit(connection, member, "habit.created", item_id, {"title": title, "target_per_week": target})
        return {"id": item_id, "title": title, "target_per_week": target, "visibility": visibility}

    def check_in_habit(self, member: dict, habit_id: str, payload: dict) -> dict:
        day = self._date(payload.get("occurred_on"))
        with self.database.session() as connection:
            habit = connection.execute(
                "SELECT owner_member_id FROM habits WHERE id=? AND household_id=? AND status='active'",
                (habit_id, member["household_id"]),
            ).fetchone()
            if not habit or (habit["owner_member_id"] != member["id"] and not member["is_admin"]):
                raise ValidationError("习惯不存在或你无权打卡")
            try:
                connection.execute(
                    "INSERT INTO habit_checkins(id,habit_id,member_id,occurred_on,created_at) VALUES (?,?,?,?,?)",
                    (str(uuid4()), habit_id, habit["owner_member_id"], day, datetime.now(timezone.utc).isoformat()),
                )
            except sqlite3.IntegrityError as exc:
                if "UNIQUE constraint failed" in str(exc):
                    raise ValidationError("今天已经打卡") from exc
                raise
            self._audit(connection, member, "habit.checked_in", habit_id, {"occurred_on": day})
        return {"id": habit_id, "occurred_on": day, "checked": True}

    @staticmethod
    def _audit(connection, member: dict, event_type: str, subject_id: str, payload: dict) -> None:
        body = {"subject_id": subject_id, **payload}
        connection.execute(
            "INSERT INTO household_events(id,household_id,actor_member_id,subject_member_id,event_type,source,payload_json,occurred_at) "
            "VALUES (?,?,?,?,?,'manual',?,?)",
            (
                str(uuid4()), member["household_id"], member["id"], member["id"], event_type,
                json.dumps(body, ensure_ascii=False, separators=(",", ":")), datetime.now(timezone.utc).isoformat(),
            ),
        )

    @staticmethod
    def _text(value: object, label: str, limit: int) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValidationError(f"请填写{label}")
        text = value.strip()
        if len(text) > limit:
            raise ValidationError(f"{label}不能超过 {limit} 个字符")
        return text

    @staticmethod
    def _visibility(value: object) -> str:
        visibility = value if isinstance(value, str) else "private"
        if visibility not in {"private", "household"}:
            raise ValidationError("可见范围必须是本人或全家")
        return visibility

    @staticmethod
    def _required_datetime(value: object, label: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValidationError(f"请填写{label}")
        return LifeService._parse_datetime(value)

    @staticmethod
    def _optional_datetime(value: object) -> str | None:
        if value is None or value == "":
            return None
        if not isinstance(value, str):
            raise ValidationError("截止时间格式不正确")
        return LifeService._parse_datetime(value)

    @staticmethod
    def _parse_datetime(value: str) -> str:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValidationError("日期时间格式不正确") from exc
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).isoformat()

    @staticmethod
    def _date(value: object) -> str:
        if not isinstance(value, str):
            raise ValidationError("打卡日期格式不正确")
        try:
            return date.fromisoformat(value).isoformat()
        except ValueError as exc:
            raise ValidationError("打卡日期格式不正确") from exc
