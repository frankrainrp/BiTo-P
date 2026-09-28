"""Authenticated streaming bridge to the pinned LAN Doorman worker."""
import json
from urllib.request import Request, urlopen
from urllib.error import URLError

from .companion import CompanionUnavailableError
from .households import ValidationError


class VoiceOutput:
    def __init__(self, url='', token=''):
        self.url = url.rstrip('/')
        self.token = token

    @property
    def configured(self):
        return bool(self.url and self.token)

    def stream(self, payload):
        if set(payload) - {'text'}:
            raise ValidationError('语音参数不受支持')
        text = payload.get('text')
        if not isinstance(text, str) or not text.strip() or len(text) > 1200:
            raise ValidationError('朗读内容须为 1–1200 个字符')
        if not self.configured:
            raise CompanionUnavailableError('Doorman 语音尚未配置')
        request = Request(self.url + '/speech/stream', data=json.dumps({'input': text}).encode(),
                          headers={'Content-Type': 'application/json', 'Authorization': 'Bearer ' + self.token})
        try:
            with urlopen(request, timeout=15) as response:
                size = 0
                while True:
                    line = response.readline(512 * 1024 + 1)
                    if not line:
                        break
                    size += len(line)
                    if len(line) > 512 * 1024 or size > 16 * 1024 * 1024:
                        raise CompanionUnavailableError('语音输出超过限制')
                    event = json.loads(line)
                    if event.get('error'):
                        raise CompanionUnavailableError('Doorman 合成失败，请缩短句子重试')
                    yield event
        except (URLError, TimeoutError, OSError, ValueError) as exc:
            raise CompanionUnavailableError('Doorman 暂不可用，请检查本机语音服务或切换浏览器声音') from exc
