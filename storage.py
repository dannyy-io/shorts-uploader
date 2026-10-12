"""Tiny JSON-file storage for settings, tokens and upload history.

Everything lives in ./data next to this file and never leaves your computer.
"""
import json
import os
import threading

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
TEMP_DIR = os.path.join(DATA_DIR, "incoming")
os.makedirs(TEMP_DIR, exist_ok=True)

_lock = threading.RLock()


def _path(name):
    return os.path.join(DATA_DIR, f"{name}.json")


def load(name, default=None):
    if default is None:
        default = {}
    with _lock:
        try:
            with open(_path(name), encoding="utf-8") as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return default


def save(name, data):
    with _lock:
        tmp = _path(name) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, _path(name))


def get_section(name, section):
    return load(name).get(section, {})


def set_section(name, section, values):
    with _lock:
        data = load(name)
        data[section] = values
        save(name, data)


def update_section(name, section, **values):
    with _lock:
        data = load(name)
        current = data.get(section, {})
        current.update(values)
        data[section] = current
        save(name, data)
        return current


def clear_section(name, section):
    with _lock:
        data = load(name)
        data.pop(section, None)
        save(name, data)
