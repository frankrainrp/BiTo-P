import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from familyos.task_webhooks import TaskWebhook


class WebhookTests(unittest.TestCase):
    def test_fixed_destination_boundary(self):
        for url in ['https://example.com', 'http://169.254.169.254', 'http://task-notify.evil',
                    'http://user:pass@task-notify', 'http://task-notify/private', 'file:///tmp/x']:
            self.assertFalse(TaskWebhook(url).ready)
        self.assertTrue(TaskWebhook('http://task-notify:80').ready)

    def test_http_delivery_has_member_topic_and_receipt(self):
        payloads = []
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                data = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                payloads.append(data)
                self.send_response(200)
                self.end_headers()
                self.wfile.write(json.dumps({'id': 'receipt', 'event': 'message', 'topic': data['topic']}).encode())
            def log_message(self, *args):
                pass
        server = HTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            adapter = TaskWebhook(f'http://127.0.0.1:{server.server_port}')
            job = {'id': 'job', 'title': '提醒', 'instructions': '消息'}
            result = adapter.send({'household_id': 'h', 'id': 'a'}, job)
            adapter.send({'household_id': 'h', 'id': 'b'}, job)
            self.assertIn('receipt', result['text'])
            self.assertNotEqual(payloads[0]['topic'], payloads[1]['topic'])
        finally:
            server.shutdown()
            server.server_close()
            thread.join()
