"""Version 1 wire format shared by Linux and Windows agents."""
import math
import time

from .collectors import temperature


VERSION = 1


def numeric(value, minimum=0, maximum=1e18):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return value if minimum <= value <= maximum else None


def short(value):
    return value[:160] if isinstance(value, str) else None


def temp(value, kind):
    raw = value.get('celsius') if isinstance(value, dict) else None
    return temperature(numeric(raw, -30, 150), kind)


def normalize_system(sample):
    """Discard unexpected keys and recalculate temperature labels on the central server."""
    if not isinstance(sample, dict):
        raise ValueError('Invalid system payload')
    cpu, gpu, ram = (sample.get(key) or {} for key in ('cpu', 'gpu', 'ram'))
    if not all(isinstance(item, dict) for item in (cpu, gpu, ram)):
        raise ValueError('Invalid hardware section')
    raw_disks = sample.get('filesystems') or []
    raw_interfaces = sample.get('network_interfaces') or {}
    if not isinstance(raw_disks, list) or not isinstance(raw_interfaces, dict):
        raise ValueError('Invalid disk or network section')
    disks = []
    for entry in raw_disks[:20]:
        if isinstance(entry, dict):
            disks.append({'mount': short(entry.get('mount')), 'source': short(entry.get('source')), 'filesystem': short(entry.get('filesystem')), 'total_bytes': numeric(entry.get('total_bytes')), 'used_bytes': numeric(entry.get('used_bytes')), 'free_bytes': numeric(entry.get('free_bytes'))})
    interfaces = {}
    for name, counters in list(raw_interfaces.items())[:16]:
        if isinstance(name, str) and isinstance(counters, dict):
            interfaces[name[:80]] = {'rx_bytes_per_sec': numeric(counters.get('rx_bytes_per_sec')), 'tx_bytes_per_sec': numeric(counters.get('tx_bytes_per_sec'))}
    normalized = {
        'ts': numeric(sample.get('ts'), 1, 1e11),
        'hostname': short(sample.get('hostname')),
        'cpu': {'model': short(cpu.get('model')), 'percent': numeric(cpu.get('percent'), 0, 100), 'mhz': numeric(cpu.get('mhz'), 0, 100000), 'temperature': temp(cpu.get('temperature'), 'cpu'), 'load': None},
        'gpu': {'model': short(gpu.get('model')), 'temperature': temp(gpu.get('temperature'), 'gpu'), 'utilization': numeric(gpu.get('utilization'), 0, 100), 'vram_used_mib': numeric(gpu.get('vram_used_mib')), 'vram_total_mib': numeric(gpu.get('vram_total_mib')), 'power_w': numeric(gpu.get('power_w'), 0, 10000), 'fan_percent': numeric(gpu.get('fan_percent'), 0, 100)},
        'ram': {'used_bytes': numeric(ram.get('used_bytes')), 'total_bytes': numeric(ram.get('total_bytes'))},
        'filesystems': disks,
        'network_interfaces': interfaces,
        'uptime_seconds': numeric(sample.get('uptime_seconds')),
        'process_count': numeric(sample.get('process_count')),
    }
    if normalized['ts'] is None or normalized['hostname'] is None or normalized['ram']['total_bytes'] is None or normalized['cpu']['model'] is None:
        raise ValueError('Required metrics missing')
    return normalized


def envelope(sample, display_name=None):
    normalized = normalize_system(sample)
    complete = normalized['cpu']['temperature']['celsius'] is not None and (not normalized['gpu']['model'] or normalized['gpu']['temperature']['celsius'] is not None)
    return {'schema_version': VERSION, 'ts': normalized['ts'], 'hostname': normalized['hostname'], 'display_name': short(display_name) or normalized['hostname'], 'status': 'ONLINE' if complete else 'DEGRADED', 'system': normalized}


def validate_report(report, now=None):
    now = time.time() if now is None else now
    if not isinstance(report, dict) or report.get('schema_version') != VERSION:
        raise ValueError('Unsupported agent schema')
    sample = normalize_system(report.get('system'))
    if report.get('hostname') != sample['hostname'] or report.get('ts') != sample['ts']:
        raise ValueError('Agent timestamps or hostname disagree')
    if abs(now - sample['ts']) > 300:
        raise ValueError('Agent clock or sample is over five minutes out of sync')
    return sample
