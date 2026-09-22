import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from desk_dash.config import load_config
from desk_dash.server import Handler, State
from desk_dash.store import Store


class ApiSecurityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(self.tmp.name + '/db.sqlite3')
        self.state = State(load_config('config.example.json'), self.store, password='test-secret', agent_token='agent-secret')
        self.state.tick()
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.server.state = self.state
        self.server.agent_mode = False
        self.base = 'http://127.0.0.1:' + str(self.server.server_port)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.store.db.close()
        self.tmp.cleanup()

    def request(self, path, data=None, headers=None):
        request = urllib.request.Request(self.base + path, data=None if data is None else json.dumps(data).encode(), headers=headers or {})
        try:
            with urllib.request.urlopen(request, timeout=2) as response:
                return response.status, json.load(response), response.headers
        except urllib.error.HTTPError as error:
            return error.code, json.load(error), error.headers

    def test_login_csrf_origin_and_allowlist(self):
        self.assertEqual(self.request('/api/snapshot')[0], 401)
        self.assertEqual(self.request('/api/login', {'password': 'test-secret'}, {'Origin': 'http://evil.local'})[0], 403)
        status, info, headers = self.request('/api/login', {'password': 'test-secret'}, {'Origin': self.base})
        self.assertEqual(status, 200)
        cookie = headers['Set-Cookie'].split(';')[0]
        self.assertEqual(self.request('/api/snapshot', headers={'Cookie': cookie})[0], 200)
        action = {'type': 'service', 'unit': 'unlisted.service', 'verb': 'restart', 'confirm': 'CONFIRM SERVICE'}
        self.assertEqual(self.request('/api/action', action, {'Cookie': cookie, 'Origin': self.base})[0], 403)
        self.assertEqual(self.request('/api/action', action, {'Cookie': cookie, 'Origin': 'http://evil.local', 'X-CSRF-Token': info['csrf']})[0], 403)
        self.assertEqual(self.request('/api/action', action, {'Cookie': cookie, 'Origin': self.base, 'X-CSRF-Token': info['csrf']})[0], 409)

    def test_agent_mode_exposes_only_bearer_metrics(self):
        self.server.agent_mode = True
        self.assertEqual(self.request('/api/snapshot')[0], 404)
        self.assertEqual(self.request('/api/agent')[0], 403)
        self.assertEqual(self.request('/api/agent', headers={'Authorization': 'Bearer agent-secret'})[0], 200)
        self.assertEqual(self.request('/api/login', {'password': 'test-secret'}, {'Origin': self.base})[0], 404)


if __name__ == '__main__':
    unittest.main()
