"""V106 limited process smoke tests; mocks do not establish Windows E2E."""
from __future__ import annotations
import importlib.util
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch, MagicMock

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "packaging/windows/smoke-onefile.py"
WORKFLOW = ROOT / ".github/workflows/jarvis-onefile.yml"


def load():
    spec = importlib.util.spec_from_file_location("onefile_smoke_v106", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class Functional(unittest.TestCase):
    def test_non_windows_refused(self):
        mod = load()
        with patch.object(mod.sys, "platform", "linux"):
            with self.assertRaisesRegex(RuntimeError, "Windows-only"):
                mod.smoke(Path("missing.exe"))

    def test_missing_binary_refused(self):
        mod = load()
        with patch.object(mod.sys, "platform", "win32"):
            with self.assertRaises(FileNotFoundError):
                mod.smoke(Path("/no/such/exe"))

    def test_seconds_bounded(self):
        mod = load()
        with tempfile.TemporaryDirectory() as d:
            exe = Path(d) / "PersonalJarvis.exe"
            exe.write_bytes(b"MZ")
            with patch.object(mod.sys, "platform", "win32"):
                for n in (0, 121):
                    with self.assertRaises(ValueError): mod.smoke(exe, seconds=n)


class SimulatedIntegration(unittest.TestCase):
    def test_early_exit_fails(self):
        mod = load()
        with tempfile.TemporaryDirectory() as d:
            exe = Path(d) / "PersonalJarvis.exe"; exe.write_bytes(b"MZ")
            proc = MagicMock(); proc.wait.return_value = 0; proc.poll.return_value = 0
            with patch.object(mod.sys, "platform", "win32"), patch.object(mod.subprocess, "CREATE_NEW_PROCESS_GROUP", 512, create=True), patch.object(mod.subprocess, "Popen", return_value=proc):
                with self.assertRaisesRegex(RuntimeError, "prematurely"): mod.smoke(exe)

    def test_surviving_process_cleanup(self):
        mod = load()
        with tempfile.TemporaryDirectory() as d:
            exe = Path(d) / "PersonalJarvis.exe"; exe.write_bytes(b"MZ")
            proc = MagicMock(); proc.pid = 1234; proc.wait.side_effect = [subprocess.TimeoutExpired("exe", 12), 0]; proc.poll.return_value = None
            with patch.object(mod.sys, "platform", "win32"), patch.object(mod.subprocess, "CREATE_NEW_PROCESS_GROUP", 512, create=True), patch.object(mod.subprocess, "Popen", return_value=proc), patch.object(mod.subprocess, "run") as kill:
                mod.smoke(exe)
                self.assertEqual(kill.call_args.args[0], ["taskkill", "/PID", "1234", "/T", "/F"])


class SecurityRegression(unittest.TestCase):
    def test_no_shell_execution(self):
        text = SCRIPT.read_text()
        self.assertNotIn("shell=True", text)
        self.assertIn("[str(executable.resolve())]", text)

    def test_workflow_smoke_after_build_before_upload(self):
        text = WORKFLOW.read_text()
        self.assertLess(text.index("Create single-EXE portable ZIP"), text.index("Smoke-test EXE startup"))
        self.assertLess(text.index("Smoke-test EXE startup"), text.index("Preserve verified ZIP"))
        self.assertIn("python packaging/windows/smoke-onefile.py dist/PersonalJarvis.exe", text)

    def test_no_false_e2e_claim(self):
        self.assertIn("GUI/voice not validated", SCRIPT.read_text())


if __name__ == "__main__": unittest.main()
