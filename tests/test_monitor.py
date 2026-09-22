import json
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from desk_dash.actions import ActionError, service_action, storage_target
from desk_dash.checks import check_site
from desk_dash.collectors import temperature, parse_nvme_smart, smart
from desk_dash.store import Store


class MonitoringTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(self.tmp.name + '/history.sqlite3')

    def tearDown(self):
        self.store.db.close()
        self.tmp.cleanup()

    def test_temperatures_have_distinct_hardware_thresholds_and_words(self):
        self.assertEqual(temperature(80, 'cpu')['level'], 'WARM')
        self.assertEqual(temperature(84, 'gpu')['level'], 'HOT')
        self.assertEqual(temperature(80, 'nvme')['level'], 'CRITICAL')
        self.assertEqual(temperature(None)['level'], 'UNAVAILABLE')

    def test_site_redirect_is_healthy_when_expected_and_history_is_recorded(self):
        class Response:
            status = 301
            def __enter__(self): return self
            def __exit__(self, *args): pass
        entry = {'id': 'test', 'name': 'Test', 'url': 'https://example.com', 'expected_statuses': [200, 301]}
        with patch('desk_dash.checks.OPENER.open', return_value=Response()):
            result = check_site(entry, self.store)
        self.assertEqual(result['status'], 'ONLINE')
        self.assertEqual(result['uptime_percent'], 100)
        self.assertEqual(len(result['checks']), 1)

    def test_actions_require_allowlist_and_cooldown(self):
        config = {'controls': {'service_units': ['safe.service']}}
        with self.assertRaises(ActionError):
            service_action(config, self.store, 'another.service', 'restart')
        with patch('desk_dash.actions.subprocess.run') as run:
            run.return_value.returncode = 0
            service_action(config, self.store, 'safe.service', 'restart')
            run.assert_called_once()
            self.assertEqual(run.call_args.args[0], ['systemctl', 'restart', 'safe.service'])
            with self.assertRaises(ActionError):
                service_action(config, self.store, 'safe.service', 'restart')

    def test_storage_target_rejects_non_nvme_directory_without_writing(self):
        config = {'storage_test': {'directory': self.tmp.name, 'size_mib': 16}}
        with patch('desk_dash.actions.nvme_drives', return_value=[]):
            with self.assertRaises(ActionError):
                storage_target(config)

    def test_persistent_action_cooldown(self):
        self.assertEqual(self.store.reserve_action('test', 3600), 0)
        self.assertGreater(self.store.reserve_action('test', 3600), 3500)

    def test_unsafe_shutdowns_are_informational(self):
        result = parse_nvme_smart({'critical_warning': 0, 'temperature': 308, 'available_spare': 100, 'available_spare_threshold': 10, 'percentage_used': 0, 'data_units_read': 9047, 'data_units_written': 364308, 'power_cycles': 7, 'power_on_hours': 18, 'unsafe_shutdowns': 7, 'media_errors': 0, 'num_err_log_entries': 0, 'warning_temp_time': 0, 'critical_comp_time': 0, 'temperature_sensor_1': 308, 'temperature_sensor_2': 300}, 'nvme-cli')
        self.assertEqual(result['health'], 'GOOD')
        self.assertEqual(result['unsafe_shutdowns'], 7)
        self.assertEqual(result['temperature']['celsius'], 35)
        self.assertEqual(result['data_written_bytes'], 364308 * 512000)
        self.assertEqual(len(result['temperature_sensors']), 2)

    def test_existing_website_database_migrates_and_retains_uptime(self):
        old = self.tmp.name + '/old.sqlite3'
        db = sqlite3.connect(old)
        db.execute('CREATE TABLE site_checks (id INTEGER PRIMARY KEY, ts REAL NOT NULL, site TEXT NOT NULL, status TEXT NOT NULL, http_status INTEGER, ms REAL)')
        db.execute("INSERT INTO site_checks(ts,site,status,http_status,ms) VALUES(1,'site','ONLINE',200,40)")
        db.commit(); db.close()
        upgraded = Store(old)
        try:
            self.assertEqual(upgraded.site_history('site')['uptime_percent'], 100)
            self.assertEqual(upgraded.site_failure_streak('site'), 0)
        finally:
            upgraded.db.close()

    def test_nvme_smart_tries_namespace_then_controller_without_sudo(self):
        payload = json.dumps({'critical_warning': 0, 'temperature': 308, 'unsafe_shutdowns': 7, 'media_errors': 0})
        with patch('desk_dash.collectors.run', side_effect=[None, payload]) as command:
            result = smart('nvme0n1')
        self.assertEqual(command.call_args_list[0].args[0][-1], '/dev/nvme0n1')
        self.assertEqual(command.call_args_list[1].args[0][-1], '/dev/nvme0')
        self.assertEqual(result['health'], 'GOOD')


if __name__ == '__main__':
    unittest.main()
