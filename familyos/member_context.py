from __future__ import annotations

from datetime import datetime, timezone
import re
import sqlite3
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .households import ValidationError


TIME_RE = re.compile(r"(?:[01]\d|2[0-3]):[0-5]\d")
SCOPES = {"calendar", "tasks", "habits", "activity_summary", "profile"}
AVATAR_TYPES = {"image/png", "image/jpeg", "image/webp"}
MAX_AVATAR_BYTES = 2 * 1024 * 1024


class MemberContextService:
    def __init__(self, database):
        self.database = database

    def get(self, member):
        with self.database.session() as connection:
            profile = connection.execute(
                "SELECT preferred_language,timezone_override,quiet_start,quiet_end,assistance_preferences,updated_at "
                "FROM member_profiles WHERE member_id=?",
                (member["id"],),
            ).fetchone()
            relationships = connection.execute(
                "SELECT r.id,r.member_id,r.related_member_id,r.relationship,r.created_at,r.updated_at,"
                "a.display_name AS member_name,b.display_name AS related_member_name "
                "FROM member_relationships r JOIN members a ON a.id=r.member_id "
                "JOIN members b ON b.id=r.related_member_id "
                "WHERE r.household_id=? AND (r.member_id=? OR r.related_member_id=?) ORDER BY r.created_at",
                (member["household_id"], member["id"], member["id"]),
            ).fetchall()
            consents = connection.execute(
                "SELECT c.id,c.grantor_member_id,c.grantee_member_id,c.data_scope,c.purpose,c.expires_at,c.revoked_at,c.created_at,"
                "a.display_name AS grantor_name,b.display_name AS grantee_name "
                "FROM consent_grants c JOIN members a ON a.id=c.grantor_member_id "
                "JOIN members b ON b.id=c.grantee_member_id "
                "WHERE c.household_id=? AND (c.grantor_member_id=? OR c.grantee_member_id=?) ORDER BY c.created_at DESC",
                (member["household_id"], member["id"], member["id"]),
            ).fetchall()
            household_members = connection.execute(
                "SELECT id,display_name,role FROM members WHERE household_id=? ORDER BY created_at",
                (member["household_id"],),
            ).fetchall()
        default = {
            "preferred_language": "zh-CN", "timezone_override": None,
            "quiet_start": None, "quiet_end": None, "assistance_preferences": "", "updated_at": None,
        }
        return {
            "member": {key: member[key] for key in ("id", "username", "display_name", "role", "is_admin")},
            "profile": dict(profile) if profile else default,
            "household_members": [
                {
                    "id": row["id"],
                    "display_name": row["display_name"],
                    "role": row["role"],
                    "is_current": row["id"] == member["id"],
                }
                for row in household_members
            ],
            "relationships": [dict(row) for row in relationships],
            "consents": [dict(row) for row in consents],
        }

    def avatar(self, member):
        with self.database.session() as connection:
            row = connection.execute(
                "SELECT content_type,image FROM member_avatars WHERE member_id=?", (member["id"],)
            ).fetchone()
        return (row["content_type"], row["image"]) if row else None

    def save_avatar(self, member, image: bytes, content_type: str):
        if content_type not in AVATAR_TYPES:
            raise ValidationError("头像仅支持 PNG、JPEG 或 WebP 图片")
        if not image or len(image) > MAX_AVATAR_BYTES:
            raise ValidationError("头像不能为空且不能超过 2MB")
        signatures = {
            "image/png": b"\x89PNG\r\n\x1a\n",
            "image/jpeg": b"\xff\xd8\xff",
            "image/webp": b"RIFF",
        }
        if not image.startswith(signatures[content_type]) or (content_type == "image/webp" and image[8:12] != b"WEBP"):
            raise ValidationError("头像文件与声明的图片类型不一致")
        now = datetime.now(timezone.utc).isoformat()
        with self.database.session() as connection:
            connection.execute(
                "INSERT INTO member_avatars(member_id,content_type,image,updated_at) VALUES (?,?,?,?) "
                "ON CONFLICT(member_id) DO UPDATE SET content_type=excluded.content_type,image=excluded.image,updated_at=excluded.updated_at",
                (member["id"], content_type, image, now),
            )
        return {"updated_at": now}

    def assistant_context(self, member):
        """Return only household facts the signed-in member may give their assistant."""
        context = self.get(member)
        with self.database.session() as connection:
            household_members = connection.execute(
                "SELECT display_name,role,is_admin FROM members WHERE household_id=? ORDER BY created_at",
                (member["household_id"],),
            ).fetchall()
        return {
            "member": context["member"],
            "profile": context["profile"],
            "relationships": context["relationships"],
            "household_members": [dict(row) for row in household_members],
        }

    def save_profile(self, member, payload):
        allowed = {"preferred_language", "timezone_override", "quiet_start", "quiet_end", "assistance_preferences"}
        if set(payload) - allowed:
            raise ValidationError("包含不支持的成员设置")
        values = {**self.get(member)["profile"], **payload}
        language = values["preferred_language"]
        if not isinstance(language, str) or not re.fullmatch(r"[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})?", language):
            raise ValidationError("语言代码不正确")
        timezone_name = values["timezone_override"]
        if timezone_name in (None, ""):
            timezone_name = None
        else:
            try:
                ZoneInfo(timezone_name)
            except (ZoneInfoNotFoundError, TypeError) as exc:
                raise ValidationError("成员时区不正确") from exc
        for key in ("quiet_start", "quiet_end"):
            if values[key] in (None, ""):
                values[key] = None
            elif not isinstance(values[key], str) or not TIME_RE.fullmatch(values[key]):
                raise ValidationError("安静时段请使用 HH:MM 格式")
        assistance = values["assistance_preferences"]
        if not isinstance(assistance, str) or len(assistance) > 2000:
            raise ValidationError("协助偏好不能超过 2000 个字符")
        now = datetime.now(timezone.utc).isoformat()
        with self.database.session() as connection:
            connection.execute(
                "INSERT INTO member_profiles(member_id,preferred_language,timezone_override,quiet_start,quiet_end,assistance_preferences,updated_at) "
                "VALUES (?,?,?,?,?,?,?) ON CONFLICT(member_id) DO UPDATE SET preferred_language=excluded.preferred_language,"
                "timezone_override=excluded.timezone_override,quiet_start=excluded.quiet_start,quiet_end=excluded.quiet_end,"
                "assistance_preferences=excluded.assistance_preferences,updated_at=excluded.updated_at",
                (member["id"], language, timezone_name, values["quiet_start"], values["quiet_end"], assistance.strip(), now),
            )
        return self.get(member)["profile"]

    def grant(self, member, payload):
        if set(payload) != {"grantee_member_id", "data_scope", "purpose", "expires_at"}:
            raise ValidationError("授权字段不完整")
        grantee = payload["grantee_member_id"]
        scope = payload["data_scope"]
        purpose = payload["purpose"]
        expires = payload["expires_at"] or None
        if scope not in SCOPES or not isinstance(purpose, str) or not purpose.strip() or len(purpose) > 240:
            raise ValidationError("授权范围或用途不正确")
        if expires:
            try:
                datetime.fromisoformat(expires)
            except (TypeError, ValueError) as exc:
                raise ValidationError("授权到期时间不正确") from exc
        try:
            with self.database.session() as connection:
                target = connection.execute(
                    "SELECT 1 FROM members WHERE id=? AND household_id=?", (grantee, member["household_id"])
                ).fetchone()
                if not target or grantee == member["id"]:
                    raise ValidationError("只能授权给同一家庭中的其他成员")
                now = datetime.now(timezone.utc).isoformat()
                connection.execute(
                    "INSERT INTO consent_grants(id,household_id,grantor_member_id,grantee_member_id,data_scope,purpose,expires_at,created_at) "
                    "VALUES (?,?,?,?,?,?,?,?)",
                    (str(uuid4()), member["household_id"], member["id"], grantee, scope, purpose.strip(), expires, now),
                )
        except sqlite3.IntegrityError as exc:
            raise ValidationError("这项授权已经存在，请先撤销后再重新设置") from exc
        return self.get(member)["consents"]

    def revoke(self, member, consent_id):
        with self.database.session() as connection:
            cursor = connection.execute(
                "UPDATE consent_grants SET revoked_at=? WHERE id=? AND household_id=? AND grantor_member_id=? AND revoked_at IS NULL",
                (datetime.now(timezone.utc).isoformat(), consent_id, member["household_id"], member["id"]),
            )
            if cursor.rowcount != 1:
                raise ValidationError("没有找到可撤销的授权")
        return {"revoked": True}
