#!/usr/bin/env bash
# Mac/Linux: run the app (auto-updates included). On Windows use Install.bat instead.
cd "$(dirname "$0")"
if [ ! -d .venv ]; then
  echo "First run: setting things up..."
  python3 -m venv .venv || exit 1
fi
exec .venv/bin/python launcher.py "$@"
