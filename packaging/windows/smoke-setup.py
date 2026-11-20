"""Verify the NSIS payload and exercise its installed Windows launcher."""

from __future__ import annotations

import argparse
import ctypes
import importlib.util
import os
import runpy
import sys
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[2]
SITE = "python/Lib/site-packages/"
DIST = "jarvis/ui/web/dist"
REGISTRY_PROBE = "jarvis/ui/shell/runtime_check.py"


def _check_windows_runtime(archive: ZipFile, path: str) -> None:
    with archive.open(path) as executable:
        header = executable.read(64)
        if len(header) != 64 or header[:2] != b"MZ":
            raise ValueError(f"{path} is missing its DOS executable header")
        pe_offset = int.from_bytes(header[60:64], "little")
        if not 64 <= pe_offset <= 0x10000:
            raise ValueError(f"{path} has an invalid PE header offset")
        executable.seek(pe_offset)
        if executable.read(6) != b"PE\0\0\x64\x86":
            raise ValueError(f"{path} is not a Windows x64 executable")


def check_payload(payload: Path, source: Path, extra: list[str]) -> None:
    """Validate ZIP CRCs and compare critical packaged files to source."""
    checks = {
        "launch.pyw": "packaging/windows/launch-installed.pyw",
        **{
            SITE + path: path
            for path in (
                REGISTRY_PROBE,
                f"{DIST}/index.html",
                f"{DIST}/jarvis-hud.js",
                f"{DIST}/jarvis-hud.css",
                *extra,
            )
        },
    }
    with ZipFile(payload) as archive:
        names = archive.namelist()
        if len(names) != len(set(names)):
            raise ValueError("The payload contains duplicate ZIP entries")
        for path in ("python/python.exe", "python/pythonw.exe", "node/node.exe"):
            _check_windows_runtime(archive, path)
        for packaged, relative in checks.items():
            path = PurePosixPath(relative)
            if path.is_absolute() or ".." in path.parts or "\\" in relative:
                raise ValueError(f"Expected a project-relative POSIX path: {relative}")
            if archive.read(packaged) != (source / relative).read_bytes():
                raise ValueError(f"Payload differs from source: {relative}")
        broken = archive.testzip()
        if broken is not None:
            raise ValueError(f"Payload ZIP CRC check failed: {broken}")
    print(f"Payload verified: {len(names)} entries, Windows x64 runtimes and source parity")


def check_launch(app: Path) -> None:
    """Use real winreg through the no-argument launcher; substitute the desktop."""
    if sys.platform != "win32":
        raise RuntimeError("The installed launcher smoke requires Windows or Wine")
    if not Path(sys.executable).samefile(app / "python" / "python.exe"):
        raise RuntimeError("Run this check with the installed app's bundled python.exe")

    import winreg

    module_name = "jarvis.ui.shell.runtime_check"
    spec = importlib.util.spec_from_file_location(module_name, app / SITE / REGISTRY_PROBE)
    if spec is None or spec.loader is None:
        raise RuntimeError("The installed WebView2 probe could not be loaded")
    probe = importlib.util.module_from_spec(spec)
    modules = {
        module_name: probe,
        "jarvis.core.path_augment": SimpleNamespace(ensure_cli_paths=lambda: None),
        "jarvis.ui.web.launcher": SimpleNamespace(main=lambda: 42),
    }
    key_name = rf"Software\JarvisSetupSmoke-{uuid4()}"
    initial_dir = Path.cwd()
    with (
        TemporaryDirectory(prefix="jarvis-setup-smoke-") as profile,
        patch.dict(os.environ, {"LOCALAPPDATA": profile}),
        patch.dict(sys.modules, modules),
        patch.object(sys, "argv", [str(app / "launch.pyw")]),
        patch.object(ctypes.windll.user32, "MessageBoxW") as message_box,
    ):
        spec.loader.exec_module(probe)
        probe._CLIENT_KEY = key_name
        launch = runpy.run_path(str(app / "launch.pyw"))["main"]
        try:
            if (
                launch() != 1
                or message_box.call_count != 1
                or "WebView2" not in message_box.call_args.args[1]
            ):
                raise RuntimeError("Launcher did not handle missing WebView2")
            message_box.reset_mock()
            with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, key_name) as key:
                winreg.SetValueEx(key, "pv", 0, winreg.REG_SZ, "123.0.0.1")
            if launch() != 42 or message_box.called:
                raise RuntimeError("Launcher did not reach the desktop with WebView2 present")
        finally:
            os.chdir(initial_dir)
            try:
                winreg.DeleteKey(winreg.HKEY_CURRENT_USER, key_name)
            except FileNotFoundError:
                pass
    print("Installed launcher verified with real winreg and missing/present WebView2 records")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="check", required=True)
    payload = commands.add_parser("payload")
    payload.add_argument("archive", type=Path)
    payload.add_argument("--source-root", type=Path, default=ROOT)
    payload.add_argument("--verify", action="append", default=[], metavar="PACKAGE_PATH")
    launch = commands.add_parser("launch")
    launch.add_argument("app_dir", type=Path)
    args = parser.parse_args()
    if args.check == "payload":
        check_payload(args.archive, args.source_root, args.verify)
    else:
        check_launch(args.app_dir.resolve())


if __name__ == "__main__":
    main()
