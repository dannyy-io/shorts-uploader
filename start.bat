@echo off
rem Runs the app with a visible console (for troubleshooting). Normally use the Start Menu icon.
cd /d "%~dp0"
where python >nul 2>nul || (echo Python is not installed. Get it from https://www.python.org/downloads/ and tick "Add python.exe to PATH". & pause & exit /b 1)
if not exist .venv (
  echo First run: setting things up...
  python -m venv .venv || (pause & exit /b 1)
  .venv\Scripts\python -m pip install -q --disable-pip-version-check --upgrade pip
)
.venv\Scripts\python launcher.py
pause
