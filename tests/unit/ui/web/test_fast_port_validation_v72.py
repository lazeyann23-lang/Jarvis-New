"""Real file I/O and subprocess checks for safe fast-boot TCP port parsing.

The tests execute the production launcher functions extracted as AST without
triggering startup side effects or importing paid AI client libraries.
"""
from __future__ import annotations

from pathlib import Path
import os
import subprocess
import sys
import tempfile
import unittest
from test_config_read_race_v71 import get_fast_config_functions


class FastPortValidationTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name) / 'jarvis.toml'
        previous = os.environ.get('JARVIS_CONFIG')
        os.environ['JARVIS_CONFIG'] = str(self.path)
        self.addCleanup(self.reset_env, previous)
        self.ns = get_fast_config_functions()

    @staticmethod
    def reset_env(previous):
        if previous is None:
            os.environ.pop('JARVIS_CONFIG',None)
        else:
            os.environ['JARVIS_CONFIG'] = previous

    def cfg(self, port):
        self.path.write_text(f'[ui]\nadmin_api_port = {port}\n',encoding='utf8')
        return self.ns['_fast_admin_base_port']()

    def test_F1_accept_valid_configured_port(self):
        self.assertEqual(self.cfg('47122'), 47122)

    def test_F2_accept_tcp_boundary_ports(self):
        self.assertEqual(self.cfg('1'), 1)
        self.assertEqual(self.cfg('65535'), 65535)

    def test_F3_reject_boolean_values_not_real_ports(self):
        self.assertEqual(self.cfg('true'), 47821)
        self.assertEqual(self.cfg('false'), 47821)

    def test_I1_subprocess_rejects_boolean_via_real_toml_file(self):
        self.cfg('true')
        proc = subprocess.run([sys.executable,str(Path(__file__).resolve()),'--probe-port',str(self.path)],
                              capture_output=True,text=True,timeout=10,check=True)
        self.assertEqual(proc.stdout.strip(),'47821')

    def test_I2_subprocess_accepts_valid_port(self):
        self.cfg('51111')
        proc = subprocess.run([sys.executable,str(Path(__file__).resolve()),'--probe-port',str(self.path)],
                              capture_output=True,text=True,timeout=10,check=True)
        self.assertEqual(proc.stdout.strip(),'51111')

    def test_I3_file_update_recovers_from_bad_to_valid(self):
        self.assertEqual(self.cfg('65536'),47821)
        self.assertEqual(self.cfg('47000'),47000)

    def test_S1_zero_and_negative_ports_fallback(self):
        self.assertEqual(self.cfg('0'),47821)
        self.assertEqual(self.cfg('-1'),47821)

    def test_S2_out_of_range_positive_ports_fallback(self):
        self.assertEqual(self.cfg('65536'),47821)
        self.assertEqual(self.cfg('999999'),47821)

    def test_S3_noninteger_ports_fallback(self):
        self.assertEqual(self.cfg('"55555"'),47821)
        self.assertEqual(self.cfg('4.5'),47821)

    def test_S4_other_ui_settings_remain_available(self):
        self.path.write_text('[ui]\nadmin_api_port=true\nauth_token_env="MY_TOKEN_VAR"\n',encoding='utf8')
        self.assertEqual(self.ns['_fast_admin_base_port'](),47821)
        self.assertEqual(self.ns['_fast_auth_token_env'](),'MY_TOKEN_VAR')
        self.assertEqual(self.ns['_read_fast_ui_config'].cache_info().misses,1)


if __name__=='__main__':
    if len(sys.argv)==3 and sys.argv[1]=='--probe-port':
        os.environ['JARVIS_CONFIG']=sys.argv[2]
        print(get_fast_config_functions()['_fast_admin_base_port']())
    else:
        unittest.main(verbosity=2)
