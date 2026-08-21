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
echo Starting the loopback-only Saige remote analysis origin...
".venv\Scripts\python.exe" "%~dp0run_remote.py" --port 8770 --expected-hostname saige-label-reviewer-beta.saigeai.com --allowed-domain saigeai.com %*
endlocal
