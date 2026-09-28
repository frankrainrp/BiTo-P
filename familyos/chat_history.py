from __future__ import annotations

from datetime import datetime, timezone
import re
from uuid import UUID, uuid4

from .households import ValidationError


class ChatHistoryService:
    """Member-owned local conversation archive.

    The archive is deliberately separate from model context: callers must name one
    conversation before its latest messages can be supplied to a model.
    """

    RETENTION_DAYS = {0, 7, 30, 90, 365}

    def __init__(self, database):
        self.database = database

    @staticmethod
    def _conversation_id(value):
        if not isinstance(value, str):
            raise ValidationError('对话编号格式不正确')
        try:
            parsed = UUID(value)
        except (ValueError, AttributeError) as exc:
            raise ValidationError('对话编号格式不正确') from exc
        if str(parsed) != value.lower():
            raise ValidationError('对话编号格式不正确')
        return str(parsed)

    @staticmethod
    def _title(value, *, default='新对话'):
        if value is None:
            return default
        if not isinstance(value, str):
            raise ValidationError('对话标题格式不正确')
        value = re.sub(r'[\x00-\x1f\x7f]+', ' ', value).strip()
        if not value or len(value) > 120:
            raise ValidationError('对话标题必须为 1–120 个字符')
        return value

    @classmethod
    def _retention(cls, value):
        if type(value) is not int or value not in cls.RETENTION_DAYS:
            raise ValidationError('保留期只能是 7、30、90、365 天或永久')
        return value

    @staticmethod
    def _automatic_title(message):
        title = re.sub(r'\s+', ' ', message).strip()
        return title[:80] or '新对话'

    def _cleanup(self, connection):
        connection.execute(
            "DELETE FROM companion_conversations "
            "WHERE retention_days > 0 AND datetime(updated_at) < datetime('now', '-' || retention_days || ' days')"
        )

    def create(self, member, payload=None):
        payload = payload or {}
        if not isinstance(payload, dict) or set(payload) - {'title', 'retention_days'}:
            raise ValidationError('包含不支持的对话设置')
        title = self._title(payload.get('title'))
        retention = self._retention(payload.get('retention_days', 365))
        now = datetime.now(timezone.utc).isoformat()
        conversation_id = str(uuid4())
        with self.database.session() as connection:
            self._cleanup(connection)
            connection.execute(
                'INSERT INTO companion_conversations(id,member_id,title,retention_days,created_at,updated_at) VALUES (?,?,?,?,?,?)',
                (conversation_id, member['id'], title, retention, now, now),
            )
        return self.get(member, conversation_id)

    def list(self, member):
        with self.database.session() as connection:
            self._cleanup(connection)
            rows = connection.execute(
                "SELECT c.id,c.title,c.retention_days,c.created_at,c.updated_at,COUNT(m.id) AS message_count,"
                "COALESCE((SELECT content FROM companion_messages latest WHERE latest.conversation_id=c.id "
                "ORDER BY latest.created_at DESC,latest.rowid DESC LIMIT 1),'') AS preview "
                "FROM companion_conversations c LEFT JOIN companion_messages m ON m.conversation_id=c.id "
                "WHERE c.member_id=? GROUP BY c.id ORDER BY c.updated_at DESC LIMIT 100",
                (member['id'],),
            ).fetchall()
        return [dict(row) for row in rows]

    def get(self, member, conversation_id):
        conversation_id = self._conversation_id(conversation_id)
        with self.database.session() as connection:
            self._cleanup(connection)
            row = connection.execute(
                'SELECT id,title,retention_days,created_at,updated_at FROM companion_conversations WHERE id=? AND member_id=?',
                (conversation_id, member['id']),
            ).fetchone()
            if not row:
                raise ValidationError('对话不存在')
            messages = connection.execute(
                'SELECT id,role,content,provider,model,created_at FROM companion_messages '
                'WHERE conversation_id=? ORDER BY created_at,rowid LIMIT 500',
                (conversation_id,),
            ).fetchall()
            attachments = connection.execute(
                'SELECT id,message_id,original_name,content_type,size_bytes,created_at FROM companion_attachments '
                'WHERE conversation_id=? ORDER BY created_at,id',
                (conversation_id,),
            ).fetchall()
        result = dict(row)
        result['messages'] = [dict(message) for message in messages]
        result['attachments'] = [dict(attachment) for attachment in attachments]
        return result

    def context(self, member, conversation_id):
        if conversation_id is None:
            return []
        conversation_id = self._conversation_id(conversation_id)
        with self.database.session() as connection:
            self._cleanup(connection)
            owner = connection.execute(
                'SELECT 1 FROM companion_conversations WHERE id=? AND member_id=?',
                (conversation_id, member['id']),
            ).fetchone()
            if not owner:
                raise ValidationError('对话不存在')
            rows = connection.execute(
                'SELECT role,content FROM companion_messages WHERE conversation_id=? '
                'ORDER BY created_at DESC,rowid DESC LIMIT 8',
                (conversation_id,),
            ).fetchall()
        return [dict(row) for row in reversed(rows)]

    def append_exchange(self, member, conversation_id, user_text, assistant_text, *, provider='', model=''):
        if conversation_id is None:
            conversation_id = self.create(member)['id']
        conversation_id = self._conversation_id(conversation_id)
        now = datetime.now(timezone.utc).isoformat()
        with self.database.session() as connection:
            self._cleanup(connection)
            row = connection.execute(
                'SELECT title FROM companion_conversations WHERE id=? AND member_id=?',
                (conversation_id, member['id']),
            ).fetchone()
            if not row:
                raise ValidationError('对话不存在')
            count = connection.execute(
                'SELECT COUNT(*) FROM companion_messages WHERE conversation_id=?', (conversation_id,)
            ).fetchone()[0]
            connection.executemany(
                'INSERT INTO companion_messages(id,conversation_id,role,content,provider,model,created_at) VALUES (?,?,?,?,?,?,?)',
                [
                    (str(uuid4()), conversation_id, 'user', user_text, '', '', now),
                    (str(uuid4()), conversation_id, 'assistant', assistant_text, provider, model, now),
                ],
            )
            title = self._automatic_title(user_text) if count == 0 and row['title'] == '新对话' else row['title']
            connection.execute(
                'UPDATE companion_conversations SET title=?,updated_at=? WHERE id=? AND member_id=?',
                (title, now, conversation_id, member['id']),
            )
        return conversation_id

    def update(self, member, conversation_id, payload):
        if not isinstance(payload, dict) or not payload or set(payload) - {'title', 'retention_days'}:
            raise ValidationError('包含不支持的对话设置')
        assignments, values = [], []
        if 'title' in payload:
            assignments.append('title=?')
            values.append(self._title(payload['title']))
        if 'retention_days' in payload:
            assignments.append('retention_days=?')
            values.append(self._retention(payload['retention_days']))
        conversation_id = self._conversation_id(conversation_id)
        assignments.append('updated_at=?')
        values.extend([datetime.now(timezone.utc).isoformat(), conversation_id, member['id']])
        with self.database.session() as connection:
            self._cleanup(connection)
            cursor = connection.execute(
                f"UPDATE companion_conversations SET {','.join(assignments)} WHERE id=? AND member_id=?", values
            )
            if cursor.rowcount != 1:
                raise ValidationError('对话不存在')
        return self.get(member, conversation_id)

    def delete(self, member, conversation_id):
        conversation_id = self._conversation_id(conversation_id)
        with self.database.session() as connection:
            cursor = connection.execute(
                'DELETE FROM companion_conversations WHERE id=? AND member_id=?',
                (conversation_id, member['id']),
            )
            if cursor.rowcount != 1:
                raise ValidationError('对话不存在')
        return {'deleted': True, 'id': conversation_id}

    def export(self, member, conversation_id):
        conversation = self.get(member, conversation_id)
        return {
            'format': 'familyos-chat-history-v1',
            'exported_at': datetime.now(timezone.utc).isoformat(),
            'conversation': conversation,
        }
