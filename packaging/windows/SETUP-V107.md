# JARVIS V107 Windows setup

The x64 installer includes the V107 application wheel, CPython, Node.js and the
`desktop` dependencies from `uv.lock`. It installs for the current user
under `%LOCALAPPDATA%\Programs\JARVIS Desktop`. The installer offers
Microsoft's WebView2 bootstrapper when needed. The bootstrapper downloads
WebView2 from Microsoft.

The Start menu shortcut launches the application with its own Python and Node
runtime. Existing settings at `%LOCALAPPDATA%\Jarvis\jarvis.toml` and
data under `%LOCALAPPDATA%\Jarvis` remain available after uninstall.
A previous source checkout can have its own configuration and data paths.
This setup leaves that checkout in place and does not import its data.

## Rebuild

Run from the project root with Python 3.12+, uv, 7-Zip and NSIS 3.11+ on the PATH.
Internet access is required to build the installer.

```shell
python packaging/windows/build-setup.py --work-dir ./dist/setup-work --output ./dist/JARVIS-V107-Setup-x64.exe
```

The build checks pinned hashes for downloaded runtimes, installs the locked
Windows x64 desktop dependencies in a staging directory, and packages the
provided Vite output under `jarvis/ui/web/dist`. Rebuild that output with
`npm ci` and `npm run build` in `jarvis/ui/web/frontend` after changing
the React sources. For HUD-only changes, the matching public assets must also
be in `dist`.

## Desktop jobs

The dashboard's PC control panel uses the existing computer-use controller.
Configure a vision-capable Tool Model, open PC control and submit a desktop
goal. The panel shows recent runs and their recorded outcomes. The stop
button cancels queued and running computer-use missions. The global Escape
key remains available while the desktop controller has the mouse.
