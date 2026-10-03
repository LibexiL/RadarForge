@echo off
rem ---------------------------------------------------------------------------
rem  Run RadarForge straight from this folder, without installing it.
rem  The first run creates a .venv folder here and downloads the dependencies.
rem  Options can be added, e.g.:  run.bat --site KTLX
rem ---------------------------------------------------------------------------
setlocal EnableExtensions

rem the program files live one folder up from this Windows folder
for %%I in ("%~dp0..") do set "ROOT=%%~fI"
if not exist "%ROOT%\radarforge\__init__.py" goto :notextracted
set "VENV=%~dp0.venv"
if exist "%VENV%\Scripts\python.exe" goto :run

set "PY="
py -3 "%ROOT%\scripts\check_python.py" >nul 2>nul && set "PY=py -3"
if not defined PY python "%ROOT%\scripts\check_python.py" >nul 2>nul && set "PY=python"
if not defined PY (
  echo Python 3.10 or newer was not found.
  echo Download it from https://www.python.org/downloads/ and tick "Add python.exe to PATH".
  pause
  exit /b 1
)
echo First run: setting up %VENV% (a few minutes)...
%PY% -m venv "%VENV%"
if errorlevel 1 goto :fail
"%VENV%\Scripts\python.exe" -m pip install --quiet --upgrade pip wheel
"%VENV%\Scripts\python.exe" -m pip install -r "%ROOT%\requirements.txt"
if errorlevel 1 goto :fail

:run
rem a newer version may need packages the existing environment doesn't have yet
"%VENV%\Scripts\python.exe" -c "import h5py, imageio_ffmpeg" 2>nul || "%VENV%\Scripts\python.exe" -m pip install --quiet -r "%ROOT%\requirements.txt"
set "PYTHONPATH=%ROOT%"
"%VENV%\Scripts\python.exe" -m radarforge %*
exit /b %errorlevel%

:notextracted
echo This file was started from inside the ZIP, or away from the rest of RadarForge.
echo Right-click the downloaded ZIP, choose "Extract All...", then use the extracted Windows folder.
pause
exit /b 1

:fail
echo Setup failed - see the messages above.
pause
exit /b 1
