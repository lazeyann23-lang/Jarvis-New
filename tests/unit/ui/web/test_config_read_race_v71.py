"""Portable focused checks of the production fast-boot TOML path.

Uses AST extraction to avoid desktop/audio/paid-provider imports in CI.
The extracted functions execute the exact expressions from launcher.py.
"""
from __future__ import annotations

import ast
import json
import os
from functools import lru_cache
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

PROJECT_ROOT = Path(__file__).resolve().parents[4]
LAUNCHER = PROJECT_ROOT / 'jarvis' / 'ui' / 'web' / 'launcher.py'


def get_fast_config_functions():
    tree = ast.parse(LAUNCHER.read_text(encoding='utf-8'), filename=str(LAUNCHER))
    names = {'_fast_ui_config', '_read_fast_ui_config', '_fast_admin_base_port', '_fast_auth_token_env', '_fast_vite_dev_url'}
    nodes = [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in names]
    if len(nodes) != len(names):
        raise AssertionError('Production launcher functions are missing')
    module = ast.Module(body=nodes, type_ignores=[])
    namespace = {'os': os, 'lru_cache': lru_cache, 'CONFIG_FILE_NAME': 'jarvis.toml', '__file__': str(LAUNCHER), '_DEFAULT_ADMIN_PORT': 47821}
    exec(compile(ast.fix_missing_locations(module), str(LAUNCHER), 'exec'), namespace)
    return namespace


class SwapOnStat:
    """Replaces a TOML file immediately after stat() returned old metadata."""
    def __init__(self, target: Path, next_file: Path):
        self.target = target
        self.next_file = next_file
        self.swapped = False

    def __getattr__(self, key):
        return getattr(os, key)

    def stat(self, path):
        result = os.stat(path)
        if not self.swapped and os.fspath(path) == os.fspath(self.target):
            os.replace(self.next_file, self.target)
            self.swapped = True
        return result


class FastConfigRaceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'jarvis.toml'
        self.original_config = os.environ.get('JARVIS_CONFIG')
        os.environ['JARVIS_CONFIG'] = str(self.path)
        self.addCleanup(self.restore_env)
        self.ns = get_fast_config_functions()

    def restore_env(self):
        if self.original_config is None:
            os.environ.pop('JARVIS_CONFIG', None)
        else:
            os.environ['JARVIS_CONFIG'] = self.original_config

    def write(self, text):
        self.path.write_text(text, encoding='utf8')

    # Function checks
    def test_F1_parse_cache_reused_for_unchanged_file(self):
        self.write('[ui]\nadmin_api_port=41712\n')
        self.assertEqual(self.ns['_fast_admin_base_port'](), 41712)
        self.assertEqual(self.ns['_fast_admin_base_port'](), 41712)
        info = self.ns['_read_fast_ui_config'].cache_info()
        self.assertEqual(info.misses, 1)
        self.assertGreaterEqual(info.hits, 1)

    def test_F2_replacement_between_stat_and_open_never_caches_wrong_identity(self):
        self.write('[ui]\nauth_token_env="OLD_ENV"\n')
        incoming = Path(self.tmp.name) / 'next.toml'
        incoming.write_text('[ui]\nauth_token_env="NEW_ENV"\n', encoding='utf8')
        swapped_os = SwapOnStat(self.path,incoming)
        self.ns['os'] = swapped_os
        self.assertEqual(self.ns['_fast_ui_config'](), {})
        self.assertTrue(swapped_os.swapped)
        self.assertEqual(self.ns['_read_fast_ui_config'].cache_info().currsize, 0)
        self.ns['os'] = os
        self.assertEqual(self.ns['_fast_auth_token_env'](), 'NEW_ENV')

    def test_F3_direct_stale_key_rejected_and_not_cached(self):
        self.write('[ui]\ndev_mode=true\n')
        st = self.path.stat()
        self.write('[ui]\ndev_mode=false\nother=10\n')
        with self.assertRaises(OSError):
            self.ns['_read_fast_ui_config'](str(self.path),st.st_mtime_ns,st.st_size,st.st_dev,st.st_ino)
        self.assertEqual(self.ns['_read_fast_ui_config'].cache_info().currsize, 0)

    # Integration checks
    def test_I1_python_subprocess_uses_real_file_and_launcher_function(self):
        self.write('[ui]\nauth_token_env="CUSTOM_TOKEN_ENV"\n')
        result = subprocess.run([sys.executable, str(Path(__file__).resolve()), '--probe', str(self.path)],
                                capture_output=True, text=True, check=True, timeout=10)
        self.assertEqual(result.stdout.strip(), 'CUSTOM_TOKEN_ENV')

    def test_I2_missing_file_then_creation_restores_ui(self):
        self.assertEqual(self.ns['_fast_ui_config'](), {})
        self.write('[ui]\nadmin_api_port=47122\n')
        self.assertEqual(self.ns['_fast_admin_base_port'](),47122)

    def test_I3_atomic_replacement_updates_value_on_next_lookup(self):
        self.write('[ui]\nauth_token_env="FIRST_TOKEN_ENV"\n')
        self.assertEqual(self.ns['_fast_auth_token_env'](), 'FIRST_TOKEN_ENV')
        incoming = Path(self.tmp.name) / 'incoming.toml'
        incoming.write_text('[ui]\nauth_token_env="FINAL_TOKEN_ENV"\n', encoding='utf8')
        os.replace(incoming,self.path)
        self.assertEqual(self.ns['_fast_auth_token_env'](), 'FINAL_TOKEN_ENV')

    # Security and regression checks
    def test_S1_malformed_toml_does_not_break_boot(self):
        self.write('[ui\ninvalid')
        self.assertEqual(self.ns['_fast_ui_config'](),{})
        self.assertEqual(self.ns['_fast_auth_token_env'](),'JARVIS_UI_TOKEN')

    def test_S2_corrected_file_can_recover_after_failed_parse(self):
        self.write('[ui\ninvalid')
        self.assertEqual(self.ns['_fast_ui_config'](),{})
        self.write('[ui]\nauth_token_env="RECOVERED_TOKEN_ENV"\n')
        self.assertEqual(self.ns['_fast_auth_token_env'](),'RECOVERED_TOKEN_ENV')

    def test_S3_nonint_port_uses_existing_safe_default(self):
        self.write('[ui]\nadmin_api_port="not a port"\n')
        self.assertEqual(self.ns['_fast_admin_base_port'](),47821)

    def test_S4_cache_stays_bounded(self):
        self.assertEqual(self.ns['_read_fast_ui_config'].cache_info().maxsize,8)
        for i in range(12):
            self.write(f'[ui]\nadmin_api_port={47000+i}\n')
            self.assertEqual(self.ns['_fast_admin_base_port'](),47000+i)
        self.assertLessEqual(self.ns['_read_fast_ui_config'].cache_info().currsize,8)


if __name__ == '__main__':
    if len(sys.argv) == 3 and sys.argv[1] == '--probe':
        os.environ['JARVIS_CONFIG'] = sys.argv[2]
        print(get_fast_config_functions()['_fast_auth_token_env']())
    else:
        unittest.main(verbosity=2)
