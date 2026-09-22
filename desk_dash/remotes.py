"""Central polling and state machine for authenticated remote sensor agents."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
import threading
import time
import urllib.request
from urllib.parse import urlparse

from .protocol import validate_report


OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class RemoteMonitor:
    def __init__(self, config, store):
        self.config, self.store = config, store
        self.lock = threading.RLock()
        self.state = {entry['id']: {'status': 'CONNECTING', 'last_success': store.last_remote(entry['id']), 'system': None, 'last_polled': None, 'error': None} for entry in config}

    def poll_one(self, entry, now=None):
        now = time.time() if now is None else now
        machine = entry['id']
        secret = os.environ.get(entry['token_env'], '')
        with self.lock:
            before = self.state[machine]['status']
        sample = None
        try:
            if len(secret) < 32:
                raise ValueError('Agent token is not configured')
            url = entry['url'].rstrip('/') + '/api/agent/v1'
            parsed = urlparse(url)
            if parsed.scheme not in ('http', 'https') or not parsed.hostname:
                raise ValueError('Invalid agent URL')
            request = urllib.request.Request(url, headers={'Authorization': 'Bearer ' + secret, 'Accept': 'application/json'})
            with OPENER.open(request, timeout=min(max(entry.get('timeout_seconds', 3), 1), 10)) as response:
                if response.status != 200:
                    raise ValueError('Agent returned non-200 status')
                raw = response.read(65537)
                if len(raw) > 65536:
                    raise ValueError('Agent report too large')
                sample = validate_report(json.loads(raw), now)
                if now - sample['ts'] >= entry.get('stale_after_seconds', 30):
                    raise ValueError('Agent measurement is stale')
            status = 'ONLINE' if sample['cpu']['temperature']['celsius'] is not None and (not sample['gpu']['model'] or sample['gpu']['temperature']['celsius'] is not None) else 'DEGRADED'
            self.store.remote_sample(machine, sample, now)
            self.store.remote_check(machine, status, now)
            updated = {'status': status, 'last_success': now, 'system': sample, 'last_polled': now, 'error': None}
        except (OSError, ValueError, TypeError, KeyError, AttributeError, TimeoutError):
            with self.lock:
                last = self.state[machine]['last_success']
            age = now - last if last is not None else float('inf')
            status = 'OFFLINE' if age >= entry.get('offline_after_seconds', 90) else 'STALE' if age >= entry.get('stale_after_seconds', 30) else 'DEGRADED'
            # Never render old temperatures as a live report after a failed poll.
            updated = {'status': status, 'last_success': last, 'system': None, 'last_polled': now, 'error': 'Sensor report unavailable'}
            self.store.remote_check(machine, status, now)
        with self.lock:
            self.state[machine] = updated
        if before not in ('CONNECTING', status):
            self.store.event('machine', 'error' if status == 'OFFLINE' else 'warning' if status in ('STALE', 'DEGRADED') else 'success', f"{entry['name']}: {before} → {status}")
        return self.view_one(entry, now)

    def poll_all(self):
        if not self.config:
            return
        with ThreadPoolExecutor(max_workers=min(4, len(self.config))) as pool:
            list(pool.map(self.poll_one, self.config))

    def view_one(self, entry, now=None):
        now = time.time() if now is None else now
        with self.lock:
            record = dict(self.state[entry['id']])
        last = record['last_success']
        if last is not None and now - last >= entry.get('offline_after_seconds', 90):
            record.update(status='OFFLINE', system=None)
        elif last is not None and now - last >= entry.get('stale_after_seconds', 30):
            record.update(status='STALE', system=None)
        return {'id': entry['id'], 'name': entry['name'], 'status': record['status'], 'last_success': last, 'last_polled': record['last_polled'], 'system': record['system'], 'error': record['error']}

    def snapshot(self):
        return [self.view_one(entry) for entry in self.config]
