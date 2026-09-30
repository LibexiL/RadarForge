@echo off
rem ---------------------------------------------------------------------------
rem  Remove RadarForge (installed with install.bat).
rem ---------------------------------------------------------------------------
setlocal EnableExtensions
title RadarForge uninstaller
cd /d "%~dp0"
set "APPDIR=%LOCALAPPDATA%\RadarForge"

powershell -NoProfile -ExecutionPolicy Bypass -File "scripts\windows\shortcuts.ps1" -Action remove
if exist "%APPDIR%\venv" rmdir /s /q "%APPDIR%\venv"
del /q "%APPDIR%\radarforge.ico" "%APPDIR%\radarforge.bat" 2>nul
echo RadarForge has been removed.
echo.

choice /c YN /m "Also delete your settings, themes and downloaded radar data"
if errorlevel 2 goto :keep
if exist "%APPDATA%\RadarForge" rmdir /s /q "%APPDATA%\RadarForge"
if exist "%APPDIR%" rmdir /s /q "%APPDIR%"
echo Settings and downloads deleted.
goto :end
:keep
echo Kept your settings in %APPDATA%\RadarForge
:end
echo.
pause
