"""Exercise real HTTP endpoints with synthetic data in a temporary database."""
from http.cookiejar import CookieJar
from pathlib import Path
import json
import sys
import tempfile
import threading
from urllib.error import HTTPError
from urllib.request import Request, build_opener, HTTPCookieProcessor, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from familyos.db import Database
from familyos.http import FamilyOSServer


def main():
    with tempfile.TemporaryDirectory(prefix='familyos-demo-') as directory:
        database = Database(str(Path(directory) / 'demo.db'))
        database.migrate()
        server = FamilyOSServer(('127.0.0.1', 0), database)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f'http://127.0.0.1:{server.server_port}'
        alice = build_opener(HTTPCookieProcessor(CookieJar()))
        bob = build_opener(HTTPCookieProcessor(CookieJar()))

        def request(client, path, body=None):
            req = Request(base + path, data=json.dumps(body).encode() if body is not None else None,
                          headers={'Content-Type': 'application/json'})
            with client.open(req, timeout=5) as response:
                return json.load(response)

        try:
            request(alice, '/api/setup', {'household_name': 'Demo household', 'member_name': 'Alex',
                                         'username': 'alex', 'timezone': 'UTC'})
            print('PASS  Household initialized through the HTTP API')
            request(alice, '/api/members', {'display_name': 'Sam', 'username': 'sam', 'role': 'adult'})
            private = request(alice, '/api/tasks', {'title': 'Review architecture notes', 'visibility': 'private'})
            shared = request(alice, '/api/tasks', {'title': 'Plan the weekend', 'visibility': 'household'})
            request(bob, '/api/session', {'username': 'sam'})
            tasks = request(bob, '/api/today')['tasks']
            assert shared['id'] in {t['id'] for t in tasks}
            assert private['id'] not in {t['id'] for t in tasks}
            print('PASS  Second member sees the shared task, not the private task')
            request(alice, f"/api/tasks/{private['id']}/complete", {})
            with database.session() as connection:
                row = connection.execute('SELECT status FROM tasks WHERE id=?', (private['id'],)).fetchone()
                assert row['status'] == 'completed'
            print('PASS  Task completion persisted in SQLite')
            try:
                urlopen(base + '/api/today', timeout=5)
                raise AssertionError('Unauthenticated request unexpectedly succeeded')
            except HTTPError as error:
                assert error.code == 401
                error.close()
            print('PASS  Request without a session rejected with HTTP 401')
            print('Demo complete. Temporary data is removed on exit; no model or external service needed.')
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


if __name__ == '__main__':
    main()
