"""V69 regression: real startup TOML helper functions, no heavy app imports.

Run: python -m unittest -v tests.test_v69_fast_ui_config
On Linux and Windows, AST extraction runs the unchanged function bodies from the
actual launcher file with a realistic minimal config path / process environment.
"""
import ast
import builtins
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from functools import lru_cache
from unittest import mock

LAUNCHER = Path(__file__).resolve().parents[1] / 'jarvis' / 'ui' / 'web' / 'launcher.py'
NAMES = {
    '_fast_ui_config', '_read_fast_ui_config', '_fast_admin_base_port',
    '_fast_auth_token_env', '_fast_vite_dev_url',
}


def load_helpers(launcher_file=LAUNCHER, config_root=None):
    source = launcher_file.read_text(encoding='utf-8')
    tree = ast.parse(source, filename=str(launcher_file))
    nodes = [node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in NAMES]
    assert {n.name for n in nodes} == NAMES, 'Startup functions missing'
    module = ast.Module(body=nodes, type_ignores=[])
    ast.fix_missing_locations(module)
    # Keep runtime imports narrow: the target functions must work without
    # importing the entire desktop/WebView/FastAPI dependency tree.
    fake_file = str((config_root or launcher_file.parents[3]) / 'jarvis' / 'ui' / 'web' / 'launcher.py')
    ns = {'__file__': fake_file, 'os': os, 'lru_cache': lru_cache,
          'CONFIG_FILE_NAME': 'jarvis.toml', '_DEFAULT_ADMIN_PORT': 8741}
    exec(compile(module, str(launcher_file), 'exec'), ns)
    return ns


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.dir = Path(self.tempdir.name)
        self.conf = self.dir / 'jarvis.toml'
        self.env = mock.patch.dict(os.environ, {'JARVIS_CONFIG': str(self.conf)})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.f = load_helpers(config_root=self.dir)

    def put(self, content):
        self.conf.write_text(content, encoding='utf-8')
        # Filesystem timestamps can be coarse on SMB/FAT, ensure invalidation
        # without sleeping and without assuming nano-resolution clock support.
        before = getattr(self, '_last_stamp', 1_800_000_000_000_000_000)
        stamp = before + 2_000_000_000
        os.utime(self.conf, ns=(stamp, stamp))
        self._last_stamp = stamp


class FunctionTests(Fixture):
    def test_missing_config_uses_packaged_defaults(self):
        self.assertEqual(self.f['_fast_admin_base_port'](), 8741)
        self.assertEqual(self.f['_fast_auth_token_env'](), 'JARVIS_UI_TOKEN')
        self.assertIsNone(self.f['_fast_vite_dev_url'](False))
        self.assertEqual(self.f['_fast_vite_dev_url'](True), 'http://localhost:5173')

    def test_custom_values_are_read_correctly(self):
        self.put('[ui]\nadmin_api_port=9988\nauth_token_env="AUTH_TEST"\ndev_mode=true\nvite_dev_url="http://localhost:6789"\n')
        self.assertEqual(self.f['_fast_admin_base_port'](), 9988)
        self.assertEqual(self.f['_fast_auth_token_env'](), 'AUTH_TEST')
        self.assertEqual(self.f['_fast_vite_dev_url'](False), 'http://localhost:6789')

    def test_config_edit_refreshes_cache_without_restart(self):
        self.put('[ui]\nadmin_api_port=9091\n')
        self.assertEqual(self.f['_fast_admin_base_port'](), 9091)
        self.put('[ui]\nadmin_api_port=9092\n')
        self.assertEqual(self.f['_fast_admin_base_port'](), 9092)
        self.assertEqual(self.f['_read_fast_ui_config'].cache_info().misses, 2)


class IntegrationTests(Fixture):
    def test_three_startup_checks_parse_toml_once(self):
        self.put('[ui]\nadmin_api_port=9911\nauth_token_env="AUTH_TEST"\ndev_mode=true\n')
        old_open = builtins.open
        opens = []
        def counted_open(path, *args, **kwargs):
            if os.fspath(path) == str(self.conf):
                opens.append(path)
            return old_open(path, *args, **kwargs)
        with mock.patch('builtins.open', side_effect=counted_open):
            self.assertEqual(self.f['_fast_admin_base_port'](), 9911)
            self.assertEqual(self.f['_fast_auth_token_env'](), 'AUTH_TEST')
            self.assertEqual(self.f['_fast_vite_dev_url'](False), 'http://localhost:5173')
        self.assertEqual(len(opens), 1, 'one TOML read on unchanged startup config')

    def test_switch_config_path_while_running(self):
        self.put('[ui]\nadmin_api_port=9921\n')
        other = self.dir / 'other.toml'
        other.write_text('[ui]\nadmin_api_port=9922\n')
        self.assertEqual(self.f['_fast_admin_base_port'](), 9921)
        with mock.patch.dict(os.environ, {'JARVIS_CONFIG': str(other)}):
            self.assertEqual(self.f['_fast_admin_base_port'](), 9922)
        self.assertEqual(self.f['_fast_admin_base_port'](), 9921)

    def test_actual_subprocess_reads_config(self):
        self.put('[ui]\nadmin_api_port=9876\nauth_token_env="SAFE_ENV"\n')
        result = subprocess.run([sys.executable, str(Path(__file__).resolve()), '--probe', str(self.conf)],
                                text=True, capture_output=True, timeout=10, check=True)
        self.assertEqual(json.loads(result.stdout), [9876, 'SAFE_ENV', None])


class SecurityRegressionTests(Fixture):
    def test_malformed_toml_does_not_break_boot(self):
        self.put('[ui\nport = [ garbage')
        self.assertEqual(self.f['_fast_admin_base_port'](), 8741)
        self.assertEqual(self.f['_fast_auth_token_env'](), 'JARVIS_UI_TOKEN')
        self.assertIsNone(self.f['_fast_vite_dev_url'](False))

    def test_invalid_ui_types_keep_safe_defaults(self):
        self.put('ui = "not a table"\n')
        self.assertEqual(self.f['_fast_admin_base_port'](), 8741)
        self.assertEqual(self.f['_fast_auth_token_env'](), 'JARVIS_UI_TOKEN')
        self.assertIsNone(self.f['_fast_vite_dev_url'](False))

    def test_toml_content_does_not_execute_shell_code(self):
        marker = self.dir / 'unexpected_execution'
        self.put('[ui]\nauth_token_env="$(touch '+str(marker).replace('\\', '/')+')"\n')
        self.assertIn('$(touch', self.f['_fast_auth_token_env']())
        self.assertFalse(marker.exists())

    def test_token_values_not_read_from_environment(self):
        self.put('[ui]\nauth_token_env="JARVIS_UI_TOKEN"\n')
        with mock.patch.dict(os.environ, {'JARVIS_UI_TOKEN': 'PRIVATE_TEST_VALUE'}):
            self.assertEqual(self.f['_fast_auth_token_env'](), 'JARVIS_UI_TOKEN')
            self.assertNotIn('PRIVATE_TEST_VALUE', str(self.f['_fast_ui_config']()))


if __name__ == '__main__':
    if len(sys.argv) == 3 and sys.argv[1] == '--probe':
        os.environ['JARVIS_CONFIG'] = sys.argv[2]
        f = load_helpers()
        print(json.dumps([f['_fast_admin_base_port'](), f['_fast_auth_token_env'](), f['_fast_vite_dev_url'](False)]))
    else:
        unittest.main()
