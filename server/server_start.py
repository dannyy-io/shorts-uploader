"""Starts Shorts Uploader inside Docker (see server/compose.yaml).

First start: downloads the app from GitHub into /app. Every start: makes sure the
app's Python environment (/app/.venv) works, then hands over to launcher.py, which
installs updates, installs dependencies, and restarts the app when it updates.
Uses only the Python standard library, so it runs on a plain python image.
"""
import io
import json
import os
import shutil
import subprocess
import sys
import urllib.request
import zipfile

APP = os.environ.get("SHORTS_UPLOADER_HOME", "/app")
REPO = "dannyy-io/shorts-uploader"
BASE = os.environ.get("SHORTS_UPLOADER_UPDATE_BASE")  # testing only
ZIP_URL = (BASE + "/zip") if BASE else f"https://codeload.github.com/{REPO}/zip/refs/heads/main"
KEEP = {"data", "downloads", ".venv"}


def say(msg):
    print(f"[server_start] {msg}", flush=True)


def download_app():
    say(f"Downloading Shorts Uploader from GitHub into {APP}")
    with urllib.request.urlopen(ZIP_URL, timeout=120) as r:
        data = r.read()
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        top = z.namelist()[0].split("/")[0] + "/"
        for member in z.infolist():
            rel = member.filename[len(top):]
            if not rel or member.is_dir():
                continue
            if rel.split("/")[0] in KEEP or ".." in rel.split("/"):
                continue
            dest = os.path.join(APP, rel)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with z.open(member) as src, open(dest, "wb") as out:
                shutil.copyfileobj(src, out)


def venv_python():
    return os.path.join(APP, ".venv", "bin", "python")


def ensure_venv():
    py = venv_python()
    marker = os.path.join(APP, ".venv", "python-version")
    wanted = f"{sys.version_info.major}.{sys.version_info.minor}"
    ok = os.path.exists(py)
    if ok:
        try:
            ok = open(marker).read().strip() == wanted and subprocess.run([py, "-c", "pass"]).returncode == 0
        except OSError:
            ok = False
    if ok:
        return py
    say("Setting up the app's Python environment")
    shutil.rmtree(os.path.join(APP, ".venv"), ignore_errors=True)
    subprocess.run([sys.executable, "-m", "venv", os.path.join(APP, ".venv")], check=True)
    with open(marker, "w") as f:
        f.write(wanted)
    # make the launcher reinstall dependencies into the new environment
    state_path = os.path.join(APP, "data", "update", "state.json")
    try:
        with open(state_path) as f:
            state = json.load(f)
        state.pop("req_hash", None)
        with open(state_path, "w") as f:
            json.dump(state, f, indent=2)
    except (OSError, ValueError):
        pass
    return py


def main():
    os.makedirs(APP, exist_ok=True)
    if not os.path.exists(os.path.join(APP, "launcher.py")):
        download_app()
    py = ensure_venv()
    os.environ.setdefault("SHORTS_UPLOADER_SERVER", "1")
    say("Starting the app (first start installs a few things and can take a couple of minutes)")
    os.chdir(APP)
    os.execv(py, [py, os.path.join(APP, "launcher.py"), "--background"])


if __name__ == "__main__":
    main()
