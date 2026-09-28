import json
import socket
import unittest

from familyos.companion import CompanionUnavailableError
from familyos.households import ValidationError
from familyos.voice import VoiceService


class FakeWebSocket:
    def __init__(self, *, partial="", final="", fail_binary=False):
        self.partial = partial
        self.final = final
        self.fail_binary = fail_binary
        self.sent_text = []
        self.sent_binary = []
        self.responses = []
        self.closed = False

    def send(self, value):
        self.sent_text.append(value)
        payload = json.loads(value)
        if payload.get("is_end") and self.final:
            self.responses.append(json.dumps({"mode": "2pass-offline", "text": self.final, "is_final": True}))

    def send_binary(self, value):
        if self.fail_binary:
            self.fail_binary = False
            raise OSError("connection reset")
        self.sent_binary.append(bytes(value))
        if self.partial:
            self.responses.append(json.dumps({"mode": "2pass-online", "text": self.partial}))

    def recv(self):
        if self.responses:
            return self.responses.pop(0)
        raise socket.timeout()

    def settimeout(self, value):
        self.timeout = value

    def close(self):
        self.closed = True


class VoiceStreamTests(unittest.TestCase):
    member = {"id": "member-one"}

    def test_two_pass_stream_config_partial_and_corrected_final(self):
        socket_one = FakeWebSocket(partial="今天买", final="今天买牛奶。")
        service = VoiceService(funasr_url="ws://funasr:10095", websocket_factory=lambda _url, _timeout: socket_one)
        started = service.start_stream(self.member, {"sample_rate": 16000})
        config = json.loads(socket_one.sent_text[0])
        self.assertEqual((config["mode"], config["chunk_size"], config["audio_fs"]), ("2pass", [8, 8, 4], 16000))
        partial = service.stream_chunk(self.member, started["session_id"], b"\x00\x00" * 7680)
        self.assertEqual((partial["text"], partial["mode"]), ("今天买", "2pass-online"))
        final = service.finish_stream(self.member, started["session_id"])
        self.assertEqual((final["text"], final["mode"], final["reconnects"]), ("今天买牛奶。", "2pass-offline", 0))
        self.assertIn("first_partial", final["latency_ms"])
        self.assertTrue(socket_one.closed)

    def test_session_is_member_scoped_and_bounded(self):
        connection = FakeWebSocket(partial="测试")
        service = VoiceService(funasr_url="ws://funasr:10095", websocket_factory=lambda _url, _timeout: connection)
        started = service.start_stream(self.member, {})
        with self.assertRaises(ValidationError):
            service.stream_chunk({"id": "member-two"}, started["session_id"], b"\x00\x00")
        with self.assertRaises(ValidationError):
            service.stream_chunk(self.member, started["session_id"], b"x")
        with self.assertRaises(ValidationError):
            service.stream_chunk(self.member, started["session_id"], b"\x00\x00" * 32769)
        aborted = service.abort_stream(self.member, started["session_id"])
        self.assertTrue(aborted["aborted"])

    def test_online_fragments_accumulate_and_final_wait_survives_poll_timeout(self):
        class DelayedFinal(FakeWebSocket):
            final_poll = False
            def send(self, value):
                super().send(value)
                if json.loads(value).get('is_end'):
                    self.final_poll = True
            def recv(self):
                if self.final_poll:
                    self.final_poll = False
                    raise socket.timeout()
                return super().recv()
        connection = DelayedFinal(partial='今天', final='今天买牛奶。')
        service = VoiceService(funasr_url='ws://funasr:10095', websocket_factory=lambda *_: connection)
        sid = service.start_stream(self.member, {})['session_id']
        service.stream_chunk(self.member, sid, b'\x00\x00')
        connection.partial = '买牛奶'
        partial = service.stream_chunk(self.member, sid, b'\x00\x00')
        self.assertEqual(partial['text'], '今天买牛奶')
        self.assertEqual(service.finish_stream(self.member, sid)['text'], '今天买牛奶。')

    def test_missing_offline_correction_raises_instead_of_submitting_fragment(self):
        connection = FakeWebSocket(partial='模')
        ticks = iter(range(1000))
        service = VoiceService(funasr_url='ws://funasr:10095', websocket_factory=lambda *_: connection,
                               clock=lambda: next(ticks) * .1)
        sid = service.start_stream(self.member, {})['session_id']
        service.stream_chunk(self.member, sid, b'\x00\x00')
        with self.assertRaises(CompanionUnavailableError):
            service.finish_stream(self.member, sid)
        self.assertTrue(connection.closed)

    def test_reconnects_once_and_replays_bounded_audio(self):
        first = FakeWebSocket(fail_binary=True)
        second = FakeWebSocket(partial="重连成功", final="重连成功。")
        sockets = iter([first, second])
        service = VoiceService(funasr_url="ws://funasr:10095", websocket_factory=lambda _url, _timeout: next(sockets))
        started = service.start_stream(self.member, {})
        audio = b"\x01\x00" * 400
        partial = service.stream_chunk(self.member, started["session_id"], audio)
        self.assertEqual((partial["text"], partial["reconnects"]), ("重连成功", 1))
        self.assertEqual(second.sent_binary, [audio])
        final = service.finish_stream(self.member, started["session_id"])
        self.assertEqual(final["text"], "重连成功。")

    def test_second_transport_failure_discards_session(self):
        sockets = iter([FakeWebSocket(fail_binary=True), FakeWebSocket(fail_binary=True)])
        service = VoiceService(funasr_url="ws://funasr:10095", websocket_factory=lambda _url, _timeout: next(sockets))
        started = service.start_stream(self.member, {})
        with self.assertRaises(CompanionUnavailableError):
            service.stream_chunk(self.member, started["session_id"], b"\x00\x00")
        with self.assertRaises(ValidationError):
            service.abort_stream(self.member, started["session_id"])

    def test_invalid_external_endpoints_are_rejected(self):
        for value in ["wss://public.example", "ws://user:pass@funasr:10095", "ws://funasr:10095/path", "http://funasr:10095"]:
            with self.assertRaises(ValueError):
                VoiceService(funasr_url=value)


if __name__ == "__main__":
    unittest.main()
