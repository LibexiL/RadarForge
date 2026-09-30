@echo off
rem ---------------------------------------------------------------------------
rem  Run RadarForge straight from this folder, without installing it.
rem  The first run creates .venv here and downloads the dependencies.
rem  Options can be added, e.g.:  run.bat --site KTLX
rem ---------------------------------------------------------------------------
setlocal EnableExtensions
cd /d "%~dp0"
set "VENV=%~dp0.venv"
if exist "%VENV%\Scripts\python.exe" goto :run

set "PY="
py -3 scripts\check_python.py >nul 2>nul && set "PY=py -3"
if not defined PY python scripts\check_python.py >nul 2>nul && set "PY=python"
if not defined PY (
  echo Python 3.10 or newer was not found - see README.md
  pause
  exit /b 1
)
echo First run: setting up %VENV% (a few minutes)...
%PY% -m venv "%VENV%"
if errorlevel 1 goto :fail
"%VENV%\Scripts\python.exe" -m pip install --quiet --upgrade pip wheel
"%VENV%\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto :fail

:run
"%VENV%\Scripts\python.exe" -m radarforge %*
exit /b %errorlevel%

:fail
echo Setup failed - see the messages above.
pause
exit /b 1
