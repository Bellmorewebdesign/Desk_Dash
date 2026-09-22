"""HTTP server and background scheduler; Python standard library only."""
import argparse
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hmac
import json
import logging
import mimetypes
import os
from pathlib import Path
import secrets
import subprocess
import threading
import time
from urllib.parse import parse_qs, urlparse
import urllib.request

from .actions import ActionError, reboot, service_action, storage_target, storage_test, wake
from .checks import check_site
from .collectors import Sampler, discover_alfred, network_probe, service, temperature
from .config import load_config, load_env
from .store import Store

STATIC = Path(__file__).resolve().parent.parent / 'static'
LOG = logging.getLogger('desk_dash')


class State:
    def __init__(self, config, store, password='', agent_token=''):
        self.config, self.store = config, store
        self.password, self.agent_token = password, agent_token
        self.lock = threading.RLock()
        self.sampler = Sampler()
        self.system = None
        self.sites = {}
        self.services = []
        self.network = None
        self.prev_service_cpu = {}
        self.prev_service_at = None
        self.remotes = []
        self.sessions = {}
        self.failed_logins = {}
        self.started = time.time()
        self.stop = threading.Event()
        self.refresh_sites = threading.Event()

    def tick(self):
        with self.lock:
            old = self.system
            self.system = self.sampler.collect()
            configured = [service(entry) for entry in self.config['services']]
            now = time.monotonic()
            for entry in configured:
                usage, previous = entry.get('cpu_seconds_total'), self.prev_service_cpu.get(entry['id'])
                if usage is not None and previous is not None and self.prev_service_at and now > self.prev_service_at:
                    entry['cpu_percent'] = round(max(0, usage - previous) * 100 / (now - self.prev_service_at), 1)
                if usage is not None:
                    self.prev_service_cpu[entry['id']] = usage
            self.prev_service_at = now
            alfred = discover_alfred()
            if alfred:
                matching = next((i for i, item in enumerate(configured) if item['name'].lower() == 'alfred' and item['status'] == 'UNAVAILABLE'), None)
                if matching is not None:
                    configured[matching] = alfred
                elif not any(item['pid'] == alfred['pid'] for item in configured):
                    configured.append(alfred)
            self.services = configured
            if old:
                for key in ('cpu', 'gpu'):
                    before, after = old[key]['temperature']['level'], self.system[key]['temperature']['level']
                    if after != before and after != 'UNAVAILABLE':
                        self.store.event('temperature', 'warning' if after in ('WARM', 'HOT') else 'error' if after == 'CRITICAL' else 'success', f'{key.upper()} temperature: {before} → {after}')
            self.store.sample({'cpu': self.system['cpu']['percent'], 'cpu_temp': self.system['cpu']['temperature']['celsius'], 'gpu_temp': self.system['gpu']['temperature']['celsius'], 'gpu_usage': self.system['gpu']['utilization'], 'ram_used': self.system['ram']['used_bytes'], 'nvme_temp': self.system['drives'][0]['smart']['temperature']['celsius'] if self.system['drives'] else None, 'dns_ms': self.network['dns_ms'] if self.network else None, 'gateway_ms': self.network['gateway_ms'] if self.network else None})

    def sites_tick(self):
        for entry in self.config['sites']:
            if not entry.get('enabled', True):
                with self.lock:
                    self.sites[entry['id']] = {'id': entry['id'], 'name': entry['name'], 'url': entry.get('url', ''), 'status': 'UNCONFIGURED', 'http_status': None, 'ms': None, 'checked_at': None, 'checks': [], 'last_success': None, 'uptime_percent': None}
                continue
            with self.lock:
                previous = self.sites.get(entry['id'], {}).get('status')
            result = check_site(entry, self.store, previous)
            with self.lock:
                self.sites[entry['id']] = result

    def remotes_tick(self):
        results = []
        for entry in self.config.get('remote_agents', []):
            try:
                url = entry['url']
                if not url.startswith(('http://', 'https://')):
                    raise ValueError('Bad URL')
                request = urllib.request.Request(url.rstrip('/') + '/api/agent', headers={'Authorization': 'Bearer ' + entry['token']})
                with urllib.request.urlopen(request, timeout=3) as response:
                    data = json.load(response)
                results.append({'name': entry['name'], 'status': 'RUNNING', 'system': data})
            except Exception:
                results.append({'name': entry.get('name', 'Remote'), 'status': 'OFFLINE', 'system': None})
        with self.lock:
            self.remotes = results

    def loop(self):
        next_web, next_network, next_prune = 0, 0, 0
        while not self.stop.is_set():
            started = time.monotonic()
            try:
                self.tick()
                if started >= next_web or self.refresh_sites.is_set():
                    self.refresh_sites.clear()
                    self.sites_tick()
                    self.remotes_tick()
                    next_web = started + 60
                if started >= next_network:
                    probe = network_probe()
                    with self.lock:
                        self.network = probe
                    next_network = started + 60
                if started >= next_prune:
                    self.store.prune()
                    next_prune = started + 3600
            except Exception:
                LOG.exception('Monitoring cycle failed')
            self.stop.wait(max(1, 10 - (time.monotonic() - started)))

    def snapshot(self):
        with self.lock:
            return {'system': self.system, 'sites': list(self.sites.values()), 'services': self.services, 'network': self.network, 'remotes': self.remotes, 'events': self.store.events(limit=100), 'server_time': time.time(), 'started': self.started, 'storage_test_configured': bool(self.config.get('storage_test', {}).get('directory')), 'controls': {'services': self.config['controls'].get('service_units', []), 'reboot': self.config['controls'].get('allow_reboot', False), 'wake_targets': [x['id'] for x in self.config['controls'].get('wake_targets', [])]}}


class Handler(BaseHTTPRequestHandler):
    server_version = 'DeskDash/1.0'

    @property
    def state(self):
        return self.server.state

    def log_message(self, format, *args):
        LOG.info('%s - %s', self.address_string(), format % args)

    def headers_common(self):
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('X-Frame-Options', 'DENY')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Security-Policy', "default-src 'self'; connect-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; object-src 'none'; frame-ancestors 'none'; base-uri 'none'")

    def json(self, data, code=200, extra=None):
        payload = json.dumps(data, separators=(',', ':'), allow_nan=False).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(payload)))
        self.headers_common()
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(payload)

    def session(self):
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get('Cookie', ''))
            token = cookie['desk_session'].value if 'desk_session' in cookie else ''
        except Exception:
            return None
        with self.state.lock:
            info = self.state.sessions.get(token)
            if info and info['expires'] > time.time():
                return info
        return None

    def authorized(self):
        if not self.state.password:
            return True
        return self.session() is not None

    def body(self):
        size = int(self.headers.get('Content-Length', '0'))
        if size < 0 or size > 4096:
            raise ValueError('Request too large')
        return json.loads(self.rfile.read(size))

    def do_GET(self):
        path = urlparse(self.path).path
        if path == '/health':
            return self.json({'ok': True, 'monitoring': self.state.system is not None})
        if self.server.agent_mode and path != '/api/agent':
            return self.json({'error': 'Not found'}, 404)
        if path == '/api/auth':
            info = self.session()
            return self.json({'authenticated': bool(info), 'password_required': bool(self.state.password), 'controls_enabled': bool(self.state.password), 'csrf': info['csrf'] if info else None})
        if path == '/api/agent':
            bearer = self.headers.get('Authorization', '')
            if not self.state.agent_token or not hmac.compare_digest(bearer, 'Bearer ' + self.state.agent_token):
                return self.json({'error': 'Forbidden'}, 403)
            return self.json(self.state.system or {})
        if path.startswith('/api/'):
            if not self.authorized():
                return self.json({'error': 'Authentication required'}, 401)
            if path == '/api/snapshot':
                return self.json(self.state.snapshot())
            if path == '/api/history':
                return self.json({'samples': self.state.store.history()})
            if path == '/api/events':
                query = parse_qs(urlparse(self.path).query)
                kind = query.get('kind', [''])[0]
                if kind not in ('', 'website', 'service', 'temperature', 'storage', 'control', 'system'):
                    return self.json({'error': 'Invalid filter'}, 400)
                return self.json({'events': self.state.store.events(kind)})
            if path == '/api/storage-test/preview':
                try:
                    return self.json(storage_target(self.state.config))
                except (ActionError, ValueError, OSError) as exc:
                    return self.json({'error': str(exc)}, 409)
            if path == '/api/logs':
                unit = parse_qs(urlparse(self.path).query).get('unit', [''])[0]
                allowed = [s.get('unit') for s in self.state.config.get('services', [])]
                if unit not in allowed:
                    return self.json({'error': 'Unit is not allowlisted'}, 403)
                try:
                    output = subprocess.run(['journalctl', '-u', unit, '-n', '40', '--no-pager', '--output=short-iso'], capture_output=True, text=True, timeout=4, check=False)
                    return self.json({'unit': unit, 'lines': output.stdout[-12000:] if output.returncode == 0 else 'Journal unavailable'})
                except (OSError, subprocess.TimeoutExpired):
                    return self.json({'error': 'Journal unavailable'}, 503)
            return self.json({'error': 'Not found'}, 404)
        name = 'index.html' if path == '/' else path.lstrip('/')
        if name not in ('index.html', 'app.js', 'styles.css'):
            return self.json({'error': 'Not found'}, 404)
        payload = (STATIC / name).read_bytes()
        self.send_response(200)
        self.send_header('Content-Type', mimetypes.guess_type(name)[0] or 'application/octet-stream')
        self.send_header('Content-Length', str(len(payload)))
        self.headers_common()
        self.end_headers()
        self.wfile.write(payload)

    def do_POST(self):
        if self.server.agent_mode:
            return self.json({'error': 'Not found'}, 404)
        path = urlparse(self.path).path
        if path not in ('/api/login', '/api/logout', '/api/action'):
            return self.json({'error': 'Not found'}, 404)
        # Browsers send Origin on fetch POST; exact same-origin is mandatory for mutations.
        origin = self.headers.get('Origin', '')
        host = self.headers.get('Host', '')
        if origin not in ('http://' + host, 'https://' + host):
            return self.json({'error': 'Origin mismatch'}, 403)
        try:
            body = self.body()
            if not isinstance(body, dict):
                raise ValueError('Expected object')
        except (ValueError, json.JSONDecodeError):
            return self.json({'error': 'Invalid JSON body'}, 400)
        if path == '/api/login':
            if not self.state.password:
                return self.json({'error': 'Set DASH_PASSWORD first'}, 403)
            ip = self.client_address[0]
            with self.state.lock:
                attempts, window = self.state.failed_logins.get(ip, (0, time.time()))
                if time.time() - window > 300:
                    attempts, window = 0, time.time()
                if attempts >= 5:
                    return self.json({'error': 'Too many attempts; try again later'}, 429)
            if not hmac.compare_digest(str(body.get('password', '')), self.state.password):
                with self.state.lock:
                    self.state.failed_logins[ip] = (attempts + 1, window)
                return self.json({'error': 'Incorrect password'}, 401)
            token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
            with self.state.lock:
                self.state.failed_logins.pop(ip, None)
                self.state.sessions[token] = {'csrf': csrf, 'expires': time.time() + 30 * 86400}
            cookie = 'desk_session=' + token + '; HttpOnly; SameSite=Strict; Path=/; Max-Age=2592000' + ('; Secure' if origin.startswith('https://') else '')
            return self.json({'authenticated': True, 'csrf': csrf}, extra={'Set-Cookie': cookie})
        session = self.session()
        if not session or not hmac.compare_digest(self.headers.get('X-CSRF-Token', ''), session['csrf']):
            return self.json({'error': 'Authentication or CSRF token missing'}, 403)
        if path == '/api/logout':
            cookie = SimpleCookie(); cookie.load(self.headers.get('Cookie', ''))
            with self.state.lock:
                self.state.sessions.pop(cookie['desk_session'].value if 'desk_session' in cookie else '', None)
            return self.json({'ok': True}, extra={'Set-Cookie': 'desk_session=; Max-Age=0; Path=/; HttpOnly; SameSite=Strict'})
        kind = body.get('type')
        try:
            if kind == 'refresh_sites':
                if self.state.store.reserve_action('refresh_sites', 30):
                    raise ActionError('Refresh cooldown active')
                self.state.refresh_sites.set()
                result = {'queued': True}
            elif kind == 'network_test':
                if self.state.store.reserve_action('network_test', 30):
                    raise ActionError('Network test cooldown active')
                result = network_probe()
                self.state.store.event('control', 'info', 'Network test requested')
            elif kind in ('storage_test', 'service', 'wake', 'reboot'):
                confirmation = {'storage_test': 'RUN TEST', 'service': 'CONFIRM SERVICE', 'wake': 'SEND WAKE', 'reboot': 'REBOOT'}[kind]
                if body.get('confirm') != confirmation:
                    raise ActionError('Explicit confirmation required: ' + confirmation)
                if kind == 'storage_test':
                    result = storage_test(self.state.config, self.state.store)
                elif kind == 'service':
                    result = service_action(self.state.config, self.state.store, body.get('unit'), body.get('verb'))
                elif kind == 'wake':
                    result = wake(self.state.config, self.state.store, body.get('target'))
                else:
                    result = reboot(self.state.config, self.state.store)
            else:
                raise ActionError('Unknown action')
            return self.json(result)
        except (ActionError, ValueError, OSError, subprocess.TimeoutExpired) as exc:
            LOG.warning('Action rejected or failed: %s', type(exc).__name__)
            return self.json({'error': str(exc) if isinstance(exc, ActionError) else 'Action failed'}, 409)


def main():
    parser = argparse.ArgumentParser(description='Desk Dash Linux monitoring dashboard')
    parser.add_argument('--agent', action='store_true', help='Expose only a token-protected metric endpoint')
    args = parser.parse_args()
    root = Path(__file__).resolve().parent.parent
    load_env(root / '.env')
    config = load_config(os.getenv('DASH_CONFIG', str(root / 'config.json')))
    data_dir = Path(os.getenv('DASH_DATA_DIR', str(root / 'data'))).resolve()
    data_dir.mkdir(parents=True, exist_ok=True)
    store = Store(data_dir / 'desk-dash.sqlite3')
    password, agent_token = os.getenv('DASH_PASSWORD', ''), os.getenv('DASH_AGENT_TOKEN', '')
    host = os.getenv('DASH_HOST', '127.0.0.1')
    if host not in ('127.0.0.1', '::1', 'localhost') and not (password or (args.agent and agent_token)):
        parser.error('LAN binding requires DASH_PASSWORD (or DASH_AGENT_TOKEN in agent mode)')
    if args.agent and not agent_token:
        parser.error('Agent mode requires DASH_AGENT_TOKEN')
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    state = State(config, store, password, agent_token)
    state.tick()
    state.store.event('system', 'info', 'Desk Dash backend started')
    thread = threading.Thread(target=state.loop, daemon=True, name='monitor')
    thread.start()
    class Server(ThreadingHTTPServer):
        daemon_threads = True
    server = Server((host, int(os.getenv('DASH_PORT', '8765'))), Handler)
    server.state = state
    server.agent_mode = args.agent
    LOG.info('Desk Dash listening on http://%s:%s', host, server.server_port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        state.stop.set(); server.server_close(); store.db.close()


if __name__ == '__main__':
    main()
