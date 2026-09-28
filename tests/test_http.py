from pathlib import Path
from datetime import datetime, timedelta, timezone
import base64
import json
import tempfile
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from familyos.db import Database
from familyos.http import FamilyOSServer


class HttpTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        database = Database(str(Path(self.temp.name) / "familyos.db"))
        database.migrate()
        self.server = FamilyOSServer(("127.0.0.1", 0), database)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.temp.cleanup()

    def get_json(self, path, cookie=None):
        headers = {"Cookie": cookie} if cookie else {}
        with urlopen(Request(self.base + path, headers=headers)) as response:
            result_headers = dict(response.headers)
            cookies = response.headers.get_all("Set-Cookie")
            if cookies:
                result_headers["Set-Cookie"] = next((c for c in cookies if c.startswith("familyos_session=")), cookies[0])
            return response.status, result_headers, json.load(response)

    def post_json(self, path, payload, cookie=None, origin=None, user_agent=None):
        headers = {"Content-Type": "application/json"}
        if cookie:
            headers["Cookie"] = cookie
        if origin:
            headers["Origin"] = origin
        if user_agent:
            headers["User-Agent"] = user_agent
        request = Request(
            self.base + path,
            data=json.dumps(payload, ensure_ascii=False).encode(),
            method="POST",
            headers=headers,
        )
        with urlopen(request) as response:
            result_headers = dict(response.headers)
            cookies = response.headers.get_all("Set-Cookie")
            if cookies:
                result_headers["Set-Cookie"] = next((c for c in cookies if c.startswith("familyos_session=")), cookies[0])
            return response.status, result_headers, json.load(response)

    def post_audio(self, path, audio, content_type, cookie=None, origin=None):
        headers = {"Content-Type": content_type}
        if cookie:
            headers["Cookie"] = cookie
        if origin:
            headers["Origin"] = origin
        request = Request(self.base + path, data=audio, method="POST", headers=headers)
        with urlopen(request) as response:
            result_headers = dict(response.headers)
            cookies = response.headers.get_all("Set-Cookie")
            if cookies:
                result_headers["Set-Cookie"] = next((c for c in cookies if c.startswith("familyos_session=")), cookies[0])
            return response.status, result_headers, json.load(response)

    def post_bytes(self, path, body, content_type, cookie=None, origin=None):
        headers = {"Content-Type": content_type}
        if cookie:
            headers["Cookie"] = cookie
        if origin:
            headers["Origin"] = origin
        request = Request(self.base + path, data=body, method="POST", headers=headers)
        with urlopen(request) as response:
            result_headers = dict(response.headers)
            cookies = response.headers.get_all("Set-Cookie")
            if cookies:
                result_headers["Set-Cookie"] = next((c for c in cookies if c.startswith("familyos_session=")), cookies[0])
            return response.status, result_headers, json.load(response)

    def setup(self):
        status, headers, body = self.post_json("/api/setup", {
            "household_name": "验收家庭", "member_name": "验收成员",
            "username": "tester", "timezone": "Asia/Shanghai"
        })
        cookie = headers["Set-Cookie"].split(";", 1)[0]
        return status, headers, body, cookie

    def test_health_and_security_headers(self):
        status, headers, body = self.get_json("/api/health")
        self.assertEqual((status, body["status"], body["schema_version"]), (200, "ok", 24))
        self.assertEqual(headers["X-Frame-Options"], "DENY")
        self.assertIn("default-src 'self'", headers["Content-Security-Policy"])
        self.assertEqual(headers["Permissions-Policy"], "camera=(), microphone=(self), geolocation=()")

    def test_service_directory_session_and_roundtrip(self):
        with self.assertRaises(HTTPError) as failure:
            self.get_json('/api/services')
        self.assertEqual(failure.exception.code, 401)
        _, _, _, cookie = self.setup()
        _, _, entry = self.post_json('/api/services', {'name':'影音库', 'category':'娱乐', 'url':'http://192.168.50.10:8096'}, cookie)
        _, _, result = self.get_json('/api/services', cookie)
        self.assertEqual(result['entries'][0]['id'], entry['id'])
        with urlopen(Request(self.base + '/api/services/' + entry['id'], method='DELETE', headers={'Cookie':cookie})) as response:
            self.assertTrue(json.load(response)['deleted'])
        self.assertEqual(self.get_json('/api/services', cookie)[2]['entries'], [])

    def test_setup_creates_private_session_without_public_member_list(self):
        status, headers, body, cookie = self.setup()
        self.assertEqual(status, 201)
        self.assertIn("HttpOnly", headers["Set-Cookie"])
        self.assertEqual(body["member"]["username"], "tester")
        self.assertEqual(body["member"]["household_name"], "验收家庭")
        _, _, public = self.get_json("/api/setup")
        self.assertEqual(public["members"], [])
        _, _, session = self.get_json("/api/session", cookie)
        self.assertTrue(session["authenticated"])

    def test_username_login_and_authenticated_life_flow(self):
        self.setup()
        _, headers, _ = self.post_json("/api/session", {"username": "TESTER"})
        cookie = headers["Set-Cookie"].split(";", 1)[0]
        self.post_json("/api/tasks", {"title": "验收任务"}, cookie)
        _, _, today = self.get_json("/api/today", cookie)
        self.assertEqual(today["tasks"][0]["title"], "验收任务")
        _, _, insights = self.get_json("/api/insights", cookie)
        self.assertEqual(insights["metrics"][0]["confidence"], "insufficient_data")

    def test_member_consent_http_flow_uses_safe_household_directory(self):
        _, _, _, cookie = self.setup()
        self.post_json(
            '/api/members',
            {'display_name': '共享成员', 'username': 'shared-member', 'role': 'adult'},
            cookie,
        )
        _, _, context = self.get_json('/api/member/context', cookie)
        self.assertEqual([row['display_name'] for row in context['household_members']], ['验收成员', '共享成员'])
        self.assertTrue(all('username' not in row for row in context['household_members']))
        target = next(row for row in context['household_members'] if not row['is_current'])

        status, _, result = self.post_json(
            '/api/member/consents',
            {
                'grantee_member_id': target['id'],
                'data_scope': 'activity_summary',
                'purpose': '共同回顾屏幕使用时间',
                'expires_at': None,
            },
            cookie,
        )
        self.assertEqual(status, 201)
        consent_id = result['consents'][0]['id']
        request = Request(
            self.base + f'/api/member/consents/{consent_id}',
            method='DELETE',
            headers={'Cookie': cookie},
        )
        with urlopen(request) as response:
            self.assertTrue(json.load(response)['revoked'])

    def test_login_device_directory_is_private_and_revocation_is_confirmed(self):
        _, _, _, first_cookie = self.setup()
        _, headers, _ = self.post_json(
            "/api/session", {"username": "tester"},
            user_agent="Mozilla/5.0 (Windows NT 10.0) AppleWebKit/537.36 Chrome/140.0 Safari/537.36 Edg/140.0",
        )
        current_cookie = headers["Set-Cookie"].split(";", 1)[0]
        _, _, directory = self.get_json("/api/sessions", current_cookie)
        self.assertEqual(len(directory["sessions"]), 2)
        self.assertNotIn("token_hash", json.dumps(directory))
        current = next(row for row in directory["sessions"] if row["current"])
        previous = next(row for row in directory["sessions"] if not row["current"])
        self.assertEqual(current["device_label"], "Edge · Windows")

        _, _, trusted = self.post_json(
            f"/api/sessions/{current['id']}/trust", {"trusted": True}, current_cookie)
        self.assertTrue(trusted["trusted"])
        with self.assertRaises(HTTPError) as missing_confirmation:
            self.post_json(f"/api/sessions/{previous['id']}/revoke", {"confirm": False}, current_cookie)
        self.assertEqual(missing_confirmation.exception.code, 400)
        missing_confirmation.exception.close()
        _, _, revoked = self.post_json(
            f"/api/sessions/{previous['id']}/revoke", {"confirm": True}, current_cookie)
        self.assertFalse(revoked["current_session_revoked"])
        self.assertEqual(revoked["session"]["status"], "revoked")
        self.assertEqual(self.get_json("/api/session", current_cookie)[0], 200)
        with self.assertRaises(HTTPError) as old_session:
            self.get_json("/api/session", first_cookie)
        self.assertEqual(old_session.exception.code, 401)
        old_session.exception.close()

    def test_private_api_requires_session(self):
        with self.assertRaises(HTTPError) as error:
            self.get_json("/api/today")
        self.assertEqual(error.exception.code, 401)
        error.exception.close()

    def test_agent_jobs_are_authenticated_member_scoped_and_bounded(self):
        with self.assertRaises(HTTPError) as unauthenticated:
            self.get_json('/api/agent-jobs')
        self.assertEqual(unauthenticated.exception.code, 401)
        unauthenticated.exception.close()
        _, _, _, cookie = self.setup()
        status, _, job = self.post_json('/api/agent-jobs', {
            'title': 'Daily review', 'instructions': 'Summarize the visible tasks.',
        }, cookie)
        self.assertEqual((status, job['status']), (201, 'queued'))
        _, _, listing = self.get_json('/api/agent-jobs', cookie)
        self.assertEqual(listing['jobs'][0]['id'], job['id'])
        self.assertNotIn('member_id', listing['jobs'][0])
        self.assertNotIn('household_id', listing['jobs'][0])
        self.assertNotIn('member_id', listing['agent'])
        _, _, cancelled = self.post_json(f"/api/agent-jobs/{job['id']}/cancel", {}, cookie)
        self.assertEqual(cancelled['status'], 'cancelled')

    def test_schedule_endpoints_require_auth_and_preview_real_times(self):
        with self.assertRaises(HTTPError) as error:
            self.get_json('/api/agent-task-definitions')
        self.assertEqual(error.exception.code, 401)
        error.exception.close()
        _, _, _, cookie = self.setup()
        spec = {'title': 'Morning', 'instructions': 'Summarize tasks', 'schedule_kind': 'daily', 'timezone': 'Asia/Shanghai', 'local_time': '08:00'}
        _, _, preview = self.post_json('/api/agent-task-definitions/preview', spec, cookie)
        self.assertEqual(len(preview['next_occurrences']), 3)
        _, _, result = self.post_json('/api/agent-task-definitions', spec, cookie)
        definition = result['definitions'][0]
        self.assertNotIn('member_id', definition)
        _, _, job = self.post_json('/api/agent-task-definitions/' + definition['id'] + '/run', {}, cookie)
        self.assertEqual(job['status'], 'queued')

    def test_companion_stream_is_authenticated_and_ndjson(self):
        _, _, _, cookie = self.setup()
        self.server.companion.chat_stream = lambda member, payload: iter((
            {'type': 'meta', 'provider': 'ollama', 'model': 'test'},
            {'type': 'delta', 'text': '你好'},
            {'type': 'done', 'text': '你好', 'conversation_id': 'example'},
        ))
        request = Request(
            self.base + '/api/companion/chat/stream',
            data=json.dumps({'message': 'hi'}).encode(), method='POST',
            headers={'Content-Type': 'application/json', 'Cookie': cookie},
        )
        with urlopen(request) as response:
            rows = [json.loads(line) for line in response if line.strip()]
            self.assertEqual(response.headers['Content-Type'], 'application/x-ndjson; charset=utf-8')
        self.assertEqual([row['type'] for row in rows], ['meta', 'delta', 'done'])

    def test_server_status_is_private_and_fails_closed_without_snapshot(self):
        with self.assertRaises(HTTPError) as error:
            self.get_json("/api/server/status")
        self.assertEqual(error.exception.code, 401)
        error.exception.close()
        _, _, _, cookie = self.setup()
        status, _, payload = self.get_json("/api/server/status", cookie)
        self.assertEqual(status, 200)
        self.assertFalse(payload["available"])
        self.assertEqual(payload["services"], [])

    def test_cross_origin_mutation_is_rejected(self):
        with self.assertRaises(HTTPError) as error:
            self.post_json("/api/session", {"username": "anything"}, origin="http://attacker.invalid")
        self.assertEqual(error.exception.code, 403)
        error.exception.close()



    def test_activity_record_report_and_table_endpoints(self):
        _, _, _, cookie = self.setup()
        icon = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M/wHwAF/gL+8oI2WQAAAABJRU5ErkJggg=="
        )
        _, _, icon_result = self.post_json("/api/activity/icons", {
            "source_device": "windows-main", "app_name": "Code",
            "icon_png_base64": base64.b64encode(icon).decode("ascii"),
        }, cookie)
        ended = datetime.now(timezone.utc) - timedelta(minutes=1)
        started = ended - timedelta(minutes=1)
        day = started.astimezone().date().isoformat()
        record = {
            "client_event_id": "event-http-0001", "source_device": "windows-main",
            "started_at": started.isoformat(), "ended_at": ended.isoformat(),
            "app_name": "Code", "window_title": "FamilyOS", "ocr_excerpt": "editing tests",
            "activity_summary": "实现电脑日报", "category": "work",
            "analysis_method": "windows_ocr", "analysis_confidence": 0.8, "redacted": True,
        }
        status, _, _ = self.post_json("/api/activity/records", record, cookie)
        self.assertEqual(status, 201)
        report = {
            "client_report_id": "report-http-20260903", "source_device": "windows-main",
            "report_date": day, "title": "电脑日报", "summary": "完成后端接口",
            "highlights": ["完成测试"], "next_actions": ["部署"],
            "analysis_method": "deterministic", "model_name": "", "sample_size": 1,
            "observed_minutes": 1, "generated_at": datetime.now(timezone.utc).isoformat(), "redacted": True,
        }
        status, _, _ = self.post_json("/api/activity/reports", report, cookie)
        self.assertEqual(status, 201)
        _, _, timeline = self.get_json(f"/api/activity/timeline?date={day}", cookie)
        _, _, reports = self.get_json(f"/api/activity/reports?date={day}", cookie)
        self.assertEqual(timeline["records"][0]["app_name"], "Code")
        self.assertEqual(timeline["records"][0]["app_icon_id"], icon_result["id"])
        self.assertEqual(reports["reports"][0]["summary"], "完成后端接口")
        _, _, history = self.get_json("/api/activity/history?range=7d", cookie)
        self.assertEqual(history["range"], "7d")
        self.assertEqual(history["totals"]["sample_size"], 1)
        self.assertTrue(history["progress_note"])
        with urlopen(Request(self.base + f"/api/activity/icons/{icon_result['id']}", headers={"Cookie": cookie})) as response:
            self.assertEqual(response.headers["Content-Type"], "image/png")
            self.assertEqual(response.read(), icon)

    def test_setup_endpoint_validates_json_and_content_type(self):
        request = Request(self.base + "/api/setup", data=b"not-json", method="POST", headers={"Content-Type": "application/json"})
        with self.assertRaises(HTTPError) as error:
            urlopen(request)
        self.assertEqual(error.exception.code, 400)
        error.exception.close()

    def test_voice_output_requires_member_and_same_origin_without_exposing_token(self):
        for method, path in [('GET', '/api/voice/output'), ('POST', '/api/voice/speak')]:
            with self.assertRaises(HTTPError) as error:
                if method == 'GET': self.get_json(path)
                else: self.post_json(path, {'text': 'hello'})
            self.assertEqual(error.exception.code, 401)
            error.exception.close()
        _, _, _, cookie = self.setup()
        self.server.voice_output.url = 'http://configured-worker'
        self.server.voice_output.token = 'fixture-private-token'
        self.assertEqual(self.get_json('/api/voice/output', cookie)[2], {'doorman': True})
        with self.assertRaises(HTTPError) as error:
            self.post_json('/api/voice/speak', {'text': 'hello'}, cookie, 'http://attacker.invalid')
        self.assertEqual(error.exception.code, 403)
        error.exception.close()
        self.server.voice_output.stream = lambda payload: iter([{'pcm': 'AAA=', 'sample_rate': 32000}, {'done': True}])
        req = Request(self.base + '/api/voice/speak', data=b'{"text":"hello"}',
                      headers={'Content-Type':'application/json', 'Cookie':cookie})
        with urlopen(req) as response:
            self.assertEqual(response.headers['Content-Type'], 'application/x-ndjson; charset=utf-8')
            lines = response.read().decode().splitlines()
        self.assertTrue(json.loads(lines[-1])['done'])
        self.assertNotIn('fixture-private-token', ''.join(lines))

    def test_voice_transcription_is_private_bounded_and_same_origin(self):
        _, _, _, cookie = self.setup()
        self.server.voice.transcribe = lambda audio, content_type: {
            "text": "今天有什么计划", "engine": "faster-whisper"
        }
        status, _, result = self.post_audio('/api/voice/transcribe', b'voice-bytes', 'audio/webm', cookie)
        self.assertEqual((status, result['text'], result['engine']), (200, '今天有什么计划', 'faster-whisper'))
        with self.assertRaises(HTTPError) as error:
            self.post_audio('/api/voice/transcribe', b'voice-bytes', 'audio/webm', cookie, 'http://attacker.invalid')
        self.assertEqual(error.exception.code, 403)
        error.exception.close()

    def test_realtime_voice_stream_is_private_member_scoped_and_same_origin(self):
        _, _, _, cookie = self.setup()
        session_id = '12345678-1234-1234-1234-123456789abc'
        calls = []
        self.server.voice.start_stream = lambda member, payload: {
            'session_id': session_id, 'engine': 'funasr', 'mode': '2pass', 'sample_rate': 16000, 'chunk_ms': 480
        }
        self.server.voice.stream_chunk = lambda member, value, audio: (
            calls.append(('chunk', member['id'], value, audio)) or
            {'session_id': value, 'text': '实时', 'final': False, 'engine': 'funasr', 'mode': '2pass-online', 'reconnects': 0}
        )
        self.server.voice.finish_stream = lambda member, value: {
            'session_id': value, 'text': '实时语音。', 'final': True, 'engine': 'funasr', 'mode': '2pass-offline',
            'reconnects': 0, 'latency_ms': {'first_partial': 200, 'final_after_last_chunk': 300}
        }
        self.server.voice.abort_stream = lambda member, value: {'aborted': True, 'session_id': value}
        status, _, started = self.post_json('/api/voice/stream/start', {'sample_rate': 16000}, cookie)
        self.assertEqual((status, started['session_id']), (201, session_id))
        status, _, partial = self.post_bytes(
            f'/api/voice/stream/{session_id}/chunk', b'\x00\x00' * 64, 'application/octet-stream', cookie)
        self.assertEqual((status, partial['text'], calls[0][3]), (200, '实时', b'\x00\x00' * 64))
        status, _, final = self.post_json(f'/api/voice/stream/{session_id}/finish', {}, cookie)
        self.assertEqual((status, final['text'], final['mode']), (200, '实时语音。', '2pass-offline'))
        status, _, aborted = self.post_json(f'/api/voice/stream/{session_id}/abort', {}, cookie)
        self.assertEqual((status, aborted['aborted']), (200, True))
        with self.assertRaises(HTTPError) as unauthenticated:
            self.post_json('/api/voice/stream/start', {'sample_rate': 16000})
        self.assertEqual(unauthenticated.exception.code, 401)
        unauthenticated.exception.close()
        with self.assertRaises(HTTPError) as cross_origin:
            self.post_json('/api/voice/stream/start', {'sample_rate': 16000}, cookie, 'http://attacker.invalid')
        self.assertEqual(cross_origin.exception.code, 403)
        cross_origin.exception.close()
        with self.assertRaises(HTTPError) as wrong_type:
            self.post_bytes(f'/api/voice/stream/{session_id}/chunk', b'\x00\x00', 'audio/wav', cookie)
        self.assertEqual(wrong_type.exception.code, 400)
        wrong_type.exception.close()

    def test_member_avatar_is_private_validated_and_same_origin(self):
        _, _, _, cookie = self.setup()
        image = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M/wHwAF/gL+8oI2WQAAAABJRU5ErkJggg=="
        )
        status, _, payload = self.post_bytes('/api/member/avatar', image, 'image/png', cookie)
        self.assertEqual(status, 200)
        self.assertIn('updated_at', payload)
        with urlopen(Request(self.base + '/api/member/avatar', headers={'Cookie': cookie})) as response:
            self.assertEqual(response.headers['Content-Type'], 'image/png')
            self.assertEqual(response.read(), image)
        with self.assertRaises(HTTPError) as unauthenticated:
            urlopen(self.base + '/api/member/avatar')
        self.assertEqual(unauthenticated.exception.code, 401)
        unauthenticated.exception.close()
        with self.assertRaises(HTTPError) as cross_origin:
            self.post_bytes('/api/member/avatar', image, 'image/png', cookie, 'http://attacker.invalid')
        self.assertEqual(cross_origin.exception.code, 403)
        cross_origin.exception.close()
        with self.assertRaises(HTTPError) as mismatched:
            self.post_bytes('/api/member/avatar', image, 'image/jpeg', cookie)
        self.assertEqual(mismatched.exception.code, 400)
        mismatched.exception.close()
        with self.assertRaises(HTTPError) as error:
            self.post_audio('/api/voice/transcribe', b'voice-bytes', 'text/plain', cookie)
        self.assertEqual(error.exception.code, 400)
        error.exception.close()

    def test_new_features_are_private_and_preferences_reject_cross_origin(self):
        for path in ['/api/monitors', '/api/calendar?month=2026-09', '/api/preferences', '/api/companion/prompt', '/api/briefing', '/api/briefing/scheduled']:
            with self.assertRaises(HTTPError) as error:
                self.get_json(path)
            self.assertEqual(error.exception.code, 401)
            error.exception.close()
        _, _, _, cookie = self.setup()
        req = Request(self.base + '/api/preferences', data=b'{"companion_name":"Jarvis"}', method='PUT',
                      headers={'Content-Type': 'application/json', 'Cookie': cookie})
        with urlopen(req) as response:
            self.assertEqual(json.load(response)['companion_name'], 'Jarvis')
        req.add_header('Origin', 'http://attacker.invalid')
        with self.assertRaises(HTTPError) as error:
            urlopen(req)
        self.assertEqual(error.exception.code, 403)
        error.exception.close()
        _, _, monitors = self.get_json('/api/monitors', cookie)
        self.assertEqual(monitors['subscriptions'], [])
        _, _, briefing = self.get_json('/api/briefing', cookie)
        self.assertIn('Jarvis', briefing['text'])
        _, _, prompt = self.get_json('/api/companion/prompt', cookie)
        self.assertIn('当前成员：验收成员', prompt['context_preview'])
        self.assertIn('其他成员的私密记录', prompt['locked_rule'])
        self.assertIn('云端模型', prompt['locked_rule'])
        self.server.companion.ollama_url = 'http://ollama:11434'
        self.server.companion.ollama_model = 'test-local-model'
        self.server.companion.chat_transport = lambda payload: '这是本地回答。'
        status, _, answer = self.post_json('/api/companion/chat', {'message':'我今天有什么安排？'}, cookie)
        self.assertEqual(status, 200)
        self.assertEqual({key: answer[key] for key in ('text','model','provider','source','local_only','fallback')}, {'text':'这是本地回答。','model':'test-local-model','provider':'ollama','source':'Ollama · 服务器配置','local_only':True,'fallback':True})
        self.assertRegex(answer['conversation_id'], r'^[0-9a-f-]{36}$')
        request = Request(self.base + "/api/setup", data=b"name=x", method="POST")
        with self.assertRaises(HTTPError) as error:
            urlopen(request)
        self.assertEqual(error.exception.code, 400)
        error.exception.close()

    def test_bito_action_requires_member_confirmation_before_write(self):
        with self.assertRaises(HTTPError) as unauthenticated:
            self.get_json('/api/companion/functions')
        self.assertEqual(unauthenticated.exception.code, 401)
        unauthenticated.exception.close()
        _, _, _, cookie = self.setup()
        self.server.companion.ollama_url = 'http://ollama:11434'
        self.server.companion.ollama_model = 'tool-model'
        self.server.companion.chat_transport = lambda payload: {
            'text': '',
            'tool_call': {'name': 'propose_task_create', 'arguments': {'title': '确认后写入'}},
        }
        _, _, chat = self.post_json('/api/companion/chat', {'message': '帮我添加任务'}, cookie)
        self.assertEqual(chat['proposal']['title'], '新增任务')
        self.assertEqual(self.get_json('/api/today', cookie)[2]['tasks'], [])
        proposal_id = chat['proposal']['id']
        self.assertEqual(self.get_json('/api/companion/actions/pending', cookie)[2]['proposal']['id'], proposal_id)
        registry = self.get_json('/api/companion/functions', cookie)[2]
        self.assertEqual(len(registry['functions']), 4)
        self.assertEqual(registry['calls'][0]['status'], 'proposed')
        self.assertNotIn('handler_key', registry['functions'][0])
        self.assertNotIn('input_schema_json', registry['functions'][0])
        _, _, confirmed = self.post_json(f'/api/companion/actions/{proposal_id}/confirm', {}, cookie)
        self.assertEqual(confirmed['proposal']['status'], 'confirmed')
        self.assertEqual(self.get_json('/api/companion/functions', cookie)[2]['calls'][0]['status'], 'succeeded')
        self.assertEqual(self.get_json('/api/today', cookie)[2]['tasks'][0]['title'], '确认后写入')

    def test_chat_history_http_contract_is_private_same_origin_and_complete(self):
        with self.assertRaises(HTTPError) as unauthenticated:
            self.get_json('/api/companion/conversations')
        self.assertEqual(unauthenticated.exception.code, 401)
        unauthenticated.exception.close()
        _, _, _, cookie = self.setup()
        status, _, conversation = self.post_json('/api/companion/conversations', {'title': 'HTTP 验收', 'retention_days': 30}, cookie)
        self.assertEqual(status, 201)
        conversation_id = conversation['id']
        self.assertEqual(self.get_json('/api/companion/conversations', cookie)[2]['conversations'][0]['id'], conversation_id)
        update = Request(
            self.base + f'/api/companion/conversations/{conversation_id}',
            data=json.dumps({'title': '已更新', 'retention_days': 90}, ensure_ascii=False).encode(), method='PUT',
            headers={'Content-Type': 'application/json', 'Cookie': cookie},
        )
        with urlopen(update) as response:
            self.assertEqual(json.load(response)['title'], '已更新')
        cross_origin = Request(
            self.base + f'/api/companion/conversations/{conversation_id}', data=b'{"title":"blocked"}', method='PUT',
            headers={'Content-Type': 'application/json', 'Cookie': cookie, 'Origin': 'http://attacker.invalid'},
        )
        with self.assertRaises(HTTPError) as blocked:
            urlopen(cross_origin)
        self.assertEqual(blocked.exception.code, 403)
        blocked.exception.close()
        exported = self.get_json(f'/api/companion/conversations/{conversation_id}/export', cookie)[2]
        self.assertEqual(exported['conversation']['title'], '已更新')
        delete = Request(self.base + f'/api/companion/conversations/{conversation_id}', method='DELETE', headers={'Cookie': cookie})
        with urlopen(delete) as response:
            self.assertTrue(json.load(response)['deleted'])
        self.assertEqual(self.get_json('/api/companion/conversations', cookie)[2]['conversations'], [])


if __name__ == "__main__":
    unittest.main()
