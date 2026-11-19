"""Build the per-user Windows x64 installer with NSIS and the locked desktop dependencies."""

from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
from urllib.request import urlopen
from zipfile import ZIP_DEFLATED, ZipFile

ROOT = Path(__file__).resolve().parents[2]
PACKAGING = Path(__file__).resolve().parent
RUNTIMES = (
    (
        "python.tar.gz",
        "https://github.com/astral-sh/python-build-standalone/releases/download/"
        "20261110/cpython-3.12.16%2B20261110-x86_64-pc-windows-msvc-install_only_stripped.tar.gz",
        "355ee61b8207b91574491b1c6dc9b0ebb4408fc56bfafe498f3ef439314b52dc",
    ),
    (
        "node.zip",
        "https://nodejs.org/dist/v22.23.5/node-v22.23.5-win-x64.zip",
        "4a191082e9706866830379d577d2cc8a50c776fcd9436a8d061b063ed0d9a0e3",
    ),
    (
        "MicrosoftEdgeWebview2Setup.exe",
        "https://go.microsoft.com/fwlink/p/?LinkId=2124703",
        "cf2f2445fef0c5c14e372045f84998771eeb8bb01d59be4afd00692fa160082e",
    ),
    (
        "7zip-extra.7z",
        "https://github.com/ip7z/7zip/releases/download/26.06/7z2606-extra.7z",
        "396c180f12d2247c24b0b1fcd482c6fc2a2700f61211ffede1061dd971f5e104",
    ),
)


def sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def download(work: Path, name: str, url: str, expected: str) -> Path:
    path = work / name
    if not path.exists():
        partial = path.with_suffix(path.suffix + ".part")
        try:
            with urlopen(url, timeout=60) as response, partial.open("wb") as stream:
                shutil.copyfileobj(response, stream)
            if sha256(partial) != expected:
                raise ValueError(f"Unexpected SHA256 for {name}")
            partial.replace(path)
        finally:
            partial.unlink(missing_ok=True)
    if sha256(path) != expected:
        raise ValueError(f"Unexpected SHA256 for {path}")
    return path


def run(*args: str | Path, env: dict[str, str]) -> None:
    subprocess.run(
        [str(value) for value in args], cwd=ROOT, env=env, check=True,
        encoding="utf-8", errors="replace",
    )


def replace_dir(path: Path) -> None:
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True)


def build(work: Path, output: Path, makensis: str, seven_zip: str) -> None:
    work.mkdir(parents=True, exist_ok=True)
    stage = work / "payload"
    replace_dir(stage)
    python_archive, node_archive, webview, extractor_archive = (
        download(work, *entry) for entry in RUNTIMES
    )
    with tarfile.open(python_archive) as archive:
        archive.extractall(stage, filter="data")
    with ZipFile(node_archive) as archive:
        archive.extractall(stage)
    (stage / "node-v22.23.5-win-x64").rename(stage / "node")

    env = dict(os.environ)
    env.setdefault("UV_CACHE_DIR", str(work / "uv-cache"))
    extractor_dir = work / "extractor"
    replace_dir(extractor_dir)
    run(
        seven_zip, "x", "-y", "-bso0", f"-o{extractor_dir}",
        extractor_archive, "x64/7za.exe", "License.txt", env=env,
    )
    requirements = work / "requirements-desktop.txt"
    run(
        "uv", "export", "--frozen", "--no-dev", "--extra", "desktop",
        "--no-emit-project", "--format", "requirements-txt", "-o", requirements,
        "--quiet", env=env,
    )
    wheels = work / "app-wheel"
    replace_dir(wheels)
    run("uv", "build", "--wheel", "--out-dir", wheels, "--quiet", env=env)
    (wheel,) = wheels.glob("*.whl")
    site = stage / "python" / "Lib" / "site-packages"
    run(
        "uv", "pip", "install", "--no-config", "--python-platform", "x86_64-pc-windows-msvc",
        "--python-version", "3.12", "--target", site, "--require-hashes", "-r", requirements,
        env=env,
    )
    run(
        "uv", "pip", "install", "--no-config", "--python-platform", "x86_64-pc-windows-msvc",
        "--python-version", "3.12", "--target", site, "--no-deps", wheel, env=env,
    )
    # A cross-install generates host console wrappers. Jarvis and all of its
    # Python helpers launch via the bundled python.exe and their module names.
    for name in ("bin", "Scripts"):
        if (site / name).exists():
            shutil.rmtree(site / name)

    shutil.copytree(ROOT / "assets" / "icons", site / "assets" / "icons")
    shutil.copytree(ROOT / "docs" / "product", site / "docs" / "product", dirs_exist_ok=True)
    for name in ("LICENSE", "NOTICE", "TRADEMARK.md"):
        shutil.copy2(ROOT / name, stage / name)
    shutil.copy2(extractor_dir / "License.txt", stage / "7-Zip-LICENSE.txt")
    icon = site / "jarvis" / "ui" / "web" / "dist" / "jarvis-gigi.ico"
    shutil.copy2(icon, stage / "jarvis.ico")
    shutil.copy2(icon, work / "jarvis.ico")
    shutil.copy2(PACKAGING / "launch-installed.pyw", stage / "launch.pyw")

    dist = site / "jarvis" / "ui" / "web" / "dist"
    if not (dist / "index.html").is_file() or "mountDesktopPanel" not in (
        dist / "jarvis-hud.js"
    ).read_text(encoding="utf-8"):
        raise RuntimeError("The wheel is missing the compiled dashboard or its desktop panel")

    payload = work / "payload.zip"
    print("Compressing the Windows application", flush=True)
    with ZipFile(payload, "w", compression=ZIP_DEFLATED, compresslevel=3) as archive:
        for file in stage.rglob("*"):
            if file.is_file():
                archive.write(file, file.relative_to(stage).as_posix())
    shutil.rmtree(stage)

    staged_exe = work / output.name
    staged_exe.unlink(missing_ok=True)
    define_prefix = "/" if os.name == "nt" else "-"
    run(
        makensis,
        f"{define_prefix}V2",
        f"{define_prefix}DPAYLOAD={payload}",
        f"{define_prefix}DEXTRACTOR={extractor_dir / 'x64' / '7za.exe'}",
        f"{define_prefix}DAPP_ICON={work / 'jarvis.ico'}",
        f"{define_prefix}DWEBVIEW_BOOTSTRAPPER={webview}",
        f"{define_prefix}DOUTPUT={staged_exe}",
        PACKAGING / "JarvisSetup.nsi",
        env=env,
    )
    with staged_exe.open("rb") as stream:
        if stream.read(2) != b"MZ":
            raise RuntimeError("NSIS output is not a Windows executable")
    output.parent.mkdir(parents=True, exist_ok=True)
    staged_exe.replace(output)
    print(f"{sha256(output)}  {output.name}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--makensis", default="makensis")
    parser.add_argument("--seven-zip", default="7z")
    args = parser.parse_args()
    build(args.work_dir.resolve(), args.output.resolve(), args.makensis, args.seven_zip)


if __name__ == "__main__":
    main()
