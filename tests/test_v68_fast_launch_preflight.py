"""V68 launch fast path: contract, deployment and regression tests.

Portable source-level checks. These do NOT claim Windows cmd.exe E2E coverage.
"""
from pathlib import Path
import os
import re
import tempfile
import shutil
import unittest

ROOT = Path(__file__).resolve().parents[1]
BATCH = ROOT / 'run.bat'


def preboot_section() -> str:
    data = BATCH.read_text(encoding='utf-8')
    return data[data.index('REM Pre-Boot-Drift-Check:'):data.index('REM Starte Jarvis Desktop-App')]


def assert_batch_preflight_contract(testcase: unittest.TestCase) -> None:
    """Contract of nested if-exist checks; not an interpreter for all CMD syntax."""
    section = preboot_section()
    expected = re.compile(
        r'if exist "\.git" \(\s*'
        r'if exist "scripts\\check-working-tree\.ps1" \(\s*'
        r'powershell -NoProfile -ExecutionPolicy Bypass -File "scripts\\check-working-tree\.ps1"\s*'
        r'\)\s*\)',
        re.MULTILINE,
    )
    testcase.assertRegex(section, expected)
    testcase.assertEqual(section.count('powershell -NoProfile'), 1)


class FunctionTests(unittest.TestCase):
    def test_nested_guard_syntax(self):
        assert_batch_preflight_contract(self)

    def test_zip_release_without_git_has_no_preflight_trigger(self):
        with tempfile.TemporaryDirectory() as p:
            base = Path(p)
            shutil.copy2(BATCH, base / 'run.bat')
            shutil.copy2(ROOT / 'scripts' / 'check-working-tree.ps1', base / 'check-working-tree.ps1')
            self.assertFalse((base / '.git').exists())
            # Run is skipped by the outer guard regardless of script presence.
            self.assertFalse((base / '.git').exists() and (base / 'scripts/check-working-tree.ps1').exists())

    def test_local_checkout_keeps_preflight(self):
        with tempfile.TemporaryDirectory() as p:
            base = Path(p)
            (base / '.git').mkdir()
            (base / 'scripts').mkdir()
            (base / 'scripts' / 'check-working-tree.ps1').write_text('exit 0')
            self.assertTrue((base / '.git').exists() and (base / 'scripts/check-working-tree.ps1').exists())
            assert_batch_preflight_contract(self)


class IntegrationTests(unittest.TestCase):
    def test_linked_worktree_git_file_is_supported(self):
        with tempfile.TemporaryDirectory() as p:
            base=Path(p); (base / '.git').write_text('gitdir: ../worktree/.git')
            (base / 'scripts').mkdir(); shutil.copy2(ROOT/'scripts/check-working-tree.ps1',base/'scripts/check-working-tree.ps1')
            self.assertTrue((base/'.git').exists())
            self.assertTrue((base/'scripts/check-working-tree.ps1').is_file())

    def test_incomplete_checkout_skips_preflight(self):
        with tempfile.TemporaryDirectory() as p:
            base=Path(p);(base/'.git').mkdir()
            self.assertFalse((base/'scripts/check-working-tree.ps1').exists())
            assert_batch_preflight_contract(self)

    def test_packaged_project_has_expected_installer_and_boot_files(self):
        self.assertTrue((ROOT/'run.bat').is_file())
        self.assertTrue((ROOT/'pyproject.toml').is_file())
        self.assertTrue((ROOT/'scripts/check-working-tree.ps1').is_file())


class SecurityRegressionTests(unittest.TestCase):
    def test_no_escalation_or_git_restore_for_zip(self):
        section = preboot_section()
        self.assertNotIn('runas ',section.lower())
        self.assertNotIn('git checkout',section.lower())
        assert_batch_preflight_contract(self)

    def test_debug_and_headless_modes_retained(self):
        data=BATCH.read_text(encoding='utf-8')
        for snippet in ('%1"=="--debug"', '%1"=="--headless"', '%1"=="--dev"',
                        'python -m jarvis.ui.web.launcher',
                        'start "" pythonw -m jarvis.ui.web.launcher',
                        'cd /d "%~dp0"'):
            self.assertIn(snippet,data)

    def test_actual_preflight_script_is_unchanged(self):
        text=(ROOT/'scripts/check-working-tree.ps1').read_text(encoding='utf-8')
        self.assertIn('git ls-tree HEAD --name-only -r',text)
        self.assertIn('git checkout HEAD --',text)
        self.assertIn('exit 0',text)

    def test_no_hardcoded_secret_or_external_download_added(self):
        section=preboot_section().lower()
        self.assertNotIn('http://',section)
        self.assertNotIn('https://',section)
        self.assertNotIn('password=',section)
        self.assertNotIn('invoke-expression',section)


if __name__ == '__main__':
    unittest.main(verbosity=2)
