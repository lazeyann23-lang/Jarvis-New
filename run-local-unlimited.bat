@echo off
REM JARVIS: lokale Ollama-KI ohne API-Tokenkosten starten.
REM Beeinflusst nur diesen Start, keine dauerhafte Config-Aenderung.
setlocal
cd /d "%~dp0"
if exist ".venv\Scripts\activate.bat" call ".venv\Scripts\activate.bat"
python -m jarvis.local_unlimited %*
if errorlevel 1 (
  echo.
  echo Hinweis: Zuerst Ollama starten und mindestens ein lokales Modell laden.
  pause
)
endlocal
