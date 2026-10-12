@echo off
title Install Shorts Uploader
cd /d "%~dp0"
set PY=
where python >nul 2>nul && set PY=python
if not defined PY where py >nul 2>nul && set PY=py -3
if not defined PY (
  echo Python is not installed. Get it from https://www.python.org/downloads/
  echo and tick "Add python.exe to PATH" on the first screen, then run this again.
  pause
  exit /b 1
)
%PY% install.py
echo.
pause
