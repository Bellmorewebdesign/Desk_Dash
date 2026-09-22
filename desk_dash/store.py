import json
import sqlite3
import threading
import time


class Store:
    def __init__(self, path):
        self.lock = threading.RLock()
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, ts REAL NOT NULL, kind TEXT NOT NULL, severity TEXT NOT NULL, message TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS site_checks (id INTEGER PRIMARY KEY, ts REAL NOT NULL, site TEXT NOT NULL, status TEXT NOT NULL, http_status INTEGER, ms REAL);
            CREATE INDEX IF NOT EXISTS checks_site_time ON site_checks(site, ts);
            CREATE TABLE IF NOT EXISTS samples (id INTEGER PRIMARY KEY, ts REAL NOT NULL, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS action_times (action TEXT PRIMARY KEY, ts REAL NOT NULL);
        ''')
        self.db.commit()

    def event(self, kind, severity, message):
        with self.lock:
            self.db.execute('INSERT INTO events(ts,kind,severity,message) VALUES(?,?,?,?)', (time.time(), kind, severity, message[:400]))
            self.db.commit()

    def events(self, kind='', limit=100):
        with self.lock:
            rows = self.db.execute('SELECT id,ts,kind,severity,message FROM events WHERE (?="" OR kind=?) ORDER BY id DESC LIMIT ?', (kind, kind, min(max(limit, 1), 200))).fetchall()
        return [dict(zip(('id', 'ts', 'kind', 'severity', 'message'), row)) for row in rows]

    def check(self, site, status, http_status, ms):
        with self.lock:
            self.db.execute('INSERT INTO site_checks(ts,site,status,http_status,ms) VALUES(?,?,?,?,?)', (time.time(), site, status, http_status, ms))
            self.db.commit()

    def site_history(self, site, limit=60):
        with self.lock:
            rows = self.db.execute('SELECT ts,status,http_status,ms FROM site_checks WHERE site=? ORDER BY id DESC LIMIT ?', (site, limit)).fetchall()
            success = self.db.execute("SELECT SUM(status='ONLINE'), COUNT(*), MAX(CASE WHEN status='ONLINE' THEN ts END) FROM site_checks WHERE site=?", (site,)).fetchone()
        return {'checks': [dict(zip(('ts', 'status', 'http_status', 'ms'), row)) for row in reversed(rows)], 'uptime_percent': round(100 * (success[0] or 0) / success[1], 1) if success[1] else None, 'last_success': success[2]}

    def sample(self, payload):
        with self.lock:
            self.db.execute('INSERT INTO samples(ts,payload) VALUES(?,?)', (time.time(), json.dumps(payload)))
            self.db.commit()

    def history(self, limit=90):
        with self.lock:
            rows = self.db.execute('SELECT ts,payload FROM samples ORDER BY id DESC LIMIT ?', (min(max(limit, 1), 720),)).fetchall()
        return [{'ts': ts, **json.loads(payload)} for ts, payload in reversed(rows)]

    def reserve_action(self, key, cooldown):
        now = time.time()
        with self.lock:
            previous = self.db.execute('SELECT ts FROM action_times WHERE action=?', (key,)).fetchone()
            if previous and now - previous[0] < cooldown:
                return round(cooldown - (now - previous[0]))
            self.db.execute('INSERT INTO action_times(action,ts) VALUES(?,?) ON CONFLICT(action) DO UPDATE SET ts=excluded.ts', (key, now))
            self.db.commit()
        return 0

    def prune(self):
        with self.lock:
            cutoff = time.time() - 30 * 86400
            for table in ('events', 'site_checks', 'samples'):
                self.db.execute(f'DELETE FROM {table} WHERE ts < ?', (cutoff,))
            self.db.commit()
