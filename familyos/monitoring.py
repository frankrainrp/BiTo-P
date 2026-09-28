"""Member-private, deterministic read-only website subscriptions.

No browser automation or model output can execute code through this module.
Outbound targets supplied by members are public HTTP(S), DNS checked on each
request and connected by the already checked numeric address. Notifications
only use operator-configured destinations and never expose their credentials.
"""
from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
from html.parser import HTMLParser
import http.client
import ipaddress
import json
import math
import os
from queue import Queue, Empty
import re
import socket
import ssl
import threading
import time
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

from .db import Database
from .households import ValidationError

MAX_BYTES = 262144
FETCH_TIMEOUT = 12
_DNS_SLOTS = threading.BoundedSemaphore(4)


def _iso(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat()


def validate_public_url(value: object) -> tuple[str, object]:
    if not isinstance(value, str) or not 1 <= len(value) <= 2048:
        raise ValidationError("请填写有效的公开网页或 JSON 地址")
    if any(ord(char) <= 32 or ord(char) == 127 for char in value) or "\\" in value:
        raise ValidationError("地址包含无效字符")
    try:
        parsed = urlsplit(value)
        host, port = parsed.hostname, parsed.port
    except ValueError as exc:
        raise ValidationError("地址格式不正确") from exc
    if (parsed.scheme not in {"http", "https"} or not host or parsed.username is not None
            or parsed.password is not None or parsed.fragment or port not in {None, 80, 443}):
        raise ValidationError("仅支持无账号密码的公开 HTTP/HTTPS 地址（80/443 端口）")
    try:
        host = host.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise ValidationError("域名格式不正确") from exc
    if not re.fullmatch(r"[A-Za-z0-9.:-]+", host):
        raise ValidationError("域名格式不正确")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        if "." not in host or host.rstrip(".").lower().endswith((".localhost", ".local", ".internal", ".test", ".invalid")):
            raise ValidationError("监控地址必须是公开网站")
    else:
        if not _is_public(address):
            raise ValidationError("不能监控本机、内网或保留地址")
    authority = f"[{host}]" if ":" in host else host
    if port is not None:
        authority += f":{port}"
    normalized = urlunsplit((parsed.scheme, authority, parsed.path or "/", parsed.query, ""))
    return normalized, urlsplit(normalized)


def _is_public(address) -> bool:
    if isinstance(address, ipaddress.IPv6Address):
        # Reject mapped/transition addresses, even if a public v4 is embedded.
        if address.ipv4_mapped or address.sixtofour or address.teredo:
            return False
    return address.is_global and not address.is_multicast and not address.is_reserved


def _resolve_public(host: str, port: int) -> tuple:
    if not _DNS_SLOTS.acquire(blocking=False):
        raise ValidationError("域名解析繁忙，请稍后重试")
    result: Queue = Queue(maxsize=1)

    def resolve():
        try:
            result.put(socket.getaddrinfo(host, port, type=socket.SOCK_STREAM))
        except Exception:
            result.put(None)
        finally:
            _DNS_SLOTS.release()

    threading.Thread(target=resolve, daemon=True, name="monitor-dns").start()
    try:
        addresses = result.get(timeout=4)
    except Empty as exc:
        raise ValidationError("网站域名解析超时") from exc
    if not addresses:
        raise ValidationError("网站域名无法解析")
    for address in addresses:
        if address[0] not in {socket.AF_INET, socket.AF_INET6} or not _is_public(ipaddress.ip_address(address[4][0])):
            raise ValidationError("网站解析到了非公开地址，已停止检查")
    return addresses[0]


def fetch_public(url: str) -> tuple[str, str]:
    _, parsed = validate_public_url(url)
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    address = _resolve_public(parsed.hostname, port)
    started = time.monotonic()
    raw_socket = socket.socket(address[0], address[1], address[2])
    connection = http.client.HTTPConnection(parsed.hostname, port, timeout=FETCH_TIMEOUT)
    def expire():
        try:
            raw_socket.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        raw_socket.close()
    deadline = threading.Timer(FETCH_TIMEOUT, expire)
    deadline.daemon = True
    deadline.start()
    try:
        raw_socket.settimeout(FETCH_TIMEOUT)
        raw_socket.connect(address[4])
        if parsed.scheme == "https":
            raw_socket = ssl.create_default_context().wrap_socket(raw_socket, server_hostname=parsed.hostname)
        connection.sock = raw_socket
        path = parsed.path or "/"
        if parsed.query:
            path += "?" + parsed.query
        connection.request("GET", path, headers={"User-Agent": "FamilyOS-Monitor/1.0", "Accept": "text/html,application/json,text/plain", "Accept-Encoding": "identity"})
        response = connection.getresponse()
        if 300 <= response.status < 400:
            raise ValidationError("网站返回跳转，请填写跳转后的最终公开地址")
        if response.status != 200:
            raise ValidationError(f"网站返回 HTTP {response.status}")
        if response.getheader("Content-Encoding", "identity").lower() not in {"", "identity"}:
            raise ValidationError("网站返回压缩内容，暂不支持此数据源")
        content_type = response.getheader("Content-Type", "text/plain").split(";", 1)[0].lower().strip()
        if content_type not in {"text/html", "text/plain", "application/json", "application/ld+json"} and not content_type.endswith("+json"):
            raise ValidationError("仅支持文本网页或 JSON 数据")
        body = bytearray()
        while True:
            remaining = FETCH_TIMEOUT - (time.monotonic() - started)
            if remaining <= 0:
                raise ValidationError("网站读取超时")
            raw_socket.settimeout(remaining)
            chunk = response.read1(min(16384, MAX_BYTES + 1 - len(body)))
            body.extend(chunk)
            if len(body) > MAX_BYTES:
                raise ValidationError("网页超过 256 KB，请改用较小的 JSON 数据地址")
            if not chunk:
                break
        return body.decode("utf-8", errors="replace"), content_type
    except (OSError, http.client.HTTPException) as exc:
        raise ValidationError("网站连接失败或证书无效，请稍后重试") from exc
    finally:
        deadline.cancel()
        connection.close()
        raw_socket.close()


class _VisibleText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "noscript", "template"}:
            self.hidden += 1

    def handle_endtag(self, tag):
        if tag in {"script", "style", "noscript", "template"}:
            self.hidden = max(0, self.hidden - 1)

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def extract_value(body: str, source_type: str, json_path: str, content_type: str) -> str:
    if source_type == "json":
        try:
            value = json.loads(body)
            for key in json_path.split(".") if json_path else []:
                value = value[int(key)] if isinstance(value, list) and key.isdecimal() else value[key]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise ValidationError("JSON 路径不存在或响应不是有效 JSON") from exc
        if isinstance(value, (dict, list)) or value is None or isinstance(value, bool):
            raise ValidationError("JSON 路径必须指向一个数字或文本")
        return str(value).strip()[:12000]
    if "html" in content_type:
        parser = _VisibleText()
        parser.feed(body)
        return " ".join(" ".join(parser.parts).split())[:12000]
    return " ".join(body.split())[:12000]


def _numeric(value: str) -> float:
    # Require the entire selected value to be a number. No guessing which
    # number on a property/stock page represents the requested price.
    candidate = value.strip().replace(",", "")
    if not re.fullmatch(r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?", candidate):
        raise ValidationError("选中内容不是单一数字；数值条件请使用 JSON 数字字段")
    result = float(candidate)
    if not math.isfinite(result):
        raise ValidationError("数值必须是有限数字")
    return result


class NotificationChannels:
    """Operator configuration only; no destination/token comes from the UI."""
    def __init__(self, environ=None):
        env = os.environ if environ is None else environ
        self.webhook = env.get("FAMILYOS_MONITOR_WEBHOOK_URL", "").strip()
        self.token = env.get("FAMILYOS_TELEGRAM_BOT_TOKEN", "").strip()
        self.chat = env.get("FAMILYOS_TELEGRAM_CHAT_ID", "").strip()

    def status(self) -> dict:
        return {"in_app": {"ready": True}, "webhook": {"ready": bool(self.webhook)}, "telegram": {"ready": bool(self.token and self.chat)}}

    def deliver(self, channel: str, alert: dict) -> str:
        if channel == "in_app":
            return "in_app"
        if not self.status().get(channel, {}).get("ready"):
            return "failed"
        from urllib.request import Request, build_opener, HTTPRedirectHandler, ProxyHandler

        class NoRedirect(HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                return None

        try:
            if channel == "telegram":
                if not re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]+", self.token):
                    return "failed"
                url = "https://api.telegram.org/bot" + self.token + "/sendMessage"
                payload = {"chat_id": self.chat, "text": alert["title"] + "\n" + alert["message"][:3000]}
            else:
                url = self.webhook
                parsed = urlsplit(url)
                if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
                    return "failed"
                payload = {"event": "familyos.monitor.triggered", "alert": alert}
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            request = Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST")
            with build_opener(ProxyHandler({}), NoRedirect()).open(request, timeout=8) as response:
                return "sent" if 200 <= response.status < 300 else "failed"
        except Exception:
            # Do not persist exception text: it may contain webhook or bot secrets.
            return "failed"


class MonitoringService:
    def __init__(self, database: Database, *, fetcher=None, channels=None, clock=None):
        self.database = database
        self.fetcher = fetcher or fetch_public
        self.channels = channels or NotificationChannels()
        self.clock = clock or time.time

    def channel_status(self) -> dict:
        return self.channels.status()

    @staticmethod
    def _public(row) -> dict:
        fields = ("id", "title", "url", "source_type", "json_path", "condition", "target", "interval_seconds", "cooldown_seconds", "enabled", "channel", "created_at", "updated_at", "last_checked_at", "last_value", "last_status", "last_error", "next_check_at")
        value = {key: row[key] for key in fields}
        value["enabled"] = bool(value["enabled"])
        value["next_check_at"] = _iso(value["next_check_at"])
        return value

    @staticmethod
    def _owned(connection, member, subscription_id):
        row = connection.execute("SELECT * FROM monitor_subscriptions WHERE id=? AND member_id=? AND household_id=?", (subscription_id, member["id"], member["household_id"])).fetchone()
        if not row:
            raise ValidationError("监控不存在或不属于当前成员")
        return row

    def list_subscriptions(self, member: dict) -> list:
        with self.database.session() as connection:
            rows = connection.execute("SELECT * FROM monitor_subscriptions WHERE member_id=? AND household_id=? ORDER BY created_at DESC", (member["id"], member["household_id"])).fetchall()
        return [self._public(row) for row in rows]

    def _validate(self, payload, current=None) -> dict:
        if not isinstance(payload, dict):
            raise ValidationError("监控配置必须是对象")
        data = dict(current or {})
        data.update(payload)
        title = data.get("title", "")
        if not isinstance(title, str) or not 1 <= len(title.strip()) <= 120:
            raise ValidationError("请填写 1–120 字的监控名称")
        url, _ = validate_public_url(data.get("url"))
        source = data.get("source_type", "text")
        condition = data.get("condition", "change")
        path, target = data.get("json_path", ""), data.get("target", "")
        if source not in {"text", "json"} or condition not in {"change", "contains", "above", "below"}:
            raise ValidationError("数据类型或触发条件不受支持")
        if not isinstance(path, str) or len(path) > 200 or (path and not re.fullmatch(r"[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*", path)):
            raise ValidationError("JSON 路径请使用 quote.price 或 items.0.price 形式")
        if not isinstance(target, (str, int, float)) or isinstance(target, bool) or len(str(target)) > 500:
            raise ValidationError("目标值格式不正确")
        target = str(target).strip()
        if condition == "contains" and not target:
            raise ValidationError("请填写要匹配的文字")
        if condition in {"above", "below"}:
            _numeric(target)
        values = {}
        for name, default in (("interval_seconds", 3600), ("cooldown_seconds", 3600)):
            raw = data.get(name, default)
            if isinstance(raw, bool):
                raise ValidationError("检查间隔与通知冷却必须为 60–604800 秒")
            try:
                number = int(raw)
            except (ValueError, TypeError, OverflowError) as exc:
                raise ValidationError("检查间隔与通知冷却必须为 60–604800 秒") from exc
            if number != float(raw) or not 60 <= number <= 604800:
                raise ValidationError("检查间隔与通知冷却必须为 60–604800 秒")
            values[name] = number
        enabled = data.get("enabled", True)
        if type(enabled) is not bool and not (current and enabled in (0, 1)):
            raise ValidationError("启用状态必须为 true 或 false")
        channel = data.get("channel", "in_app")
        if channel not in {"in_app", "webhook", "telegram"}:
            raise ValidationError("通知方式不受支持")
        if channel != "in_app" and not self.channel_status()[channel]["ready"]:
            raise ValidationError("此通知方式尚未由服务器配置，请先选择站内通知")
        return {"title": title.strip(), "url": url, "source_type": source, "json_path": path, "condition": condition, "target": target, **values, "enabled": int(enabled), "channel": channel}

    def create_subscription(self, member: dict, payload: dict) -> dict:
        data = self._validate(payload)
        stamp = self.clock()
        item_id = str(uuid4())
        with self.database.session() as connection:
            connection.execute("BEGIN IMMEDIATE")
            count = connection.execute("SELECT COUNT(*) FROM monitor_subscriptions WHERE member_id=?", (member["id"],)).fetchone()[0]
            if count >= 30:
                raise ValidationError("每位成员最多保存 30 个监控")
            fields = ",".join(data)
            connection.execute(f"INSERT INTO monitor_subscriptions(id,household_id,member_id,{fields},created_at,updated_at,next_check_at) VALUES ({','.join('?' for _ in range(len(data) + 6))})", (item_id, member["household_id"], member["id"], *data.values(), _iso(stamp), _iso(stamp), int(stamp)))
            row = self._owned(connection, member, item_id)
        return self._public(row)

    def update_subscription(self, member: dict, subscription_id: str, payload: dict) -> dict:
        with self.database.session() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = self._owned(connection, member, subscription_id)
            data = self._validate(payload, current)
            stamp = self.clock()
            # Configuration edits reset comparison baseline; in-flight old
            # fetches cannot publish after a revision change or pause.
            assignments = ",".join(key + "=?" for key in data)
            connection.execute(f"UPDATE monitor_subscriptions SET {assignments},updated_at=?,next_check_at=?,last_fingerprint=NULL,last_matched=0,pending_notification=0,claimed_until=0,revision=revision+1 WHERE id=?", (*data.values(), _iso(stamp), int(stamp), subscription_id))
            row = self._owned(connection, member, subscription_id)
        return self._public(row)

    def delete_subscription(self, member: dict, subscription_id: str) -> dict:
        with self.database.session() as connection:
            self._owned(connection, member, subscription_id)
            connection.execute("DELETE FROM monitor_subscriptions WHERE id=?", (subscription_id,))
        return {"id": subscription_id, "deleted": True}

    def list_runs(self, member: dict, subscription_id: str) -> list:
        with self.database.session() as connection:
            self._owned(connection, member, subscription_id)
            rows = connection.execute("SELECT * FROM monitor_runs WHERE subscription_id=? ORDER BY checked_at DESC LIMIT 100", (subscription_id,)).fetchall()
        return [self._run_public(row) for row in rows]

    @staticmethod
    def _run_public(row) -> dict:
        data = dict(row)
        data["matched"], data["notified"] = bool(data["matched"]), bool(data["notified"])
        data["evidence"] = json.loads(data.pop("evidence_json"))
        return data

    def list_alerts(self, member: dict) -> list:
        with self.database.session() as connection:
            rows = connection.execute("SELECT a.* FROM monitor_alerts a JOIN monitor_subscriptions s ON s.id=a.subscription_id WHERE s.member_id=? AND s.household_id=? ORDER BY a.created_at DESC LIMIT 100", (member["id"], member["household_id"])).fetchall()
        return [dict(row) for row in rows]

    def acknowledge_alert(self, member: dict, alert_id: str) -> dict:
        with self.database.session() as connection:
            row = connection.execute("SELECT a.id FROM monitor_alerts a JOIN monitor_subscriptions s ON s.id=a.subscription_id WHERE a.id=? AND s.member_id=? AND s.household_id=?", (alert_id, member["id"], member["household_id"])).fetchone()
            if not row:
                raise ValidationError("通知不存在或不属于当前成员")
            stamp = _iso(self.clock())
            connection.execute("UPDATE monitor_alerts SET read_at=COALESCE(read_at,?) WHERE id=?", (stamp, alert_id))
        return {"id": alert_id, "read_at": stamp}

    def run_subscription(self, member: dict, subscription_id: str) -> dict:
        stamp = int(self.clock())
        with self.database.session() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = dict(self._owned(connection, member, subscription_id))
            if not row["enabled"]:
                raise ValidationError("请先启用此监控再检查")
            if row["claimed_until"] > stamp:
                raise ValidationError("此监控正在检查")
            if row["last_checked_at"] and stamp - datetime.fromisoformat(row["last_checked_at"]).timestamp() < 60:
                raise ValidationError("距上次检查不足 60 秒，请稍后重试")
            connection.execute("UPDATE monitor_subscriptions SET claimed_until=?,next_check_at=? WHERE id=?", (stamp + 60, stamp + row["interval_seconds"], subscription_id))
        error, value, fingerprint, matched = "", None, None, False
        try:
            body, content_type = self.fetcher(row["url"])
            value = extract_value(body, row["source_type"], row["json_path"], content_type)
            if not value:
                raise ValidationError("网页没有可读取的文本；动态网页请使用其公开 JSON 接口")
            fingerprint = sha256(value.encode("utf-8")).hexdigest()
            if row["condition"] == "change":
                matched = row["last_fingerprint"] is not None and fingerprint != row["last_fingerprint"]
            elif row["condition"] == "contains":
                matched = row["target"].casefold() in value.casefold()
            elif row["condition"] == "above":
                matched = _numeric(value) > _numeric(row["target"])
            else:
                matched = _numeric(value) < _numeric(row["target"])
        except ValidationError as exc:
            error = str(exc)
        except Exception:
            error = "检查未完成，下一周期会自动重试"
        finished = int(self.clock())
        status, run_id, alert = "error" if error else "ok", str(uuid4()), None
        evidence = {"window_start": _iso(stamp), "window_end": _iso(finished), "numerator": int(matched and not error), "denominator": int(not error), "sample_size": int(not error), "confidence": "single_observation" if not error else "unavailable", "limitation": "一次网站响应的规则匹配，不保证来源实时性或准确性；不能据此推断趋势或因果。"}
        pending = bool(row["pending_notification"])
        notified = False
        if not error:
            if row["condition"] == "change":
                pending = pending or matched
            elif not matched:
                pending = False
            elif not row["last_matched"]:
                pending = True
            notified = pending and (row["last_notified_at"] is None or finished - row["last_notified_at"] >= row["cooldown_seconds"])
        delivery = "in_app" if notified else "none"
        with self.database.session() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute("SELECT revision,enabled FROM monitor_subscriptions WHERE id=?", (subscription_id,)).fetchone()
            if not current or current["revision"] != row["revision"] or not current["enabled"]:
                raise ValidationError("监控配置已改变，本次旧结果已丢弃")
            connection.execute("INSERT INTO monitor_runs(id,subscription_id,checked_at,status,value,matched,notified,error,delivery_status,evidence_json) VALUES (?,?,?,?,?,?,?,?,?,?)", (run_id, subscription_id, _iso(finished), status, value[:1200] if value else None, int(matched), int(notified), error, delivery, json.dumps(evidence, ensure_ascii=False)))
            if error:
                connection.execute("UPDATE monitor_subscriptions SET last_checked_at=?,last_status='error',last_error=?,claimed_until=0 WHERE id=?", (_iso(finished), error, subscription_id))
            else:
                connection.execute("UPDATE monitor_subscriptions SET last_checked_at=?,last_status='ok',last_error='',last_value=?,last_fingerprint=?,last_matched=?,pending_notification=?,last_notified_at=?,claimed_until=0 WHERE id=?", (_iso(finished), value[:1200], fingerprint, int(matched), int(pending and not notified), finished if notified else row["last_notified_at"], subscription_id))
            if notified:
                alert = {"id": str(uuid4()), "subscription_id": subscription_id, "run_id": run_id, "title": row["title"], "message": "监控条件已满足：" + value[:500], "value": value[:1200], "created_at": _iso(finished), "read_at": None, "delivery_status": delivery}
                connection.execute("INSERT INTO monitor_alerts(id,subscription_id,run_id,title,message,value,created_at,delivery_status) VALUES (?,?,?,?,?,?,?,?)", tuple(alert[key] for key in ("id", "subscription_id", "run_id", "title", "message", "value", "created_at", "delivery_status")))
            # Bound history per member subscription, including old alerts.
            connection.execute("DELETE FROM monitor_runs WHERE subscription_id=? AND id NOT IN (SELECT id FROM monitor_runs WHERE subscription_id=? ORDER BY checked_at DESC LIMIT 1000)", (subscription_id, subscription_id))
        if alert and row["channel"] != "in_app":
            delivery = self.channels.deliver(row["channel"], alert)
            with self.database.session() as connection:
                connection.execute("UPDATE monitor_runs SET delivery_status=? WHERE id=?", (delivery, run_id))
                connection.execute("UPDATE monitor_alerts SET delivery_status=? WHERE id=?", (delivery, alert["id"]))
        return {"id": run_id, "subscription_id": subscription_id, "checked_at": _iso(finished), "status": status, "value": value[:1200] if value else None, "matched": bool(matched), "notified": bool(notified), "error": error, "delivery_status": delivery, "evidence": evidence}

    def run_due(self, *, limit: int = 10) -> int:
        stamp = int(self.clock())
        with self.database.session() as connection:
            rows = connection.execute("SELECT id,member_id,household_id FROM monitor_subscriptions WHERE enabled=1 AND next_check_at<=? AND claimed_until<=? ORDER BY next_check_at LIMIT ?", (stamp, stamp, min(30, max(1, limit)))).fetchall()
        completed = 0
        for row in rows:
            try:
                self.run_subscription({"id": row["member_id"], "household_id": row["household_id"]}, row["id"])
                completed += 1
            except Exception:
                # A broken subscription must not stop others or the worker.
                continue
        return completed


class MonitoringWorker:
    def __init__(self, service: MonitoringService, *, interval: float = 5):
        self.service, self.interval = service, interval
        self._stop = threading.Event()
        self._thread = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="familyos-monitor")
        self._thread.start()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.service.run_due(limit=1)
            except Exception:
                pass
            self._stop.wait(self.interval)

    def stop(self) -> None:
        self._stop.set()
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=1)
