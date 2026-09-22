"""Read-only Linux collectors. All external commands use fixed argument lists and timeouts."""
import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import time


def run(args, timeout=3):
    if not shutil.which(args[0]):
        return None
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False, env={**os.environ, 'LC_ALL': 'C', 'SYSTEMD_PAGER': ''})
        return result.stdout if result.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def read(path):
    try:
        return Path(path).read_text().strip()
    except (OSError, UnicodeError):
        return None


def number(value):
    try:
        return float(value)
    except (ValueError, TypeError):
        return None


def temperature(value, kind='cpu'):
    if value is None:
        return {'celsius': None, 'level': 'UNAVAILABLE'}
    good, warm, hot = {'cpu': (70, 85, 95), 'gpu': (72, 83, 90), 'nvme': (55, 70, 80)}[kind]
    return {'celsius': round(value, 1), 'level': 'GOOD' if value < good else 'WARM' if value < warm else 'HOT' if value < hot else 'CRITICAL'}


def cpu_temperature():
    candidates = []
    for sensor in Path('/sys/class/hwmon').glob('hwmon*'):
        name = (read(sensor / 'name') or '').lower()
        for input_file in sensor.glob('temp*_input'):
            label = (read(str(input_file).replace('_input', '_label')) or '').lower()
            value = number(read(input_file))
            if value is not None and -20 < value / 1000 < 130:
                priority = 2 if name in ('k10temp', 'coretemp') else 1 if 'package' in label or 'cpu' in label else 0
                candidates.append((priority, value / 1000))
    if candidates:
        candidates.sort(reverse=True)
        return temperature(candidates[0][1])
    return temperature(None)


def gpu():
    fields = ['name', 'temperature.gpu', 'utilization.gpu', 'memory.used', 'memory.total', 'power.draw']
    output = run(['nvidia-smi', '--query-gpu=' + ','.join(fields), '--format=csv,noheader,nounits'])
    if not output:
        return {'model': None, 'temperature': temperature(None, 'gpu'), 'utilization': None, 'vram_used_mib': None, 'vram_total_mib': None, 'power_w': None, 'fan_percent': None}
    parts = [part.strip() for part in output.splitlines()[0].split(',')]
    parts += [None] * (6 - len(parts))
    fan = run(['nvidia-smi', '--query-gpu=fan.speed', '--format=csv,noheader,nounits'])
    return {'model': parts[0], 'temperature': temperature(number(parts[1]), 'gpu'), 'utilization': number(parts[2]), 'vram_used_mib': number(parts[3]), 'vram_total_mib': number(parts[4]), 'power_w': number(parts[5]), 'fan_percent': number(fan.splitlines()[0].strip()) if fan else None}


def cpu_times():
    line = read('/proc/stat')
    if not line:
        return None
    fields = [int(x) for x in line.splitlines()[0].split()[1:]]
    return sum(fields), fields[3] + fields[4]


def disk_counters(device):
    for line in (read('/proc/diskstats') or '').splitlines():
        parts = line.split()
        if len(parts) >= 14 and parts[2] == device:
            return int(parts[5]) * 512, int(parts[9]) * 512
    return None


def net_counters():
    result = {}
    for line in (read('/proc/net/dev') or '').splitlines()[2:]:
        if ':' not in line:
            continue
        name, values = line.split(':', 1)
        vals = values.split()
        if name.strip() != 'lo' and len(vals) >= 9:
            result[name.strip()] = (int(vals[0]), int(vals[8]))
    return result


def filesystems():
    seen, result = set(), []
    for line in (read('/proc/mounts') or '').splitlines():
        parts = line.split()
        if len(parts) < 3 or not parts[0].startswith('/dev/') or parts[1] in seen:
            continue
        seen.add(parts[1])
        try:
            usage = shutil.disk_usage(parts[1].replace('\\040', ' '))
            result.append({'source': parts[0], 'mount': parts[1].replace('\\040', ' '), 'filesystem': parts[2], 'total_bytes': usage.total, 'used_bytes': usage.used, 'free_bytes': usage.free})
        except OSError:
            pass
    return result


def nvme_drives():
    drives = []
    for dev in sorted(Path('/sys/class/block').glob('nvme*n*')):
        if not re.fullmatch(r'nvme\d+n\d+', dev.name):
            continue
        model = read(dev / 'device/model') or read(dev / 'device/device/model') or 'NVMe drive'
        sectors = number(read(dev / 'size'))
        paths = []
        mounts = read('/proc/mounts') or ''
        for line in mounts.splitlines():
            parts = line.split()
            if len(parts) < 3:
                continue
            source, mount = parts[0], parts[1].replace('\\040', ' ')
            if re.fullmatch(r'/dev/' + re.escape(dev.name) + r'(p\d+)?', source):
                try:
                    usage = shutil.disk_usage(mount)
                    paths.append({'mount': mount, 'source': source, 'filesystem': parts[2], 'capacity_bytes': usage.total, 'used_bytes': usage.used, 'free_bytes': usage.free})
                except OSError:
                    pass
        drives.append({'device': dev.name, 'model': model.strip(), 'capacity_bytes': int(sectors * 512) if sectors is not None else None, 'mounts': paths})
    return drives


def smart(device):
    # NVMe CLI may require permissions. No secrets or serial numbers are returned.
    raw = run(['nvme', 'smart-log', '-o', 'json', '/dev/' + device])
    if raw:
        try:
            data = json.loads(raw)
            kelvin = number(data.get('temperature'))
            return {'source': 'nvme-cli', 'temperature': temperature(kelvin - 273.15 if kelvin and kelvin > 200 else kelvin, 'nvme'), 'health': 'GOOD' if number(data.get('critical_warning')) == 0 else 'WARNING', 'percentage_used': number(data.get('percentage_used')), 'power_on_hours': number(data.get('power_on_hours')), 'unsafe_shutdowns': number(data.get('unsafe_shutdowns')), 'media_errors': number(data.get('media_errors')), 'data_units_read': number(data.get('data_units_read')), 'data_units_written': number(data.get('data_units_written'))}
        except (ValueError, TypeError):
            pass
    raw = run(['smartctl', '-j', '-a', '/dev/' + device])
    if raw:
        try:
            data = json.loads(raw)
            log = data.get('nvme_smart_health_information_log', {})
            return {'source': 'smartctl', 'temperature': temperature(number(data.get('temperature', {}).get('current')), 'nvme'), 'health': 'GOOD' if data.get('smart_status', {}).get('passed') is True else 'WARNING' if data.get('smart_status', {}).get('passed') is False else 'UNAVAILABLE', 'percentage_used': number(log.get('percentage_used')), 'power_on_hours': number(log.get('power_on_hours')), 'unsafe_shutdowns': number(log.get('unsafe_shutdowns')), 'media_errors': number(log.get('media_errors')), 'data_units_read': number(log.get('data_units_read')), 'data_units_written': number(log.get('data_units_written'))}
        except (ValueError, TypeError):
            pass
    for sensor in Path('/sys/class/hwmon').glob('hwmon*'):
        if 'nvme' in (read(sensor / 'name') or '').lower():
            value = number(read(sensor / 'temp1_input'))
            if value is not None:
                return {'source': 'hwmon', 'temperature': temperature(value / 1000, 'nvme'), 'health': 'UNAVAILABLE'}
    return {'source': None, 'temperature': temperature(None, 'nvme'), 'health': 'UNAVAILABLE'}


def gateway():
    for line in (read('/proc/net/route') or '').splitlines()[1:]:
        parts = line.split()
        if len(parts) > 2 and parts[1] == '00000000':
            try:
                return socket.inet_ntoa(bytes.fromhex(parts[2])[::-1])
            except (ValueError, OSError):
                pass
    return None


def local_ip():
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(('192.0.2.1', 80))
            return s.getsockname()[0]
    except OSError:
        return None


def network_probe():
    start = time.monotonic()
    try:
        socket.getaddrinfo('example.com', 443)
        dns_ms = round((time.monotonic() - start) * 1000)
        dns_ok = True
    except OSError:
        dns_ms, dns_ok = None, False
    gw = gateway()
    start = time.monotonic()
    try:
        with socket.create_connection((gw, 53), timeout=1):
            pass
        gateway_ms = round((time.monotonic() - start) * 1000)
    except (OSError, TypeError):
        gateway_ms = None
    return {'dns_ok': dns_ok, 'dns_ms': dns_ms, 'gateway': gw, 'gateway_ms': gateway_ms, 'local_ip': local_ip(), 'checked_at': time.time()}


def service(entry):
    unit = entry.get('unit', '')
    if not re.fullmatch(r'[A-Za-z0-9_.@:-]+\.service', unit):
        return {'id': entry.get('id'), 'name': entry.get('name'), 'status': 'UNAVAILABLE', 'detail': 'Invalid unit configuration'}
    args = ['systemctl'] + (['--user'] if entry.get('scope') == 'user' else []) + ['show', unit, '--property=LoadState,ActiveState,SubState,MainPID,MemoryCurrent,CPUUsageNSec,ActiveEnterTimestamp,ActiveEnterTimestampMonotonic,ExecMainStartTimestamp', '--no-pager']
    output = run(args)
    values = dict(line.split('=', 1) for line in (output or '').splitlines() if '=' in line)
    active = values.get('ActiveState')
    status = 'RUNNING' if active == 'active' else 'FAILED' if active == 'failed' else 'STOPPED' if values.get('LoadState') == 'loaded' else 'UNAVAILABLE'
    uptime = number((read('/proc/uptime') or '0').split()[0])
    active_mono = number(values.get('ActiveEnterTimestampMonotonic'))
    return {'id': entry['id'], 'name': entry['name'], 'unit': unit, 'status': status, 'pid': int(values.get('MainPID') or 0) or None, 'memory_bytes': number(values.get('MemoryCurrent')), 'cpu_seconds_total': round(float(values['CPUUsageNSec']) / 1e9, 1) if values.get('CPUUsageNSec', '').isdigit() else None, 'cpu_percent': None, 'uptime_seconds': max(0, round(uptime - active_mono / 1e6)) if status == 'RUNNING' and uptime is not None and active_mono else None, 'started_at': values.get('ExecMainStartTimestamp') or None, 'last_active_at': values.get('ActiveEnterTimestamp') or None}


def discover_alfred():
    for proc in Path('/proc').glob('[0-9]*'):
        comm = read(proc / 'comm')
        if comm and 'alfred' in comm.lower():
            return {'id': 'alfred-process', 'name': 'Alfred (process)', 'status': 'RUNNING', 'pid': int(proc.name), 'memory_bytes': None, 'cpu_seconds_total': None, 'started_at': None}
    return None


class Sampler:
    def __init__(self):
        self.prev_cpu = None
        self.prev_net = None
        self.prev_disks = {}
        self.prev_at = None

    def collect(self):
        now = time.monotonic()
        cpu = cpu_times()
        cpu_percent = None
        if cpu and self.prev_cpu:
            total, idle = cpu[0] - self.prev_cpu[0], cpu[1] - self.prev_cpu[1]
            cpu_percent = round(100 * (total - idle) / total, 1) if total > 0 else None
        self.prev_cpu = cpu
        delta = now - self.prev_at if self.prev_at else None
        nets = net_counters()
        throughput = {}
        for name, vals in nets.items():
            previous = (self.prev_net or {}).get(name)
            throughput[name] = {'rx_bytes_per_sec': round(max(0, vals[0] - previous[0]) / delta) if previous and delta else None, 'tx_bytes_per_sec': round(max(0, vals[1] - previous[1]) / delta) if previous and delta else None}
        self.prev_net = nets
        drives = nvme_drives()
        for drive in drives:
            counters = disk_counters(drive['device'])
            previous = self.prev_disks.get(drive['device'])
            drive['read_bytes_per_sec'] = round(max(0, counters[0] - previous[0]) / delta) if counters and previous and delta else None
            drive['write_bytes_per_sec'] = round(max(0, counters[1] - previous[1]) / delta) if counters and previous and delta else None
            if counters:
                self.prev_disks[drive['device']] = counters
            drive['smart'] = smart(drive['device'])
        self.prev_at = now
        mem = {}
        for line in (read('/proc/meminfo') or '').splitlines():
            if ':' in line:
                key, raw = line.split(':', 1)
                mem[key] = int(raw.split()[0]) * 1024
        cpuinfo = read('/proc/cpuinfo') or ''
        model = next((line.split(':', 1)[1].strip() for line in cpuinfo.splitlines() if line.startswith('model name')), None)
        mhz = [number(line.split(':', 1)[1].strip()) for line in cpuinfo.splitlines() if line.startswith('cpu MHz')]
        uptime = (read('/proc/uptime') or '0').split()[0]
        try:
            loads = [round(x, 2) for x in os.getloadavg()]
        except OSError:
            loads = None
        return {'ts': time.time(), 'hostname': socket.gethostname(), 'cpu': {'model': model, 'percent': cpu_percent, 'mhz': round(sum(mhz) / len(mhz)) if mhz else None, 'temperature': cpu_temperature(), 'load': loads}, 'ram': {'total_bytes': mem.get('MemTotal'), 'used_bytes': mem.get('MemTotal', 0) - mem.get('MemAvailable', 0)}, 'gpu': gpu(), 'uptime_seconds': int(float(uptime)), 'process_count': len(list(Path('/proc').glob('[0-9]*'))), 'network_interfaces': throughput, 'filesystems': filesystems(), 'drives': drives}
