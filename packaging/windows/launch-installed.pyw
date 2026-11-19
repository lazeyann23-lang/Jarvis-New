"""Start the installed desktop with per-user settings and data."""

import ctypes
import os
from pathlib import Path
import sys


def main() -> int:
    root = Path(__file__).resolve().parent
    home = Path(os.environ["LOCALAPPDATA"]) / "Jarvis"
    home.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("JARVIS_CONFIG", str(home / "jarvis.toml"))
    os.environ.setdefault("JARVIS_DATA_DIR", str(home / "data"))
    os.environ["JARVIS_RUNTIME_HOME"] = str(home)
    os.environ["PYTHONNOUSERSITE"] = "1"
    os.environ["PATH"] = str(root / "node") + os.pathsep + os.environ.get("PATH", "")
    os.chdir(home)

    if not sys.argv[1:]:
        from jarvis.ui.shell.runtime_check import check_webview2

        if not check_webview2().installed:
            ctypes.windll.user32.MessageBoxW(
                None,
                "Microsoft Edge WebView2 fehlt. Starte das JARVIS-Setup erneut, "
                "um die Laufzeit zu installieren.",
                "JARVIS V107",
                0x10,
            )
            return 1

    from jarvis.core.path_augment import ensure_cli_paths
    from jarvis.ui.web.launcher import main as launch

    ensure_cli_paths()
    return launch()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        message = (
            "JARVIS konnte nicht gestartet werden.\n\n"
            f"{type(error).__name__}: {error}"
        )
        if sys.stderr is not None:
            print(message, file=sys.stderr)
        else:
            ctypes.windll.user32.MessageBoxW(None, message, "JARVIS V107", 0x10)
        raise SystemExit(1) from error
