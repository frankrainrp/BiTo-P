"""Persistent member-owned schedules. Runs use the existing bounded Agent queue."""
from datetime import datetime, timedelta, timezone
import json
from threading import Event, Thread
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .agent_jobs import AgentJobService
from .households import ValidationError


class AgentScheduleService:
    def __init__(self, database):
        self.database = database

    @staticmethod
    def next_times(spec, after, count=3):
        zone = ZoneInfo(spec['timezone'])
        if spec['schedule_kind'] == 'once':
            point = datetime.fromisoformat(spec['one_time_at'])
            return [point.astimezone(timezone.utc).isoformat()] if point > after else []
        hour, minute = map(int, spec['local_time'].split(':'))
        result = []
        first_day = after.astimezone(zone).date()
        for offset in range(32):
            day = first_day + timedelta(days=offset)
            if spec['schedule_kind'] == 'weekly' and day.weekday() not in spec['weekdays']:
                continue
            local = datetime(day.year, day.month, day.day, hour, minute, tzinfo=zone, fold=0)
            utc = local.astimezone(timezone.utc)
            # Skip a nonexistent DST wall time; repeated wall times use fold=0 once.
            if utc.astimezone(zone).replace(tzinfo=None) != local.replace(tzinfo=None):
                continue
            if utc > after:
                result.append(utc.isoformat())
                if len(result) == count:
                    break
        return result

    def clean(self, member, payload):
        allowed = {'title','instructions','schedule_kind','timezone','local_time','weekdays','one_time_at','enabled','version','execution_kind'}
        if not isinstance(payload, dict) or set(payload) - allowed:
            raise ValidationError('定时任务包含不支持的字段')
        instructions = AgentJobService._clean_text(payload.get('instructions'), '任务说明', 1000)
        execution = payload.get('execution_kind', 'agent')
        if execution not in ('agent', 'webhook'):
            raise ValidationError('任务类型不正确')
        title = AgentJobService._clean_text(payload.get('title') or instructions[:60], '任务名称', 120)
        kind = payload.get('schedule_kind')
        if kind not in ('once','daily','weekly'):
            raise ValidationError('请选择指定时刻、每日或每周')
        zone = payload.get('timezone') or member['household_timezone']
        try:
            if not isinstance(zone, str) or len(zone) > 80:
                raise ValueError()
            ZoneInfo(zone)
        except (ValueError, ZoneInfoNotFoundError):
            raise ValidationError('时区不正确')
        local_time = payload.get('local_time', '08:00')
        try:
            if not isinstance(local_time, str) or len(local_time) != 5 or local_time[2] != ':':
                raise ValueError()
            datetime.strptime(local_time, '%H:%M')
        except ValueError:
            raise ValidationError('时间必须是 HH:MM')
        days = payload.get('weekdays', [])
        if not isinstance(days, list) or len(days) > 7 or any(type(d) is not int or d not in range(7) for d in days):
            raise ValidationError('星期选择不正确')
        if kind == 'weekly' and not days:
            raise ValidationError('至少选择一天')
        once = payload.get('one_time_at')
        if kind == 'once':
            try:
                point = datetime.fromisoformat(once)
                if point.tzinfo is None or (point <= datetime.now(timezone.utc) and payload.get('enabled', True)):
                    raise ValueError()
                once = point.astimezone(timezone.utc).isoformat()
            except (ValueError, TypeError):
                raise ValidationError('指定时间必须是带时区的未来时间')
        else:
            once = None
        enabled = payload.get('enabled', True)
        if type(enabled) is not bool:
            raise ValidationError('启用状态不正确')
        return dict(title=title, instructions=instructions, schedule_kind=kind, timezone=zone, execution_kind=execution,
                    local_time=local_time, weekdays=sorted(set(days)), one_time_at=once, enabled=enabled)

    @staticmethod
    def public(row):
        spec = dict(row)
        spec.pop('member_id', None)
        spec.pop('household_id', None)
        spec['weekdays'] = json.loads(spec.pop('weekdays_json'))
        spec['enabled'] = bool(spec['enabled'])
        return spec

    def list(self, member):
        with self.database.session() as conn:
            rows = conn.execute('SELECT * FROM agent_task_definitions WHERE member_id=? AND household_id=? ORDER BY created_at DESC LIMIT 100', (member['id'], member['household_id'])).fetchall()
            occurrences = conn.execute('SELECT o.id,o.definition_id,o.scheduled_for,o.status,o.job_id,o.title FROM agent_task_occurrences o JOIN agent_task_definitions d ON d.id=o.definition_id WHERE d.member_id=? AND d.household_id=? ORDER BY o.scheduled_for DESC LIMIT 50', (member['id'], member['household_id'])).fetchall()
        specs = [self.public(row) for row in rows]
        for spec in specs:
            spec['next_occurrences'] = self.next_times(spec, datetime.now(timezone.utc)) if spec['enabled'] else []
        return {'definitions': specs, 'occurrences': [dict(row) for row in occurrences], 'timezone': member['household_timezone']}

    def save(self, member, payload, definition_id=None):
        now = datetime.now(timezone.utc)
        spec = self.clean(member, payload)
        times = self.next_times(spec, now)
        with self.database.session() as conn:
            conn.execute('BEGIN IMMEDIATE')
            if definition_id:
                old = conn.execute('SELECT * FROM agent_task_definitions WHERE id=? AND member_id=? AND household_id=?', (definition_id, member['id'], member['household_id'])).fetchone()
                if not old:
                    raise ValidationError('没有找到这项定时任务')
                if type(payload.get('version')) is not int or payload['version'] != old['version']:
                    raise ValidationError('任务已经变更，请刷新后再保存')
                conn.execute("UPDATE agent_task_occurrences SET status='skipped' WHERE definition_id=? AND status='waiting'", (definition_id,))
                conn.execute('UPDATE agent_task_definitions SET title=?,instructions=?,schedule_kind=?,timezone=?,local_time=?,weekdays_json=?,one_time_at=?,enabled=?,next_run_at=?,version=version+1,updated_at=? WHERE id=?',
                             (spec['title'],spec['instructions'],spec['schedule_kind'],spec['timezone'],spec['local_time'],json.dumps(spec['weekdays']),spec['one_time_at'],int(spec['enabled']),times[0] if times else None,now.isoformat(),definition_id))
            else:
                if conn.execute('SELECT COUNT(*) FROM agent_task_definitions WHERE member_id=?', (member['id'],)).fetchone()[0] >= 100:
                    raise ValidationError('最多保存 100 项定时任务')
                definition_id = str(uuid4())
                conn.execute('INSERT INTO agent_task_definitions(id,household_id,member_id,title,instructions,schedule_kind,timezone,local_time,weekdays_json,one_time_at,next_run_at,enabled,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                             (definition_id,member['household_id'],member['id'],spec['title'],spec['instructions'],spec['schedule_kind'],spec['timezone'],spec['local_time'],json.dumps(spec['weekdays']),spec['one_time_at'],times[0] if times else None,int(spec['enabled']),now.isoformat(),now.isoformat()))
            conn.execute('UPDATE agent_task_definitions SET execution_kind=? WHERE id=?', (spec['execution_kind'], definition_id))
        return self.list(member)

    def tick(self, now=None):
        now = now or datetime.now(timezone.utc)
        stamp = now.isoformat()
        with self.database.session() as conn:
            conn.execute('BEGIN IMMEDIATE')
            due = conn.execute('SELECT * FROM agent_task_definitions WHERE enabled=1 AND next_run_at<=? ORDER BY next_run_at LIMIT 100', (stamp,)).fetchall()
            for row in due:
                spec = self.public(row)
                scheduled = datetime.fromisoformat(row['next_run_at'])
                # Five-minute grace covers normal restarts, not stale news catch-up.
                state = 'waiting' if (now - scheduled).total_seconds() <= 300 else 'missed'
                conn.execute('INSERT OR IGNORE INTO agent_task_occurrences(id,definition_id,scheduled_for,definition_version,title,instructions,status,created_at) VALUES (?,?,?,?,?,?,?,?)',
                             (str(uuid4()),row['id'],row['next_run_at'],row['version'],row['title'],row['instructions'],state,stamp))
                future = self.next_times(spec, now, 1)
                conn.execute("UPDATE agent_task_occurrences SET execution_kind=? WHERE definition_id=? AND scheduled_for=? AND status='waiting'",
                             (row['execution_kind'], row['id'], row['next_run_at']))
                conn.execute('UPDATE agent_task_definitions SET next_run_at=?,enabled=?,updated_at=? WHERE id=?', (future[0] if future else None,int(bool(future)),stamp,row['id']))
            waiting = conn.execute("SELECT o.*,d.member_id,d.household_id FROM agent_task_occurrences o JOIN agent_task_definitions d ON d.id=o.definition_id WHERE o.status='waiting' ORDER BY o.scheduled_for LIMIT 100").fetchall()
            for row in waiting:
                if (now - datetime.fromisoformat(row['scheduled_for'])).total_seconds() > 300:
                    conn.execute("UPDATE agent_task_occurrences SET status='missed' WHERE id=?", (row['id'],))
                    continue
                if conn.execute("SELECT 1 FROM agent_jobs WHERE member_id=? AND status IN ('queued','running')", (row['member_id'],)).fetchone():
                    continue
                job_id = str(uuid4())
                conn.execute("INSERT INTO agent_jobs(id,household_id,member_id,title,instructions,status,stage,progress,created_at,updated_at) VALUES (?,?,?,?,?,'queued','queued',0,?,?)",
                             (job_id,row['household_id'],row['member_id'],row['title'],row['instructions'],stamp,stamp))
                AgentJobService._event(conn, job_id, row['member_id'], 'queued', '定时任务已进入队列', 0, stamp)
                conn.execute('UPDATE agent_jobs SET execution_kind=? WHERE id=?', (row['execution_kind'], job_id))
                conn.execute("UPDATE agent_task_occurrences SET status='dispatched',job_id=? WHERE id=? AND status='waiting'", (job_id,row['id']))


class AgentScheduleWorker:
    def __init__(self, service):
        self.service, self.stop_event, self.thread = service, Event(), None

    def start(self):
        if self.thread and self.thread.is_alive():
            return
        self.stop_event.clear()
        self.thread = Thread(target=self._run, daemon=True, name='familyos-schedules')
        self.thread.start()

    def _run(self):
        import logging
        while not self.stop_event.is_set():
            try:
                self.service.tick()
            except Exception:
                logging.getLogger(__name__).exception('Schedule dispatch failed')
            self.stop_event.wait(10)

    def stop(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=5)
