@echo off
rem ---------------------------------------------------------------------------
rem  RadarForge installer for Windows 10 / 11
rem  Double-click this file (or run it from a Command Prompt).
rem
rem  * creates a private Python environment in %LOCALAPPDATA%\RadarForge\venv
rem  * adds RadarForge to the Start Menu and the Desktop
rem  * running it again updates an existing install (settings are kept)
rem ---------------------------------------------------------------------------
setlocal EnableExtensions
title RadarForge installer

rem the program files live one folder up from this Windows folder
for %%I in ("%~dp0..") do set "ROOT=%%~fI"
if not exist "%ROOT%\radarforge\__init__.py" goto :notextracted
cd /d "%ROOT%"

set "APPDIR=%LOCALAPPDATA%\RadarForge"
set "VENV=%APPDIR%\venv"

rem ---- find 64-bit Python 3.10 or newer ---------------------------------------
set "PY="
py -3 scripts\check_python.py >nul 2>nul && set "PY=py -3"
if not defined PY python scripts\check_python.py >nul 2>nul && set "PY=python"
if not defined PY goto :nopython
echo.
echo ==^> Using:
%PY% scripts\check_python.py

rem ---- virtual environment ----------------------------------------------------
if exist "%VENV%\Scripts\python.exe" goto :venv_ready
echo.
echo ==^> Creating the Python environment in %VENV%
if not exist "%APPDIR%" mkdir "%APPDIR%"
%PY% -m venv "%VENV%"
if errorlevel 1 goto :fail
:venv_ready
"%VENV%\Scripts\python.exe" -m pip install --quiet --upgrade pip wheel
if errorlevel 1 goto :fail

echo.
echo ==^> Installing dependencies (PySide6, NumPy, SciPy, MetPy, scikit-image, PyOpenGL)
echo     This can take a few minutes the first time.
"%VENV%\Scripts\python.exe" -m pip install --upgrade -r requirements.txt
if errorlevel 1 goto :fail

echo.
echo ==^> Installing RadarForge
"%VENV%\Scripts\python.exe" -m pip install --quiet --force-reinstall --no-deps .
if errorlevel 1 goto :fail

rem ---- icon, command-line launcher, shortcuts ---------------------------------
copy /y "radarforge\assets\radarforge.ico" "%APPDIR%\radarforge.ico" >nul
> "%APPDIR%\radarforge.bat" echo @"%VENV%\Scripts\python.exe" -m radarforge %%*
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0helpers\shortcuts.ps1" -Action create -Target "%VENV%\Scripts\pythonw.exe" -Icon "%APPDIR%\radarforge.ico" -WorkDir "%APPDIR%"
if errorlevel 1 echo     Could not create the shortcuts - start RadarForge with "%APPDIR%\radarforge.bat" instead.

echo.
echo ==^> Done!
echo     Start RadarForge from the Start Menu or the "RadarForge" icon on your desktop.
echo     To see its messages in a console window, run:  "%APPDIR%\radarforge.bat"
echo.
pause
exit /b 0

:notextracted
echo.
echo This file was started from inside the ZIP, or away from the rest of RadarForge.
echo.
echo  1. Close this window.
echo  2. Right-click the downloaded ZIP file and choose "Extract All...", then "Extract".
echo  3. In the extracted folder, open the "Windows" folder and double-click install.bat.
echo.
pause
exit /b 1

:nopython
echo.
echo Python 3.10 or newer (64-bit) was not found.
echo.
echo  1. Download Python from https://www.python.org/downloads/
echo  2. In the installer, tick "Add python.exe to PATH", then click "Install Now".
echo  3. Double-click install.bat again.
echo.
pause
exit /b 1

:fail
echo.
echo Installation failed - see the messages above.
echo If the download of a package failed, check your internet connection and run install.bat again.
echo.
pause
exit /b 1
