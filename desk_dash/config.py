import json
import os
from pathlib import Path
import re
from urllib.parse import urlparse


def load_env(path):
    """Read local environment settings without overwriting process environment."""
    p = Path(path)
    if not p.is_file():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        key, value = line.split('=', 1)
        if key.startswith('DASH_'):
            os.environ.setdefault(key, value.strip().strip('"').strip("'"))


def load_config(path):
    default = Path(__file__).resolve().parent.parent / 'config.example.json'
    config = json.loads(default.read_text())
    p = Path(path)
    if p.is_file():
        provided = json.loads(p.read_text())
        if not isinstance(provided, dict):
            raise ValueError('Configuration must be an object')
        config.update(provided)
        config['controls'] = {**json.loads(default.read_text())['controls'], **provided.get('controls', {})}
        config['storage_test'] = {**json.loads(default.read_text())['storage_test'], **provided.get('storage_test', {})}
    site_ids = set()
    for site in config['sites']:
        env_key = site.get('url_env')
        if env_key:
            if not re.fullmatch(r'DASH_[A-Z0-9_]+', env_key):
                raise ValueError('Invalid site URL environment key')
            if os.environ.get(env_key):
                site['url'], site['enabled'] = os.environ[env_key], True
        parsed = urlparse(site.get('url', ''))
        if not re.fullmatch(r'[a-zA-Z0-9-]+', site.get('id', '')) or site['id'] in site_ids:
            raise ValueError('Site ID must be unique and URL-safe')
        site_ids.add(site['id'])
        if site.get('enabled', True) and (parsed.scheme not in ('https', 'http') or not parsed.hostname or parsed.username or parsed.password):
            raise ValueError('Configured website URL must be HTTP(S) without credentials')
        if not 1 <= site.get('timeout_seconds', 6) <= 30 or not 1 <= site.get('failure_threshold', 3) <= 10:
            raise ValueError('Invalid website timeout or failure threshold')
    remote_ids = set()
    for entry in config.get('remote_agents', []):
        url = urlparse(entry.get('url', ''))
        if not re.fullmatch(r'[a-zA-Z0-9-]+', entry.get('id', '')) or entry['id'] in remote_ids:
            raise ValueError('Remote agent ID must be unique and URL-safe')
        remote_ids.add(entry['id'])
        if url.scheme not in ('http', 'https') or not url.hostname or url.username or url.password or url.path not in ('', '/') or url.query or url.fragment:
            raise ValueError('Agent URL must be an HTTP(S) origin without credentials or path')
        if not re.fullmatch(r'DASH_AGENT_[A-Z0-9_]+', entry.get('token_env', '')):
            raise ValueError('Agent token_env must name a DASH_AGENT_ environment variable')
        if not 5 <= entry.get('stale_after_seconds', 45) < entry.get('offline_after_seconds', 120) <= 3600:
            raise ValueError('Remote stale/offline thresholds invalid')
    if not 5 <= config.get('remote_poll_seconds', 15) <= 60:
        raise ValueError('remote_poll_seconds must be 5–60')
    return config
