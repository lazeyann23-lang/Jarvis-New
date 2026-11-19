"""Persistent user choice for *acknowledged voice/chat* Wiki fact capture.

This does not disable explicit Wiki edits, manual ingest, contact mirroring,
existing journal consolidation or the user's ability to read saved memories.
Missing settings preserve the pre-existing behaviour (capture enabled).
Unreadable/malformed settings fail closed instead of silently opting back in.
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path

_LOCK = threading.RLock()
_SCHEMA = 1


def _path() -> Path:
    from jarvis.core.paths import user_data_dir

    return user_data_dir() / "data" / "wiki_capture_policy.json"


def capture_enabled(*, path: Path | None = None) -> bool:
    """Read the on-disk preference on each check (including after restart)."""
    target = path if path is not None else _path()
    with _LOCK:
        try:
            data = json.loads(target.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return True  # existing installations keep their current behaviour
        except (OSError, ValueError, UnicodeError):
            return False
        return (
            isinstance(data, dict)
            and type(data.get("schema")) is int
            and data["schema"] == _SCHEMA
            and type(data.get("enabled")) is bool
            and data["enabled"]
        )


def set_capture_enabled(enabled: bool, *, path: Path | None = None) -> None:
    """Persist a deliberate choice atomically; raise on failed persistence.

    A same-user process can edit the settings file; this is a privacy switch,
    not a security boundary against other programs running as that user.
    """
    if type(enabled) is not bool:
        raise TypeError("enabled must be a boolean")
    target = path if path is not None else _path()
    with _LOCK:
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix=".wiki_capture_", dir=target.parent)
        try:
            if hasattr(os, "fchmod"):
                os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as out:
                json.dump({"schema": _SCHEMA, "enabled": enabled}, out)
                out.flush()
                os.fsync(out.fileno())
            os.replace(tmp_name, target)
            if os.name == "posix":
                target.chmod(0o600)
        finally:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)
