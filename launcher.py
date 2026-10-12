"""Starts Shorts Uploader like a desktop app.

  launcher.py               start (or focus) the app and open its window
  launcher.py --background  start without opening a window (used at Windows startup)

It checks GitHub for a newer version before starting, keeps dependencies installed,
restarts the app when it asks to update (exit code 3), and rolls back an update
that fails to start. Output goes to data/app.log.
"""
import hashlib
import os
import signal
import socket
import subprocess
import sys
import time

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)
DATA_DIR = os.path.join(BASE_DIR, "data")
os.makedirs(DATA_DIR, exist_ok=True)
LOG_FILE = os.path.join(DATA_DIR, "app.log")
PORT = int(os.environ.get("SHORTS_UPLOADER_PORT", "5005"))
URL = f"http://127.0.0.1:{PORT}"
RESTART_CODE = 3
SERVER = os.environ.get("SHORTS_UPLOADER_SERVER") == "1"  # in Docker: log to the container output
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
EXTRAS = ["yt-dlp[default]", "imageio-ffmpeg"]
OPTIONAL = ["deno"]


def log(msg):
    if SERVER:
        print(time.strftime("%Y-%m-%d %H:%M:%S ") + f"[launcher] {msg}", flush=True)
    try:
        if os.path.exists(LOG_FILE) and os.path.getsize(LOG_FILE) > 5 * 1024 * 1024:
            os.replace(LOG_FILE, LOG_FILE + ".old")
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + f"[launcher] {msg}\n")
    except OSError:
        pass


def console_python():
    """python.exe next to pythonw.exe, so child processes have working output streams."""
    exe = sys.executable
    if exe.lower().endswith("pythonw.exe"):
        alt = exe[:-len("pythonw.exe")] + "python.exe"
        if os.path.exists(alt):
            return alt
    return exe


def port_open():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", PORT)) == 0


def pip(*args):
    proc = subprocess.run([console_python(), "-m", "pip", "install", "-q", "--disable-pip-version-check", *args],
                          cwd=BASE_DIR, capture_output=True, text=True, creationflags=NO_WINDOW)
    if proc.returncode != 0:
        log("pip failed: " + (proc.stderr or proc.stdout)[-800:])
    return proc.returncode == 0


def deps_importable():
    """True if the app's core packages actually load in this Python."""
    probe = subprocess.run([console_python(), "-c", "import flask, requests"], cwd=BASE_DIR,
                           capture_output=True, creationflags=NO_WINDOW)
    return probe.returncode == 0


def ensure_deps():
    """Install the app's packages if they're missing or requirements.txt changed (e.g. after an update)."""
    import updater  # standard library only, so this works before anything is installed
    try:
        with open(os.path.join(BASE_DIR, "requirements.txt"), "rb") as f:
            digest = hashlib.sha256(f.read()).hexdigest()
    except OSError:
        return
    if updater.state().get("req_hash") == digest and deps_importable():
        return
    log("Installing what the app needs (first start or after an update; can take a few minutes)")
    if pip("-r", "requirements.txt"):
        pip("-U", *EXTRAS)
        pip("-U", *OPTIONAL)
        updater.save_state(req_hash=digest, extras_checked=time.time())
        log("Packages installed")
    else:
        log("Installing packages failed; check the internet connection. Details above.")


def try_update():
    """Apply a newer version from GitHub if there is one. Returns the new version or None."""
    import updater
    if not updater.auto_enabled():
        return None
    try:
        latest = updater.check()
        if not updater.is_newer(latest) or latest == updater.state().get("skip"):
            return None
        log(f"Updating {updater.local_version()} -> {latest}")
        version = updater.apply(expected=latest)
        log(f"Updated to {version}")
        return version
    except updater.UpdateError as e:
        log(f"Update skipped: {e}")
    except Exception as e:  # never let an update problem stop the app from starting
        log(f"Update error: {type(e).__name__}: {e}")
    return None


def run_app():
    env = dict(os.environ, SHORTS_UPLOADER_NO_BROWSER="1", SHORTS_UPLOADER_LAUNCHER="1",
               PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8")
    if SERVER:  # Docker/Dockge shows the container output, so let the app print there
        logf = open(os.devnull, "w")
        return subprocess.Popen([console_python(), os.path.join(BASE_DIR, "app.py")], cwd=BASE_DIR, env=env), logf
    logf = open(LOG_FILE, "a", encoding="utf-8")
    return subprocess.Popen([console_python(), os.path.join(BASE_DIR, "app.py")], cwd=BASE_DIR, env=env,
                            stdout=logf, stderr=subprocess.STDOUT, creationflags=NO_WINDOW), logf


_child = None
_stopping = False


def _on_stop(signum, frame):
    """Docker/Dockge 'Stop' (SIGTERM): ask the app to stop; main() sees it exit and returns.
    (Waiting for the app here would deadlock with main()'s own wait, so we don't.)"""
    global _stopping
    _stopping = True
    log("Stop requested; shutting down")
    if _child is not None:
        try:
            _child.terminate()
        except OSError:
            pass


def open_window():
    import winapp
    winapp.open_window(URL)


def main():
    global _child
    if hasattr(signal, "SIGTERM") and os.name != "nt":
        signal.signal(signal.SIGTERM, _on_stop)
    background = "--background" in sys.argv
    if port_open():  # already running: just show it
        if not background:
            open_window()
        return

    first_start, crashes = True, 0
    while not _stopping:
        ensure_deps()  # first, so a fresh install has its packages
        just_updated = try_update()
        if just_updated:
            ensure_deps()  # the update may need new packages
        started = time.time()
        proc, logf = run_app()
        _child = proc
        if first_start and not background:
            for _ in range(160):  # wait up to 40 s for the server
                if port_open() or proc.poll() is not None:
                    break
                time.sleep(0.25)
            if proc.poll() is None:
                open_window()
        first_start = False
        code = proc.wait()
        logf.close()
        log(f"App exited with code {code}")
        if _stopping:
            return

        if code == 0:
            return  # Quit from the tray / Settings
        if code == RESTART_CODE:
            continue  # app asked to restart (usually to install an update)

        quick = time.time() - started < 45
        if just_updated and quick:
            import updater
            import winapp
            try:
                back = updater.rollback()
                log(f"Version {just_updated} failed to start; rolled back to {back}")
                updater.save_state(req_hash=None)  # reinstall the old dependencies
                winapp.message_box(f"The update to version {just_updated} didn't start correctly, so the "
                                   f"previous version ({back}) was restored. Your data is safe.")
                continue
            except Exception as e:
                log(f"Rollback failed: {e}")
        crashes += 1
        if quick or crashes > 3:
            import winapp
            winapp.message_box("Shorts Uploader couldn't start. Details are in data\\app.log inside the app "
                               "folder. Send that file to whoever is helping you.", error=True)
            return
        time.sleep(5)  # crashed after running a while: restart it


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log(f"Launcher crashed: {type(e).__name__}: {e}")
        try:
            import winapp
            winapp.message_box(f"Shorts Uploader couldn't start: {e}", error=True)
        except Exception:
            pass
