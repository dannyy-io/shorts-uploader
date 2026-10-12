"""Self-update from the GitHub repo.

Checks the VERSION file on the main branch; if it's newer than ours, downloads the
branch as a zip and copies the code over this install. Your data/ and downloads/
folders (accounts, queue, history, videos) and the .venv are never touched.
The previous version is kept in data/update/backup so a bad update can be rolled back.
"""
import json
import os
import shutil
import time
import zipfile

import requests

REPO = "dannyy-io/shorts-uploader"
BRANCH = "main"
VERSION_URL = f"https://raw.githubusercontent.com/{REPO}/{BRANCH}/VERSION"
ZIP_URL = f"https://codeload.github.com/{REPO}/zip/refs/heads/{BRANCH}"
if os.environ.get("SHORTS_UPLOADER_UPDATE_BASE"):  # testing only: a local stand-in for GitHub
    VERSION_URL = os.environ["SHORTS_UPLOADER_UPDATE_BASE"] + "/VERSION"
    ZIP_URL = os.environ["SHORTS_UPLOADER_UPDATE_BASE"] + "/zip"

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
WORK_DIR = os.path.join(DATA_DIR, "update")
BACKUP_DIR = os.path.join(WORK_DIR, "backup")
STATE_FILE = os.path.join(WORK_DIR, "state.json")
PROTECTED = {"data", "downloads", ".venv", ".git", "__pycache__"}


class UpdateError(Exception):
    pass


def parse(v):
    try:
        return tuple(int(x) for x in str(v).strip().lstrip("v").split("."))
    except ValueError:
        return (0,)


def local_version():
    try:
        with open(os.path.join(BASE_DIR, "VERSION"), encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return "0.0.0"


def state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_state(**values):
    os.makedirs(WORK_DIR, exist_ok=True)
    st = state()
    st.update(values)
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, indent=2)
    os.replace(tmp, STATE_FILE)
    return st


def auto_enabled():
    return state().get("auto", True)


def check(timeout=8):
    """Return the newest version on GitHub (string), or raise UpdateError."""
    try:
        r = requests.get(VERSION_URL, params={"t": int(time.time())}, timeout=timeout,
                         headers={"Cache-Control": "no-cache"})
    except requests.RequestException as e:
        raise UpdateError(f"Couldn't reach GitHub ({type(e).__name__}).")
    if r.status_code != 200:
        raise UpdateError(f"GitHub answered {r.status_code} when checking for updates.")
    latest = r.text.strip()
    if parse(latest) == (0,):
        raise UpdateError("The version on GitHub looks wrong; not updating.")
    save_state(last_check=time.time(), latest=latest)
    return latest


def is_newer(latest):
    return parse(latest) > parse(local_version())


def _walk(root):
    """Relative paths of all files under root, skipping protected folders."""
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = os.path.relpath(dirpath, root)
        if rel_dir == ".":
            dirnames[:] = [d for d in dirnames if d not in PROTECTED]
        else:
            dirnames[:] = [d for d in dirnames if d != "__pycache__"]
        for name in filenames:
            rel = os.path.normpath(os.path.join(rel_dir, name))
            out.append(rel)
    return sorted(out)


def _copy_tree(src_root, files, dst_root):
    for rel in files:
        dst = os.path.join(dst_root, rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(os.path.join(src_root, rel), dst)


def _remove_extra(root, keep):
    keep = set(keep)
    for rel in _walk(root):
        if rel not in keep:
            try:
                os.remove(os.path.join(root, rel))
            except OSError:
                pass


def apply(expected=None, timeout=60):
    """Download and install the latest version. Returns the installed version."""
    os.makedirs(WORK_DIR, exist_ok=True)
    zip_path = os.path.join(WORK_DIR, "new.zip")
    extract_dir = os.path.join(WORK_DIR, "new")
    try:
        with requests.get(ZIP_URL, stream=True, timeout=timeout) as r:
            if r.status_code != 200:
                raise UpdateError(f"Download failed (HTTP {r.status_code}).")
            with open(zip_path, "wb") as f:
                for chunk in r.iter_content(1024 * 256):
                    f.write(chunk)
    except requests.RequestException as e:
        raise UpdateError(f"Download failed ({type(e).__name__}).")

    shutil.rmtree(extract_dir, ignore_errors=True)
    try:
        with zipfile.ZipFile(zip_path) as z:
            for member in z.namelist():  # refuse anything that would land outside the folder
                target = os.path.realpath(os.path.join(extract_dir, member))
                if not target.startswith(os.path.realpath(extract_dir) + os.sep):
                    raise UpdateError("The update archive looks unsafe; skipped.")
            z.extractall(extract_dir)
    except zipfile.BadZipFile:
        raise UpdateError("The downloaded update was damaged; will try again later.")
    tops = [d for d in os.listdir(extract_dir) if os.path.isdir(os.path.join(extract_dir, d))]
    if len(tops) != 1:
        raise UpdateError("Unexpected update layout.")
    new_root = os.path.join(extract_dir, tops[0])
    if not os.path.exists(os.path.join(new_root, "app.py")) or not os.path.exists(os.path.join(new_root, "VERSION")):
        raise UpdateError("The update is missing files; skipped.")
    with open(os.path.join(new_root, "VERSION"), encoding="utf-8") as f:
        new_version = f.read().strip()
    if expected and parse(new_version) < parse(expected):
        raise UpdateError("GitHub is still serving an older copy; will try again later.")
    if parse(new_version) <= parse(local_version()):
        raise UpdateError("Already up to date.")

    # back up what's installed now, then copy the new version in
    current = _walk(BASE_DIR)
    shutil.rmtree(BACKUP_DIR, ignore_errors=True)
    _copy_tree(BASE_DIR, current, BACKUP_DIR)
    new_files = _walk(new_root)
    _copy_tree(new_root, new_files, BASE_DIR)
    _remove_extra(BASE_DIR, new_files)

    shutil.rmtree(extract_dir, ignore_errors=True)
    try:
        os.remove(zip_path)
    except OSError:
        pass
    save_state(previous=_backup_version(), updated_at=time.time(), just_updated=new_version)
    return new_version


def _backup_version():
    try:
        with open(os.path.join(BACKUP_DIR, "VERSION"), encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return None


def rollback():
    """Put the previous version back (used when a fresh update fails to start)."""
    if not os.path.exists(os.path.join(BACKUP_DIR, "app.py")):
        raise UpdateError("No backup to roll back to.")
    bad = local_version()
    files = _walk(BACKUP_DIR)
    _copy_tree(BACKUP_DIR, files, BASE_DIR)
    _remove_extra(BASE_DIR, files)
    save_state(skip=bad, just_updated=None, rolled_back_from=bad, rolled_back_at=time.time())
    return local_version()
