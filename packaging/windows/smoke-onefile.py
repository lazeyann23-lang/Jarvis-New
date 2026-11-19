"""Limited Windows onefile process-start smoke test, not a GUI/voice E2E test."""
from __future__ import annotations

import argparse
from pathlib import Path
import subprocess
import sys


def smoke(executable: Path, *, seconds: int = 12) -> None:
    if sys.platform != "win32":
        raise RuntimeError("Windows-only smoke test")
    if not executable.is_file():
        raise FileNotFoundError(executable)
    if seconds < 1 or seconds > 120:
        raise ValueError("seconds must be in 1..120")
    proc = subprocess.Popen(
        [str(executable.resolve())],
        cwd=str(executable.resolve().parent),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
    )
    try:
        try:
            code = proc.wait(timeout=seconds)
        except subprocess.TimeoutExpired:
            # The windowed launcher stayed alive for the smoke interval.
            return
        raise RuntimeError(f"EXE exited prematurely with code {code}")
    finally:
        if proc.poll() is None:
            # Kill only the process tree started by this smoke test.
            subprocess.run(
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=20,
            )
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("executable", type=Path)
    parser.add_argument("--seconds", type=int, default=12)
    args = parser.parse_args()
    try:
        smoke(args.executable, seconds=args.seconds)
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
        print(f"Windows launch smoke FAILED: {exc}", file=sys.stderr)
        return 1
    print("Windows launch smoke passed: process survived the observation window; GUI/voice not validated")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
