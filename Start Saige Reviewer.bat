@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" goto setup
".venv\Scripts\python.exe" "%~dp0run.py" --check-setup >nul 2>&1
if errorlevel 1 goto setup
goto launch

:setup
echo Installing or repairing the local analysis environment...
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup.ps1"
if errorlevel 1 (
  pause
  exit /b 1
)
goto launch

:launch
".venv\Scripts\python.exe" "%~dp0run.py" --open-browser %*
endlocal
