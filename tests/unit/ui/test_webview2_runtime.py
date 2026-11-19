"""Regression tests for the installed Windows desktop's WebView2 check."""

import sys
from contextlib import nullcontext
from types import SimpleNamespace

import pytest

from jarvis.ui.shell.runtime_check import WEBVIEW2_CLIENT_GUID, check_webview2

_MACHINE_32 = ("machine", 0x0200)
_MACHINE_64 = ("machine", 0x0100)
_USER = ("user", 0)


@pytest.mark.parametrize(
    ("versions", "scope"),
    [
        ({_MACHINE_32: "145.0.0.1", _USER: "146.0.0.1"}, "machine"),
        ({_MACHINE_64: "145.0.0.1"}, "machine"),
        ({_MACHINE_32: "0.0.0.0", _USER: "145.0.0.1"}, "user"),
        ({_MACHINE_64: 0, _USER: ""}, "none"),
        ({}, "none"),
    ],
)
def test_runtime_uses_predefined_hives_and_registry_views(monkeypatch, versions, scope):
    def open_key(hive, key, reserved, access):
        assert key == rf"SOFTWARE\Microsoft\EdgeUpdate\Clients\{WEBVIEW2_CLIENT_GUID}"
        assert reserved == 0
        candidate = (hive, access & 0x0300)
        if candidate not in versions:
            raise FileNotFoundError(key)
        return nullcontext(versions[candidate])

    monkeypatch.setitem(
        sys.modules,
        "winreg",
        SimpleNamespace(
            HKEY_LOCAL_MACHINE="machine",
            HKEY_CURRENT_USER="user",
            KEY_WOW64_32KEY=0x0200,
            KEY_WOW64_64KEY=0x0100,
            KEY_READ=0x20019,
            OpenKey=open_key,
            QueryValueEx=lambda value, name: (value, 1),
        ),
    )

    result = check_webview2()
    assert result.scope == scope
    assert result.installed is (scope != "none")
    assert result.version == (None if scope == "none" else "145.0.0.1")
