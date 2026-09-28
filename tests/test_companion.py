from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch
from urllib.error import URLError

from familyos.auth import SessionService
from familyos.activity import ActivityService
from familyos.companion import CompanionService
from familyos.db import Database
from familyos.households import HouseholdService, ValidationError
from familyos.life import LifeService


class CompanionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Database(str(Path(self.temp.name) / 'db.sqlite'))
        self.db.migrate()
        household = HouseholdService(self.db)
        household.initialize({'household_name': 'Test', 'member_name': 'Lin', 'username': 'lin', 'timezone': 'Asia/Shanghai'})
        _, self.member = SessionService(self.db).create('lin')
        household.add_member(self.member, {'display_name': 'Yan', 'username': 'yan', 'role': 'adult'})
        _, self.other = SessionService(self.db).create('yan')
        self.service = CompanionService(self.db)

    def tearDown(self):
        self.temp.cleanup()

    def test_preferences_are_private_validated_and_persistent(self):
        self.assertEqual(self.service.preferences(self.member)['companion_name'], 'Bito')
        self.assertIn('可靠、克制', self.service.preferences(self.member)['system_prompt'])
        self.service.save_preferences(self.member, {'companion_name': '小管家', 'morning_enabled': True, 'morning_time': '08:05', 'system_prompt': '你是我的简洁生活助手。'})
        self.assertEqual(CompanionService(self.db).preferences(self.member)['companion_name'], '小管家')
        self.assertEqual(CompanionService(self.db).preferences(self.member)['system_prompt'], '你是我的简洁生活助手。')
        self.assertFalse(self.service.preferences(self.other)['morning_enabled'])
        for payload in [{'morning_enabled': 'false'}, {'morning_time': '25:99'}, {'companion_name': ''}, {'system_prompt': ''}, {'system_prompt': 'x' * 4001}, {'member_id': self.other['id']}]:
            with self.assertRaises(ValidationError):
                self.service.save_preferences(self.member, payload)

    def test_briefing_uses_real_visible_records_and_declares_hardware(self):
        life = LifeService(self.db)
        life.create_task(self.other, {'title': 'Private task'})
        life.create_task(self.member, {'title': 'Water plants'})
        briefing = self.service.briefing(self.member, now=datetime(2026, 9, 5, 0, tzinfo=timezone.utc))
        self.assertIn('Water plants', briefing['text'])
        self.assertNotIn('Private task', briefing['text'])
        self.assertFalse(briefing['hardware']['connected'])
        self.assertTrue(briefing['local_only'])

    def test_morning_generates_once_per_local_day_with_restart_catchup(self):
        self.service.save_preferences(self.member, {'morning_enabled': True, 'morning_time': '07:30'})
        self.service.generate_due(now=datetime(2026, 9, 4, 23, 29, tzinfo=timezone.utc))
        self.assertIsNone(self.service.scheduled_briefing(self.member)['briefing'])
        self.service.generate_due(now=datetime(2026, 9, 4, 23, 30, tzinfo=timezone.utc))
        CompanionService(self.db).generate_due(now=datetime(2026, 9, 5, 1, tzinfo=timezone.utc))
        with self.db.session() as connection:
            self.assertEqual(connection.execute('SELECT count(*) FROM morning_briefings').fetchone()[0], 1)
        self.assertIsNone(self.service.scheduled_briefing(self.other)['briefing'])

    def test_calendar_month_overlap_timezone_and_member_isolation(self):
        life = LifeService(self.db)
        life.create_event(self.member, {'title': 'Month boundary', 'starts_at': '2026-08-31T15:00:00Z', 'ends_at': '2026-08-31T17:00:00Z'})
        life.create_event(self.other, {'title': 'Private calendar', 'starts_at': '2026-09-03T00:00:00Z', 'ends_at': '2026-09-03T01:00:00Z'})
        life.create_task(self.member, {'title': 'Due at local midnight', 'due_at': '2026-08-31T16:00:00Z'})
        result = life.calendar(self.member, '2026-09')
        self.assertEqual([event['title'] for event in result['events']], ['Month boundary'])
        self.assertEqual(len(result['tasks']), 1)
        with self.assertRaises(ValidationError):
            life.calendar(self.member, '2026-13')

    def test_chat_is_local_bounded_and_receives_member_visible_facts(self):
        requests = []
        service = CompanionService(self.db, ollama_url='http://ollama:11434', ollama_model='local-test',
                                   chat_transport=lambda payload: requests.append(payload) or '今天先完成浇花。')
        LifeService(self.db).create_task(self.member, {'title': '浇花'})
        LifeService(self.db).create_task(self.other, {'title': '全家买菜', 'visibility': 'household'})
        LifeService(self.db).create_task(self.other, {'title': '另一成员私事', 'visibility': 'private'})
        self.service.member_context.save_profile(self.member, {'assistance_preferences': '先告诉我最急的事项'})
        result = service.chat(self.member, {'message': '我今天该做什么？'})
        self.assertEqual(result['text'], '今天先完成浇花。')
        self.assertTrue(result['local_only'])
        self.assertIn('浇花', requests[0]['messages'][0]['content'])
        self.assertIn('全家买菜', requests[0]['messages'][0]['content'])
        self.assertNotIn('另一成员私事', requests[0]['messages'][0]['content'])
        self.assertIn('先告诉我最急的事项', requests[0]['messages'][0]['content'])
        self.assertIn('不要添加标题、小标题', requests[0]['messages'][0]['content'])
        self.assertEqual(requests[0]['messages'][-1], {'role': 'user', 'content': '我今天该做什么？'})
        for invalid in ({'message': ''}, {'message': 'x' * 1001}, {'message': 'x', 'history': []}, {'message': 'x', 'admin': True}):
            with self.assertRaises(ValidationError):
                service.chat(self.member, invalid)

    def test_chat_stream_emits_incremental_text_and_persists_once(self):
        service = CompanionService(self.db, ollama_url='http://ollama:11434', ollama_model='local-test')
        service.save_preferences(self.member, {'model_route': 'ollama', 'ollama_model_override': 'local-test'})
        service._ollama_chat_stream = lambda endpoint, payload: iter(({'text': '先做'}, {'text': '最重要的事。'}))
        events = list(service.chat_stream(self.member, {'message': '今天做什么'}))
        self.assertEqual([event['type'] for event in events], ['meta', 'delta', 'delta', 'done'])
        self.assertEqual(events[-1]['text'], '先做最重要的事。')
        conversation = service.history.get(self.member, events[-1]['conversation_id'])
        self.assertEqual([row['role'] for row in conversation['messages']], ['user', 'assistant'])

    def test_read_today_function_is_member_scoped_audited_and_returns_to_model_once(self):
        LifeService(self.db).create_task(self.member, {'title': '我的今日任务'})
        LifeService(self.db).create_task(self.other, {'title': '另一成员私事'})
        calls = []

        def transport(payload):
            calls.append(payload)
            if payload['tools']:
                return {'text': '', 'tool_call': {'name': 'read_today', 'arguments': {}}}
            return '你今天先完成自己的任务。'

        service = CompanionService(self.db, ollama_url='http://ollama:11434', ollama_model='local-test',
                                   chat_transport=transport)
        result = service.chat(self.member, {'message': '重新核对今天安排'})
        self.assertEqual(result['text'], '你今天先完成自己的任务。')
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[1]['tools'], [])
        self.assertIn('我的今日任务', calls[1]['messages'][1]['content'])
        self.assertNotIn('另一成员私事', calls[1]['messages'][1]['content'])
        self.assertEqual(calls[1]['messages'][-1], {'role': 'user', 'content': '重新核对今天安排'})
        self.assertIsNone(service.actions.latest_pending(self.member))
        audit = service.actions.registry(self.member)['calls']
        self.assertEqual((len(audit), audit[0]['function_key'], audit[0]['status']), (1, 'today.read', 'succeeded'))
        self.assertEqual(audit[0]['result']['tasks'], 1)
        self.assertNotIn('我的今日任务', json.dumps(audit[0]['result'], ensure_ascii=False))
        self.assertEqual(service.actions.registry(self.other)['calls'], [])

    def test_streamed_read_today_returns_one_bounded_followup(self):
        service = CompanionService(self.db, ollama_url='http://ollama:11434', ollama_model='local-test')
        calls = []

        def stream(endpoint, payload):
            calls.append(payload)
            if payload['tools']:
                return iter(({'tool_call': {'name': 'read_today', 'arguments': {}}},))
            return iter(({'text': '核对后，'}, {'text': '今天没有任务。'},
                         {'tool_call': {'name': 'read_today', 'arguments': {}}}))

        service._ollama_chat_stream = stream
        events = list(service.chat_stream(self.member, {'message': '核对今天'}))
        self.assertEqual([event['type'] for event in events], ['meta', 'delta', 'delta', 'done'])
        self.assertEqual(events[-1]['text'], '核对后，今天没有任务。')
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[1]['tools'], [])
        self.assertEqual(len(service.actions.registry(self.member)['calls']), 1)

    def test_read_today_rejects_extra_arguments_disabled_handler_and_audits_failures(self):
        read = {'name': 'read_today', 'arguments': {}}
        with self.assertRaises(ValidationError):
            self.service.actions.execute_read(self.member, {'name': 'read_today', 'arguments': {'member_id': self.other['id']}})
        with self.db.session() as connection:
            connection.execute("UPDATE companion_function_registry SET handler_key='wrong' WHERE function_key='today.read'")
        self.assertNotIn('read_today', [row['function']['name'] for row in self.service.actions.tool_definitions(self.member)])
        with self.assertRaises(ValidationError):
            self.service.actions.execute_read(self.member, read)
        with self.db.session() as connection:
            connection.execute("UPDATE companion_function_registry SET handler_key='today.read' WHERE function_key='today.read'")
        original = self.service.actions.life.today
        self.service.actions.life.today = lambda member: (_ for _ in ()).throw(RuntimeError('injected read failure'))
        try:
            with self.assertRaises(RuntimeError):
                self.service.actions.execute_read(self.member, read)
        finally:
            self.service.actions.life.today = original
        audit = self.service.actions.registry(self.member)['calls']
        self.assertEqual((audit[0]['status'], audit[0]['error_code']), ('failed', 'RuntimeError'))

    def test_prompt_overview_is_member_scoped_and_explains_access(self):
        LifeService(self.db).create_task(self.member, {'title': '我的私密任务'})
        LifeService(self.db).create_task(self.other, {'title': '对方私密任务'})
        overview = self.service.prompt_overview(self.member)
        self.assertIn('我的私密任务', overview['context_preview'])
        self.assertNotIn('对方私密任务', overview['context_preview'])
        self.assertIn('全家可见', '；'.join(overview['scopes']))

    def test_bito_activity_permission_is_explicit_and_member_scoped(self):
        requests = []
        service = CompanionService(self.db, ollama_url='http://ollama:11434', ollama_model='local-test',
                                   chat_transport=lambda payload: requests.append(payload) or '已完成复盘。')
        now = datetime.now(timezone.utc)
        activity = ActivityService(self.db)
        common = {'redacted': True, 'source_device': 'PC', 'app_name': 'Code', 'window_title': '',
                  'ocr_excerpt': '', 'category': 'work', 'analysis_method': 'local_vision', 'analysis_confidence': 0.9,
                  'started_at': (now - timedelta(minutes=20)).isoformat(), 'ended_at': (now - timedelta(minutes=10)).isoformat()}
        activity.ingest(self.member, {**common, 'client_event_id': 'member-event-001', 'activity_summary': '正在实现家庭日程页面。'}, now=now)
        activity.ingest(self.other, {**common, 'client_event_id': 'other-event-001', 'activity_summary': '另一位成员的私人工作。'}, now=now)

        service.chat(self.member, {'message': '总结我今天做了什么'})
        self.assertNotIn('正在实现家庭日程页面', requests[-1]['messages'][0]['content'])
        service.save_preferences(self.member, {'activity_access_enabled': True})
        service.chat(self.member, {'message': '总结我今天做了什么'})
        prompt = requests[-1]['messages'][0]['content']
        self.assertIn('正在实现家庭日程页面', prompt)
        self.assertIn('工作/学习观察分钟', prompt)
        self.assertNotIn('另一位成员的私人工作', prompt)

    def test_model_route_is_saved_per_member_and_never_accepts_a_key(self):
        requests = []
        service = CompanionService(self.db, ollama_url='http://ollama:11434', ollama_model='local-default',
                                   chat_transport=lambda payload: requests.append(payload) or '本地回复。')
        saved = service.save_preferences(self.member, {
            'model_route': 'ollama', 'ollama_model_override': 'qwen-local',
        })
        self.assertEqual(saved['model_route'], 'ollama')
        result = service.chat(self.member, {'message': '测试本地路由'})
        self.assertEqual(result['model'], 'qwen-local')
        self.assertEqual(result['source'], 'Ollama · 服务器配置')
        self.assertEqual(requests[-1]['model'], 'qwen-local')
        for invalid in ({'model_route': 'remote'}, {'cloud_model_override': 'x\napi-key'}, {'ollama_model_override': 'attacker::model'}, {'api_key': 'secret'}):
            with self.assertRaises(ValidationError):
                service.save_preferences(self.member, invalid)

    def test_common_local_services_are_discovered_and_selectable(self):
        service = CompanionService(
            self.db, ollama_url='http://configured-ollama:11434', ollama_model='server-default',
            local_model_hosts='windows.local,8.8.8.8,evil.example',
        )

        def fake_urlopen(request, timeout):
            if request.full_url == 'http://configured-ollama:11434/api/tags':
                payload = {'models': [{'name': 'qwen3:8b'}]}
            elif request.full_url == 'http://host.docker.internal:1234/v1/models':
                payload = {'data': [{'id': 'lmstudio/qwen'}]}
            elif request.full_url == 'http://windows.local:11434/api/tags':
                payload = {'models': [{'name': 'windows-qwen:latest'}]}
            else:
                raise URLError('not running')
            response = MagicMock()
            response.__enter__.return_value = response
            response.read.return_value = json.dumps(payload).encode()
            return response

        with patch('familyos.companion.urlopen', side_effect=fake_urlopen):
            status = service.provider_status(self.member)
        self.assertEqual(service.local_model_hosts, ('windows.local',))
        self.assertEqual([source['name'] for source in status['local']['sources']], ['Ollama · 服务器配置', 'LM Studio', 'Ollama · windows'])
        self.assertIn('lm-studio::lmstudio/qwen', [model['ref'] for model in status['local']['models']])
        self.assertIn('lan-0-ollama::windows-qwen:latest', [model['ref'] for model in status['local']['models']])
        self.assertNotIn('endpoint', status['local']['sources'][0])

        service.save_preferences(self.member, {'model_route': 'ollama', 'ollama_model_override': 'lm-studio::lmstudio/qwen'})
        calls = []
        service._openai_local_chat_at = lambda endpoint, payload: calls.append((endpoint, payload)) or '来自 LM Studio。'
        result = service.chat(self.member, {'message': '测试'})
        self.assertEqual((result['source'], result['model']), ('LM Studio', 'lmstudio/qwen'))
        self.assertEqual(calls[0][0], 'http://host.docker.internal:1234/v1')

    def test_model_tool_creates_member_bound_proposal_and_waits_for_confirmation(self):
        requests = []
        service = CompanionService(
            self.db, ollama_url='http://ollama:11434', ollama_model='tool-model',
            chat_transport=lambda payload: requests.append(payload) or {
                'text': '已经处理',
                'tool_call': {'name': 'propose_task_create', 'arguments': {'title': '买牛奶'}},
            },
        )
        result = service.chat(self.member, {'message': '帮我增加一个买牛奶的任务'})
        self.assertEqual(result['proposal']['title'], '新增任务')
        self.assertIn('同意后才会写入', result['text'])
        self.assertEqual(LifeService(self.db).today(self.member)['tasks'], [])
        self.assertEqual(requests[0]['tools'][0]['function']['name'], 'propose_task_create')
        with self.assertRaises(ValidationError):
            service.actions.confirm(self.other, result['proposal']['id'])
        confirmed = service.actions.confirm(self.member, result['proposal']['id'])
        self.assertEqual(confirmed['proposal']['status'], 'confirmed')
        self.assertEqual(LifeService(self.db).today(self.member)['tasks'][0]['title'], '买牛奶')

    def test_function_registry_is_allowlisted_and_audit_is_member_scoped(self):
        registry = self.service.actions.registry(self.member)
        self.assertEqual(
            [row['key'] for row in registry['functions']],
            ['calendar.create', 'task.complete', 'task.create', 'today.read'],
        )
        self.assertEqual(registry['connections'], {'mcp': 0, 'webhook': 0})
        self.assertEqual([row['risk'] for row in registry['functions']], ['confirm', 'confirm', 'confirm', 'read'])
        self.assertNotIn('handler_key', registry['functions'][0])
        self.assertEqual(registry['calls'], [])

        proposal = self.service.actions.from_tool_call(self.member, {
            'name': 'propose_task_create', 'arguments': {'title': '成员私有审计'},
        })
        pending = self.service.actions.registry(self.member)['calls']
        self.assertEqual((len(pending), pending[0]['status']), (1, 'proposed'))
        self.assertEqual(pending[0]['request'], {'title': '成员私有审计'})
        self.assertEqual(self.service.actions.registry(self.other)['calls'], [])

        self.service.actions.confirm(self.member, proposal['id'])
        completed = self.service.actions.registry(self.member)['calls'][0]
        self.assertEqual(completed['status'], 'succeeded')
        self.assertEqual(completed['result']['title'], '成员私有审计')
        repeated = self.service.actions.confirm(self.member, proposal['id'])
        self.assertEqual(repeated['result']['id'], completed['result']['id'])
        self.assertEqual(len(self.service.actions.registry(self.member)['calls']), 1)
        with self.db.session() as connection:
            task_count = connection.execute(
                "SELECT COUNT(*) FROM tasks WHERE owner_member_id=? AND title='成员私有审计'",
                (self.member['id'],),
            ).fetchone()[0]
        self.assertEqual(task_count, 1)

        with self.db.session() as connection:
            connection.execute("UPDATE companion_function_registry SET enabled=0,health_status='disabled' WHERE function_key='task.create'")
        self.assertNotIn('propose_task_create', [row['function']['name'] for row in self.service.actions.tool_definitions(self.member)])
        with self.assertRaises(ValidationError):
            self.service.actions.from_tool_call(self.member, {
                'name': 'propose_task_create', 'arguments': {'title': '不应写入'},
            })

    def test_function_runtime_health_rejects_a_mismatched_handler(self):
        with self.db.session() as connection:
            connection.execute(
                "UPDATE companion_function_registry SET handler_key='wrong.handler' "
                "WHERE function_key='task.create'"
            )
        functions = {row['key']: row for row in self.service.actions.registry(self.member)['functions']}
        self.assertEqual(functions['task.create']['health_status'], 'unconfigured')
        self.assertNotIn(
            'propose_task_create',
            [row['function']['name'] for row in self.service.actions.tool_definitions(self.member)],
        )
        with self.assertRaises(ValidationError):
            self.service.actions.from_tool_call(self.member, {
                'name': 'propose_task_create', 'arguments': {'title': '不应提出'},
            })

    def test_function_failure_rolls_back_the_effect_and_records_one_failure(self):
        proposal = self.service.actions.from_tool_call(self.member, {
            'name': 'propose_task_create', 'arguments': {'title': '必须回滚'},
        })
        original = self.service.actions._execute_in_transaction

        def fail_after_write(connection, member, action_type, payload):
            original(connection, member, action_type, payload)
            raise RuntimeError('injected failure')

        self.service.actions._execute_in_transaction = fail_after_write
        with self.assertRaises(RuntimeError):
            self.service.actions.confirm(self.member, proposal['id'])
        self.assertFalse(any(row['title'] == '必须回滚' for row in LifeService(self.db).today(self.member)['tasks']))
        failed = self.service.actions.registry(self.member)['calls'][0]
        self.assertEqual((failed['status'], failed['error_code']), ('failed', 'RuntimeError'))

    def test_function_timeout_rolls_back_and_uses_the_registry_deadline(self):
        from familyos.companion_actions import CompanionActionService, FunctionTimeoutError

        with self.db.session() as connection:
            connection.execute(
                "UPDATE companion_function_registry SET timeout_seconds=1 WHERE function_key='task.create'"
            )
        ticks = {'count': 0}

        def clock():
            ticks['count'] += 1
            return 0.0 if ticks['count'] == 1 else 2.0

        actions = CompanionActionService(self.db, LifeService(self.db), monotonic=clock)
        proposal = actions.from_tool_call(self.member, {
            'name': 'propose_task_create', 'arguments': {'title': '不能迟到'},
        })
        with self.assertRaises(FunctionTimeoutError):
            actions.confirm(self.member, proposal['id'])
        self.assertFalse(any(row['title'] == '不能迟到' for row in LifeService(self.db).today(self.member)['tasks']))
        failed = actions.registry(self.member)['calls'][0]
        self.assertEqual((failed['status'], failed['error_code']), ('failed', 'FunctionTimeoutError'))

    def test_explicit_task_command_falls_back_to_confirmation_card(self):
        service = CompanionService(
            self.db, ollama_url='http://ollama:11434', ollama_model='plain-model',
            chat_transport=lambda payload: '我来帮你整理。',
        )
        result = service.chat(self.member, {'message': '记录任务：寄快递'})
        self.assertEqual(result['proposal']['fields'][0]['value'], '寄快递')
        self.assertEqual(LifeService(self.db).today(self.member)['tasks'], [])
        service.actions.reject(self.member, result['proposal']['id'])
        self.assertIsNone(service.actions.latest_pending(self.member))

    def test_chat_history_is_member_owned_and_only_current_conversation_is_model_context(self):
        requests = []
        service = CompanionService(
            self.db, ollama_url='http://ollama:11434', ollama_model='local-test',
            chat_transport=lambda payload: requests.append(payload) or '收到。',
        )
        first = service.chat(self.member, {'message': '只属于第一段对话'})
        second = service.chat(self.member, {'message': '继续第一段', 'conversation_id': first['conversation_id']})
        service.chat(self.member, {'message': '这是独立的新对话'})
        self.assertEqual(second['conversation_id'], first['conversation_id'])
        self.assertIn({'role': 'user', 'content': '只属于第一段对话'}, requests[1]['messages'])
        self.assertNotIn({'role': 'user', 'content': '只属于第一段对话'}, requests[2]['messages'])
        archive = service.history.get(self.member, first['conversation_id'])
        self.assertEqual([row['role'] for row in archive['messages']], ['user', 'assistant', 'user', 'assistant'])
        self.assertEqual(archive['title'], '只属于第一段对话')
        for operation in (
            lambda: service.history.get(self.other, first['conversation_id']),
            lambda: service.history.export(self.other, first['conversation_id']),
            lambda: service.history.delete(self.other, first['conversation_id']),
            lambda: service.chat(self.other, {'message': '越权继续', 'conversation_id': first['conversation_id']}),
        ):
            with self.assertRaises(ValidationError):
                operation()

    def test_chat_history_supports_title_retention_export_and_cascade_delete(self):
        conversation = self.service.history.create(self.member, {'title': '待整理', 'retention_days': 30})
        updated = self.service.history.update(self.member, conversation['id'], {'title': '九月计划', 'retention_days': 0})
        self.assertEqual((updated['title'], updated['retention_days']), ('九月计划', 0))
        exported = self.service.history.export(self.member, conversation['id'])
        self.assertEqual(exported['format'], 'familyos-chat-history-v1')
        self.assertNotIn('member_id', exported['conversation'])
        with self.assertRaises(ValidationError):
            self.service.history.update(self.member, conversation['id'], {'retention_days': 31})
        self.service.history.append_exchange(self.member, conversation['id'], '你好', '你好。', provider='ollama', model='qwen')
        self.assertTrue(self.service.history.delete(self.member, conversation['id'])['deleted'])
        with self.db.session() as connection:
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM companion_messages WHERE conversation_id=?', (conversation['id'],)).fetchone()[0], 0)
        expired = self.service.history.create(self.member, {'title': '已过期', 'retention_days': 7})
        with self.db.session() as connection:
            connection.execute("UPDATE companion_conversations SET updated_at=datetime('now','-8 days') WHERE id=?", (expired['id'],))
        with self.assertRaises(ValidationError):
            self.service.history.context(self.member, expired['id'])
