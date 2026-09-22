import io
import json
import tempfile
import threading
import unittest
from unittest.mock import patch
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from desk_dash.checks import check_site
from desk_dash.protocol import envelope
from desk_dash.remotes import RemoteMonitor
from desk_dash.store import Store
from desk_dash.windows import WindowsSampler, lhm_metrics


def report(ts):
    return envelope({'ts': ts, 'hostname': 'second-pc', 'cpu': {'model': 'Ryzen', 'percent': 22, 'temperature': {'celsius': 42, 'level': 'CRITICAL'}}, 'gpu': {'model': 'RTX', 'utilization': 9, 'temperature': {'celsius': 39}}, 'ram': {'total_bytes': 32000000000, 'used_bytes': 6000000000}, 'filesystems': [{'mount': 'C:', 'total_bytes': 1000, 'used_bytes': 100}], 'network_interfaces': {}, 'uptime_seconds': 1000})


class Response(io.BytesIO):
    status = 200
    def __enter__(self): return self
    def __exit__(self, *args): self.close()


class RemoteAndWebsiteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(self.tmp.name + '/db.sqlite3')

    def tearDown(self):
        self.store.db.close()
        self.tmp.cleanup()

    def test_agent_history_stale_offline_recovery_and_no_stale_temperature(self):
        entry = {'id': 'second', 'name': 'Second PC', 'url': 'http://127.0.0.1:8766', 'token_env': 'DASH_AGENT_SECOND_TOKEN', 'stale_after_seconds': 45, 'offline_after_seconds': 120}
        monitor = RemoteMonitor([entry], self.store)
        with patch.dict('os.environ', {'DASH_AGENT_SECOND_TOKEN': 'token-token-token-token-token-token'}):
            with patch('desk_dash.remotes.OPENER.open', return_value=Response(json.dumps(report(1000)).encode())):
                live = monitor.poll_one(entry, now=1000)
            self.assertEqual(live['status'], 'ONLINE')
            self.assertEqual(live['system']['cpu']['temperature']['level'], 'GOOD')
            self.assertEqual(len(self.store.remote_history('second')), 1)
            with patch('desk_dash.remotes.OPENER.open', side_effect=urllib.error.URLError('down')):
                degraded = monitor.poll_one(entry, now=1020)
                stale = monitor.poll_one(entry, now=1050)
                offline = monitor.poll_one(entry, now=1121)
            for item, expected in ((degraded, 'DEGRADED'), (stale, 'STALE'), (offline, 'OFFLINE')):
                self.assertEqual(item['status'], expected)
                self.assertIsNone(item['system'])
                self.assertEqual(item['last_success'], 1000)
            restarted = RemoteMonitor([entry], self.store)
            self.assertEqual(restarted.view_one(entry, now=1121)['status'], 'OFFLINE')
            with patch('desk_dash.remotes.OPENER.open', return_value=Response(json.dumps(report(1130)).encode())):
                recovered = monitor.poll_one(entry, now=1130)
            self.assertEqual(recovered['status'], 'ONLINE')
            self.assertEqual(len(self.store.remote_history('second')), 2)
            self.assertTrue(any('OFFLINE → ONLINE' in x['message'] for x in self.store.events('machine')))

    def test_stale_agent_report_is_not_counted_as_success(self):
        entry = {'id': 'second', 'name': 'Second PC', 'url': 'http://127.0.0.1:8766', 'token_env': 'DASH_AGENT_SECOND_TOKEN', 'stale_after_seconds': 30, 'offline_after_seconds': 90}
        monitor = RemoteMonitor([entry], self.store)
        with patch.dict('os.environ', {'DASH_AGENT_SECOND_TOKEN': 'token-token-token-token-token-token'}), patch('desk_dash.remotes.OPENER.open', return_value=Response(json.dumps(report(1000)).encode())):
            result = monitor.poll_one(entry, now=1040)
        self.assertEqual(result['status'], 'OFFLINE')
        self.assertIsNone(result['system'])
        self.assertIsNone(result['last_success'])

    def test_website_requires_three_failed_checks_and_records_recovery(self):
        site = {'id': 'example', 'name': 'Example', 'url': 'https://example.com', 'failure_threshold': 3, 'expected_statuses': [200]}
        statuses = []
        with patch('desk_dash.checks.OPENER.open', side_effect=urllib.error.URLError('offline')):
            for i in range(3):
                statuses.append(check_site(site, self.store, statuses[-1] if statuses else None)['status'])
        self.assertEqual(statuses, ['DEGRADED', 'DEGRADED', 'OFFLINE'])
        with patch('desk_dash.checks.OPENER.open', return_value=Response(b'ok')):
            result = check_site(site, self.store, 'OFFLINE')
        self.assertEqual(result['status'], 'ONLINE')
        self.assertEqual(result['uptime_percent'], 25)
        self.assertIsNotNone(result['last_success'])
        self.assertTrue(any('OFFLINE → ONLINE' in event['message'] for event in self.store.events('website')))

    def test_website_follows_redirect_to_final_response(self):
        class RedirectHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path == '/start':
                    self.send_response(302); self.send_header('Location', '/healthy'); self.end_headers()
                else:
                    self.send_response(200); self.end_headers(); self.wfile.write(b'ok')
            def log_message(self, *args): pass
        server = ThreadingHTTPServer(('127.0.0.1', 0), RedirectHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        try:
            site = {'id': 'redirect', 'name': 'Redirect', 'url': 'http://127.0.0.1:' + str(server.server_port) + '/start', 'expected_statuses': [200], 'allow_redirects': True}
            result = check_site(site, self.store)
            self.assertEqual(result['status'], 'ONLINE')
            self.assertEqual(result['http_status'], 200)
        finally:
            server.shutdown(); server.server_close(); thread.join(timeout=2)

    def test_libre_hardware_monitor_sensor_tree_is_mapped_by_hardware(self):
        tree = {'Text': 'Sensor', 'Children': [{'HardwareId': '/amdcpu/0', 'Text': 'Ryzen CPU', 'Children': [{'SensorId': '/amdcpu/0/temperature/0', 'Text': 'CPU Package', 'Type': 'Temperature', 'RawValue': 42.0}]}, {'HardwareId': '/nvidiagpu/0', 'Text': 'RTX 2060', 'Children': [{'SensorId': '/nvidiagpu/0/temperature/0', 'Text': 'GPU Core', 'Type': 'Temperature', 'RawValue': 39.0}]}]}
        with patch('desk_dash.windows.urllib.request.urlopen', return_value=Response(json.dumps(tree).encode())):
            metrics = lhm_metrics('http://127.0.0.1:8085/data.json')
        self.assertEqual((metrics['cpu_temp'], metrics['gpu_temp']), (42, 39))
        self.assertEqual(metrics['gpu_model'], 'RTX 2060')
        with self.assertRaises(ValueError):
            lhm_metrics('http://192.168.1.2:8085/data.json')

    def test_windows_sampler_combines_real_sensor_values_with_os_metrics(self):
        class Process:
            stdout = json.dumps({'cpu_model': 'Ryzen 5 3600', 'cpu_percent': 22, 'cpu_mhz': 4100, 'ram_total_bytes': 32_000_000_000, 'ram_free_bytes': 20_000_000_000, 'uptime_seconds': 8000, 'process_count': 100, 'filesystems': [{'mount': 'C:', 'total_bytes': 100000, 'used_bytes': 25000}], 'network': {'Ethernet': {'rx': 100, 'tx': 200}}})
        fallback = {'model': None, 'temperature': {'celsius': None}, 'utilization': None, 'vram_used_mib': None, 'vram_total_mib': None, 'power_w': None, 'fan_percent': None}
        with patch('desk_dash.windows.subprocess.run', return_value=Process()), patch('desk_dash.windows.lhm_metrics', return_value={'cpu_model': 'Ryzen 5 3600', 'cpu_temp': 42, 'gpu_model': 'RTX 2060', 'gpu_temp': 39, 'gpu_usage': 8, 'gpu_vram_used': 1000, 'gpu_vram_total': 8192, 'gpu_power': 40, 'gpu_fan': 25}), patch('desk_dash.windows.nvidia_gpu', return_value=fallback):
            sample = WindowsSampler().collect()
        self.assertEqual(sample['cpu']['temperature']['celsius'], 42)
        self.assertEqual(sample['gpu']['temperature']['celsius'], 39)
        self.assertEqual(sample['gpu']['vram_total_mib'], 8192)
        self.assertEqual(sample['ram']['used_bytes'], 12_000_000_000)

    def test_agent_protocol_rejects_false_good_label_and_invalid_timestamp(self):
        from desk_dash.protocol import validate_report
        normalized = validate_report(report(1000), now=1000)
        self.assertEqual(normalized['cpu']['temperature']['level'], 'GOOD')
        with self.assertRaises(ValueError):
            validate_report(report(1000), now=1400)


if __name__ == '__main__':
    unittest.main()
