@echo off
setlocal
pushd "%~dp0"
if not exist "pyproject.toml" (
    echo Fehler: Bitte dieses Skript im Ordner Dodaios-main starten.
    pause
    exit /b 2
)
if exist ".venv\Scripts\python.exe" (
    set "PYTHON_CMD=%CD%\.venv\Scripts\python.exe"
) else (
    set "PYTHON_CMD=py"
)
echo === JARVIS Diagnosetest fuer Windows ===
echo Python:
"%PYTHON_CMD%" --version
if errorlevel 1 goto :missing_python
"%PYTHON_CMD%" -m pytest --version >nul 2>&1
if errorlevel 1 goto :missing_pytest
"%PYTHON_CMD%" -m pytest -q ^
    tests/missions/experience/test_experience_memory.py ^
    tests/missions/kontrollierer/test_decomposer.py ^
    tests/missions/test_state_machine.py ^
    tests/missions/test_event_bus.py ^
    tests/missions/test_ids.py ^
    tests/missions/test_budget.py ^
    tests/missions/test_injection_scanner.py ^
    tests/missions/test_path_guard.py ^
    tests/missions/test_destructive_confirm.py ^
    tests/missions/critic/test_verdict.py ^
    tests/missions/critic/test_prompts.py
set "STATUS=%ERRORLEVEL%"
if "%STATUS%"=="0" (
    echo Die ausgewaehlten Tests sind bestanden.
) else (
    echo ACHTUNG: Tests fehlgeschlagen. Fehlende Abhaengigkeiten sind moeglich.
)
echo.
echo Diese Tests pruefen nicht Mikrofon, Windows-Steuerung oder echte KI-Logins.
popd
pause
exit /b %STATUS%
:missing_pytest
echo Das Python-Modul pytest fehlt. Die Entwicklungs-Abhaengigkeiten installieren:
echo   "%PYTHON_CMD%" -m pip install -e ".[dev]"
popd
pause
exit /b 3
:missing_python
echo Python fehlt. Installiere Python 3.11 bis 3.14 und erstelle eine venv.
popd
pause
exit /b 4
