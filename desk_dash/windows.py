"""Windows host collector: OS counters plus LibreHardwareMonitor sensor values."""
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import time
import urllib.request
from urllib.parse import urlparse

from .collectors import gpu as nvidia_gpu, number, temperature


SCRIPT = Path(__file__).resolve().parent.parent / 'deploy' / 'windows-metrics.ps1'


def sensor_nodes(tree):
    """Walk LHM data.json; hardware ancestry disambiguates CPU/GPU sensors."""
    def visit(node, hardware_id='', hardware_name=''):
        if not isinstance(node, dict):
            return
        hardware_id = node.get('HardwareId') or hardware_id
        if node.get('HardwareId'):
            hardware_name = node.get('Text') or hardware_name
        if 'SensorId' in node:
            yield {'hardware_id': str(hardware_id).lower(), 'hardware_name': str(hardware_name), 'name': str(node.get('Text', '')), 'type': str(node.get('Type', '')), 'value': number(node.get('RawValue'))}
        for child in node.get('Children', []):
            yield from visit(child, hardware_id, hardware_name)
    return list(visit(tree))


def pick(sensors, sensor_type, names):
    for name in names:
        for item in sensors:
            if item['type'].lower() == sensor_type.lower() and item['name'].lower() == name.lower() and item['value'] is not None:
                return item['value']
    return None


def lhm_metrics(url):
    parsed = urlparse(url)
    if parsed.scheme != 'http' or parsed.hostname not in ('127.0.0.1', 'localhost', '::1') or parsed.path != '/data.json':
        raise ValueError('LibreHardwareMonitor URL must be loopback /data.json')
    with urllib.request.urlopen(url, timeout=2) as response:
        raw = response.read(1048577)
        if len(raw) > 1048576:
            raise ValueError('Sensor report is too large')
    sensors = sensor_nodes(json.loads(raw))
    cpus = [x for x in sensors if re.search(r'/(?:intelcpu|amdcpu)/', x['hardware_id'])]
    gpus = [x for x in sensors if re.search(r'/(?:nvidiagpu|atigpu|intelgpu|gpu-nvidia|gpu-amd|gpu-intel)/', x['hardware_id'])]
    cpu_temp = pick(cpus, 'Temperature', ('CPU Package', 'CPU Tctl/Tdie', 'CPU (Tctl/Tdie)', 'CPU Core Max', 'Core Max', 'Core Average'))
    gpu_temp = pick(gpus, 'Temperature', ('GPU Core', 'GPU Temperature', 'GPU Hot Spot'))
    return {
        'cpu_model': cpus[0]['hardware_name'] if cpus else None,
        'cpu_temp': cpu_temp,
        'gpu_model': gpus[0]['hardware_name'] if gpus else None,
        'gpu_temp': gpu_temp,
        'gpu_usage': pick(gpus, 'Load', ('GPU Core', 'GPU Core Load', 'GPU Total')),
        'gpu_power': pick(gpus, 'Power', ('GPU Power', 'GPU Package', 'GPU Board Power')),
        'gpu_fan': pick(gpus, 'Control', ('GPU Fan', 'GPU Fan 1')),
        'gpu_vram_used': pick(gpus, 'SmallData', ('GPU Memory Used', 'GPU Dedicated Memory Used')),
        'gpu_vram_total': pick(gpus, 'SmallData', ('GPU Memory Total', 'GPU Dedicated Memory Total')),
    }


class WindowsSampler:
    def __init__(self):
        self.previous = {}
        self.previous_at = None

    def collect(self):
        result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-File', str(SCRIPT)], capture_output=True, text=True, timeout=10, check=True)
        os_data = json.loads(result.stdout)
        now = time.time()
        sensors = {}
        try:
            sensors = lhm_metrics(os.environ.get('DASH_LHM_URL', 'http://127.0.0.1:8085/data.json'))
        except (OSError, ValueError, TimeoutError):
            pass
        nvidia = nvidia_gpu()
        gpu_model = sensors.get('gpu_model') or nvidia['model']
        cpu_temp = sensors.get('cpu_temp')
        gpu_temp = sensors.get('gpu_temp')
        if gpu_temp is None:
            gpu_temp = nvidia['temperature']['celsius']
        network = {}
        for name, counters in (os_data.get('network') or {}).items():
            previous = self.previous.get(name)
            delta = now - self.previous_at if self.previous_at else 0
            network[name] = {'rx_bytes_per_sec': max(0, round((counters['rx'] - previous['rx']) / delta)) if previous and delta else None, 'tx_bytes_per_sec': max(0, round((counters['tx'] - previous['tx']) / delta)) if previous and delta else None}
        self.previous, self.previous_at = os_data.get('network') or {}, now
        return {
            'ts': now, 'hostname': socket.gethostname(),
            'cpu': {'model': sensors.get('cpu_model') or os_data.get('cpu_model'), 'percent': number(os_data.get('cpu_percent')), 'mhz': number(os_data.get('cpu_mhz')), 'temperature': temperature(cpu_temp, 'cpu'), 'load': None},
            'gpu': {'model': gpu_model, 'temperature': temperature(gpu_temp, 'gpu'), 'utilization': sensors.get('gpu_usage') if sensors.get('gpu_usage') is not None else nvidia['utilization'], 'vram_used_mib': sensors.get('gpu_vram_used') if sensors.get('gpu_vram_used') is not None else nvidia['vram_used_mib'], 'vram_total_mib': sensors.get('gpu_vram_total') if sensors.get('gpu_vram_total') is not None else nvidia['vram_total_mib'], 'power_w': sensors.get('gpu_power') if sensors.get('gpu_power') is not None else nvidia['power_w'], 'fan_percent': sensors.get('gpu_fan') if sensors.get('gpu_fan') is not None else nvidia['fan_percent']},
            'ram': {'used_bytes': os_data['ram_total_bytes'] - os_data['ram_free_bytes'], 'total_bytes': os_data['ram_total_bytes']},
            'filesystems': os_data.get('filesystems') or [], 'network_interfaces': network,
            'uptime_seconds': os_data.get('uptime_seconds'), 'process_count': os_data.get('process_count'),
        }
