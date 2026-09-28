"""Private adapters for LAN-only speech engines.

The browser talks only to the authenticated FamilyOS API. A bounded in-memory
session bridges 16 kHz PCM chunks to FunASR's official 2-pass WebSocket
protocol; the existing faster-whisper HTTP service remains the automatic
whole-recording fallback.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import json
import socket
from threading import Lock, RLock
import time
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen
from uuid import uuid4

from .companion import CompanionUnavailableError
from .households import ValidationError


@dataclass
class _StreamingSession:
    id: str
    member_id: str
    sample_rate: int
    connection: object
    created_at: float
    updated_at: float
    audio: bytearray = field(default_factory=bytearray)
    chunks: int = 0
    partial_text: str = ""
    final_text: str = ""
    reconnects: int = 0
    first_audio_at: float | None = None
    last_audio_at: float | None = None
    first_text_at: float | None = None
    lock: RLock = field(default_factory=RLock)


class VoiceService:
    SAMPLE_RATE = 16_000
    MAX_CHUNK_BYTES = 65_536
    MAX_AUDIO_BYTES = 1_024_000
    MAX_CHUNKS = 96
    SESSION_TTL_SECONDS = 45
    MAX_SESSIONS = 4
    CONNECT_TIMEOUT_SECONDS = 1.0

    def __init__(self, *, asr_url: str = "", funasr_url: str = "", timeout: int = 45,
                 websocket_factory: Callable[[str, float], object] | None = None,
                 clock: Callable[[], float] = time.monotonic):
        self.asr_url = self._http_endpoint(asr_url)
        self.funasr_url = self._websocket_endpoint(funasr_url)
        self.timeout = timeout
        self._websocket_factory = websocket_factory
        self._clock = clock
        self._sessions: dict[str, _StreamingSession] = {}
        self._member_sessions: dict[str, str] = {}
        self._sessions_lock = Lock()

    @staticmethod
    def _http_endpoint(value: str) -> str:
        value = value.rstrip("/")
        if not value:
            return ""
        target = urlparse(value)
        if target.scheme != "http" or not target.hostname or target.username or target.password or target.path not in {"", "/"}:
            raise ValueError("本地语音识别地址配置不正确")
        return value

    @staticmethod
    def _websocket_endpoint(value: str) -> str:
        value = value.rstrip("/")
        if not value:
            return ""
        target = urlparse(value)
        if (target.scheme != "ws" or not target.hostname or target.username or target.password
                or target.query or target.fragment or target.path not in {"", "/"}):
            raise ValueError("FunASR 实时识别地址配置不正确")
        return value

    def _connect(self) -> object:
        if not self.funasr_url:
            raise CompanionUnavailableError("FunASR 实时识别尚未配置")
        try:
            if self._websocket_factory:
                connection = self._websocket_factory(self.funasr_url, self.CONNECT_TIMEOUT_SECONDS)
            else:
                from websocket import create_connection
                connection = create_connection(self.funasr_url, timeout=self.CONNECT_TIMEOUT_SECONDS, enable_multithread=True,
                                               http_proxy_host=None, http_proxy_port=None)
            self._send_config(connection)
            return connection
        except Exception as exc:
            raise CompanionUnavailableError("FunASR 实时识别暂不可用，将改用兼容识别") from exc

    @classmethod
    def _send_config(cls, connection: object) -> None:
        connection.send(json.dumps({
            "mode": "2pass", "chunk_size": [8, 8, 4], "chunk_interval": 10,
            "encoder_chunk_look_back": 4, "decoder_chunk_look_back": 1,
            "audio_fs": cls.SAMPLE_RATE, "wav_name": "familyos-live", "wav_format": "pcm",
            "is_speaking": True, "itn": True,
        }, ensure_ascii=False))

    @staticmethod
    def _member_id(member: dict) -> str:
        value = member.get("id")
        if not isinstance(value, str) or not value:
            raise ValidationError("成员身份无效")
        return value

    @staticmethod
    def _close_connection(connection: object) -> None:
        try:
            connection.close()
        except Exception:
            pass

    def _discard(self, session: _StreamingSession) -> None:
        with self._sessions_lock:
            self._sessions.pop(session.id, None)
            if self._member_sessions.get(session.member_id) == session.id:
                self._member_sessions.pop(session.member_id, None)
        self._close_connection(session.connection)

    def _cleanup_stale(self) -> None:
        cutoff = self._clock() - self.SESSION_TTL_SECONDS
        with self._sessions_lock:
            stale = [session for session in self._sessions.values() if session.updated_at < cutoff]
        for session in stale:
            self._discard(session)

    def start_stream(self, member: dict, payload: dict) -> dict:
        if set(payload) - {"sample_rate"}:
            raise ValidationError("实时语音启动参数不受支持")
        sample_rate = payload.get("sample_rate", self.SAMPLE_RATE)
        if sample_rate != self.SAMPLE_RATE:
            raise ValidationError("实时语音必须使用 16000Hz 单声道 PCM16")
        self._cleanup_stale()
        member_id = self._member_id(member)
        connection = self._connect()
        now = self._clock()
        session = _StreamingSession(str(uuid4()), member_id, sample_rate, connection, now, now)
        with self._sessions_lock:
            old_id = self._member_sessions.get(member_id)
            old_session = self._sessions.get(old_id) if old_id else None
            if len(self._sessions) - (1 if old_session else 0) >= self.MAX_SESSIONS:
                self._close_connection(connection)
                raise CompanionUnavailableError("实时语音会话已满，请稍后重试")
            if old_session:
                self._sessions.pop(old_session.id, None)
            self._sessions[session.id] = session
            self._member_sessions[member_id] = session.id
        if old_session:
            self._close_connection(old_session.connection)
        return {"session_id": session.id, "engine": "funasr", "mode": "2pass",
                "sample_rate": self.SAMPLE_RATE, "chunk_ms": 480}

    def _owned_session(self, member: dict, session_id: str) -> _StreamingSession:
        self._cleanup_stale()
        member_id = self._member_id(member)
        with self._sessions_lock:
            session = self._sessions.get(session_id)
        if not session or session.member_id != member_id:
            raise ValidationError("实时语音会话不存在或已过期")
        return session

    @staticmethod
    def _is_timeout(exc: Exception) -> bool:
        return isinstance(exc, (socket.timeout, TimeoutError)) or exc.__class__.__name__ == "WebSocketTimeoutException"

    def _apply_message(self, session: _StreamingSession, raw: object) -> tuple[bool, bool]:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            return False, False
        text = payload.get("text")
        mode = payload.get("mode", "")
        changed = False
        final = False
        if isinstance(text, str) and text.strip():
            clean = text.strip()
            if len(clean) > 1000:
                raise CompanionUnavailableError("实时语音识别结果异常")
            if mode in {"2pass-offline", "offline"} or payload.get("is_final") is True:
                session.final_text = clean
                final = True
            else:
                # FunASR online frames are incremental fragments, not snapshots.
                session.partial_text = (session.partial_text + clean)[:1000]
            if session.first_text_at is None:
                session.first_text_at = self._clock()
            changed = True
        if payload.get("is_final") is True or payload.get("is_end") is True:
            final = True
        return changed, final

    def _drain(self, session: _StreamingSession, *, wait_seconds: float, wait_for_final: bool = False) -> bool:
        deadline = self._clock() + wait_seconds
        received_final = False
        session.connection.settimeout(min(.12, max(.01, wait_seconds)))
        while self._clock() < deadline:
            try:
                raw = session.connection.recv()
                if raw in {None, ""}:
                    break
                _, final = self._apply_message(session, raw)
                received_final = received_final or final
                if received_final:
                    break
                session.connection.settimeout(min(.12, max(.01, deadline - self._clock())))
            except Exception as exc:
                if self._is_timeout(exc):
                    if wait_for_final:
                        continue
                    break
                raise
        return received_final

    def _reconnect_and_replay(self, session: _StreamingSession) -> None:
        if session.reconnects >= 1:
            raise CompanionUnavailableError("实时语音连接中断，请重试或使用兼容识别")
        self._close_connection(session.connection)
        session.connection = self._connect()
        session.reconnects += 1
        # Replayed audio yields all fragments again; do not duplicate old text.
        session.partial_text = ""
        session.final_text = ""
        if session.audio:
            session.connection.send_binary(bytes(session.audio))

    def stream_chunk(self, member: dict, session_id: str, audio: bytes) -> dict:
        if not audio or len(audio) > self.MAX_CHUNK_BYTES:
            raise ValidationError("实时语音分片为空或超过 64KB")
        if len(audio) % 2:
            raise ValidationError("实时语音分片必须是 PCM16 数据")
        session = self._owned_session(member, session_id)
        with session.lock:
            if len(session.audio) + len(audio) > self.MAX_AUDIO_BYTES or session.chunks >= self.MAX_CHUNKS:
                self._discard(session)
                raise ValidationError("实时语音超过 30 秒限制")
            now = self._clock()
            session.audio.extend(audio)
            session.chunks += 1
            session.updated_at = now
            session.first_audio_at = session.first_audio_at or now
            session.last_audio_at = now
            try:
                session.connection.send_binary(audio)
                self._drain(session, wait_seconds=.08)
            except Exception:
                try:
                    self._reconnect_and_replay(session)
                    self._drain(session, wait_seconds=.08)
                except Exception as exc:
                    self._discard(session)
                    if isinstance(exc, CompanionUnavailableError):
                        raise
                    raise CompanionUnavailableError("实时语音连接中断，请使用兼容识别") from exc
            return {"session_id": session.id, "text": session.final_text or session.partial_text,
                    "final": bool(session.final_text), "engine": "funasr", "mode": "2pass-online",
                    "reconnects": session.reconnects}

    def finish_stream(self, member: dict, session_id: str) -> dict:
        session = self._owned_session(member, session_id)
        with session.lock:
            if not session.audio:
                self._discard(session)
                raise ValidationError("录音为空")
            try:
                session.connection.send(json.dumps({"is_speaking": False, "is_end": True}))
                self._drain(session, wait_seconds=8.0, wait_for_final=True)
            except Exception as exc:
                self._discard(session)
                raise CompanionUnavailableError("实时语音最终校正失败，请使用兼容识别") from exc
            now = self._clock()
            # Never submit a truncated online fragment as a completed utterance.
            # Missing offline correction activates the existing WAV fallback.
            text = session.final_text
            result = {
                "session_id": session.id, "text": text, "final": True, "engine": "funasr",
                "mode": "2pass-offline" if session.final_text else "2pass-online",
                "reconnects": session.reconnects,
                "latency_ms": {
                    "first_partial": round((session.first_text_at - session.first_audio_at) * 1000)
                    if session.first_text_at and session.first_audio_at else None,
                    "final_after_last_chunk": round((now - session.last_audio_at) * 1000)
                    if session.last_audio_at else None,
                },
            }
            self._discard(session)
        if not text:
            raise CompanionUnavailableError("没有识别到清晰的语音，请再说一次")
        return result

    def abort_stream(self, member: dict, session_id: str) -> dict:
        session = self._owned_session(member, session_id)
        with session.lock:
            self._discard(session)
        return {"aborted": True, "session_id": session_id}

    def close(self) -> None:
        with self._sessions_lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
            self._member_sessions.clear()
        for session in sessions:
            self._close_connection(session.connection)

    def transcribe(self, audio: bytes, content_type: str) -> dict:
        if not self.asr_url:
            raise CompanionUnavailableError("本地语音识别尚未配置")
        if not audio or len(audio) > 16 * 1024 * 1024:
            raise ValidationError("录音为空或超过 16MB")
        if content_type not in {"audio/webm", "audio/ogg", "audio/wav", "audio/mp4"}:
            raise ValidationError("录音格式不受支持")
        request = Request(f"{self.asr_url}/transcribe", data=audio,
                          headers={"Content-Type": content_type, "Accept": "application/json"}, method="POST")
        try:
            with urlopen(request, timeout=self.timeout) as response:
                if response.status != 200:
                    raise CompanionUnavailableError("本地语音识别暂不可用")
                result = json.loads(response.read().decode("utf-8"))
        except (HTTPError, URLError, TimeoutError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise CompanionUnavailableError("本地语音识别暂不可用，请稍后重试或直接输入文字") from exc
        text = result.get("text") if isinstance(result, dict) else None
        if not isinstance(text, str) or not text.strip() or len(text.strip()) > 1000:
            raise CompanionUnavailableError("没有识别到清晰的语音，请再说一次")
        return {"text": text.strip(), "engine": "faster-whisper"}
