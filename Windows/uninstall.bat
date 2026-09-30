@echo off
rem ---------------------------------------------------------------------------
rem  Remove RadarForge (installed with install.bat).
rem ---------------------------------------------------------------------------
setlocal EnableExtensions
title RadarForge uninstaller
set "APPDIR=%LOCALAPPDATA%\RadarForge"

if exist "%~dp0helpers\shortcuts.ps1" powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0helpers\shortcuts.ps1" -Action remove
rem (in case the helper is missing, remove the shortcuts directly as well)
del /q "%APPDATA%\Microsoft\Windows\Start Menu\Programs\RadarForge.lnk" 2>nul
del /q "%USERPROFILE%\Desktop\RadarForge.lnk" 2>nul
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
