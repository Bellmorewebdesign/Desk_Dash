import hashlib
import hmac
import os
from pathlib import Path
import re
import socket
import subprocess
import tempfile
import time

from .collectors import nvme_drives, smart


class ActionError(Exception):
    pass


def storage_target(config):
    settings = config.get('storage_test', {})
    raw = settings.get('directory', '')
    if not raw:
        raise ActionError('Configure storage_test.directory on the NVMe first')
    path = Path(raw).resolve(strict=True)
    if not path.is_dir() or not os.access(path, os.W_OK):
        raise ActionError('Test directory is not writable')
    # The filesystem must be mounted directly on a detected NVMe partition. Avoid root/overlay,
    # removable drives, bind mounts, dm-crypt and LVM ambiguity until explicitly supported.
    valid = [(drive, mount) for drive in nvme_drives() for mount in drive['mounts']]
    matches = [(drive, mount) for drive, mount in valid if Path(mount['mount']).resolve() == path or Path(mount['mount']).resolve() in path.parents]
    if not matches:
        raise ActionError('Directory is not on a directly mounted detected NVMe filesystem')
    drive, mount = max(matches, key=lambda pair: len(pair[1]['mount']))
    if os.stat(path).st_dev != os.stat(mount['mount']).st_dev:
        raise ActionError('Test directory crosses a filesystem boundary')
    size = int(settings.get('size_mib', 128))
    if size < 16 or size > 256:
        raise ActionError('Test size must be between 16 and 256 MiB')
    if os.statvfs(path).f_bavail * os.statvfs(path).f_frsize < (size + 512) * 1048576:
        raise ActionError('At least test size + 512 MiB of free space is required')
    return {'device': drive['device'], 'model': drive['model'], 'mount': mount['mount'], 'directory': str(path), 'size_mib': size, 'cooldown_seconds': max(600, int(settings.get('cooldown_seconds', 3600)))}


def storage_test(config, store):
    target = storage_target(config)
    remaining = store.reserve_action('storage_test', target['cooldown_seconds'])
    if remaining:
        raise ActionError(f'Storage test cooldown: {remaining}s remaining')
    store.event('storage', 'info', f"NVMe test started on {target['device']}")
    before = smart(target['device'])['temperature']
    size_bytes = target['size_mib'] * 1048576
    block = os.urandom(1048576)
    path = None
    start = time.monotonic()
    try:
        # mkstemp uses exclusive creation; never open a user-supplied filename or block device.
        fd, path = tempfile.mkstemp(prefix='.desk-dash-test-', suffix='.tmp', dir=target['directory'])
        digest = hashlib.sha256()
        with os.fdopen(fd, 'w+b') as f:
            write_start = time.monotonic()
            for _ in range(target['size_mib']):
                f.write(block)
                digest.update(block)
            f.flush()
            os.fsync(f.fileno())
            write_seconds = time.monotonic() - write_start
            verify = hashlib.sha256()
            read_start = time.monotonic()
            f.seek(0)
            while data := f.read(1048576):
                verify.update(data)
        read_seconds = time.monotonic() - read_start
        if not hmac.compare_digest(verify.digest(), digest.digest()):
            raise ActionError('Read-back verification failed')
        result = {'success': True, 'target': target, 'write_mib_per_sec': round(target['size_mib'] / write_seconds, 1), 'read_mib_per_sec': round(target['size_mib'] / read_seconds, 1), 'duration_seconds': round(time.monotonic() - start, 2), 'verified_bytes': size_bytes, 'temperature_before': before, 'temperature_after': smart(target['device'])['temperature'], 'note': 'Approximate buffered sequential throughput; read-back may be served from the OS page cache.'}
        store.event('storage', 'success', f"NVMe test completed on {target['device']}: {result['write_mib_per_sec']} MiB/s write")
        return result
    except Exception as exc:
        store.event('storage', 'error', f"NVMe test failed on {target['device']}: {type(exc).__name__}")
        raise
    finally:
        if path:
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass


def service_action(config, store, unit, verb):
    allowed = config.get('controls', {}).get('service_units', [])
    if verb not in ('start', 'stop', 'restart') or not isinstance(unit, str) or not re.fullmatch(r'[A-Za-z0-9_.@:-]+\.service', unit) or unit not in allowed:
        raise ActionError('Service action is not allowlisted')
    remaining = store.reserve_action('service:' + unit, 30)
    if remaining:
        raise ActionError(f'Service action cooldown: {remaining}s remaining')
    result = subprocess.run(['systemctl', verb, unit], capture_output=True, text=True, timeout=15, check=False)
    if result.returncode:
        store.event('service', 'error', f'{verb} failed for {unit}')
        raise ActionError('systemctl failed; check service permissions and journal')
    store.event('service', 'success', f'{verb} requested for {unit}')
    return {'unit': unit, 'action': verb, 'success': True}


def wake(config, store, target_id):
    target = next((x for x in config.get('controls', {}).get('wake_targets', []) if x.get('id') == target_id), None)
    if not target or not re.fullmatch(r'(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}', target.get('mac', '')):
        raise ActionError('Wake target is not allowlisted')
    if store.reserve_action('wake:' + target_id, 60):
        raise ActionError('Wake action cooldown active')
    mac = bytes.fromhex(target['mac'].replace(':', ''))
    packet = b'\xff' * 6 + mac * 16
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.sendto(packet, (target.get('broadcast', '255.255.255.255'), 9))
    store.event('control', 'info', f'Wake packet sent to {target_id}')
    return {'success': True, 'target': target_id}


def reboot(config, store):
    if config.get('controls', {}).get('allow_reboot') is not True:
        raise ActionError('Reboot is disabled in configuration')
    if store.reserve_action('reboot', 300):
        raise ActionError('Reboot cooldown active')
    store.event('control', 'warning', 'Machine reboot requested')
    subprocess.Popen(['systemctl', 'reboot'], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return {'success': True, 'message': 'Reboot requested'}
