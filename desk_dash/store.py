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
            CREATE TABLE IF NOT EXISTS site_checks (id INTEGER PRIMARY KEY, ts REAL NOT NULL, site TEXT NOT NULL, status TEXT NOT NULL, http_status INTEGER, ms REAL, success INTEGER);
            CREATE INDEX IF NOT EXISTS checks_site_time ON site_checks(site, ts);
            CREATE TABLE IF NOT EXISTS samples (id INTEGER PRIMARY KEY, ts REAL NOT NULL, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS action_times (action TEXT PRIMARY KEY, ts REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS remote_samples (id INTEGER PRIMARY KEY, machine TEXT NOT NULL, received_ts REAL NOT NULL, sample_ts REAL NOT NULL, payload TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS remote_samples_machine_time ON remote_samples(machine, received_ts);
            CREATE TABLE IF NOT EXISTS remote_checks (id INTEGER PRIMARY KEY, machine TEXT NOT NULL, ts REAL NOT NULL, status TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS remote_checks_machine_time ON remote_checks(machine, ts);
        ''')
        # Upgrade existing production SQLite databases in place.
        if not any(row[1] == 'success' for row in self.db.execute('PRAGMA table_info(site_checks)')):
            self.db.execute('ALTER TABLE site_checks ADD COLUMN success INTEGER')
        self.db.execute("UPDATE site_checks SET success = CASE WHEN status='ONLINE' THEN 1 ELSE 0 END WHERE success IS NULL")
        self.db.commit()

    def event(self, kind, severity, message):
        with self.lock:
            self.db.execute('INSERT INTO events(ts,kind,severity,message) VALUES(?,?,?,?)', (time.time(), kind, severity, message[:400]))
            self.db.commit()

    def events(self, kind='', limit=100):
        with self.lock:
            rows = self.db.execute('SELECT id,ts,kind,severity,message FROM events WHERE (?="" OR kind=?) ORDER BY id DESC LIMIT ?', (kind, kind, min(max(limit, 1), 200))).fetchall()
        return [dict(zip(('id', 'ts', 'kind', 'severity', 'message'), row)) for row in rows]

    def check(self, site, status, http_status, ms, success=None):
        with self.lock:
            self.db.execute('INSERT INTO site_checks(ts,site,status,http_status,ms,success) VALUES(?,?,?,?,?,?)', (time.time(), site, status, http_status, ms, int(success) if success is not None else int(status == 'ONLINE')))
            self.db.commit()

    def site_failure_streak(self, site):
        with self.lock:
            rows = self.db.execute('SELECT success FROM site_checks WHERE site=? ORDER BY id DESC LIMIT 50', (site,)).fetchall()
        streak = 0
        for (success,) in rows:
            if success:
                break
            streak += 1
        return streak

    def site_history(self, site, limit=60):
        with self.lock:
            rows = self.db.execute('SELECT ts,status,http_status,ms FROM site_checks WHERE site=? ORDER BY id DESC LIMIT ?', (site, limit)).fetchall()
            success = self.db.execute('SELECT SUM(success), COUNT(*), MAX(CASE WHEN success=1 THEN ts END) FROM site_checks WHERE site=?', (site,)).fetchone()
        return {'checks': [dict(zip(('ts', 'status', 'http_status', 'ms'), row)) for row in reversed(rows)], 'uptime_percent': round(100 * (success[0] or 0) / success[1], 1) if success[1] else None, 'last_success': success[2]}

    def sample(self, payload):
        with self.lock:
            self.db.execute('INSERT INTO samples(ts,payload) VALUES(?,?)', (time.time(), json.dumps(payload)))
            self.db.commit()

    def history(self, limit=90):
        with self.lock:
            rows = self.db.execute('SELECT ts,payload FROM samples ORDER BY id DESC LIMIT ?', (min(max(limit, 1), 720),)).fetchall()
        return [{'ts': ts, **json.loads(payload)} for ts, payload in reversed(rows)]

    def remote_sample(self, machine, sample, received_ts=None):
        received_ts = time.time() if received_ts is None else received_ts
        with self.lock:
            self.db.execute('INSERT INTO remote_samples(machine,received_ts,sample_ts,payload) VALUES(?,?,?,?)', (machine, received_ts, sample['ts'], json.dumps(sample)))
            self.db.commit()

    def last_remote(self, machine):
        with self.lock:
            row = self.db.execute('SELECT received_ts FROM remote_samples WHERE machine=? ORDER BY id DESC LIMIT 1', (machine,)).fetchone()
        return row[0] if row else None

    def remote_history(self, machine, limit=90):
        with self.lock:
            rows = self.db.execute('SELECT received_ts,payload FROM remote_samples WHERE machine=? ORDER BY id DESC LIMIT ?', (machine, min(max(limit, 1), 720))).fetchall()
        return [{'received_at': ts, 'system': json.loads(payload)} for ts, payload in reversed(rows)]

    def remote_check(self, machine, status, ts=None):
        with self.lock:
            self.db.execute('INSERT INTO remote_checks(machine,ts,status) VALUES(?,?,?)', (machine, time.time() if ts is None else ts, status))
            self.db.commit()

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
            self.db.execute('DELETE FROM remote_samples WHERE received_ts < ?', (cutoff,))
            self.db.execute('DELETE FROM remote_checks WHERE ts < ?', (cutoff,))
            self.db.commit()
