"""CLI port validation in the original launcher parser without GUI imports."""
from __future__ import annotations

import argparse
import ast
import subprocess
import sys
from pathlib import Path
import unittest

PROJECT_ROOT = Path(__file__).resolve().parents[4]
LAUNCHER = PROJECT_ROOT / 'jarvis' / 'ui' / 'web' / 'launcher.py'


def original_parse_args(argv):
    tree = ast.parse(LAUNCHER.read_text(encoding='utf-8'), filename=str(LAUNCHER))
    functions = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == '_parse_args']
    if len(functions) != 1:
        raise AssertionError('_parse_args missing from production launcher')
    module = ast.fix_missing_locations(ast.Module(body=functions, type_ignores=[]))
    ns = {'argparse': argparse}
    exec(compile(module, str(LAUNCHER), 'exec'), ns)
    return ns['_parse_args'](argv)


class CLIPortV74Tests(unittest.TestCase):
    # Function: valid cases
    def test_F1_default_port_override_is_none(self):
        self.assertIsNone(original_parse_args([]).port)

    def test_F2_valid_port_and_boundary_values(self):
        for value in ['1', '47000', '65535']:
            with self.subTest(value=value):
                self.assertEqual(original_parse_args(['--port', value]).port, int(value))

    def test_F3_zero_and_above_limit_rejected(self):
        for value in ['0', '65536']:
            with self.subTest(value=value):
                with self.assertRaises(SystemExit) as exit_error:
                    original_parse_args(['--port', value])
                self.assertEqual(exit_error.exception.code, 2)

    # Integration: independent Python processes and real CLI invocation
    def test_I1_process_rejects_zero_with_message(self):
        result = self.probe('--port', '0')
        self.assertEqual(result.returncode, 2)
        self.assertIn('--port must be between 1 and 65535', result.stderr)

    def test_I2_process_rejects_out_of_range(self):
        result = self.probe('--port=65536')
        self.assertEqual(result.returncode, 2)
        self.assertIn('--port must be between 1 and 65535', result.stderr)

    def test_I3_process_accepts_valid_port_and_flags(self):
        result = self.probe('--port', '47821', '--headless', '--no-lock')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), '47821,True,True')

    # Safety/regression: other invalid inputs and established flags
    def test_S1_negative_port_rejected(self):
        with self.assertRaises(SystemExit) as cm:
            original_parse_args(['--port=-1'])
        self.assertEqual(cm.exception.code, 2)

    def test_S2_non_integer_port_rejected_by_argparse(self):
        with self.assertRaises(SystemExit) as cm:
            original_parse_args(['--port', 'not-a-port'])
        self.assertEqual(cm.exception.code, 2)

    def test_S3_explicit_flags_unchanged(self):
        args = original_parse_args(['--dev', '--headless', '--no-lock', '--instance', 'dev'])
        self.assertTrue(args.dev)
        self.assertTrue(args.headless)
        self.assertTrue(args.no_lock)
        self.assertEqual(args.instance, 'dev')

    def test_S4_no_spurious_port_rewrite_or_override(self):
        args = original_parse_args(['--port=50123', '--connect', 'local'])
        self.assertEqual(args.port, 50123)
        self.assertEqual(args.connect, 'local')

    def probe(self, *args):
        return subprocess.run([sys.executable, str(Path(__file__).resolve()), '--probe-cli', *args],
                              text=True, capture_output=True, timeout=10)


if __name__ == '__main__':
    if '--probe-cli' in sys.argv:
        try:
            args = original_parse_args(sys.argv[sys.argv.index('--probe-cli')+1:])
        except SystemExit as exc:
            sys.exit(exc.code)
        print(f'{args.port},{args.headless},{args.no_lock}')
    else:
        unittest.main(verbosity=2)
