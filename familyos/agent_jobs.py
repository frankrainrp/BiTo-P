from __future__ import annotations

from datetime import datetime, timezone
import logging
import json
from threading import Event, Thread
import sqlite3
import time
from uuid import uuid4

from .households import ValidationError
from .task_webhooks import TaskWebhook


FINAL_STATES = {'completed', 'failed', 'cancelled', 'needs_attention'}


class AgentJobService:
    """Member-scoped queue for real Bito background work."""

    def __init__(self, database, companion, webhook=None):
        self.database = database
        self.companion = companion
        self.webhook = webhook or TaskWebhook()

    @staticmethod
    def _now():
        return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def _clean_text(value, label, maximum):
        if not isinstance(value, str) or not value.strip() or len(value.strip()) > maximum or '\x00' in value:
            raise ValidationError(f'{label}必须为 1–{maximum} 个字符')
        return value.strip()

    def create(self, member, payload):
        if set(payload) - {'title', 'instructions', 'execution_kind'}:
            raise ValidationError('工作票包含不支持的字段')
        kind = payload.get('execution_kind', 'agent')
        if kind not in ('agent', 'webhook'):
            raise ValidationError('任务类型不正确')
        if kind == 'webhook' and not self.webhook.ready:
            raise ValidationError('本地 Webhook 通知服务尚未配置')
        instructions = self._clean_text(payload.get('instructions'), '任务说明', 1000)
        raw_title = payload.get('title') or instructions[:60]
        title = self._clean_text(raw_title, '任务名称', 120)
        now = self._now()
        job_id = str(uuid4())
        try:
            with self.database.session() as conn:
                conn.execute(
                    'INSERT INTO agent_jobs(id,household_id,member_id,title,instructions,status,stage,progress,created_at,updated_at) '
                    "VALUES (?,?,?,?,?,'queued','queued',0,?,?)",
                    (job_id, member['household_id'], member['id'], title, instructions, now, now),
                )
                self._event(conn, job_id, member['id'], 'queued', '任务已进入 Bito 队列', 0, now)
                conn.execute('UPDATE agent_jobs SET execution_kind=? WHERE id=?', (kind, job_id))
        except sqlite3.IntegrityError as exc:
            if 'agent_jobs_one_active_per_member' in str(exc) or 'UNIQUE constraint failed: agent_jobs.member_id' in str(exc):
                raise ValidationError('你已有一项等待或运行中的 Bito 任务') from exc
            raise
        return self.get(member, job_id)

    def list(self, member, limit=30):
        self.reconcile(member)
        with self.database.session() as conn:
            rows = conn.execute(
                'SELECT * FROM agent_jobs WHERE member_id=? AND household_id=? ORDER BY created_at DESC LIMIT ?',
                (member['id'], member['household_id'], min(max(int(limit), 1), 100)),
            ).fetchall()
            jobs = [self._public(conn, row) for row in rows]
        active = next((row for row in jobs if row['status'] in {'queued', 'running'}), None)
        return {'agent': self._agent_state(active), 'jobs': jobs, 'webhook_ready': self.webhook.ready}

    def get(self, member, job_id):
        self.reconcile(member)
        with self.database.session() as conn:
            row = conn.execute(
                'SELECT * FROM agent_jobs WHERE id=? AND member_id=? AND household_id=?',
                (job_id, member['id'], member['household_id']),
            ).fetchone()
            if not row:
                raise ValidationError('没有找到这项工作票')
            return self._public(conn, row)

    def cancel(self, member, job_id):
        self.reconcile(member)
        now = self._now()
        with self.database.session() as conn:
            conn.execute('BEGIN IMMEDIATE')
            row = self._owned(conn, member, job_id)
            if row['stage'] == 'awaiting_confirmation':
                proposal = conn.execute('SELECT status FROM companion_action_proposals WHERE id=?', (row['proposal_id'],)).fetchone()
                if proposal and proposal['status'] == 'confirmed':
                    conn.execute("UPDATE agent_jobs SET status='completed',stage='completed',finished_at=?,updated_at=? WHERE id=?", (now, now, job_id))
                    return self._public(conn, conn.execute('SELECT * FROM agent_jobs WHERE id=?', (job_id,)).fetchone())
                self._reject_pending(conn, member, row['proposal_id'])
                conn.execute("UPDATE agent_jobs SET status='cancelled',stage='cancelled',finished_at=?,updated_at=? WHERE id=?", (now, now, job_id))
                self._event(conn, job_id, member['id'], 'cancelled', '已取消待确认改动', 100, now)
                return self._public(conn, conn.execute('SELECT * FROM agent_jobs WHERE id=?', (job_id,)).fetchone())
            if row['status'] in FINAL_STATES:
                return self._public(conn, row)
            if row['status'] == 'queued':
                conn.execute(
                    "UPDATE agent_jobs SET status='cancelled',stage='cancelled',progress=100,cancel_requested=1,finished_at=?,updated_at=? WHERE id=?",
                    (now, now, job_id),
                )
                self._event(conn, job_id, member['id'], 'cancelled', '任务已在运行前取消', 100, now)
            else:
                conn.execute('UPDATE agent_jobs SET cancel_requested=1,stage=?,updated_at=? WHERE id=?', ('cancelling', now, job_id))
                self._event(conn, job_id, member['id'], 'cancel_requested', '已请求取消；当前模型调用结束后停止', row['progress'], now)
            row = conn.execute('SELECT * FROM agent_jobs WHERE id=?', (job_id,)).fetchone()
            return self._public(conn, row)

    def takeover(self, member, job_id):
        now = self._now()
        with self.database.session() as conn:
            row = self._owned(conn, member, job_id)
            if row['status'] in FINAL_STATES:
                return self._public(conn, row)
            if row['status'] == 'queued':
                conn.execute(
                    "UPDATE agent_jobs SET status='needs_attention',stage='manual_takeover',progress=100,takeover_requested=1,finished_at=?,updated_at=? WHERE id=?",
                    (now, now, job_id),
                )
                self._event(conn, job_id, member['id'], 'takeover', '任务已交回给你处理', 100, now)
            else:
                conn.execute('UPDATE agent_jobs SET takeover_requested=1,cancel_requested=1,stage=?,updated_at=? WHERE id=?', ('handoff', now, job_id))
                self._event(conn, job_id, member['id'], 'takeover_requested', '将在当前模型调用结束后交回给你', row['progress'], now)
            row = conn.execute('SELECT * FROM agent_jobs WHERE id=?', (job_id,)).fetchone()
            return self._public(conn, row)

    def claim_next(self):
        now = self._now()
        with self.database.session() as conn:
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute("SELECT id FROM agent_jobs WHERE status='queued' ORDER BY created_at LIMIT 1").fetchone()
            if not row:
                return None
            changed = conn.execute(
                "UPDATE agent_jobs SET status='running',stage='context',progress=12,started_at=?,updated_at=? WHERE id=? AND status='queued'",
                (now, now, row['id']),
            ).rowcount
            if not changed:
                return None
            job = conn.execute('SELECT * FROM agent_jobs WHERE id=?', (row['id'],)).fetchone()
            self._event(conn, job['id'], job['member_id'], 'started', 'Bito 已开始整理上下文', 12, now)
            return dict(job)

    def execute(self, job):
        try:
            member = self._member(job['member_id'])
            if job.get('execution_kind') == 'webhook':
                with self.database.session() as conn:
                    changed = conn.execute("UPDATE agent_jobs SET delivery_started_at=? WHERE id=? AND status='running' AND delivery_started_at IS NULL AND cancel_requested=0",
                                           (self._now(), job['id'])).rowcount
                if not changed:
                    self._finish_failed(job['id'], 'delivery_not_started', '未投递：任务已取消或已尝试发送')
                    return
                self._set_stage(job['id'], 'webhook', 35, '正在向本地通知服务投递')
                result = self.webhook.send(member, job)
            else:
                self._set_stage(job['id'], 'model', 35, '正在请求已选择的模型')
                result = self.companion.chat(member, {'message': job['instructions']})
        except Exception as exc:
            if job.get('execution_kind') == 'webhook':
                self._finish_failed(job['id'], 'delivery_unknown', '未取得投递回执，请核对接收端；不会自动重发')
            else:
                logging.getLogger(__name__).exception('Bito agent job failed')
                self._finish_failed(job['id'], 'model_error', '模型执行失败；可在对话中重试')
            return
        now = self._now()
        with self.database.session() as conn:
            conn.execute('BEGIN IMMEDIATE')
            current = conn.execute('SELECT * FROM agent_jobs WHERE id=?', (job['id'],)).fetchone()
            if not current or current['status'] != 'running':
                return
            if job.get('execution_kind') == 'webhook':
                status, stage, message = 'completed', 'completed', '本地 Webhook 已确认接收；已发送消息不能撤回'
                result_text = result['text'][:4000]
            elif current['takeover_requested']:
                status, stage, message = 'needs_attention', 'manual_takeover', '任务已交回给你处理'
                result_text = None
            elif current['cancel_requested']:
                status, stage, message = 'cancelled', 'cancelled', '任务已取消'
                result_text = None
            else:
                status, stage, message = 'completed', 'completed', 'Bito 已完成任务'
                result_text = result['text'][:4000]
            proposal_id = (result.get('proposal') or {}).get('id')
            if proposal_id:
                proposal = conn.execute('SELECT * FROM companion_action_proposals WHERE id=? AND member_id=? AND household_id=?',
                                        (proposal_id, member['id'], member['household_id'])).fetchone()
                if not proposal:
                    proposal_id = None
                elif current['cancel_requested'] or current['takeover_requested']:
                    self._reject_pending(conn, member, proposal_id)
                    if proposal['status'] == 'confirmed':
                        status, stage, message = 'completed', 'completed', '改动已执行，无法通过取消撤回'
                        result_text = result['text'][:4000]
                else:
                    status, stage, message = 'needs_attention', 'awaiting_confirmation', '改动等待你确认后执行'
            conn.execute(
                'UPDATE agent_jobs SET status=?,stage=?,progress=100,result_text=?,conversation_id=?,proposal_id=?,finished_at=?,updated_at=? WHERE id=?',
                (status, stage, result_text, result.get('conversation_id'), proposal_id, None if stage == 'awaiting_confirmation' else now, now, job['id']),
            )
            self._event(conn, job['id'], job['member_id'], status, message, 100, now)

    @staticmethod
    def _reject_pending(conn, member, proposal_id):
        changed = conn.execute("UPDATE companion_action_proposals SET status='rejected' WHERE id=? AND member_id=? AND household_id=? AND status='pending'",
                               (proposal_id, member['id'], member['household_id'])).rowcount
        if changed:
            conn.execute("UPDATE companion_function_calls SET status='rejected',updated_at=? WHERE proposal_id=? AND status='proposed'",
                         (AgentJobService._now(), proposal_id))

    def reconcile(self, member):
        now = self._now()
        with self.database.session() as conn:
            conn.execute('BEGIN IMMEDIATE')
            rows = conn.execute("SELECT j.id,j.proposal_id,p.status,p.expires_at FROM agent_jobs j LEFT JOIN companion_action_proposals p ON p.id=j.proposal_id AND p.member_id=j.member_id AND p.household_id=j.household_id WHERE j.member_id=? AND j.household_id=? AND j.stage='awaiting_confirmation'",
                                (member['id'], member['household_id'])).fetchall()
            for row in rows:
                status = row['status']
                if status == 'pending' and row['expires_at'] <= now:
                    status = 'expired'
                    conn.execute("UPDATE companion_action_proposals SET status='expired' WHERE id=? AND status='pending'", (row['proposal_id'],))
                    conn.execute("UPDATE companion_function_calls SET status='expired',updated_at=? WHERE proposal_id=? AND status='proposed'", (now, row['proposal_id']))
                if status in ('pending', 'executing'):
                    continue
                state = 'completed' if status == 'confirmed' else ('cancelled' if status == 'rejected' else 'needs_attention')
                stage = 'completed' if status == 'confirmed' else 'proposal_' + (status or 'missing')
                conn.execute('UPDATE agent_jobs SET status=?,stage=?,finished_at=?,updated_at=? WHERE id=?', (state, stage, now, now, row['id']))
                self._event(conn, row['id'], member['id'], stage, '确认结果：' + (status or 'missing'), 100, now)

    def recover_interrupted(self):
        now = self._now()
        with self.database.session() as conn:
            conn.execute('BEGIN IMMEDIATE')
            for row in conn.execute("SELECT id,member_id FROM agent_jobs WHERE status='running'").fetchall():
                conn.execute("UPDATE agent_jobs SET status='needs_attention',stage='interrupted',error_code='worker_restarted',finished_at=?,updated_at=? WHERE id=?", (now, now, row['id']))
                self._event(conn, row['id'], row['member_id'], 'interrupted', '服务重启中断了执行，请核对结果后重新提交', 100, now)

    def _set_stage(self, job_id, stage, progress, message):
        now = self._now()
        with self.database.session() as conn:
            row = conn.execute('SELECT member_id,status FROM agent_jobs WHERE id=?', (job_id,)).fetchone()
            if not row or row['status'] != 'running':
                return
            conn.execute('UPDATE agent_jobs SET stage=?,progress=?,updated_at=? WHERE id=?', (stage, progress, now, job_id))
            self._event(conn, job_id, row['member_id'], stage, message, progress, now)

    def _finish_failed(self, job_id, code, message):
        now = self._now()
        with self.database.session() as conn:
            row = conn.execute('SELECT member_id,status FROM agent_jobs WHERE id=?', (job_id,)).fetchone()
            if not row or row['status'] != 'running':
                return
            conn.execute(
                "UPDATE agent_jobs SET status='failed',stage='failed',progress=100,error_code=?,finished_at=?,updated_at=? WHERE id=?",
                (code, now, now, job_id),
            )
            self._event(conn, job_id, row['member_id'], 'failed', message, 100, now)

    def _member(self, member_id):
        with self.database.session() as conn:
            row = conn.execute(
                'SELECT m.id,m.household_id,m.username,m.display_name,m.role,m.is_admin,'
                'h.name AS household_name,h.timezone AS household_timezone '
                'FROM members m JOIN households h ON h.id=m.household_id WHERE m.id=?',
                (member_id,),
            ).fetchone()
        if not row:
            raise ValidationError('任务所属成员已不存在')
        result = dict(row)
        result['is_admin'] = bool(result['is_admin'])
        return result

    @staticmethod
    def _event(conn, job_id, member_id, event_type, message, progress, created_at):
        conn.execute(
            'INSERT INTO agent_job_events(id,job_id,member_id,event_type,message,progress,created_at) VALUES (?,?,?,?,?,?,?)',
            (str(uuid4()), job_id, member_id, event_type, message, progress, created_at),
        )

    @staticmethod
    def _owned(conn, member, job_id):
        row = conn.execute(
            'SELECT * FROM agent_jobs WHERE id=? AND member_id=? AND household_id=?',
            (job_id, member['id'], member['household_id']),
        ).fetchone()
        if not row:
            raise ValidationError('没有找到这项工作票')
        return row

    @staticmethod
    def _public(conn, row):
        result = dict(row)
        result.pop('household_id', None)
        result.pop('member_id', None)
        for key in ('cancel_requested', 'takeover_requested'):
            result[key] = bool(result[key])
        events = conn.execute(
            'SELECT event_type,message,progress,created_at FROM agent_job_events WHERE job_id=? ORDER BY created_at DESC,id DESC LIMIT 12',
            (row['id'],),
        ).fetchall()
        result['events'] = [dict(event) for event in events]
        proposal = conn.execute('SELECT id,action_type,status,payload_json,expires_at FROM companion_action_proposals WHERE id=? AND member_id=? AND household_id=?',
                                (row['proposal_id'], row['member_id'], row['household_id'])).fetchone() if row['proposal_id'] else None
        result['proposal'] = ({'id': proposal['id'], 'action_type': proposal['action_type'], 'status': proposal['status'],
                               'fields': json.loads(proposal['payload_json']), 'expires_at': proposal['expires_at']} if proposal else None)
        return result

    @staticmethod
    def _agent_state(active):
        if not active:
            return {'key': 'bito', 'status': 'idle', 'stage': 'idle', 'progress': 0, 'job_id': None}
        return {'key': 'bito', 'status': active['status'], 'stage': active['stage'], 'progress': active['progress'], 'job_id': active['id']}


class AgentJobWorker:
    def __init__(self, service, interval=0.5):
        self.service = service
        self.interval = interval
        self.stop_event = Event()
        self.thread = None

    def start(self):
        if self.thread and self.thread.is_alive():
            return
        self.service.recover_interrupted()
        self.stop_event.clear()
        self.thread = Thread(target=self._run, name='familyos-agent-jobs', daemon=True)
        self.thread.start()

    def _run(self):
        while not self.stop_event.is_set():
            try:
                job = self.service.claim_next()
                if job:
                    self.service.execute(job)
                    continue
            except Exception:
                logging.getLogger(__name__).exception('Agent job worker cycle failed')
            self.stop_event.wait(self.interval)

    def stop(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=5)
