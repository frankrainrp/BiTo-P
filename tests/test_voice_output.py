import io
import unittest
from unittest.mock import patch
from familyos.voice_output import VoiceOutput
from familyos.households import ValidationError
from familyos.companion import CompanionUnavailableError

class VoiceOutputTests(unittest.TestCase):
    def test_rejects_unconfigured_or_invalid_input(self):
        for payload in ({'text':''},{'text':'x'*1201},{'text':'hi','url':'http://other'}):
            with self.assertRaises(ValidationError): list(VoiceOutput().stream(payload))
        with self.assertRaises(CompanionUnavailableError): list(VoiceOutput().stream({'text':'hi'}))

    def test_stream_forwards_token_only_to_configured_worker(self):
        stream=io.BytesIO(b'{"pcm":"AAABAA==","sample_rate":32000}\n{"done":true}\n')
        with patch('familyos.voice_output.urlopen',return_value=stream) as opened:
            events=list(VoiceOutput('http://voice:19881','test-token').stream({'text':'hello'}))
        self.assertTrue(events[-1]['done'])
        req=opened.call_args.args[0]
        self.assertEqual(req.full_url,'http://voice:19881/speech/stream')
        self.assertEqual(req.get_header('Authorization'),'Bearer test-token')
        self.assertNotIn('test-token',str(events))

    def test_worker_error_is_not_audio(self):
        with patch('familyos.voice_output.urlopen',return_value=io.BytesIO(b'{"error":"internal path"}\n')):
            with self.assertRaises(CompanionUnavailableError) as error:
                list(VoiceOutput('http://voice:19881','test').stream({'text':'hello'}))
        self.assertNotIn('internal path',str(error.exception))
