@echo off
REM Personal Jarvis — Windows Launcher
REM Startet Jarvis im User-Context (kein Admin nötig)

setlocal
cd /d "%~dp0"

REM Aktiviere venv wenn vorhanden
if exist ".venv\Scripts\activate.bat" (
    call .venv\Scripts\activate.bat
)

REM Pre-Boot-Drift-Check: only useful in a Git working tree.
REM ZIP releases have no .git and used to spawn PowerShell + Git on EVERY
REM launch for a guaranteed no-op. Avoid that process startup on releases.
REM Preserve the drift-recovery check in real repositories (including .git
REM files used by linked worktrees). The script itself always exits 0.
if exist ".git" (
    if exist "scripts\check-working-tree.ps1" (
        powershell -NoProfile -ExecutionPolicy Bypass -File "scripts\check-working-tree.ps1"
    )
)

REM Starte Jarvis Desktop-App (Phase 1a: FastAPI + pywebview + React).
REM --debug → Console sichtbar, --headless → nur Backend ohne Fenster,
REM --dev → Frontend aus Vite-Dev-Server (Port 5173) statt dist/.
if "%1"=="--debug" (
    REM --debug ist batch-lokal: Console sichtbar + Verbose-Logging.
    REM Weiterreichen an den Launcher wuerde argparse-Fehler ausloesen.
    set JARVIS_DEBUG=1
    python -m jarvis.ui.web.launcher
) else if "%1"=="--headless" (
    python -m jarvis.ui.web.launcher --headless
) else if "%1"=="--dev" (
    set JARVIS_DEV=1
    python -m jarvis.ui.web.launcher --dev
) else (
    start "" pythonw -m jarvis.ui.web.launcher
)

endlocal
