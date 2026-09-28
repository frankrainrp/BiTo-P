from __future__ import annotations

from datetime import date, datetime, time, timezone
import json
import re
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo


class ButlerStateSource:
    """Read Butler's shared DDL list without importing chats, notes or sessions."""

    def __init__(self, state_url: str = '', transport=None):
        self.state_url = state_url.strip()
        self.transport = transport or self._fetch

    def tasks(self, member: dict) -> dict:
        if not self.state_url:
            return {'configured': False, 'available': False, 'items': []}
        if not member.get('is_admin'):
            return {'configured': True, 'available': True, 'items': [], 'admin_only': True}
        try:
            payload = self.transport()
            rows = payload.get('ddls') if isinstance(payload, dict) else None
            if not isinstance(rows, list):
                raise ValueError('Butler ddls is not a list')
            items = [mapped for row in rows[:5000] if (mapped := self._map(row, member))]
            return {'configured': True, 'available': True, 'items': items, 'admin_only': True}
        except (HTTPError, URLError, TimeoutError, OSError, ValueError, TypeError, json.JSONDecodeError):
            return {'configured': True, 'available': False, 'items': [], 'admin_only': True}

    def _fetch(self):
        target = urlparse(self.state_url)
        if target.scheme != 'http' or target.hostname not in {'butler-web', '127.0.0.1', 'localhost'} or target.username or target.password:
            raise ValueError('Butler state URL must stay on the internal host network')
        with urlopen(Request(self.state_url, headers={'Accept': 'application/json'}), timeout=4) as response:
            raw = response.read(4_000_001)
        if len(raw) > 4_000_000:
            raise ValueError('Butler state response is too large')
        return json.loads(raw.decode('utf-8'))

    @staticmethod
    def _map(row, member):
        if not isinstance(row, dict):
            return None
        item_id, title = row.get('id'), row.get('taskName')
        if not isinstance(item_id, str) or not item_id.strip() or len(item_id) > 160:
            return None
        if not isinstance(title, str) or not title.strip() or len(title.strip()) > 160:
            return None
        due_at = None
        due_date, due_time = row.get('dueDate'), row.get('dueTime') or '23:59'
        if isinstance(due_date, str) and due_date:
            try:
                day = date.fromisoformat(due_date)
                if not isinstance(due_time, str) or not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d', due_time):
                    due_time = '23:59'
                local = datetime.combine(day, time.fromisoformat(due_time), ZoneInfo(member['household_timezone']))
                due_at = local.astimezone(timezone.utc).isoformat()
            except (ValueError, TypeError):
                due_at = None
        status = row.get('status')
        completed = bool(row.get('completed')) or status == 'done'
        return {
            'id': f'butler:{item_id.strip()}', 'title': title.strip(), 'due_at': due_at,
            'status': 'completed' if completed else 'open', 'visibility': 'private',
            'source': 'butler', 'read_only': True,
        }
