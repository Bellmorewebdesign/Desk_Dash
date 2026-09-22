import json
import os
from pathlib import Path


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
    for site in config['sites']:
        if (site.get('enabled', True) and not site['url'].startswith(('https://', 'http://'))) or not site['id'].replace('-', '').isalnum():
            raise ValueError('Site URL or ID is invalid')
    return config
