"""Read-only host capability report. No packages or configuration are changed."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

from .collectors import Sampler, read, run


def main():
    sample = Sampler().collect()
    sample = Sampler().collect()
    report = {
        'cpu_model': sample['cpu']['model'],
        'ram_total_bytes': sample['ram']['total_bytes'],
        'gpu': sample['gpu'],
        'nvidia_driver': run(['nvidia-smi', '--query-gpu=driver_version', '--format=csv,noheader']),
        'block_devices': run(['lsblk', '-J', '-o', 'NAME,SIZE,MODEL,TYPE,FSTYPE,MOUNTPOINTS']),
        'nvme_drives': sample['drives'],
        'hwmon_sensors': [],
        'network_interfaces': list(sample['network_interfaces']),
        'utilities': {name: shutil.which(name) for name in ('nvidia-smi', 'nvme', 'smartctl', 'sensors', 'systemctl', 'docker', 'pm2', 'node', 'python3', 'journalctl')},
        'node_version': run(['node', '--version']),
        'python_version': sys.version.split()[0],
        'candidate_services': [],
        'docker_containers': run(['docker', 'ps', '--format', '{{.Names}} {{.Image}} {{.Status}}'], timeout=5),
        'pm2_apps': run(['pm2', 'ls', '--no-color'], timeout=5),
        'alfred_processes': [],
    }
    for sensor in Path('/sys/class/hwmon').glob('hwmon*'):
        report['hwmon_sensors'].append({'name': read(sensor / 'name'), 'labels': [read(x) for x in sensor.glob('temp*_label')]})
    units = run(['systemctl', 'list-units', '--type=service', '--all', '--no-legend', '--no-pager']) or ''
    markers = ('alfred', 'node', 'python', 'docker', 'pm2', 'flask', 'coursen', 'browser', 'web', 'bot')
    report['candidate_services'] = [line.strip()[:180] for line in units.splitlines() if any(marker in line.lower() for marker in markers)][:70]
    for proc in Path('/proc').glob('[0-9]*'):
        cmd = read(proc / 'comm') or ''
        if 'alfred' in cmd.lower():
            report['alfred_processes'].append({'pid': int(proc.name), 'name': cmd})
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
