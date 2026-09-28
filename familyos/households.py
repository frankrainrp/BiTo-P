from __future__ import annotations

from datetime import datetime, timezone
import sqlite3
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .auth import AuthenticationError, normalize_username
from .db import Database

ROLES = {"adult", "child", "caregiver", "visitor"}


class ValidationError(ValueError):
    pass


class HouseholdService:
    def __init__(self, database: Database):
        self.database = database

    def get_setup(self, *, include_members: bool = False) -> dict:
        with self.database.session() as connection:
            household = connection.execute("SELECT id, name, timezone, created_at FROM households LIMIT 1").fetchone()
            if not household:
                return {"configured": False, "household": None, "members": []}
            members = []
            if include_members:
                members = connection.execute(
                    "SELECT id, username, display_name, role, is_admin, created_at "
                    "FROM members WHERE household_id = ? ORDER BY created_at",
                    (household["id"],),
                ).fetchall()
        return {
            "configured": True,
            "household": dict(household),
            "members": [{**dict(member), "is_admin": bool(member["is_admin"])} for member in members],
        }

    def initialize(self, payload: dict) -> dict:
        name = self._text(payload.get("household_name"), "家庭名称")
        member_name = self._text(payload.get("member_name"), "成员名称")
        try:
            username, username_key = normalize_username(payload.get("username") or member_name)
        except AuthenticationError as exc:
            raise ValidationError(str(exc)) from exc
        timezone_name = self._text(payload.get("timezone"), "时区")
        if len(name) > 80 or len(member_name) > 80:
            raise ValidationError("名称不能超过 80 个字符")
        try:
            ZoneInfo(timezone_name)
        except ZoneInfoNotFoundError as exc:
            raise ValidationError("请选择有效的 IANA 时区") from exc

        now = datetime.now(timezone.utc).isoformat()
        household_id, member_id = str(uuid4()), str(uuid4())
        with self.database.session() as connection:
            if connection.execute("SELECT 1 FROM households LIMIT 1").fetchone():
                raise ValidationError("家庭已经初始化，请刷新页面")
            connection.execute(
                "INSERT INTO households(id, name, timezone, created_at) VALUES (?, ?, ?, ?)",
                (household_id, name, timezone_name, now),
            )
            connection.execute(
                "INSERT INTO members(id, household_id, display_name, role, is_admin, created_at, username, username_key) "
                "VALUES (?, ?, ?, 'adult', 1, ?, ?, ?)",
                (member_id, household_id, member_name, now, username, username_key),
            )
        return self.get_setup(include_members=True)

    def add_member(self, actor: dict, payload: dict) -> dict:
        if not actor["is_admin"]:
            raise ValidationError("只有家庭管理员可以新增成员")
        display_name = self._text(payload.get("display_name"), "成员称呼")
        if len(display_name) > 80:
            raise ValidationError("成员称呼不能超过 80 个字符")
        try:
            username, username_key = normalize_username(payload.get("username"))
        except AuthenticationError as exc:
            raise ValidationError(str(exc)) from exc
        role = payload.get("role", "adult")
        if role not in ROLES:
            raise ValidationError("成员角色不正确")
        member_id, now = str(uuid4()), datetime.now(timezone.utc).isoformat()
        try:
            with self.database.session() as connection:
                connection.execute(
                    "INSERT INTO members(id,household_id,display_name,role,is_admin,created_at,username,username_key) "
                    "VALUES (?,?,?,?,0,?,?,?)",
                    (member_id, actor["household_id"], display_name, role, now, username, username_key),
                )
        except sqlite3.IntegrityError as exc:
            if "members.username_key" in str(exc):
                raise ValidationError("这个用户名已经被使用") from exc
            raise
        return {
            "id": member_id,
            "username": username,
            "display_name": display_name,
            "role": role,
            "is_admin": False,
        }

    @staticmethod
    def _text(value, label: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValidationError(f"请填写{label}")
        return value.strip()
