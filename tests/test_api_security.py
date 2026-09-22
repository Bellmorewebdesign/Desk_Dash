import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from desk_dash.agent import AgentHandler, AgentState
from desk_dash.config import load_config
from desk_dash.server import Handler, State
from desk_dash.store import Store


class ApiSecurityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(self.tmp.name + '/db.sqlite3')
        self.state = State(load_config('config.example.json'), self.store, password='test-secret')
        self.state.tick()
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.server.state = self.state
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

    def test_central_backend_does_not_serve_agent_data(self):
        self.assertEqual(self.request('/api/agent/v1')[0], 401)
        self.assertEqual(self.request('/api/history?machine=unlisted', headers={'Cookie': self.login_cookie()})[0], 404)

    def login_cookie(self):
        _, _, headers = self.request('/api/login', {'password': 'test-secret'}, {'Origin': self.base})
        return headers['Set-Cookie'].split(';')[0]


class ReadOnlyAgentTests(unittest.TestCase):
    def test_token_required_and_post_is_never_available(self):
        class DummySampler:
            def collect(self):
                return {'ts': __import__('time').time(), 'hostname': 'second-pc', 'cpu': {'model': 'Test CPU', 'temperature': {'celsius': 42}}, 'gpu': {'model': 'Test GPU', 'temperature': {'celsius': 39}}, 'ram': {'total_bytes': 1024, 'used_bytes': 500}}
        state = AgentState(DummySampler(), 'long-random-test-secret-1234567890')
        state.tick()
        server = ThreadingHTTPServer(('127.0.0.1', 0), AgentHandler)
        server.state = state
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = 'http://127.0.0.1:' + str(server.server_port)
        try:
            for headers, expected in [({}, 403), ({'Authorization': 'Bearer wrong'}, 403), ({'Authorization': 'Bearer ' + state.token}, 200)]:
                req = urllib.request.Request(base + '/api/agent/v1', headers=headers)
                try:
                    with urllib.request.urlopen(req) as response:
                        code, body = response.status, json.load(response)
                except urllib.error.HTTPError as error:
                    code, body = error.code, json.load(error)
                self.assertEqual(code, expected)
                if code == 200:
                    self.assertEqual(body['schema_version'], 1)
                    self.assertEqual(body['system']['cpu']['temperature']['level'], 'GOOD')
            req = urllib.request.Request(base + '/api/agent/v1', data=b'{}', method='POST')
            with self.assertRaises(urllib.error.HTTPError) as caught:
                urllib.request.urlopen(req)
            self.assertEqual(caught.exception.code, 405)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


if __name__ == '__main__':
    unittest.main()
