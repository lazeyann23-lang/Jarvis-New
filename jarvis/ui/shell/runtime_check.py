"""WebView2 runtime check for Windows 11.

**Context:** On Windows 11 the evergreen WebView2 runtime is preinstalled by
default ([Microsoft Learn: Evergreen vs. Fixed][ms]). LTSC, Validation,
and old Enterprise images can be missing it, though, and so can custom
deployments. This check prevents pywebview from crashing at startup with a
cryptic `InitializationError`.

[ms]: https://learn.microsoft.com/en-us/microsoft-edge/webview2/concepts/evergreen-vs-fixed-version
"""
from __future__ import annotations

from dataclasses import dataclass

WEBVIEW2_CLIENT_GUID = "{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"

_CLIENT_KEY = rf"SOFTWARE\Microsoft\EdgeUpdate\Clients\{WEBVIEW2_CLIENT_GUID}"


@dataclass(slots=True)
class WebView2CheckResult:
    installed: bool
    version: str | None
    scope: str                      # "machine" | "user" | "none"
    bootstrapper_url: str = "https://go.microsoft.com/fwlink/p/?LinkId=2124703"


def check_webview2() -> WebView2CheckResult:
    """Checks whether the WebView2 runtime is installed.

    Returns version + scope; `installed=False` if it's missing.
    No raise — the caller decides whether to abort or redirect the user to
    the bootstrapper download.
    """
    try:
        import winreg  # type: ignore[import-not-found]
    except ImportError:
        # Non-Windows — WebView2 is irrelevant, pywebview uses other engines.
        return WebView2CheckResult(installed=True, version=None, scope="none")

    # Edge Update normally registers machine installs in the 32-bit view.
    # OpenKey accepts the predefined hive constants directly.
    for hive, view, scope in (
        (winreg.HKEY_LOCAL_MACHINE, winreg.KEY_WOW64_32KEY, "machine"),
        (winreg.HKEY_LOCAL_MACHINE, winreg.KEY_WOW64_64KEY, "machine"),
        (winreg.HKEY_CURRENT_USER, 0, "user"),
    ):
        try:
            with winreg.OpenKey(hive, _CLIENT_KEY, 0, winreg.KEY_READ | view) as key:
                version, _ = winreg.QueryValueEx(key, "pv")
        except OSError:
            continue
        if isinstance(version, str) and version.strip() not in {"", "0.0.0.0"}:
            return WebView2CheckResult(installed=True, version=version.strip(), scope=scope)

    return WebView2CheckResult(installed=False, version=None, scope="none")
