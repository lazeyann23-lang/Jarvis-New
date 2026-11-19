"""V75: validate CLI before expensive/side-effectful bootstrap."""
import ast
import argparse
import subprocess
import sys
import unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[4]
SRC=ROOT/'jarvis/ui/web/launcher.py'

def funcs():
    tree=ast.parse(SRC.read_text(encoding='utf8'))
    parser=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='_parse_args')
    main=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='_main')
    return parser,main

def positions():
    _,main=funcs()
    calls=[]
    for node in ast.walk(main):
        if isinstance(node,ast.Call):
            f=node.func
            name=f.id if isinstance(f,ast.Name) else f.attr if isinstance(f,ast.Attribute) else ''
            calls.append((name,node.lineno))
    return dict((name,min(line for n,line in calls if n==name)) for name in ['_parse_args','ensure_instance_data_dir','ensure_normal_process_priority','sanitize_process_environment'])

def run_cli(argv):
    parser,_=funcs()
    namespace={'argparse':argparse}
    exec(compile(ast.fix_missing_locations(ast.Module(body=[parser],type_ignores=[])),str(SRC),'exec'),namespace)
    return namespace['_parse_args'](argv)

class Function(unittest.TestCase):
    def test_F1_preflight_before_instance_seed(self):
        p=positions();self.assertLess(p['_parse_args'],p['ensure_instance_data_dir'])
    def test_F2_preflight_before_priority_repair(self):
        p=positions();self.assertLess(p['_parse_args'],p['ensure_normal_process_priority'])
    def test_F3_preflight_before_colour_sanitizer(self):
        p=positions();self.assertLess(p['_parse_args'],p['sanitize_process_environment'])
class Integration(unittest.TestCase):
    def test_I1_bad_port_process(self):
        p=subprocess.run([sys.executable,__file__,'--probe','--port','70000'],capture_output=True,text=True)
        self.assertEqual(p.returncode,2);self.assertIn('between 1 and 65535',p.stderr)
    def test_I2_valid_port_process(self):
        p=subprocess.run([sys.executable,__file__,'--probe','--port','50505'],capture_output=True,text=True)
        self.assertEqual(p.returncode,0);self.assertEqual(p.stdout.strip(),'50505')
    def test_I3_help_process(self):
        p=subprocess.run([sys.executable,__file__,'--probe','--help'],capture_output=True,text=True)
        self.assertEqual(p.returncode,0);self.assertIn('--port',p.stdout)
class Safety(unittest.TestCase):
    def test_S1_parser_exactly_once(self):
        _,main=funcs(); self.assertEqual(sum(isinstance(n,ast.Call) and isinstance(n.func,ast.Name) and n.func.id=='_parse_args' for n in ast.walk(main)),1)
    def test_S2_named_instance_unchanged(self):
        self.assertEqual(run_cli(['--instance','dev']).instance,'dev')
    def test_S3_headless_and_background_unchanged(self):
        a=run_cli(['--headless','--background-service']);self.assertTrue(a.headless);self.assertTrue(a.background_service)
    def test_S4_no_port_override(self):
        self.assertIsNone(run_cli([]).port)
if __name__=='__main__':
    if '--probe' in sys.argv:
        a=run_cli(sys.argv[sys.argv.index('--probe')+1:]);print(a.port);sys.exit(0)
    unittest.main()
