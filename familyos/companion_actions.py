from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import re
import sqlite3
import time
from typing import Callable
from uuid import UUID, uuid4

from .households import ValidationError
from .life import LifeService


TOOL_SPECS = {
    "propose_task_create": {
        "action_type": "task.create",
        "description": "Propose adding one task. This never writes until the user confirms the card.",
        "parameters": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "maxLength": 160},
                "due_at": {"type": "string", "description": "Optional ISO 8601 timestamp with timezone."},
            },
            "required": ["title"],
            "additionalProperties": False,
        },
    },
    "propose_task_complete": {
        "action_type": "task.complete",
        "description": "Propose completing one existing writable task by its supplied task ID.",
        "parameters": {
            "type": "object",
            "properties": {"task_id": {"type": "string", "format": "uuid"}},
            "required": ["task_id"],
            "additionalProperties": False,
        },
    },
    "propose_calendar_create": {
        "action_type": "calendar.create",
        "description": "Propose adding one calendar event. This never writes until the user confirms the card.",
        "parameters": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "maxLength": 160},
                "starts_at": {"type": "string", "description": "ISO 8601 timestamp with timezone."},
                "ends_at": {"type": "string", "description": "ISO 8601 timestamp with timezone."},
            },
            "required": ["title", "starts_at", "ends_at"],
            "additionalProperties": False,
        },
    },
}

READ_TOOL_SPECS = {
    "read_today": {
        "action_type": "today.read",
        "description": "Read the current member's visible tasks, calendar events and habits for today. No mutation; call at most once per answer.",
        "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
    },
}


class FunctionTimeoutError(ValidationError):
    """The bounded local Function could not finish before its configured deadline."""


LOCAL_ACTIONS = frozenset(spec["action_type"] for spec in TOOL_SPECS.values())
LOCAL_READS = frozenset(spec["action_type"] for spec in READ_TOOL_SPECS.values())

class CompanionActionService:
    """Turns untrusted model tool calls into member-bound, confirm-before-write proposals."""

    def __init__(self, database, life: LifeService, *, monotonic: Callable[[], float] | None = None):
        self.database = database
        self.life = life
        self._monotonic = monotonic or time.monotonic

    def tool_definitions(self, member: dict) -> list[dict]:
        with self.database.session() as connection:
            rows = connection.execute(
                "SELECT action_type,category,minimum_permission,scope,risk,handler_key,enabled,health_status "
                "FROM companion_function_registry "
                "WHERE enabled=1 AND health_status='ready' ORDER BY function_key"
            ).fetchall()
        allowed = {
            row["action_type"] for row in rows
            if self._runtime_health(dict(row)) == "ready"
            and (row["minimum_permission"] == "member" or member.get("is_admin"))
            and row["scope"] == "member"
            and row["risk"] == ("read" if row["action_type"] in LOCAL_READS else "confirm")
        }
        return [
            {"type": "function", "function": {
                "name": name,
                "description": spec["description"],
                "parameters": spec["parameters"],
            }}
            for name, spec in {**TOOL_SPECS, **READ_TOOL_SPECS}.items()
            if spec["action_type"] in allowed
        ]

    @staticmethod
    def is_read_tool(tool_call: object) -> bool:
        return isinstance(tool_call, dict) and tool_call.get("name") in READ_TOOL_SPECS

    def execute_read(self, member: dict, tool_call: object) -> dict:
        """One allowlisted, member-scoped read; audit only counts and never titles."""
        if not self.is_read_tool(tool_call):
            raise ValidationError("这项只读 Function 不受支持")
        arguments = tool_call.get("arguments", {})
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except (ValueError, TypeError) as exc:
                raise ValidationError("只读 Function 参数格式不正确") from exc
        if arguments != {}:
            raise ValidationError("只读 Function 不接受额外参数")
        action_type = READ_TOOL_SPECS[tool_call["name"]]["action_type"]
        with self.database.session() as connection:
            row = connection.execute(
                "SELECT function_key,action_type,category,minimum_permission,scope,risk,handler_key,"
                "enabled,health_status,timeout_seconds FROM companion_function_registry WHERE action_type=?",
                (action_type,),
            ).fetchone()
        function = dict(row) if row else None
        if not function or self._runtime_health(function) != "ready" or function["risk"] != "read" or function["scope"] != "member":
            raise ValidationError("这项只读 Function 当前未启用")
        if function["minimum_permission"] == "admin" and not member.get("is_admin"):
            raise PermissionError("当前成员无权使用这项 Function")
        call_id = str(uuid4())
        started = datetime.now(timezone.utc).isoformat()
        deadline = self._monotonic() + int(function["timeout_seconds"])
        with self.database.session() as connection:
            connection.execute(
                "INSERT INTO companion_function_calls(id,function_key,household_id,member_id,source,status,"
                "request_summary_json,created_at,updated_at) VALUES (?,?,?,?,?,'executing','{}',?,?)",
                (call_id, function["function_key"], member["household_id"], member["id"], "model", started, started),
            )
        try:
            today = self.life.today(member)
            self._check_deadline(deadline)
            result = {
                "date": today["date"], "timezone": today["timezone"],
                "tasks": [{"title": item["title"], "due_at": item.get("due_at"), "source": item.get("source", "familyos")}
                          for item in today["tasks"][:8]],
                "events": [{"title": item["title"], "starts_at": item["starts_at"], "source": item.get("source", "familyos")}
                           for item in today["events"][:8]],
                "habits": [{"title": item["title"], "checked_today": bool(item["checked_today"])}
                           for item in today["habits"][:5]],
                "sample_size": {"tasks": len(today["tasks"]), "events": len(today["events"]), "habits": len(today["habits"])},
                "limits": "Only current-member-visible records; lists capped at 8/8/5. Butler tasks are read-only and may be unavailable.",
            }
            finished = datetime.now(timezone.utc).isoformat()
            with self.database.session() as connection:
                connection.execute(
                    "UPDATE companion_function_calls SET status='succeeded',result_summary_json=?,updated_at=?,executed_at=? WHERE id=?",
                    (json.dumps(result["sample_size"], separators=(",", ":")), finished, finished, call_id),
                )
            return result
        except Exception as exc:
            with self.database.session() as connection:
                connection.execute(
                    "UPDATE companion_function_calls SET status='failed',error_code=?,updated_at=? WHERE id=?",
                    (type(exc).__name__[:80], datetime.now(timezone.utc).isoformat(), call_id),
                )
            raise

    def registry(self, member: dict, *, limit: int = 30) -> dict:
        limit = max(1, min(int(limit), 100))
        with self.database.session() as connection:
            functions = connection.execute(
                "SELECT function_key,action_type,category,display_name_zh,display_name_en,"
                "description_zh,description_en,input_schema_json,minimum_permission,scope,risk,"
                "handler_key,enabled,health_status,timeout_seconds FROM companion_function_registry "
                "ORDER BY category,function_key"
            ).fetchall()
            calls = connection.execute(
                "SELECT id,function_key,proposal_id,source,status,request_summary_json,result_summary_json,"
                "error_code,created_at,updated_at,executed_at FROM companion_function_calls "
                "WHERE member_id=? AND household_id=? ORDER BY created_at DESC LIMIT ?",
                (member["id"], member["household_id"], limit),
            ).fetchall()
        return {
            "functions": [self._function_view(dict(row)) for row in functions],
            "calls": [self._call_view(dict(row)) for row in calls],
            "connections": {"mcp": 0, "webhook": 0},
        }

    def from_tool_call(self, member: dict, tool_call: object, *, now: datetime | None = None) -> dict | None:
        if not isinstance(tool_call, dict):
            return None
        name = tool_call.get("name")
        spec = TOOL_SPECS.get(name)
        if not spec:
            return None
        action_type = spec["action_type"]
        arguments = tool_call.get("arguments", {})
        if isinstance(arguments, str):
            if len(arguments) > 4000:
                raise ValidationError("Bito 提议的内容过长")
            try:
                arguments = json.loads(arguments)
            except json.JSONDecodeError as exc:
                raise ValidationError("Bito 提议的格式不正确") from exc
        if not isinstance(arguments, dict):
            raise ValidationError("Bito 提议的格式不正确")
        return self._create(member, action_type, arguments, "model", now=now)

    def from_explicit_message(self, member: dict, message: str, *, now: datetime | None = None) -> dict | None:
        matched = re.match(
            r"^(?:帮我)?(?:记录|添加|记下)(?:一个)?任务[：:\s]+(.+)$", message.strip()
        )
        if not matched:
            return None
        title = matched.group(1).strip()
        return self._create(member, "task.create", {"title": title}, "deterministic", now=now)

    def latest_pending(self, member: dict, *, now: datetime | None = None) -> dict | None:
        current = now or datetime.now(timezone.utc)
        with self.database.session() as connection:
            connection.execute(
                "UPDATE companion_action_proposals SET status='expired' "
                "WHERE member_id=? AND status='pending' AND expires_at<=?",
                (member["id"], current.isoformat()),
            )
            connection.execute(
                "UPDATE companion_function_calls SET status='expired',updated_at=? "
                "WHERE member_id=? AND household_id=? AND status='proposed' AND proposal_id IN "
                "(SELECT id FROM companion_action_proposals WHERE member_id=? AND household_id=? AND status='expired')",
                (current.isoformat(), member["id"], member["household_id"], member["id"], member["household_id"]),
            )
            row = connection.execute(
                "SELECT * FROM companion_action_proposals WHERE member_id=? AND household_id=? "
                "AND status='pending' ORDER BY created_at DESC LIMIT 1",
                (member["id"], member["household_id"]),
            ).fetchone()
        return self._view(member, dict(row)) if row else None

    def confirm(self, member: dict, proposal_id: str, *, now: datetime | None = None) -> dict:
        self._proposal_id(proposal_id)
        current = now or datetime.now(timezone.utc)
        record = None
        try:
            with self.database.session() as connection:
                initial = connection.execute(
                    "SELECT * FROM companion_action_proposals WHERE id=? AND member_id=? AND household_id=?",
                    (proposal_id, member["id"], member["household_id"]),
                ).fetchone()
                if not initial:
                    raise ValidationError("确认卡片不存在或不属于当前成员")
                initial_record = dict(initial)
                if initial_record["status"] == "confirmed":
                    return {
                        "proposal": self._view(member, initial_record),
                        "result": json.loads(initial_record["result_json"]),
                    }
                function = self._active_function(member, initial_record["action_type"], connection=connection)
                timeout_seconds = int(function["timeout_seconds"])
                deadline = self._monotonic() + timeout_seconds
                connection.execute(f"PRAGMA busy_timeout = {timeout_seconds * 1000}")
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    "SELECT * FROM companion_action_proposals WHERE id=? AND member_id=? AND household_id=?",
                    (proposal_id, member["id"], member["household_id"]),
                ).fetchone()
                if not row:
                    raise ValidationError("确认卡片不存在或不属于当前成员")
                record = dict(row)
                if record["status"] == "confirmed":
                    return {"proposal": self._view(member, record), "result": json.loads(record["result_json"])}
                if record["status"] != "pending":
                    raise ValidationError("这张确认卡片已经失效")
                if record["expires_at"] <= current.isoformat():
                    connection.execute("UPDATE companion_action_proposals SET status='expired' WHERE id=?", (proposal_id,))
                    connection.execute(
                        "UPDATE companion_function_calls SET status='expired',updated_at=? WHERE proposal_id=?",
                        (current.isoformat(), proposal_id),
                    )
                    connection.commit()
                    raise ValidationError("这张确认卡片已超过 10 分钟，请重新告诉 Bito")
                connection.set_progress_handler(lambda: int(self._monotonic() >= deadline), 100)
                changed = connection.execute(
                    "UPDATE companion_action_proposals SET status='executing',confirmed_at=? "
                    "WHERE id=? AND status='pending'",
                    (current.isoformat(), proposal_id),
                ).rowcount
                if changed != 1:
                    raise ValidationError("这张确认卡片正在处理，请不要重复点击")
                connection.execute(
                    "UPDATE companion_function_calls SET status='executing',updated_at=? WHERE proposal_id=?",
                    (current.isoformat(), proposal_id),
                )
                payload = json.loads(record["payload_json"])
                result = self._execute_in_transaction(connection, member, record["action_type"], payload)
                self._check_deadline(deadline)
                executed_at = datetime.now(timezone.utc).isoformat()
                result_summary = self._result_summary(result)
                connection.execute(
                    "UPDATE companion_action_proposals SET status='confirmed',result_json=?,executed_at=? WHERE id=?",
                    (json.dumps(result, ensure_ascii=False, separators=(",", ":")), executed_at, proposal_id),
                )
                connection.execute(
                    "UPDATE companion_function_calls SET status='succeeded',result_summary_json=?,"
                    "updated_at=?,executed_at=? WHERE proposal_id=?",
                    (
                        json.dumps(result_summary, ensure_ascii=False, separators=(",", ":")),
                        executed_at, executed_at, proposal_id,
                    ),
                )
                row = connection.execute(
                    "SELECT * FROM companion_action_proposals WHERE id=?", (proposal_id,)
                ).fetchone()
                connection.set_progress_handler(None, 0)
                return {"proposal": self._view(member, dict(row)), "result": result}
        except Exception as exc:
            failure = self._execution_failure(exc)
            with self.database.session() as connection:
                connection.execute(
                    "UPDATE companion_action_proposals SET status='failed' "
                    "WHERE id=? AND member_id=? AND household_id=? AND status IN ('pending','executing')",
                    (proposal_id, member["id"], member["household_id"]),
                )
                connection.execute(
                    "UPDATE companion_function_calls SET status='failed',error_code=?,updated_at=? "
                    "WHERE proposal_id=? AND member_id=? AND household_id=? AND status IN ('proposed','executing')",
                    (
                        type(failure).__name__[:80], datetime.now(timezone.utc).isoformat(), proposal_id,
                        member["id"], member["household_id"],
                    ),
                )
            if failure is exc:
                raise
            raise failure from exc

    def reject(self, member: dict, proposal_id: str) -> dict:
        self._proposal_id(proposal_id)
        with self.database.session() as connection:
            changed = connection.execute(
                "UPDATE companion_action_proposals SET status='rejected' "
                "WHERE id=? AND member_id=? AND household_id=? AND status='pending'",
                (proposal_id, member["id"], member["household_id"]),
            ).rowcount
            if changed == 1:
                connection.execute(
                    "UPDATE companion_function_calls SET status='rejected',updated_at=? WHERE proposal_id=?",
                    (datetime.now(timezone.utc).isoformat(), proposal_id),
                )
        if changed != 1:
            raise ValidationError("这张确认卡片不存在、已处理或不属于当前成员")
        return {"id": proposal_id, "status": "rejected"}

    def _create(self, member: dict, action_type: str, payload: dict, source: str, *, now: datetime | None = None) -> dict:
        function = self._active_function(member, action_type)
        normalized = self._normalize(member, action_type, payload)
        current = now or datetime.now(timezone.utc)
        proposal_id = str(uuid4())
        call_id = str(uuid4())
        with self.database.session() as connection:
            connection.execute(
                "UPDATE companion_action_proposals SET status='rejected' WHERE member_id=? AND status='pending'",
                (member["id"],),
            )
            connection.execute(
                "UPDATE companion_function_calls SET status='rejected',updated_at=? "
                "WHERE member_id=? AND household_id=? AND status='proposed'",
                (current.isoformat(), member["id"], member["household_id"]),
            )
            connection.execute(
                "INSERT INTO companion_action_proposals(id,household_id,member_id,action_type,payload_json,source,status,created_at,expires_at) "
                "VALUES (?,?,?,?,?,?,'pending',?,?)",
                (
                    proposal_id, member["household_id"], member["id"], action_type,
                    json.dumps(normalized, ensure_ascii=False, separators=(",", ":")), source,
                    current.isoformat(), (current + timedelta(minutes=10)).isoformat(),
                ),
            )
            connection.execute(
                "INSERT INTO companion_function_calls(id,function_key,proposal_id,household_id,member_id,source,status,"
                "request_summary_json,created_at,updated_at) VALUES (?,?,?,?,?,?,'proposed',?,?,?)",
                (
                    call_id, function["function_key"], proposal_id, member["household_id"], member["id"], source,
                    json.dumps(self._request_summary(normalized), ensure_ascii=False, separators=(",", ":")),
                    current.isoformat(), current.isoformat(),
                ),
            )
            row = connection.execute("SELECT * FROM companion_action_proposals WHERE id=?", (proposal_id,)).fetchone()
        return self._view(member, dict(row))

    def _active_function(self, member: dict, action_type: str, *, connection=None) -> dict:
        if connection is None:
            with self.database.session() as owned_connection:
                return self._active_function(member, action_type, connection=owned_connection)
        row = connection.execute(
            "SELECT function_key,action_type,category,minimum_permission,scope,risk,handler_key,"
            "enabled,health_status,timeout_seconds FROM companion_function_registry WHERE action_type=?",
            (action_type,),
        ).fetchone()
        if not row:
            raise ValidationError("这项 Function 当前未启用")
        function = dict(row)
        if self._runtime_health(function) != "ready" or function["risk"] != "confirm":
            raise ValidationError("这项 Function 的执行策略不受支持")
        if function["minimum_permission"] == "admin" and not member.get("is_admin"):
            raise PermissionError("当前成员无权使用这项 Function")
        if function["scope"] != "member":
            raise ValidationError("这项 Function 尚未开放到当前成员范围")
        return function

    def _function_view(self, record: dict) -> dict:
        try:
            schema = json.loads(record.pop("input_schema_json"))
        except (TypeError, json.JSONDecodeError):
            schema = {}
        properties = schema.get("properties", {}) if isinstance(schema, dict) else {}
        required = set(schema.get("required", [])) if isinstance(schema, dict) else set()
        return {
            "key": record["function_key"],
            "action_type": record["action_type"],
            "category": record["category"],
            "name": {"zh-CN": record["display_name_zh"], "en-US": record["display_name_en"]},
            "description": {"zh-CN": record["description_zh"], "en-US": record["description_en"]},
            "inputs": [
                {"name": name, "required": name in required}
                for name in properties
                if isinstance(name, str)
            ],
            "minimum_permission": record["minimum_permission"],
            "scope": record["scope"],
            "risk": record["risk"],
            "enabled": bool(record["enabled"]),
            "health_status": self._runtime_health(record),
            "timeout_seconds": record["timeout_seconds"],
        }

    @staticmethod
    def _runtime_health(record: dict) -> str:
        if not record.get("enabled") or record.get("health_status") == "disabled":
            return "disabled"
        if record.get("health_status") != "ready":
            return "unconfigured"
        if record.get("category") != "local":
            return "unconfigured"
        action_type = record.get("action_type")
        if action_type not in LOCAL_ACTIONS | LOCAL_READS or record.get("handler_key") != action_type:
            return "unconfigured"
        return "ready"

    @staticmethod
    def _call_view(record: dict) -> dict:
        return {
            "id": record["id"],
            "function_key": record["function_key"],
            "proposal_id": record["proposal_id"],
            "source": record["source"],
            "status": record["status"],
            "request": json.loads(record["request_summary_json"]),
            "result": json.loads(record["result_summary_json"]) if record["result_summary_json"] else None,
            "error_code": record["error_code"],
            "created_at": record["created_at"],
            "updated_at": record["updated_at"],
            "executed_at": record["executed_at"],
        }

    @staticmethod
    def _request_summary(payload: dict) -> dict:
        return {key: payload[key] for key in ("title", "task_id", "due_at", "starts_at", "ends_at") if payload.get(key)}

    @staticmethod
    def _result_summary(result: object) -> dict:
        if not isinstance(result, dict):
            return {"completed": True}
        return {
            key: value for key, value in result.items()
            if key in {"id", "title", "status", "due_at", "starts_at", "ends_at"}
            and isinstance(value, (str, int, float, bool, type(None)))
        }

    def _normalize(self, member: dict, action_type: str, payload: dict) -> dict:
        if action_type == "task.create":
            if set(payload) - {"title", "due_at"}:
                raise ValidationError("任务提议包含不支持的字段")
            return {
                "title": LifeService._text(payload.get("title"), "任务名称", 160),
                "due_at": LifeService._optional_datetime(payload.get("due_at")),
                "visibility": "private",
            }
        if action_type == "calendar.create":
            if set(payload) - {"title", "starts_at", "ends_at"}:
                raise ValidationError("日程提议包含不支持的字段")
            starts_at = LifeService._required_datetime(payload.get("starts_at"), "开始时间")
            ends_at = LifeService._required_datetime(payload.get("ends_at"), "结束时间")
            if ends_at <= starts_at:
                raise ValidationError("结束时间必须晚于开始时间")
            return {
                "title": LifeService._text(payload.get("title"), "日程名称", 160),
                "starts_at": starts_at,
                "ends_at": ends_at,
                "visibility": "private",
            }
        if action_type == "task.complete":
            if set(payload) != {"task_id"}:
                raise ValidationError("完成任务提议的格式不正确")
            task_id = self._proposal_id(payload.get("task_id"))
            with self.database.session() as connection:
                row = connection.execute(
                    "SELECT title,owner_member_id,status FROM tasks WHERE id=? AND household_id=?",
                    (task_id, member["household_id"]),
                ).fetchone()
            if not row or (row["owner_member_id"] != member["id"] and not member["is_admin"]):
                raise ValidationError("任务不存在或你无权完成")
            if row["status"] != "open":
                raise ValidationError("这个任务已经完成")
            return {"task_id": task_id, "title": row["title"]}
        raise ValidationError("Bito 提议的操作不受支持")

    def _execute_in_transaction(self, connection, member: dict, action_type: str, payload: dict) -> dict:
        current = datetime.now(timezone.utc).isoformat()
        if action_type == "task.create":
            item_id = str(uuid4())
            connection.execute(
                "INSERT INTO tasks(id,household_id,owner_member_id,title,due_at,status,visibility,created_at,updated_at) "
                "VALUES (?,?,?,?,?,'open',?,?,?)",
                (
                    item_id, member["household_id"], member["id"], payload["title"],
                    payload.get("due_at"), payload["visibility"], current, current,
                ),
            )
            LifeService._audit(
                connection, member, "task.created", item_id,
                {"title": payload["title"], "due_at": payload.get("due_at")},
            )
            return {
                "id": item_id, "title": payload["title"], "due_at": payload.get("due_at"),
                "status": "open", "visibility": payload["visibility"],
            }
        if action_type == "task.complete":
            row = connection.execute(
                "SELECT owner_member_id,status FROM tasks WHERE id=? AND household_id=?",
                (payload["task_id"], member["household_id"]),
            ).fetchone()
            if not row or (row["owner_member_id"] != member["id"] and not member["is_admin"]):
                raise ValidationError("任务不存在或你无权完成")
            if row["status"] != "open":
                raise ValidationError("这个任务已经完成")
            connection.execute(
                "UPDATE tasks SET status='completed',completed_at=?,updated_at=? WHERE id=? AND status='open'",
                (current, current, payload["task_id"]),
            )
            LifeService._audit(connection, member, "task.completed", payload["task_id"], {})
            return {"id": payload["task_id"], "status": "completed", "completed_at": current}
        if action_type == "calendar.create":
            item_id = str(uuid4())
            connection.execute(
                "INSERT INTO calendar_events(id,household_id,owner_member_id,title,starts_at,ends_at,visibility,source,created_at) "
                "VALUES (?,?,?,?,?,?,?,'manual',?)",
                (
                    item_id, member["household_id"], member["id"], payload["title"],
                    payload["starts_at"], payload["ends_at"], payload["visibility"], current,
                ),
            )
            LifeService._audit(connection, member, "calendar_event.created", item_id, {"title": payload["title"]})
            return {
                "id": item_id, "title": payload["title"], "starts_at": payload["starts_at"],
                "ends_at": payload["ends_at"], "visibility": payload["visibility"],
            }
        raise ValidationError("Bito 提议的操作不受支持")

    def _check_deadline(self, deadline: float) -> None:
        if self._monotonic() >= deadline:
            raise FunctionTimeoutError("Function 执行超时，没有保存任何改动")

    def _execution_failure(self, exc: Exception) -> Exception:
        if isinstance(exc, FunctionTimeoutError):
            return exc
        if isinstance(exc, sqlite3.OperationalError) and str(exc).casefold() in {"interrupted", "database is locked"}:
            return FunctionTimeoutError("Function 执行超时，没有保存任何改动")
        return exc

    def _view(self, member: dict, record: dict) -> dict:
        payload = json.loads(record["payload_json"])
        action_type = record["action_type"]
        title = {"task.create": "新增任务", "task.complete": "完成任务", "calendar.create": "新增日程"}[action_type]
        if action_type == "task.create":
            fields = [{"label": "任务", "value": payload["title"]}]
            if payload.get("due_at"):
                fields.append({"label": "截止", "value": self._local_time(payload["due_at"], member)})
        elif action_type == "task.complete":
            fields = [{"label": "任务", "value": payload["title"]}, {"label": "变化", "value": "标记为已完成"}]
        else:
            fields = [
                {"label": "日程", "value": payload["title"]},
                {"label": "开始", "value": self._local_time(payload["starts_at"], member)},
                {"label": "结束", "value": self._local_time(payload["ends_at"], member)},
            ]
        return {
            "id": record["id"], "action_type": action_type, "title": title, "fields": fields,
            "status": record["status"], "expires_at": record["expires_at"],
        }

    @staticmethod
    def _local_time(value: str, member: dict) -> str:
        from zoneinfo import ZoneInfo
        parsed = datetime.fromisoformat(value).astimezone(ZoneInfo(member["household_timezone"]))
        return f"{parsed:%m月%d日 %H:%M}"

    @staticmethod
    def _proposal_id(value: object) -> str:
        if not isinstance(value, str):
            raise ValidationError("确认卡片编号不正确")
        try:
            return str(UUID(value))
        except ValueError as exc:
            raise ValidationError("确认卡片编号不正确") from exc
