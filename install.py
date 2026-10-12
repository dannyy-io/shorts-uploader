"""Installs Shorts Uploader as a desktop app (Windows).

  Install.bat   -> python install.py             install or repair
  Uninstall.bat -> python install.py --uninstall remove shortcuts and startup entry

Puts the app in %LOCALAPPDATA%\\Programs\\Shorts Uploader, brings along your accounts,
queue and downloads from the folder you run it from, creates Start Menu and desktop
shortcuts, and starts it. Safe to run again at any time.
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import time

APP_NAME = "Shorts Uploader"
SRC = os.path.dirname(os.path.abspath(__file__))
PROTECTED = {"data", "downloads", ".venv", ".git", "__pycache__"}
PORT = 5005


def say(msg=""):
    print(msg, flush=True)


def fail(msg):
    say("\n  Problem: " + msg)
    sys.exit(1)


def target_dir():
    root = os.environ.get("LOCALAPPDATA")
    if not root:
        fail("This installer is for Windows.")
    return os.path.join(root, "Programs", APP_NAME)


def port_open():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", PORT)) == 0


def stop_running_app():
    if not port_open():
        return
    try:  # new versions can quit themselves
        import urllib.request
        req = urllib.request.Request(f"http://127.0.0.1:{PORT}/api/app/quit", data=b"{}", method="POST",
                                     headers={"X-Shorts-Uploader": "1", "Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=3)
    except Exception:
        pass
    for _ in range(20):
        if not port_open():
            return
        time.sleep(0.5)
    say("  Shorts Uploader is still running. Close its black window (or Quit it from the tray),")
    input("  then press Enter here to continue...")
    if port_open():
        fail("It's still running. Close it and run Install.bat again.")


def code_files(root):
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = os.path.relpath(dirpath, root)
        if rel_dir == ".":
            dirnames[:] = [d for d in dirnames if d not in PROTECTED]
        else:
            dirnames[:] = [d for d in dirnames if d != "__pycache__"]
        out += [os.path.normpath(os.path.join(rel_dir, f)) for f in filenames]
    return out


def copy_app(dst):
    say(f"  Copying the app to {dst}")
    for rel in code_files(SRC):
        d = os.path.join(dst, rel)
        os.makedirs(os.path.dirname(d), exist_ok=True)
        shutil.copy2(os.path.join(SRC, rel), d)
    for folder in ("data", "downloads"):
        s, d = os.path.join(SRC, folder), os.path.join(dst, folder)
        if not os.path.isdir(s):
            continue
        if os.path.isdir(d) and os.listdir(d):
            say(f"  Keeping the existing {folder} folder in the installed app")
            continue
        say(f"  Bringing over your {folder} (accounts, queue, history)" if folder == "data" else
            f"  Bringing over your downloads")
        shutil.copytree(s, d, dirs_exist_ok=True, ignore=shutil.ignore_patterns("incoming", "update"))
    fix_queue_paths(dst)


def fix_queue_paths(dst):
    path = os.path.join(dst, "data", "queue.json")
    try:
        with open(path, encoding="utf-8") as f:
            items = json.load(f)
    except (OSError, ValueError):
        return
    for item in items:
        name = os.path.basename(str(item.get("file", "")).replace("\\", "/"))
        item["file"] = os.path.join(dst, "data", "queue", name)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(items, f, indent=2)


def run(args, **kw):
    return subprocess.run(args, **kw)


def make_venv(dst):
    venv = os.path.join(dst, ".venv")
    py = os.path.join(venv, "Scripts", "python.exe")
    if not os.path.exists(py):
        say("  Setting up Python for the app (first time only)...")
        if run([sys.executable, "-m", "venv", venv]).returncode != 0:
            fail("Couldn't create the app's Python environment.")
    say("  Installing what the app needs (a minute or two the first time)...")
    pip = [py, "-m", "pip", "install", "-q", "--disable-pip-version-check"]
    run(pip + ["-U", "pip"])
    if run(pip + ["-r", os.path.join(dst, "requirements.txt")]).returncode != 0:
        fail("Installing requirements failed. Check your internet connection and run Install.bat again.")
    say("  Installing the video downloader and converter...")
    run(pip + ["-U", "yt-dlp[default]", "imageio-ffmpeg"])
    run(pip + ["-U", "deno"], capture_output=True)
    return py, os.path.join(venv, "Scripts", "pythonw.exe")


def powershell(script):
    return run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
               capture_output=True, text=True)


def ps_str(s):
    return "'" + s.replace("'", "''") + "'"


def make_shortcuts(dst, pythonw):
    say("  Adding Start Menu and desktop shortcuts")
    script = f"""
$ws = New-Object -ComObject WScript.Shell
$targets = @(
  (Join-Path ([Environment]::GetFolderPath('Programs')) {ps_str(APP_NAME + '.lnk')}),
  (Join-Path ([Environment]::GetFolderPath('Desktop')) {ps_str(APP_NAME + '.lnk')})
)
foreach ($t in $targets) {{
  $s = $ws.CreateShortcut($t)
  $s.TargetPath = {ps_str(pythonw)}
  $s.Arguments = {ps_str('"' + os.path.join(dst, 'launcher.py') + '"')}
  $s.WorkingDirectory = {ps_str(dst)}
  $s.IconLocation = {ps_str(os.path.join(dst, 'static', 'icon.ico'))}
  $s.Description = 'Upload Shorts, Reels and TikToks'
  $s.Save()
}}
"""
    res = powershell(script)
    if res.returncode != 0:
        say("  (Couldn't create shortcuts: " + (res.stderr or "").strip()[:200] + ")")
        say(f"  You can still start it from {dst}\\Shorts Uploader.bat")


def remove_shortcuts():
    powershell(f"""
foreach ($d in @([Environment]::GetFolderPath('Programs'), [Environment]::GetFolderPath('Desktop'))) {{
  $p = Join-Path $d {ps_str(APP_NAME + '.lnk')}
  if (Test-Path $p) {{ Remove-Item $p }}
}}""")


def startup_file():
    return os.path.join(os.environ.get("APPDATA", ""), "Microsoft", "Windows", "Start Menu", "Programs",
                        "Startup", "Shorts Uploader.vbs")


def refresh_autostart(dst, py):
    """If 'Start with Windows' was on, point it at the installed app."""
    if os.path.exists(startup_file()):
        run([py, "-c", "import scheduler; scheduler.set_autostart(True)"], cwd=dst)
        say("  Updated 'Start with Windows' to use the installed app")


def install():
    dst = target_dir()
    say(f"\n  Installing {APP_NAME}\n")
    stop_running_app()
    if os.path.normcase(os.path.abspath(SRC)) != os.path.normcase(os.path.abspath(dst)):
        copy_app(dst)
    py, pythonw = make_venv(dst)
    make_shortcuts(dst, pythonw)
    refresh_autostart(dst, py)
    with open(os.path.join(dst, "Shorts Uploader.bat"), "w", encoding="utf-8") as f:
        f.write(f'@echo off\r\nstart "" "{pythonw}" "{os.path.join(dst, "launcher.py")}"\r\n')
    say("\n  Done! Starting Shorts Uploader...")
    subprocess.Popen([pythonw, os.path.join(dst, "launcher.py")], cwd=dst,
                     creationflags=getattr(subprocess, "DETACHED_PROCESS", 0))
    say("\n  From now on, open it from the Start Menu or the desktop icon.")
    say("  It updates itself, so you won't need to download it again.")
    if os.path.normcase(os.path.abspath(SRC)) != os.path.normcase(os.path.abspath(dst)):
        say(f"\n  Everything was copied, so you can delete this folder now:\n  {SRC}")


def uninstall():
    say(f"\n  Uninstalling {APP_NAME}\n")
    stop_running_app()
    remove_shortcuts()
    try:
        os.remove(startup_file())
    except OSError:
        pass
    dst = target_dir()
    say("  Removed the shortcuts and the 'Start with Windows' entry.")
    say(f"  Your accounts, queue and downloads are still in:\n  {dst}")
    answer = input("\n  Delete that folder too, including your downloads? Type YES to delete: ").strip()
    if answer == "YES":
        # delete after this window closes, since this script may be running from inside it
        subprocess.Popen(f'cmd /c timeout /t 3 >nul & rmdir /s /q "{dst}"', shell=True,
                         cwd=os.environ.get("TEMP") or None,
                         creationflags=getattr(subprocess, "DETACHED_PROCESS", 0))
        say("  The folder will be deleted in a moment.")
    else:
        say("  Kept the folder.")


if __name__ == "__main__":
    if os.name != "nt":
        fail("The installer is for Windows. On Mac/Linux use ./start.sh")
    if "--uninstall" in sys.argv:
        uninstall()
    else:
        install()
