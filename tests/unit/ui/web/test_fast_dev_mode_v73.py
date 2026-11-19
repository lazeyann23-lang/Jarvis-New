"""V73: only a literal TOML boolean enables the optional Vite frontend.

Uses the production launcher helpers via AST; no window, paid provider or
network service is started. Tests use real TOML files and subprocesses.
"""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from test_config_read_race_v71 import get_fast_config_functions


class FastDevModeV73Tests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.path = Path(self.dir.name) / 'jarvis.toml'
        previous = os.environ.get('JARVIS_CONFIG')
        os.environ['JARVIS_CONFIG'] = str(self.path)
        self.addCleanup(self._restore, previous)
        self.fn = get_fast_config_functions()

    @staticmethod
    def _restore(old):
        if old is None:
            os.environ.pop('JARVIS_CONFIG', None)
        else:
            os.environ['JARVIS_CONFIG'] = old

    def write(self, value, *, url=None):
        text = f'[ui]\ndev_mode={value}\n'
        if url is not None:
            text += f'vite_dev_url="{url}"\n'
        self.path.write_text(text, encoding='utf-8')

    # Functional group
    def test_F1_explicit_false_stays_bundled(self):
        self.write('false')
        self.assertIsNone(self.fn['_fast_vite_dev_url'](False))

    def test_F2_explicit_true_uses_configured_server(self):
        self.write('true', url='http://localhost:5174')
        self.assertEqual(self.fn['_fast_vite_dev_url'](False), 'http://localhost:5174')

    def test_F3_string_false_does_not_enable_development_mode(self):
        self.write('"false"')
        self.assertIsNone(self.fn['_fast_vite_dev_url'](False))

    # Integration group
    def test_I1_independent_python_process_rejects_string_false(self):
        self.write('"false"')
        self.assertEqual(self.probe(), 'None')

    def test_I2_independent_python_process_accepts_true(self):
        self.write('true', url='http://localhost:5174')
        self.assertEqual(self.probe(), "'http://localhost:5174'")

    def test_I3_change_from_invalid_to_true_then_false(self):
        self.write('"true"')
        self.assertIsNone(self.fn['_fast_vite_dev_url'](False))
        self.write('true')
        self.assertEqual(self.fn['_fast_vite_dev_url'](False), 'http://localhost:5173')
        self.write('false')
        self.assertIsNone(self.fn['_fast_vite_dev_url'](False))

    # Safety/regression group
    def test_S1_integer_and_float_are_not_boolean_opt_in(self):
        for value in ['1', '0', '1.0', '4.5']:
            with self.subTest(value=value):
                self.write(value)
                self.assertIsNone(self.fn['_fast_vite_dev_url'](False))

    def test_S2_array_and_table_do_not_enable_dev_mode(self):
        for value in ['[true]', '["false"]', '{enabled=true}']:
            with self.subTest(value=value):
                self.write(value)
                self.assertIsNone(self.fn['_fast_vite_dev_url'](False))

    def test_S3_explicit_cli_dev_override_kept(self):
        self.write('"false"', url='http://localhost:5174')
        self.assertEqual(self.fn['_fast_vite_dev_url'](True), 'http://localhost:5174')

    def test_S4_port_and_auth_settings_still_work(self):
        self.path.write_text('[ui]\ndev_mode="false"\nadmin_api_port=48123\nauth_token_env="MY_ENV"\n', encoding='utf8')
        self.assertIsNone(self.fn['_fast_vite_dev_url'](False))
        self.assertEqual(self.fn['_fast_admin_base_port'](), 48123)
        self.assertEqual(self.fn['_fast_auth_token_env'](), 'MY_ENV')
        self.assertEqual(self.fn['_read_fast_ui_config'].cache_info().misses, 1)

    def probe(self):
        result = subprocess.run(
            [sys.executable, str(Path(__file__).resolve()), '--probe-dev', str(self.path)],
            capture_output=True, text=True, check=True, timeout=10,
        )
        return result.stdout.strip()


if __name__ == '__main__':
    if len(sys.argv) == 3 and sys.argv[1] == '--probe-dev':
        os.environ['JARVIS_CONFIG'] = sys.argv[2]
        print(repr(get_fast_config_functions()['_fast_vite_dev_url'](False)))
    else:
        unittest.main(verbosity=2)
