"""Verify that the release ZIP contains exactly one real Windows executable.

Does not prove that the executable launches or passes Windows GUI/voice tests.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
import struct
import sys
import zipfile


def verify(path: Path) -> str:
    with zipfile.ZipFile(path) as z:
        infos = z.infolist()
        if len(infos) != 1 or infos[0].filename != "PersonalJarvis.exe":
            raise ValueError("Expected only PersonalJarvis.exe in ZIP")
        if infos[0].is_dir() or infos[0].file_size < 512:
            raise ValueError("EXE entry is missing or too short")
        if infos[0].flag_bits & 0x1:
            raise ValueError("Encrypted ZIP entry not supported")
        bad = z.testzip()
        if bad is not None:
            raise ValueError(f"ZIP CRC failure: {bad}")
        with z.open(infos[0]) as f:
            header = f.read(512)
        if header[:2] != b"MZ":
            raise ValueError("File does not have an MZ header")
        pe_offset = struct.unpack_from("<I", header, 0x3c)[0]
        if pe_offset < 64:
            raise ValueError("Invalid PE offset")
        with z.open(infos[0]) as f:
            f.seek(pe_offset)
            pe_header = f.read(6)
        if pe_header[:4] != b"PE\x00\x00":
            raise ValueError("File does not have PE signature")
        if len(pe_header) != 6 or struct.unpack_from("<H", pe_header, 4)[0] != 0x8664:
            raise ValueError("File is not a Windows x64 PE executable")
    with path.open("rb") as f:
        digest = hashlib.file_digest(f, "sha256").hexdigest()
    return digest


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("Usage: verify-onefile.py <windows-zip>")
    try:
        print("Verified ZIP CRC and PE header; SHA-256:", verify(Path(sys.argv[1])))
    except (OSError, ValueError, zipfile.BadZipFile) as exc:
        print("FAILED:", exc, file=sys.stderr)
        raise SystemExit(1)
