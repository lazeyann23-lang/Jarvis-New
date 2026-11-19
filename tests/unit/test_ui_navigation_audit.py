"""Tests for the offline release navigation and asset audit."""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from zipfile import ZipFile

SCRIPT = Path(__file__).resolve().parents[2] / "scripts/ci/check_ui_navigation.py"
PROJECT_ROOT = Path(__file__).resolve().parents[2]
RELEASE = Path(os.environ["JARVIS_TEST_RELEASE"]) if "JARVIS_TEST_RELEASE" in os.environ else None

spec = importlib.util.spec_from_file_location("ui_audit", SCRIPT)
assert spec and spec.loader
ui_audit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ui_audit)


class AuditFixture(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.files = (
            ui_audit.load_release(RELEASE) if RELEASE is not None
            else ui_audit.load_project(PROJECT_ROOT)
        )


class FunctionTests(AuditFixture):
    def test_navigation_destinations_are_mapped(self):
        result = ui_audit.audit_sources(self.files)
        self.assertEqual(result["sidebar_destinations"], 19)
        self.assertEqual(result["settings_hub_destinations"], 15)

    def test_profile_button_has_settings_route(self):
        broken = dict(self.files)
        key = ui_audit.SIDEBAR
        broken[key] = broken[key].replace(b'setActive("profile")', b'setActive("nonexistent")')
        with self.assertRaisesRegex(ValueError, "Profile button"):
            ui_audit.audit_sources(broken)

    def test_missing_route_is_rejected(self):
        broken = dict(self.files)
        key = ui_audit.MAIN_VIEW
        broken[key] = broken[key].replace(b'case "agents":', b'case "unrouted":')
        with self.assertRaisesRegex(ValueError, "Unrouted sidebar"):
            ui_audit.audit_sources(broken)


class IntegrationTests(AuditFixture):
    def test_complete_release_assets_resolve(self):
        result = ui_audit.audit_bundle(self.files)
        self.assertGreater(result["compiled_assets"], 1000)
        self.assertGreater(result["compiled_asset_references"], 1000)

    def test_cli_audits_real_release(self):
        target = ["--zip", str(RELEASE)] if RELEASE else ["--root", str(PROJECT_ROOT)]
        run = subprocess.run(
            [sys.executable, str(SCRIPT), *target],
            capture_output=True, text=True, timeout=30, check=True,
        )
        output = json.loads(run.stdout)
        self.assertIn("navigation", output)
        self.assertIn("bundle", output)
        self.assertIn("static", output["scope"])

    def test_missing_compiled_chunk_is_rejected(self):
        broken = dict(self.files)
        index = broken[ui_audit.DIST + "index.html"].decode("utf-8")
        referenced = ui_audit.INDEX_ASSET.findall(index)
        chunk = next(ref for ref in referenced if ref.startswith("assets/") and ref.endswith(".js"))
        broken.pop(ui_audit.DIST + chunk)
        with self.assertRaisesRegex(ValueError, "Missing compiled assets"):
            ui_audit.audit_bundle(broken)


class SecurityRegressionTests(AuditFixture):
    def test_archive_member_traversal_is_rejected(self):
        with TemporaryDirectory() as temp:
            archive = Path(temp) / "bad.zip"
            with ZipFile(archive, "w") as out:
                out.writestr("Dodaios-main/../../outside", "unsafe")
            with self.assertRaisesRegex(ValueError, "Unsafe release member"):
                ui_audit.load_release(archive)

    def test_missing_dashboard_is_rejected(self):
        broken = dict(self.files)
        broken.pop(ui_audit.DIST + "index.html")
        with self.assertRaisesRegex(ValueError, "Built dashboard missing"):
            ui_audit.audit_bundle(broken)

    def test_missing_static_import_is_rejected(self):
        broken = dict(self.files)
        script = next(name for name in broken if name.startswith(ui_audit.DIST + "assets/") and name.endswith(".js"))
        broken[script] += b'\nimport "./missing-test-chunk.js";'
        with self.assertRaisesRegex(ValueError, "Missing compiled assets"):
            ui_audit.audit_bundle(broken)

    def test_duplicate_navigation_destination_is_rejected(self):
        broken = dict(self.files)
        nav = ui_audit.NAV_GROUPS
        broken[nav] = broken[nav].replace(b'id: "agents"', b'id: "chats"')
        with self.assertRaisesRegex(ValueError, "duplicate navigation"):
            ui_audit.audit_sources(broken)


if __name__ == "__main__":
    unittest.main(verbosity=2)
