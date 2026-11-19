"""Cross-platform checks for the opt-in Windows single-EXE build.

These checks do NOT claim a Windows PyInstaller build or GUI smoke test.
"""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest
import warnings
import zipfile

ROOT = Path(__file__).resolve().parents[3]
VERIFY = ROOT / "packaging" / "windows" / "verify-onefile.py"
SPEC = ROOT / "jarvis.spec"
BUILDER = ROOT / "packaging" / "windows" / "build-onefile.ps1"
HOOK = ROOT / "packaging" / "pyinstaller_rthook_frozen.py"


def load_file(path: Path):
    spec = importlib.util.spec_from_file_location("v104_" + path.stem.replace("-", "_"), path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def fake_pe() -> bytes:
    header = bytearray(1024)
    header[:2] = b"MZ"
    struct.pack_into("<I", header, 0x3c, 0x80)
    header[0x80:0x84] = b"PE\x00\x00"
    struct.pack_into("<H", header, 0x84, 0x8664)  # AMD64 COFF machine
    return bytes(header)


def make_zip(path: Path, payload: bytes = fake_pe(), *, name: str = "PersonalJarvis.exe") -> None:
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as z:
        z.writestr(name, payload)


class FunctionalTests(unittest.TestCase):
    def test_f1_verifier_accepts_valid_pe_header(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "valid.zip"
            make_zip(p)
            self.assertEqual(len(load_file(VERIFY).verify(p)), 64)

    def test_f2_packaged_node_precedes_system_path(self):
        hook = load_file(HOOK)
        with tempfile.TemporaryDirectory() as td:
            node = Path(td) / "tools" / "node"
            node.mkdir(parents=True)
            (node / "node.exe").write_bytes(b"dummy-not-executed")
            original_meipass = getattr(sys, "_MEIPASS", None)
            had_meipass = hasattr(sys, "_MEIPASS")
            original_path = os.environ.get("PATH")
            try:
                sys._MEIPASS = td
                os.environ["PATH"] = "some-other-dir"
                hook._use_bundled_node()
                self.assertEqual(os.environ["PATH"].split(os.pathsep)[0], str(node))
                hook._use_bundled_node()
                self.assertEqual(os.environ["PATH"].split(os.pathsep).count(str(node)), 1)
            finally:
                if had_meipass:
                    sys._MEIPASS = original_meipass
                else:
                    del sys._MEIPASS
                if original_path is None: os.environ.pop("PATH", None)
                else: os.environ["PATH"] = original_path

    def test_f3_no_bundled_node_does_not_modify_path(self):
        hook = load_file(HOOK)
        with tempfile.TemporaryDirectory() as td:
            original = getattr(sys, "_MEIPASS", None)
            exists = hasattr(sys, "_MEIPASS")
            initial = os.environ.get("PATH", "")
            try:
                sys._MEIPASS = td
                hook._use_bundled_node()
                self.assertEqual(os.environ.get("PATH", ""), initial)
            finally:
                if exists: sys._MEIPASS = original
                else: del sys._MEIPASS


class IntegrationTests(unittest.TestCase):
    def test_i1_valid_zip_subprocess(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "valid.zip"
            make_zip(p)
            r = subprocess.run([sys.executable, str(VERIFY), str(p)], capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("Verified ZIP CRC", r.stdout)

    def test_i2_wrong_payload_subprocess(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "bad.zip"
            make_zip(p, payload=b"nothing" * 300)
            r = subprocess.run([sys.executable, str(VERIFY), str(p)], capture_output=True, text=True)
            self.assertNotEqual(r.returncode, 0)
            self.assertIn("MZ", r.stderr)

    def test_i3_wrong_structure_subprocess(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "wrong.zip"
            make_zip(p, name="launcher.txt")
            r = subprocess.run([sys.executable, str(VERIFY), str(p)], capture_output=True, text=True)
            self.assertNotEqual(r.returncode, 0)
            self.assertIn("only PersonalJarvis.exe", r.stderr)


class SecurityRegressionTests(unittest.TestCase):
    def test_s1_duplicate_zip_entries_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "dup.zip"
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", UserWarning)
                with zipfile.ZipFile(p, "w") as z:
                    z.writestr("PersonalJarvis.exe", fake_pe())
                    z.writestr("PersonalJarvis.exe", fake_pe())
            with self.assertRaises(ValueError): load_file(VERIFY).verify(p)

    def test_s2_corrupt_crc_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "broken.zip"
            # A STORED archive lets us flip a payload byte without changing its CRC header.
            with zipfile.ZipFile(p, "w", compression=zipfile.ZIP_STORED) as z:
                z.writestr("PersonalJarvis.exe", fake_pe() + b"UNIQUE-PAYLOAD-SUFFIX")
            blob = bytearray(p.read_bytes())
            blob[blob.index(b"UNIQUE-PAYLOAD-SUFFIX")] ^= 1
            p.write_bytes(blob)
            with self.assertRaisesRegex(ValueError, "CRC failure"): load_file(VERIFY).verify(p)

    def test_s3_onefile_opt_in_and_existing_installer_unchanged(self):
        s = SPEC.read_text()
        self.assertIn('os.environ.get("JARVIS_WINDOWS_ONEFILE") == "1"', s)
        self.assertIn("exclude_binaries=not WINDOWS_ONEFILE", s)
        self.assertIn("if not WINDOWS_ONEFILE:", s)
        self.assertIn("a.binaries, a.zipfiles, a.datas", s)
        self.assertIn("JARVIS_PORTABLE_NODE_DIR must contain verified", s)

    def test_s4_builder_checks_config_hash_and_node_vendor_hash(self):
        s = BUILDER.read_text()
        self.assertIn("Get-FileHash -Algorithm SHA256 -LiteralPath $config", s)
        self.assertIn("Personal jarvis.toml detected", s)
        self.assertIn("$actual -ne $expected", s)
        self.assertIn("SHASUMS256.txt", s)
        self.assertIn("$env:JARVIS_WINDOWS_ONEFILE = '1'", s)
        self.assertIn("verify-onefile.py", s)


if __name__ == "__main__":
    unittest.main()
