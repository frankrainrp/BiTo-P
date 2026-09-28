from __future__ import annotations

from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import mimetypes
from pathlib import Path
import re
import secrets
from threading import Lock
import time
from urllib.parse import parse_qs, urlparse

from .activity import ActivityService
from .analytics import AnalyticsService
from .auth import AuthenticationError, SessionService, SessionValidationError
from .db import Database
from .households import HouseholdService, ValidationError
from .life import LifeService
from .system_status import SystemStatusService
from .service_directory import ServiceDirectory
from .companion import CompanionService, CompanionUnavailableError, CompanionWorker
from .monitoring import MonitoringService, MonitoringWorker
from .member_context import MemberContextService
from .butler import ButlerStateSource
from .voice import VoiceService
from .voice_output import VoiceOutput
from .agent_jobs import AgentJobService, AgentJobWorker
from .agent_schedule import AgentScheduleService, AgentScheduleWorker

STATIC_DIR = Path(__file__).parent / "static"
SESSION_COOKIE = "familyos_session"
ITEM_PATH = re.compile(r"^/api/(tasks|habits)/([0-9a-f-]{36})/(complete|checkins)$")


class FixedWindowLimiter:
    def __init__(self, limit: int = 20, window_seconds: int = 60):
        self.limit = limit
        self.window_seconds = window_seconds
        self.buckets: dict[str, tuple[float, int]] = {}
        self.lock = Lock()

    def allow(self, key: str) -> bool:
        current = time.monotonic()
        with self.lock:
            started, count = self.buckets.get(key, (current, 0))
            if current - started >= self.window_seconds:
                started, count = current, 0
            if count >= self.limit:
                self.buckets[key] = (started, count)
                return False
            self.buckets[key] = (started, count + 1)
            return True


class RateLimitError(PermissionError):
    pass


class FamilyOSServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, database: Database, *, secure_cookies: bool = False, system_status_path: str | None = None,
                 ollama_url: str = '', ollama_model: str = '', cloud_api_url: str = '',
                 cloud_api_key: str = '', cloud_model: str = '', butler_state_url: str = '', local_model_hosts: str = '',
                 voice_asr_url: str = '', funasr_url: str = '', voice_tts_url: str = '', voice_tts_token: str = ''):
        self.database = database
        self.households = HouseholdService(database)
        self.sessions = SessionService(database)
        self.butler = ButlerStateSource(butler_state_url)
        self.life = LifeService(database, butler=self.butler)
        self.analytics = AnalyticsService(database)
        self.activity = ActivityService(database)
        self.system_status = SystemStatusService(system_status_path)
        self.service_directory = ServiceDirectory(database)
        self.monitoring = MonitoringService(database)
        self.member_context = MemberContextService(database)
        self.companion = CompanionService(
            database, self.monitoring, ollama_url=ollama_url, ollama_model=ollama_model,
            cloud_api_url=cloud_api_url, cloud_api_key=cloud_api_key, cloud_model=cloud_model,
            local_model_hosts=local_model_hosts, life=self.life,
        )
        self.voice_output = VoiceOutput(voice_tts_url, voice_tts_token)
        self.speech_limiter = FixedWindowLimiter(limit=120, window_seconds=60)
        self.voice = VoiceService(asr_url=voice_asr_url, funasr_url=funasr_url)
        self.agent_jobs = AgentJobService(database, self.companion)
        self.agent_schedules = AgentScheduleService(database)
        self.background_workers = [MonitoringWorker(self.monitoring), CompanionWorker(self.companion), AgentJobWorker(self.agent_jobs), AgentScheduleWorker(self.agent_schedules)]
        self.secure_cookies = secure_cookies
        self.login_limiter = FixedWindowLimiter()
        self.activity_limiter = FixedWindowLimiter(limit=120, window_seconds=60)
        self.monitor_limiter = FixedWindowLimiter(limit=10, window_seconds=60)
        self.chat_limiter = FixedWindowLimiter(limit=12, window_seconds=60)
        self.voice_limiter = FixedWindowLimiter(limit=20, window_seconds=60)
        self.action_limiter = FixedWindowLimiter(limit=20, window_seconds=60)
        self.agent_limiter = FixedWindowLimiter(limit=4, window_seconds=60)
        super().__init__(address, RequestHandler)

    def start_workers(self):
        for worker in self.background_workers:
            worker.start()

    def server_close(self):
        for worker in self.background_workers:
            worker.stop()
        self.voice.close()
        super().server_close()


class RequestHandler(BaseHTTPRequestHandler):
    server: FamilyOSServer

    def log_message(self, format, *args):
        print(f"{self.address_string()} - {format % args}")

    def do_GET(self):
        parsed_url = urlparse(self.path)
        path = parsed_url.path
        try:
            if path == "/api/health":
                return self._json(HTTPStatus.OK, self.server.database.health())
            if path == "/api/voice/output":
                self._member()
                return self._json(HTTPStatus.OK, {"doorman": self.server.voice_output.configured})
            if path == "/api/setup":
                return self._json(HTTPStatus.OK, self.server.households.get_setup())
            if path == "/api/session":
                member = self._member()
                return self._json(HTTPStatus.OK, {"authenticated": True, "member": member})
            if path == "/api/sessions":
                member = self._member()
                return self._json(HTTPStatus.OK, self.server.sessions.list(member, self._session_token()))
            if path == '/api/member/avatar':
                avatar = self.server.member_context.avatar(self._member())
                if not avatar:
                    return self._json(HTTPStatus.NOT_FOUND, {'error': '尚未上传头像'})
                return self._bytes(HTTPStatus.OK, avatar[1], avatar[0], cache_control='private, no-store')
            if path == "/api/today":
                member = self._member()
                return self._json(HTTPStatus.OK, self.server.life.today(member))
            if path == '/api/calendar':
                member = self._member()
                month = parse_qs(parsed_url.query).get('month', [None])[0]
                return self._json(HTTPStatus.OK, self.server.life.calendar(member, month))
            if path == '/api/preferences':
                return self._json(HTTPStatus.OK, self.server.companion.preferences(self._member()))
            if path == '/api/companion/providers':
                return self._json(HTTPStatus.OK, self.server.companion.provider_status(self._member()))
            if path == '/api/companion/prompt':
                return self._json(HTTPStatus.OK, self.server.companion.prompt_overview(self._member()))
            if path == '/api/companion/actions/pending':
                return self._json(HTTPStatus.OK, {'proposal': self.server.companion.actions.latest_pending(self._member())})
            if path == '/api/companion/functions':
                return self._json(HTTPStatus.OK, self.server.companion.actions.registry(self._member()))
            if path == '/api/companion/conversations':
                return self._json(HTTPStatus.OK, {'conversations': self.server.companion.history.list(self._member())})
            conversation_export = re.fullmatch(r'/api/companion/conversations/([0-9a-f-]{36})/export', path)
            if conversation_export:
                return self._json(HTTPStatus.OK, self.server.companion.history.export(self._member(), conversation_export[1]))
            conversation = re.fullmatch(r'/api/companion/conversations/([0-9a-f-]{36})', path)
            if conversation:
                return self._json(HTTPStatus.OK, self.server.companion.history.get(self._member(), conversation[1]))
            if path == '/api/member/context':
                return self._json(HTTPStatus.OK, self.server.member_context.get(self._member()))
            if path == '/api/briefing':
                return self._json(HTTPStatus.OK, self.server.companion.briefing(self._member()))
            if path == '/api/briefing/scheduled':
                return self._json(HTTPStatus.OK, self.server.companion.scheduled_briefing(self._member()))
            if path == '/api/monitors':
                member = self._member()
                return self._json(HTTPStatus.OK, {'subscriptions': self.server.monitoring.list_subscriptions(member),
                    'channels': self.server.monitoring.channel_status(), 'alerts': self.server.monitoring.list_alerts(member)})
            monitor_runs = re.fullmatch(r'/api/monitors/([0-9a-f-]{36})/runs', path)
            if monitor_runs:
                return self._json(HTTPStatus.OK, {'runs': self.server.monitoring.list_runs(self._member(), monitor_runs[1])})
            if path == "/api/insights":
                member = self._member()
                return self._json(HTTPStatus.OK, self.server.analytics.member_snapshot(member))
            if path == "/api/server/status":
                self._member()
                return self._json(HTTPStatus.OK, self.server.system_status.snapshot())
            if path == '/api/services':
                return self._json(HTTPStatus.OK, {'entries': self.server.service_directory.list(self._member())})
            if path == '/api/agent-jobs':
                return self._json(HTTPStatus.OK, self.server.agent_jobs.list(self._member()))
            if path == '/api/agent-task-definitions':
                return self._json(HTTPStatus.OK, self.server.agent_schedules.list(self._member()))
            agent_job = re.fullmatch(r'/api/agent-jobs/([0-9a-f-]{36})', path)
            if agent_job:
                return self._json(HTTPStatus.OK, self.server.agent_jobs.get(self._member(), agent_job[1]))
            if path == "/api/activity/timeline":
                member = self._member()
                day = parse_qs(parsed_url.query).get("date", [None])[0]
                return self._json(HTTPStatus.OK, self.server.activity.timeline(member, day))
            if path == "/api/activity/daily-summary":
                member = self._member()
                day = parse_qs(parsed_url.query).get("date", [None])[0]
                return self._json(HTTPStatus.OK, self.server.activity.daily_summary(member, day))
            if path == "/api/activity/history":
                member = self._member()
                history_range = parse_qs(parsed_url.query).get("range", ['30d'])[0]
                return self._json(HTTPStatus.OK, self.server.activity.history(member, history_range))
            if path == "/api/activity/reports":
                member = self._member()
                day = parse_qs(parsed_url.query).get("date", [None])[0]
                return self._json(HTTPStatus.OK, self.server.activity.reports(member, day))
            icon_match = re.fullmatch(r"/api/activity/icons/([0-9a-f-]{36})", path)
            if icon_match:
                return self._bytes(
                    HTTPStatus.OK,
                    self.server.activity.icon(self._member(), icon_match[1]),
                    "image/png",
                    cache_control="private, max-age=86400",
                )
            if path.startswith("/api/"):
                return self._json(HTTPStatus.NOT_FOUND, {"error": "未找到接口"})
            self._static(path)
        except AuthenticationError as exc:
            self._json(HTTPStatus.UNAUTHORIZED, {"error": str(exc)})
        except (ValidationError, ValueError) as exc:
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
        except PermissionError as exc:
            self._json(HTTPStatus.FORBIDDEN, {"error": str(exc)})

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            self._require_same_origin()
            if path == '/api/voice/speak':
                member = self._member()
                if not self.server.speech_limiter.allow(member['id']):
                    raise RateLimitError('朗读请求过快，请稍后重试')
                return self._ndjson(self.server.voice_output.stream(self._read_json(max_size=8192)))
            if path == '/api/voice/transcribe':
                member = self._member()
                self._check_voice_rate(member)
                if self.headers.get_content_type() not in {'audio/webm', 'audio/ogg', 'audio/wav', 'audio/mp4'}:
                    raise ValidationError('录音格式不受支持')
                return self._json(HTTPStatus.OK, self.server.voice.transcribe(
                    self._read_audio(), self.headers.get_content_type()))
            if path == '/api/voice/stream/start':
                member = self._member()
                self._check_voice_rate(member)
                return self._json(HTTPStatus.CREATED, self.server.voice.start_stream(member, self._read_json()))
            voice_stream = re.fullmatch(r'/api/voice/stream/([0-9a-f-]{36})/(chunk|finish|abort)', path)
            if voice_stream:
                member = self._member()
                session_id, action = voice_stream.groups()
                if action == 'chunk':
                    if self.headers.get_content_type() != 'application/octet-stream':
                        raise ValidationError('实时语音分片必须使用 application/octet-stream')
                    return self._json(HTTPStatus.OK, self.server.voice.stream_chunk(
                        member, session_id, self._read_audio(max_size=VoiceService.MAX_CHUNK_BYTES)))
                payload = self._read_json(max_size=128)
                if payload:
                    raise ValidationError('实时语音结束操作不接受额外字段')
                if action == 'finish':
                    return self._json(HTTPStatus.OK, self.server.voice.finish_stream(member, session_id))
                return self._json(HTTPStatus.OK, self.server.voice.abort_stream(member, session_id))
            if path == '/api/member/avatar':
                member = self._member()
                self._check_action_rate(member)
                return self._json(HTTPStatus.OK, self.server.member_context.save_avatar(
                    member, self._read_avatar(), self.headers.get_content_type()))
            payload = self._read_json()
            if path == "/api/setup":
                self._check_login_rate()
                state = self.server.households.initialize(payload)
                username = state["members"][0]["username"]
                browser_id = self._browser_identity()
                token, member = self.server.sessions.create(username, device_label=self._device_label(), browser_id=browser_id)
                return self._json(
                    HTTPStatus.CREATED,
                    {"configured": True, "household": state["household"], "member": member},
                    cookie=[self._session_cookie(token), self._browser_cookie(browser_id)],
                )
            if path == "/api/session":
                self._check_login_rate()
                browser_id = self._browser_identity()
                token, member = self.server.sessions.create(payload.get("username"), device_label=self._device_label(),
                                                            browser_id=browser_id, previous_token=self._session_token())
                return self._json(
                    HTTPStatus.CREATED,
                    {"authenticated": True, "member": member},
                    cookie=[self._session_cookie(token), self._browser_cookie(browser_id)],
                )

            member = self._member()
            session_action = re.fullmatch(r'/api/sessions/([0-9a-f-]{32,36})/(trust|revoke)', path)
            if session_action:
                self._check_action_rate(member)
                if session_action[2] == 'trust':
                    return self._json(HTTPStatus.OK, self.server.sessions.set_trusted(
                        member, session_action[1], payload.get('trusted'), self._session_token()))
                result = self.server.sessions.revoke(
                    member, session_action[1], self._session_token(), payload.get('confirm'))
                return self._json(
                    HTTPStatus.OK,
                    result,
                    cookie=self._session_cookie('', max_age=0) if result['current_session_revoked'] else None,
                )
            if path == '/api/monitors':
                self._check_monitor_rate(member)
                return self._json(HTTPStatus.CREATED, self.server.monitoring.create_subscription(member, payload))
            if path == '/api/services':
                return self._json(HTTPStatus.CREATED, self.server.service_directory.create(member, payload))
            if path == '/api/agent-jobs':
                self._check_agent_rate(member)
                return self._json(HTTPStatus.CREATED, self.server.agent_jobs.create(member, payload))
            if path == '/api/agent-task-definitions':
                self._check_action_rate(member)
                return self._json(HTTPStatus.CREATED, self.server.agent_schedules.save(member, payload))
            if path == '/api/agent-task-definitions/preview':
                from datetime import datetime, timezone
                self._check_action_rate(member)
                spec = self.server.agent_schedules.clean(member, payload)
                return self._json(HTTPStatus.OK, {'next_occurrences': self.server.agent_schedules.next_times(spec, datetime.now(timezone.utc))})
            definition_run = re.fullmatch(r'/api/agent-task-definitions/([0-9a-f-]{36})/run', path)
            if definition_run:
                self._check_agent_rate(member)
                if payload:
                    raise ValidationError('运行操作不接受额外字段')
                spec = next((item for item in self.server.agent_schedules.list(member)['definitions'] if item['id'] == definition_run[1]), None)
                if not spec:
                    raise ValidationError('没有找到这项定时任务')
                return self._json(HTTPStatus.CREATED, self.server.agent_jobs.create(member, {'title': spec['title'], 'instructions': spec['instructions'], 'execution_kind': spec['execution_kind']}))
            agent_job_action = re.fullmatch(r'/api/agent-jobs/([0-9a-f-]{36})/(cancel|takeover)', path)
            if agent_job_action:
                self._check_action_rate(member)
                if payload:
                    raise ValidationError('任务操作不接受额外字段')
                if agent_job_action[2] == 'cancel':
                    return self._json(HTTPStatus.OK, self.server.agent_jobs.cancel(member, agent_job_action[1]))
                return self._json(HTTPStatus.OK, self.server.agent_jobs.takeover(member, agent_job_action[1]))
            if path == '/api/companion/chat/stream':
                self._check_chat_rate(member)
                return self._ndjson(self.server.companion.chat_stream(member, payload))
            if path == '/api/companion/chat':
                self._check_chat_rate(member)
                return self._json(HTTPStatus.OK, self.server.companion.chat(member, payload))
            if path == '/api/companion/conversations':
                self._check_action_rate(member)
                return self._json(HTTPStatus.CREATED, self.server.companion.history.create(member, payload))
            action = re.fullmatch(r'/api/companion/actions/([0-9a-f-]{36})/(confirm|reject)', path)
            if action:
                self._check_action_rate(member)
                if payload:
                    raise ValidationError('确认操作不接受额外字段')
                if action[2] == 'confirm':
                    return self._json(HTTPStatus.OK, self.server.companion.actions.confirm(member, action[1]))
                return self._json(HTTPStatus.OK, self.server.companion.actions.reject(member, action[1]))
            if path == '/api/member/consents':
                self._check_action_rate(member)
                return self._json(HTTPStatus.CREATED, {'consents': self.server.member_context.grant(member, payload)})
            monitor_run = re.fullmatch(r'/api/monitors/([0-9a-f-]{36})/run', path)
            if monitor_run:
                self._check_monitor_rate(member)
                return self._json(HTTPStatus.OK, self.server.monitoring.run_subscription(member, monitor_run[1]))
            alert_ack = re.fullmatch(r'/api/alerts/([0-9a-f-]{36})/ack', path)
            if alert_ack:
                return self._json(HTTPStatus.OK, self.server.monitoring.acknowledge_alert(member, alert_ack[1]))
            if path == "/api/members":
                return self._json(HTTPStatus.CREATED, self.server.households.add_member(member, payload))
            if path == "/api/tasks":
                return self._json(HTTPStatus.CREATED, self.server.life.create_task(member, payload))
            if path == "/api/events":
                return self._json(HTTPStatus.CREATED, self.server.life.create_event(member, payload))
            if path == "/api/habits":
                return self._json(HTTPStatus.CREATED, self.server.life.create_habit(member, payload))
            if path == "/api/activity/records":
                self._check_activity_rate(member)
                return self._json(HTTPStatus.CREATED, self.server.activity.ingest(member, payload))
            if path == "/api/activity/icons":
                self._check_activity_rate(member)
                return self._json(HTTPStatus.CREATED, self.server.activity.ingest_icon(member, payload))
            if path == "/api/activity/reports":
                self._check_activity_rate(member)
                return self._json(HTTPStatus.CREATED, self.server.activity.ingest_report(member, payload))
            match = ITEM_PATH.fullmatch(path)
            if match and match.group(1) == "tasks" and match.group(3) == "complete":
                return self._json(HTTPStatus.OK, self.server.life.complete_task(member, match.group(2)))
            if match and match.group(1) == "habits" and match.group(3) == "checkins":
                return self._json(HTTPStatus.CREATED, self.server.life.check_in_habit(member, match.group(2), payload))
            self._json(HTTPStatus.NOT_FOUND, {"error": "未找到接口"})
        except AuthenticationError as exc:
            self._json(HTTPStatus.UNAUTHORIZED, {"error": str(exc)})
        except RateLimitError as exc:
            self._json(HTTPStatus.TOO_MANY_REQUESTS, {"error": str(exc)})
        except CompanionUnavailableError as exc:
            self._json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": str(exc)})
        except PermissionError as exc:
            self._json(HTTPStatus.FORBIDDEN, {"error": str(exc)})
        except SessionValidationError as exc:
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
        except ValidationError as exc:
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
        except (json.JSONDecodeError, TypeError, UnicodeDecodeError):
            self._json(HTTPStatus.BAD_REQUEST, {"error": "请求不是有效 JSON"})

    def do_PUT(self):
        path = urlparse(self.path).path
        try:
            self._require_same_origin()
            member = self._member()
            payload = self._read_json(max_size=100_000 if path == "/api/activity/icons" else 16_384)
            if path == '/api/preferences':
                return self._json(HTTPStatus.OK, self.server.companion.save_preferences(member, payload))
            definition = re.fullmatch(r'/api/agent-task-definitions/([0-9a-f-]{36})', path)
            if definition:
                self._check_action_rate(member)
                return self._json(HTTPStatus.OK, self.server.agent_schedules.save(member, payload, definition[1]))
            if path == '/api/member/profile':
                return self._json(HTTPStatus.OK, self.server.member_context.save_profile(member, payload))
            conversation = re.fullmatch(r'/api/companion/conversations/([0-9a-f-]{36})', path)
            if conversation:
                self._check_action_rate(member)
                return self._json(HTTPStatus.OK, self.server.companion.history.update(member, conversation[1], payload))
            match = re.fullmatch(r'/api/monitors/([0-9a-f-]{36})', path)
            if match:
                self._check_monitor_rate(member)
                return self._json(HTTPStatus.OK, self.server.monitoring.update_subscription(member, match[1], payload))
            self._json(HTTPStatus.NOT_FOUND, {'error': '未找到接口'})
        except AuthenticationError as exc:
            self._json(HTTPStatus.UNAUTHORIZED, {'error': str(exc)})
        except RateLimitError as exc:
            self._json(HTTPStatus.TOO_MANY_REQUESTS, {'error': str(exc)})
        except PermissionError as exc:
            self._json(HTTPStatus.FORBIDDEN, {'error': str(exc)})
        except (ValidationError, ValueError, TypeError, UnicodeDecodeError) as exc:
            self._json(HTTPStatus.BAD_REQUEST, {'error': str(exc) if isinstance(exc, ValidationError) else '请求格式不正确'})

    do_PATCH = do_PUT

    def do_DELETE(self):
        parsed_url = urlparse(self.path)
        path = parsed_url.path
        try:
            self._require_same_origin()
            if path == "/api/session":
                token = self._session_token()
                self.server.sessions.destroy(token)
                return self._json(
                    HTTPStatus.OK,
                    {"authenticated": False},
                    cookie=self._session_cookie("", max_age=0),
                )
            if path == "/api/activity/records":
                member = self._member()
                day = parse_qs(parsed_url.query).get("date", [None])[0]
                return self._json(HTTPStatus.OK, self.server.activity.delete_day(member, day))
            match = re.fullmatch(r'/api/monitors/([0-9a-f-]{36})', path)
            if match:
                return self._json(HTTPStatus.OK, self.server.monitoring.delete_subscription(self._member(), match[1]))
            service_match = re.fullmatch(r'/api/services/([0-9a-f-]{36})', path)
            if service_match:
                return self._json(HTTPStatus.OK, self.server.service_directory.delete(self._member(), service_match[1]))
            consent_match = re.fullmatch(r'/api/member/consents/([0-9a-f-]{36})', path)
            if consent_match:
                member = self._member()
                self._check_action_rate(member)
                return self._json(HTTPStatus.OK, self.server.member_context.revoke(member, consent_match[1]))
            conversation = re.fullmatch(r'/api/companion/conversations/([0-9a-f-]{36})', path)
            if conversation:
                member = self._member()
                self._check_action_rate(member)
                return self._json(HTTPStatus.OK, self.server.companion.history.delete(member, conversation[1]))
            return self._json(HTTPStatus.NOT_FOUND, {"error": "未找到接口"})
        except AuthenticationError as exc:
            self._json(HTTPStatus.UNAUTHORIZED, {"error": str(exc)})
        except RateLimitError as exc:
            self._json(HTTPStatus.TOO_MANY_REQUESTS, {"error": str(exc)})
        except PermissionError as exc:
            self._json(HTTPStatus.FORBIDDEN, {"error": str(exc)})
        except ValidationError as exc:
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})

    def _read_json(self, *, max_size: int = 16_384) -> dict:
        if self.headers.get_content_type() != "application/json":
            raise ValidationError("请求必须使用 application/json")
        try:
            size = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ValidationError("请求长度不正确") from exc
        if size <= 0 or size > max_size:
            raise ValidationError("请求内容为空或过大")
        payload = json.loads(self.rfile.read(size).decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValidationError("请求内容必须是对象")
        return payload

    def _read_audio(self, *, max_size: int = 16 * 1024 * 1024) -> bytes:
        try:
            size = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ValidationError("录音长度不正确") from exc
        if size <= 0 or size > max_size:
            raise ValidationError("录音为空或超过允许大小")
        return self.rfile.read(size)

    def _read_avatar(self) -> bytes:
        if self.headers.get_content_type() not in {'image/png', 'image/jpeg', 'image/webp'}:
            raise ValidationError('头像仅支持 PNG、JPEG 或 WebP 图片')
        try:
            size = int(self.headers.get('Content-Length', '0'))
        except ValueError as exc:
            raise ValidationError('头像长度不正确') from exc
        if size <= 0 or size > 2 * 1024 * 1024:
            raise ValidationError('头像不能为空且不能超过 2MB')
        return self.rfile.read(size)

    def _member(self) -> dict:
        member = self.server.sessions.resolve(self._session_token())
        if not member:
            raise AuthenticationError("请先使用家庭用户名进入")
        return member

    def _session_token(self) -> str | None:
        cookie = SimpleCookie()
        cookie.load(self.headers.get("Cookie", ""))
        morsel = cookie.get(SESSION_COOKIE)
        return morsel.value if morsel else None

    def _session_cookie(self, token: str, *, max_age: int = 30 * 86400) -> str:
        parts = [f"{SESSION_COOKIE}={token}", "Path=/", f"Max-Age={max_age}", "HttpOnly", "SameSite=Lax"]
        if self.server.secure_cookies:
            parts.append("Secure")
        return "; ".join(parts)

    def _browser_identity(self) -> str:
        cookie = SimpleCookie()
        cookie.load(self.headers.get('Cookie', ''))
        value = cookie.get('bito_browser')
        return value.value if value and re.fullmatch(r'[A-Za-z0-9_-]{43}', value.value) else secrets.token_urlsafe(32)

    def _browser_cookie(self, browser_id: str) -> str:
        secure = '; Secure' if self.server.secure_cookies else ''
        return f'bito_browser={browser_id}; Path=/; Max-Age=31536000; HttpOnly; SameSite=Lax{secure}'

    def _device_label(self) -> str:
        user_agent = self.headers.get('User-Agent', '')[:512]
        browser = 'Edge' if 'Edg/' in user_agent else ('Chrome' if 'Chrome/' in user_agent else (
            'Firefox' if 'Firefox/' in user_agent else ('Safari' if 'Safari/' in user_agent else 'Browser')))
        system = 'Windows' if 'Windows' in user_agent else ('Android' if 'Android' in user_agent else (
            'iPhone' if 'iPhone' in user_agent else ('iPad' if 'iPad' in user_agent else (
                'macOS' if 'Macintosh' in user_agent else ('Linux' if 'Linux' in user_agent else 'Device')))))
        return f'{browser} · {system}'

    def _check_login_rate(self) -> None:
        if not self.server.login_limiter.allow(self.client_address[0]):
            raise RateLimitError("尝试次数过多，请一分钟后再试")

    def _check_activity_rate(self, member: dict) -> None:
        key = f"{self.client_address[0]}:{member['id']}"
        if not self.server.activity_limiter.allow(key):
            raise RateLimitError("活动记录上传过快，请稍后再试")

    def _check_monitor_rate(self, member: dict) -> None:
        if not self.server.monitor_limiter.allow(member['id']):
            raise RateLimitError('监控操作过快，请一分钟后再试')

    def _check_chat_rate(self, member: dict) -> None:
        if not self.server.chat_limiter.allow(member['id']):
            raise RateLimitError('和 Bito 对话过快，请一分钟后再试')

    def _check_voice_rate(self, member: dict) -> None:
        if not self.server.voice_limiter.allow(member['id']):
            raise RateLimitError('语音识别过快，请一分钟后再试')

    def _check_action_rate(self, member: dict) -> None:
        if not self.server.action_limiter.allow(member['id']):
            raise RateLimitError('Bito 确认操作过快，请一分钟后再试')

    def _check_agent_rate(self, member: dict) -> None:
        if not self.server.agent_limiter.allow(member['id']):
            raise RateLimitError('Bito 工作票创建过快，请一分钟后再试')

    def _require_same_origin(self) -> None:
        origin = self.headers.get("Origin")
        host = self.headers.get("Host")
        if origin and host and urlparse(origin).netloc != host:
            raise PermissionError("请求来源不属于这台家庭服务器")

    def _security_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "same-origin")
        self.send_header("Permissions-Policy", "camera=(), microphone=(self), geolocation=()")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
            "font-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'self'; frame-ancestors 'none'; form-action 'self'",
        )

    def _json(self, status: HTTPStatus, payload: dict, *, cookie: str | None = None):
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self._security_headers()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        if cookie:
            for value in cookie if isinstance(cookie, list) else [cookie]:
                self.send_header("Set-Cookie", value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _ndjson(self, events):
        self.send_response(HTTPStatus.OK)
        self._security_headers()
        self.send_header('Content-Type', 'application/x-ndjson; charset=utf-8')
        self.send_header('Cache-Control', 'no-store, no-transform')
        self.send_header('X-Accel-Buffering', 'no')
        self.send_header('Connection', 'close')
        self.end_headers()
        try:
            for event in events:
                line = json.dumps(event, ensure_ascii=False, separators=(',', ':')).encode('utf-8') + b'\n'
                self.wfile.write(line)
                self.wfile.flush()
        except (CompanionUnavailableError, ValidationError) as exc:
            line = json.dumps({'type': 'error', 'error': str(exc)}, ensure_ascii=False).encode('utf-8') + b'\n'
            self.wfile.write(line)
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass  # A browser interrupt is a normal end to a speech stream.
        finally:
            self.close_connection = True
            close = getattr(events, 'close', None)
            if close:
                close()

    def _bytes(self, status: HTTPStatus, body: bytes, content_type: str, *, cache_control: str = "no-store"):
        self.send_response(status)
        self._security_headers()
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", cache_control)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _static(self, request_path: str):
        relative = request_path.lstrip("/") or "index.html"
        target = (STATIC_DIR / relative).resolve()
        if STATIC_DIR.resolve() not in target.parents or not target.is_file():
            if Path(relative).suffix:
                mime = mimetypes.guess_type(relative)[0] or "application/octet-stream"
                return self._bytes(HTTPStatus.NOT_FOUND, b"", mime, cache_control="no-store")
            target = STATIC_DIR / "index.html"
        body = target.read_bytes()
        mime = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        self.send_response(HTTPStatus.OK)
        self._security_headers()
        self.send_header("Content-Type", f"{mime}; charset=utf-8" if mime.startswith("text/") else mime)
        self.send_header("Cache-Control", "no-cache" if target.name in {"index.html", "sw.js"} else "public, max-age=86400")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
