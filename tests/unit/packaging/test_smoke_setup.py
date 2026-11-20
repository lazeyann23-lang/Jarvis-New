"""Exercise the checks that guard the shipped NSIS payload."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from zipfile import ZipFile

import pytest

SETUP_SMOKE = Path(__file__).resolve().parents[3] / "packaging/windows/smoke-setup.py"
spec = importlib.util.spec_from_file_location("smoke_setup", SETUP_SMOKE)
assert spec is not None and spec.loader is not None
smoke = importlib.util.module_from_spec(spec)
spec.loader.exec_module(smoke)


@pytest.fixture
def package(tmp_path):
    source = tmp_path / "source"
    files = {
        "launch.pyw": "packaging/windows/launch-installed.pyw",
        **{
            smoke.SITE + relative: relative
            for relative in (
                smoke.REGISTRY_PROBE,
                f"{smoke.DIST}/index.html",
                f"{smoke.DIST}/jarvis-hud.js",
                f"{smoke.DIST}/jarvis-hud.css",
                "jarvis/gui_extension.py",
            )
        },
    }
    payload = tmp_path / "payload.zip"
    with ZipFile(payload, "w") as archive:
        executable = bytearray(64)
        executable[:2] = b"MZ"
        executable[60:64] = (64).to_bytes(4, "little")
        for name in ("python/python.exe", "python/pythonw.exe", "node/node.exe"):
            archive.writestr(name, bytes(executable) + b"PE\0\0\x64\x86")
        for name, relative in files.items():
            file = source / relative
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_text(relative, encoding="utf-8")
            archive.write(file, name)
    return payload, source


def test_payload_accepts_matching_source(package):
    payload, source = package
    smoke.check_payload(payload, source, ["jarvis/gui_extension.py"])


def test_payload_rejects_stale_hud(package):
    payload, source = package
    (source / smoke.DIST / "jarvis-hud.js").write_text("new", encoding="utf-8")
    with pytest.raises(ValueError, match="Payload differs from source.*jarvis-hud.js"):
        smoke.check_payload(payload, source, [])


def test_payload_rejects_duplicate_start_script(package):
    payload, source = package
    with ZipFile(payload, "a") as archive, pytest.warns(UserWarning, match="Duplicate name"):
        archive.writestr("launch.pyw", "unexpected replacement")
    with pytest.raises(ValueError, match="duplicate ZIP entries"):
        smoke.check_payload(payload, source, [])


def test_payload_checks_crc_of_noncritical_file(package):
    payload, source = package
    original = b"CRC sentinel outside the critical manifest"
    with ZipFile(payload, "a") as archive:
        archive.writestr("unrelated.txt", original)
    raw = payload.read_bytes()
    payload.write_bytes(raw.replace(original, b"?" * len(original), 1))
    with pytest.raises(ValueError, match="ZIP CRC check failed: unrelated.txt"):
        smoke.check_payload(payload, source, [])
