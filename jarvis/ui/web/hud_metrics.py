"""Read-only live local metrics for the optional JARVIS Chef dashboard.

The GPU/temperature/weather/network statistics in the illustration are NOT live.
Only trustworthy local data is returned. psutil is optional in a minimal
installation, so absent values are reported as ``None`` instead of invented.
"""
from __future__ import annotations

import platform
from typing import Any


def read_hud_metrics() -> dict[str, Any]:
    result: dict[str, Any] = {
        "cpu_percent": None,
        "ram_percent": None,
        "platform": platform.system() or "unknown",
    }
    try:
        import psutil

        result["cpu_percent"] = round(float(psutil.cpu_percent(interval=None)), 1)
        result["ram_percent"] = round(float(psutil.virtual_memory().percent), 1)
    except (ImportError, OSError, RuntimeError, AttributeError, ValueError):
        # A build lacking psutil still serves the HUD and reports unavailable
        # metrics honestly. No exception details/host configuration are leaked.
        pass
    return result
