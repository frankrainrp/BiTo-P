"""Explicit, local notification jobs. No model-selected URL or shell execution."""
import hashlib
import json
import os
from urllib.parse import urlsplit
from urllib.request import Request, build_opener, ProxyHandler, HTTPRedirectHandler

from .households import ValidationError


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class TaskWebhook:
    def __init__(self, url=None):
        self.url = (os.environ.get('FAMILYOS_TASK_NOTIFY_URL', '') if url is None else url).strip()

    @property
    def ready(self):
        parsed = urlsplit(self.url)
        # A deployment-controlled internal service, not an arbitrary outbound
        # request capability. Localhost also supports isolated integration tests.
        return (parsed.scheme == 'http' and parsed.hostname in {'task-notify', '127.0.0.1'}
                and not parsed.username and not parsed.password and not parsed.query and not parsed.fragment
                and parsed.path in ('', '/'))

    def send(self, member, job):
        if not self.ready:
            raise ValidationError('本地 Webhook 通知服务尚未配置')
        topic = 'bito-' + hashlib.sha256((member['household_id'] + ':' + member['id']).encode()).hexdigest()[:32]
        data = json.dumps({'topic': topic, 'title': job['title'], 'message': job['instructions']}, ensure_ascii=False).encode()
        request = Request(self.url, data=data, headers={
            'Content-Type': 'application/json', 'X-Bito-Delivery-ID': job['id'],
        }, method='POST')
        with build_opener(ProxyHandler({}), NoRedirect()).open(request, timeout=8) as response:
            if not 200 <= response.status < 300:
                raise ValidationError('Webhook 未确认接收')
            result = json.loads(response.read(16385))
            if result.get('event') != 'message' or result.get('topic') != topic or not result.get('id'):
                raise ValidationError('Webhook 回执无效')
        return {'text': '本地 Webhook 已接收 · ' + str(result['id'])[:80], 'conversation_id': None}
