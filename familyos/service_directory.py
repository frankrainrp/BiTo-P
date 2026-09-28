from urllib.parse import urlsplit
from uuid import uuid4

from .households import ValidationError


class ServiceDirectory:
    """Household bookmarks, not a service installer or a server-side URL fetcher."""

    def __init__(self, database):
        self.database = database

    def list(self, member):
        with self.database.session() as connection:
            return [dict(row) for row in connection.execute(
                "SELECT id,name,url,category,description FROM service_directory "
                "WHERE household_id=? ORDER BY created_at,id", (member['household_id'],))]

    def create(self, member, payload):
        self._admin(member)
        values = {}
        for key, limit in [('name', 80), ('url', 500), ('category', 32), ('description', 160)]:
            value = payload.get(key, '')
            if not isinstance(value, str) or len(value.strip()) > limit or any(ord(c) < 32 for c in value):
                raise ValidationError('入口信息格式或长度不正确')
            values[key] = value.strip()
        if not all(values[key] for key in ('name', 'url', 'category')):
            raise ValidationError('请填写名称、地址和类别')
        try:
            parsed = urlsplit(values['url'])
            valid = parsed.scheme in {'http', 'https'} and parsed.hostname and not parsed.username and not parsed.password
            parsed.port
        except ValueError:
            valid = False
        if not valid or '\\' in values['url'] or any(c.isspace() for c in values['url']):
            raise ValidationError('请填写不含账号密码的完整 http 或 https 地址')
        values['category'] = {'生活':'life', '工作':'work', '监控':'monitoring', '系统':'system'}.get(values['category'], values['category'])
        if values['category'] in {'all', '全部'}:
            raise ValidationError('请使用具体类别名称')
        values['id'] = str(uuid4())
        with self.database.session() as connection:
            connection.execute('BEGIN IMMEDIATE')
            count = connection.execute('SELECT count(*) FROM service_directory WHERE household_id=?', (member['household_id'],)).fetchone()[0]
            if count >= 50:
                raise ValidationError('最多保存 50 个自定义入口')
            connection.execute('INSERT INTO service_directory(id,household_id,name,url,category,description) VALUES (?,?,?,?,?,?)',
                (values['id'], member['household_id'], values['name'], values['url'], values['category'], values['description']))
        return values

    def delete(self, member, entry_id):
        self._admin(member)
        with self.database.session() as connection:
            deleted = connection.execute('DELETE FROM service_directory WHERE id=? AND household_id=?',
                (entry_id, member['household_id'])).rowcount
        return {'deleted': bool(deleted)}

    @staticmethod
    def _admin(member):
        if not member['is_admin']:
            raise PermissionError('只有家庭管理员可以管理共享入口')
