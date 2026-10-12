@echo off
title Uninstall Shorts Uploader
set "APPDIR=%~dp0"
cd /d "%TEMP%"
set PY=
where python >nul 2>nul && set PY=python
if not defined PY where py >nul 2>nul && set PY=py -3
if not defined PY set "PY=%APPDIR%.venv\Scripts\python.exe"
%PY% "%APPDIR%install.py" --uninstall
echo.
pause
