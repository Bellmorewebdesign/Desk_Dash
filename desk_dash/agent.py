"""Read-only, authenticated LAN sensor agent for Linux or Windows."""
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hmac
import json
import logging
import os
from pathlib import Path
import secrets
import threading
import time

from .collectors import Sampler
from .config import load_env
from .protocol import envelope


class AgentState:
    def __init__(self, sampler, token, interval=10):
        self.sampler, self.token = sampler, token
        self.interval = interval
        self.lock = threading.RLock()
        self.stop = threading.Event()
        self.report = None

    def tick(self):
        report = envelope(self.sampler.collect(), os.getenv('DASH_AGENT_NAME'))
        with self.lock:
            self.report = report
        return report

    def loop(self):
        while not self.stop.is_set():
            try:
                self.tick()
            except Exception:
                logging.exception('Agent metric collection failed')
                with self.lock:
                    self.report = None
            self.stop.wait(self.interval)


class AgentHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path not in ('/health', '/api/agent/v1'):
            return self.reply({'error': 'Not found'}, 404)
        if self.path == '/health':
            return self.reply({'ok': True, 'sample_ready': self.server.state.report is not None})
        bearer = self.headers.get('Authorization', '')
        if not hmac.compare_digest(bearer, 'Bearer ' + self.server.state.token):
            return self.reply({'error': 'Forbidden'}, 403)
        with self.server.state.lock:
            report = self.server.state.report
        if report is None:
            return self.reply({'error': 'Sensors unavailable'}, 503)
        self.reply(report)

    def do_POST(self):
        self.reply({'error': 'Read-only agent'}, 405)

    def reply(self, data, status=200):
        raw = json.dumps(data, allow_nan=False).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(raw)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.end_headers()
        self.wfile.write(raw)


def main():
    parser = argparse.ArgumentParser(description='Desk Dash read-only sensor agent')
    parser.add_argument('--generate-token', action='store_true', help='Print a new random shared secret for local setup')
    args = parser.parse_args()
    if args.generate_token:
        print(secrets.token_urlsafe(32))
        return
    serve_agent()


def serve_agent():
    load_env(Path(__file__).resolve().parent.parent / '.env')
    token = os.getenv('DASH_AGENT_TOKEN', '')
    if len(token) < 32:
        raise SystemExit('Set DASH_AGENT_TOKEN to a unique random 32+ character value')
    host = os.getenv('DASH_AGENT_HOST', '127.0.0.1')
    port = int(os.getenv('DASH_AGENT_PORT', '8766'))
    if os.name == 'nt':
        from .windows import WindowsSampler
        sampler = WindowsSampler()
    else:
        sampler = Sampler()
    state = AgentState(sampler, token)
    state.tick()
    thread = threading.Thread(target=state.loop, daemon=True)
    thread.start()
    class Server(ThreadingHTTPServer):
        daemon_threads = True
    server = Server((host, port), AgentHandler)
    server.state = state
    logging.basicConfig(level=logging.INFO)
    logging.info('Desk Dash agent serving read-only metrics on %s:%s', host, port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        state.stop.set()
        server.server_close()


if __name__ == '__main__':
    main()
