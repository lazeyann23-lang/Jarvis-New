"""Offline audit of the shipped UI navigation and its bundled static assets.

This is a static preflight check, not a click-through or Windows WebView test.
Run from the project root with ``python scripts/ci/check_ui_navigation.py`` or
against a complete release with ``python scripts/ci/check_ui_navigation.py --zip X.zip``.
No browser, backend, credentials, or third-party packages are needed.
"""

from __future__ import annotations

import argparse
import json
import posixpath
import re
import sys
from pathlib import Path, PurePosixPath
from zipfile import ZipFile

FRONTEND = "jarvis/ui/web/frontend/src/"
DIST = "jarvis/ui/web/dist/"
NAV_GROUPS = FRONTEND + "components/layout/navGroups.ts"
MAIN_VIEW = FRONTEND + "components/layout/MainView.tsx"
SIDEBAR = FRONTEND + "components/layout/Sidebar.tsx"

JS_IMPORT = re.compile(
    r"(?:\bimport\s*\(\s*|\bfrom\s*|\bimport\s*)"
    r"[\"'](\.[^\"']+\.(?:js|css))[\"']"
)
INDEX_ASSET = re.compile(r"(?:src|href)=[\"']/([^\"']+)[\"']")
CSS_ASSET = re.compile(r"url\(\s*[\"']?(\.[^)'\"]+)[\"']?\s*\)")


def audit_sources(files: dict[str, bytes]) -> dict:
    """Verify that all sidebar destinations have a rendering path."""
    required = (NAV_GROUPS, MAIN_VIEW, SIDEBAR)
    missing_source = sorted(set(required) - files.keys())
    if missing_source:
        raise ValueError("Missing navigation source: " + ", ".join(missing_source))

    nav = files[NAV_GROUPS].decode("utf-8")
    view = files[MAIN_VIEW].decode("utf-8")
    sidebar = files[SIDEBAR].decode("utf-8")
    try:
        group_block = nav.split("export const NAV_GROUPS:", 1)[1].split(
            "export const NAV_FOOTER_ITEMS:", 1
        )[0]
        hub_block = nav.split("export const SETTINGS_HUB_IDS:", 1)[1].split("];", 1)[0]
    except IndexError as exc:
        raise ValueError("Navigation registry format changed; recheck audit") from exc
    destinations = re.findall(r'\bid:\s*["\']([\w-]+)["\']', group_block)
    hub = set(re.findall(r'["\']([\w-]+)["\']', hub_block))
    cases = set(re.findall(r'case\s+["\']([\w-]+)["\']\s*:', view))
    overlay = {"plugins", "mcps", "skills"} if "isPluginArea(active)" in view else set()
    missing_routes = sorted(set(destinations) - cases - hub - overlay)
    if missing_routes:
        raise ValueError("Unrouted sidebar destinations: " + ", ".join(missing_routes))
    if not destinations or len(destinations) != len(set(destinations)):
        raise ValueError("Empty or duplicate navigation destinations")
    if "profile" not in hub or 'setActive("profile")' not in sidebar:
        raise ValueError("Profile button is not wired to the Settings hub")
    if "SettingsHubDialog" not in view or "isSettingsHub(active)" not in view:
        raise ValueError("Settings hub has no routed dialog")
    if "onClick={() => { setActive(item.id); }}" not in sidebar:
        raise ValueError("Sidebar rows no longer dispatch navigation clicks")

    # Count native buttons as a follow-up checklist, NOT as proven click coverage.
    native_buttons = sum(
        len(re.findall(r"<button\b", data.decode("utf-8", "ignore")))
        for name, data in files.items()
        if name.startswith(FRONTEND) and name.endswith(".tsx")
        and not name.endswith(".test.tsx")
    )
    return {
        "sidebar_destinations": len(destinations),
        "settings_hub_destinations": len(hub),
        "sidebar_ids": destinations,
        "native_jsx_buttons_inventory": native_buttons,
    }


def audit_bundle(files: dict[str, bytes]) -> dict:
    """Check the release HTML and all compiled JS/CSS relative asset references."""
    index_name = DIST + "index.html"
    if index_name not in files:
        raise ValueError("Built dashboard missing: " + index_name)
    compiled = {
        name[len(DIST):]: data
        for name, data in files.items()
        if name.startswith(DIST)
    }
    index = files[index_name].decode("utf-8")
    checked = 0
    missing: set[str] = set()
    entrypoints = INDEX_ASSET.findall(index)
    if not entrypoints:
        raise ValueError("Dashboard HTML does not reference compiled assets")
    for asset in entrypoints:
        checked += 1
        if asset.split("?", 1)[0] not in compiled:
            missing.add("index.html -> " + asset)

    for name, data in compiled.items():
        if not name.endswith((".js", ".css")):
            continue
        content = data.decode("utf-8", "ignore")
        refs = JS_IMPORT.findall(content) if name.endswith(".js") else CSS_ASSET.findall(content)
        for reference in refs:
            # Versioned imports are permitted; only same-origin relative files
            # in the release need to be present.
            if reference.startswith(("http:", "https:", "data:")):
                continue
            expected = posixpath.normpath(posixpath.join(posixpath.dirname(name), reference.split("?", 1)[0]))
            checked += 1
            if expected.startswith("../") or expected not in compiled:
                missing.add(name + " -> " + reference)
    if missing:
        raise ValueError("Missing compiled assets: " + "; ".join(sorted(missing)[:20]))
    return {"compiled_assets": len(compiled), "compiled_asset_references": checked}


def load_project(root: Path) -> dict[str, bytes]:
    if not root.is_dir():
        raise ValueError(f"Project root does not exist: {root}")
    paths = (NAV_GROUPS, MAIN_VIEW, SIDEBAR)
    files = {name: (root / name).read_bytes() for name in paths if (root / name).is_file()}
    for asset in (root / DIST).rglob("*"):
        if asset.is_file():
            name = asset.relative_to(root).as_posix()
            files[name] = asset.read_bytes() if name.endswith((".js", ".css", ".html")) else b""
    for source in (root / FRONTEND).rglob("*.tsx"):
        if source.is_file():
            files[source.relative_to(root).as_posix()] = source.read_bytes()
    return files


def load_release(archive: Path) -> dict[str, bytes]:
    if not archive.is_file():
        raise ValueError(f"Release archive not found: {archive}")
    files: dict[str, bytes] = {}
    with ZipFile(archive) as release:
        for item in release.infolist():
            if item.is_dir():
                continue
            path = PurePosixPath(item.filename)
            if path.is_absolute() or ".." in path.parts:
                raise ValueError("Unsafe release member path: " + item.filename)
            if not path.parts or path.parts[0] != "Dodaios-main":
                continue
            relative = PurePosixPath(*path.parts[1:]).as_posix()
            if relative in (NAV_GROUPS, MAIN_VIEW, SIDEBAR) or (
                relative.startswith(DIST)
            ) or (relative.startswith(FRONTEND) and relative.endswith(".tsx")):
                files[relative] = (
                    release.read(item) if not relative.startswith(DIST)
                    or relative.endswith((".js", ".css", ".html")) else b""
                )
    return files


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--zip", type=Path, help="Audit a packaged release instead")
    args = parser.parse_args()
    try:
        files = load_release(args.zip) if args.zip else load_project(args.root)
        result = {"navigation": audit_sources(files), "bundle": audit_bundle(files)}
        result["scope"] = "static routing and assets only; interactions need desktop testing"
    except (OSError, ValueError, UnicodeError) as exc:
        # Keep command-line failures visible in non-technical support logs.
        print(f"UI navigation audit FAILED: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
