import json
import tempfile
import unittest
from unittest.mock import patch

from desk_dash.actions import ActionError, service_action, storage_target
from desk_dash.checks import check_site
from desk_dash.collectors import temperature
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


if __name__ == '__main__':
    unittest.main()
