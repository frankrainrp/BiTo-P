from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import re
import secrets
import time
import unicodedata
from uuid import uuid4

from .db import Database

USERNAME_RE = re.compile(r"^[\w.-]{2,32}$", re.UNICODE)
SESSION_DAYS = 30


class AuthenticationError(ValueError):
    pass


class SessionValidationError(ValueError):
    pass


def normalize_username(value: object) -> tuple[str, str]:
    if not isinstance(value, str):
        raise AuthenticationError("请输入用户名")
    username = unicodedata.normalize("NFKC", value).strip()
    if not USERNAME_RE.fullmatch(username):
        raise AuthenticationError("用户名需为 2–32 位文字、数字、点、横线或下划线")
    return username, username.casefold()


def public_member(row) -> dict:
    return {
        "id": row["id"],
        "username": row["username"],
        "display_name": row["display_name"],
        "role": row["role"],
        "is_admin": bool(row["is_admin"]),
        "household_id": row["household_id"],
    }


class SessionService:
    def __init__(self, database: Database):
        self.database = database

    @staticmethod
    def _hash(token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()

    @staticmethod
    def _device_label(value: object) -> str:
        if not isinstance(value, str):
            return "Unknown device"
        label = " ".join(value.strip().split())
        return label[:80] or "Unknown device"

    @staticmethod
    def _timestamp(value: int | None) -> str | None:
        if value is None:
            return None
        return datetime.fromtimestamp(value, timezone.utc).isoformat()

    def create(self, username_value: object, now_epoch: int | None = None,
               device_label: object = "Unknown device", browser_id: str | None = None,
               previous_token: str | None = None) -> tuple[str, dict]:
        _, username_key = normalize_username(username_value)
        current = int(time.time()) if now_epoch is None else now_epoch
        with self.database.session() as connection:
            connection.execute('BEGIN IMMEDIATE')
            connection.execute("DELETE FROM member_sessions WHERE browser_hash IS NULL AND expires_at <= ?", (current - 90 * 86400,))
            member = connection.execute(
                "SELECT m.id, m.household_id, m.username, m.display_name, m.role, m.is_admin, "
                "h.name AS household_name, h.timezone AS household_timezone "
                "FROM members m JOIN households h ON h.id=m.household_id WHERE m.username_key = ?",
                (username_key,),
            ).fetchone()
            if not member:
                raise AuthenticationError("用户名不存在，请向家庭管理员确认")
            token = secrets.token_urlsafe(32)
            browser_hash = self._hash(browser_id) if browser_id and re.fullmatch(r'[A-Za-z0-9_-]{43}', browser_id) else None
            existing = connection.execute(
                'SELECT session_id FROM member_sessions WHERE member_id=? AND browser_hash=?',
                (member['id'], browser_hash),
            ).fetchone() if browser_hash else None
            # Adopt only the authenticated current legacy session, never infer
            # identity from a user agent, IP address or display name.
            if not existing and browser_hash and previous_token and len(previous_token) <= 256:
                existing = connection.execute(
                    'SELECT session_id FROM member_sessions WHERE member_id=? AND token_hash=? '
                    'AND browser_hash IS NULL AND revoked_at IS NULL AND expires_at>?',
                    (member['id'], self._hash(previous_token), current),
                ).fetchone()
            session_id = existing['session_id'] if existing else str(uuid4())
            if existing:
                connection.execute(
                    'UPDATE member_sessions SET token_hash=?,expires_at=?,last_seen_at=?,device_label=?, '
                    'browser_hash=?,trusted=0,revoked_at=NULL WHERE session_id=?',
                    (self._hash(token), current + SESSION_DAYS * 86400, current,
                     self._device_label(device_label), browser_hash, session_id),
                )
            else:
                connection.execute(
                "INSERT INTO member_sessions(token_hash,member_id,created_at,expires_at,last_seen_at,session_id,device_label) "
                "VALUES (?,?,?,?,?,?,?)",
                (self._hash(token), member["id"], current, current + SESSION_DAYS * 86400, current,
                 session_id, self._device_label(device_label)),
                )
                connection.execute('UPDATE member_sessions SET browser_hash=? WHERE session_id=?', (browser_hash, session_id))
            connection.execute(
                "INSERT INTO member_session_audit(id,household_id,member_id,actor_member_id,session_id,event_type,created_at) "
                "VALUES (?,?,?,?,?,'created',?)",
                (str(uuid4()), member["household_id"], member["id"], member["id"], session_id, current),
            )
        result = public_member(member)
        result["household_name"] = member["household_name"]
        result["household_timezone"] = member["household_timezone"]
        return token, result

    def resolve(self, token: str | None, now_epoch: int | None = None) -> dict | None:
        if not token or len(token) > 256:
            return None
        current = int(time.time()) if now_epoch is None else now_epoch
        with self.database.session() as connection:
            row = connection.execute(
                "SELECT m.id, m.household_id, m.username, m.display_name, m.role, m.is_admin, "
                "h.name AS household_name, h.timezone AS household_timezone, s.last_seen_at "
                "FROM member_sessions s JOIN members m ON m.id=s.member_id "
                "JOIN households h ON h.id=m.household_id "
                "WHERE s.token_hash=? AND s.expires_at>? AND s.revoked_at IS NULL",
                (self._hash(token), current),
            ).fetchone()
            if not row:
                return None
            if current - row["last_seen_at"] >= 3600:
                connection.execute(
                    "UPDATE member_sessions SET last_seen_at=? WHERE token_hash=?",
                    (current, self._hash(token)),
                )
        member = public_member(row)
        member["household_name"] = row["household_name"]
        member["household_timezone"] = row["household_timezone"]
        return member

    def destroy(self, token: str | None) -> None:
        if not token or len(token) > 256:
            return
        current = int(time.time())
        with self.database.session() as connection:
            row = connection.execute(
                "SELECT s.session_id,s.member_id,m.household_id FROM member_sessions s "
                "JOIN members m ON m.id=s.member_id WHERE s.token_hash=? AND s.revoked_at IS NULL",
                (self._hash(token),),
            ).fetchone()
            if not row:
                return
            connection.execute(
                "UPDATE member_sessions SET revoked_at=? WHERE token_hash=? AND revoked_at IS NULL",
                (current, self._hash(token)),
            )
            self._audit(connection, row["household_id"], row["member_id"], row["member_id"],
                        row["session_id"], "signed_out", current)

    def list(self, member: dict, current_token: str | None, now_epoch: int | None = None) -> dict:
        current = int(time.time()) if now_epoch is None else now_epoch
        token_hash = self._hash(current_token) if current_token and len(current_token) <= 256 else ""
        with self.database.session() as connection:
            rows = connection.execute(
                "SELECT session_id,token_hash,device_label,trusted,created_at,last_seen_at,expires_at,revoked_at,browser_hash "
                "FROM member_sessions WHERE member_id=? ORDER BY last_seen_at DESC LIMIT 30",
                (member["id"],),
            ).fetchall()
            audit = connection.execute(
                "SELECT session_id,event_type,created_at FROM member_session_audit "
                "WHERE member_id=? AND household_id=? ORDER BY created_at DESC LIMIT 30",
                (member["id"], member["household_id"]),
            ).fetchall()
        return {
            "sessions": [self._session_view(dict(row), token_hash, current) for row in rows
                         if row['browser_hash'] or row['token_hash'] == token_hash],
            "legacy_sessions": [self._session_view(dict(row), token_hash, current) for row in rows
                                if not row['browser_hash'] and row['token_hash'] != token_hash],
            "audit": [{
                "session_id": row["session_id"],
                "event": row["event_type"],
                "created_at": self._timestamp(row["created_at"]),
            } for row in audit],
        }

    def set_trusted(self, member: dict, session_id: str, trusted: object,
                    current_token: str | None = None, now_epoch: int | None = None) -> dict:
        if not isinstance(trusted, bool):
            raise SessionValidationError("可信状态格式不正确")
        current = int(time.time()) if now_epoch is None else now_epoch
        with self.database.session() as connection:
            row = connection.execute(
                "SELECT session_id,token_hash,device_label,trusted,created_at,last_seen_at,expires_at,revoked_at "
                "FROM member_sessions WHERE session_id=? AND member_id=?",
                (session_id, member["id"]),
            ).fetchone()
            if not row:
                raise AuthenticationError("登录设备不存在")
            if row["revoked_at"] is not None or row["expires_at"] <= current:
                raise SessionValidationError("已结束的登录设备不能更改可信状态")
            if bool(row["trusted"]) != trusted:
                connection.execute("UPDATE member_sessions SET trusted=? WHERE session_id=?", (int(trusted), session_id))
                self._audit(connection, member["household_id"], member["id"], member["id"], session_id,
                            "trusted" if trusted else "untrusted", current)
            updated = dict(row)
            updated["trusted"] = int(trusted)
        token_hash = self._hash(current_token) if current_token and len(current_token) <= 256 else ""
        return self._session_view(updated, token_hash, current)

    def revoke(self, member: dict, session_id: str, current_token: str | None, confirmed: object,
               now_epoch: int | None = None) -> dict:
        if confirmed is not True:
            raise SessionValidationError("请再次确认要让这台设备下线")
        current = int(time.time()) if now_epoch is None else now_epoch
        token_hash = self._hash(current_token) if current_token and len(current_token) <= 256 else ""
        with self.database.session() as connection:
            row = connection.execute(
                "SELECT session_id,token_hash,device_label,trusted,created_at,last_seen_at,expires_at,revoked_at "
                "FROM member_sessions WHERE session_id=? AND member_id=?",
                (session_id, member["id"]),
            ).fetchone()
            if not row:
                raise AuthenticationError("登录设备不存在")
            if row["revoked_at"] is None:
                connection.execute("UPDATE member_sessions SET revoked_at=? WHERE session_id=?", (current, session_id))
                self._audit(connection, member["household_id"], member["id"], member["id"], session_id,
                            "revoked", current)
            updated = dict(row)
            updated["revoked_at"] = updated["revoked_at"] or current
        return {
            "session": self._session_view(updated, token_hash, current),
            "current_session_revoked": row["token_hash"] == token_hash,
        }

    def _session_view(self, row: dict, current_token_hash: str, now_epoch: int) -> dict:
        status = "revoked" if row["revoked_at"] is not None else ("expired" if row["expires_at"] <= now_epoch else "active")
        return {
            "id": row["session_id"],
            "device_label": row["device_label"],
            "trusted": bool(row["trusted"]),
            "current": bool(current_token_hash and row["token_hash"] == current_token_hash),
            "status": status,
            "created_at": self._timestamp(row["created_at"]),
            "last_seen_at": self._timestamp(row["last_seen_at"]),
            "expires_at": self._timestamp(row["expires_at"]),
            "revoked_at": self._timestamp(row["revoked_at"]),
        }

    @staticmethod
    def _audit(connection, household_id: str, member_id: str, actor_member_id: str,
               session_id: str, event_type: str, created_at: int) -> None:
        connection.execute(
            "INSERT INTO member_session_audit(id,household_id,member_id,actor_member_id,session_id,event_type,created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (str(uuid4()), household_id, member_id, actor_member_id, session_id, event_type, created_at),
        )
