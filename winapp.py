"""Desktop integration: open the app in its own window, tray icon, message boxes.

Everything here degrades gracefully: on a system without Edge/Chrome the app opens
in the default browser, and if the tray icon can't start the app still works.
"""
import os
import subprocess
import sys
import threading
import webbrowser

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ICON_PNG = os.path.join(BASE_DIR, "static", "icon.png")
ICON_ICO = os.path.join(BASE_DIR, "static", "icon.ico")
IS_WIN = os.name == "nt"


def _browser_for_app_window():
    """Edge (built into Windows) or Chrome, which can open a page as a standalone app window."""
    if not IS_WIN:
        return None
    candidates = []
    for env in ("ProgramFiles(x86)", "ProgramFiles", "LOCALAPPDATA"):
        root = os.environ.get(env)
        if root:
            candidates.append(os.path.join(root, "Microsoft", "Edge", "Application", "msedge.exe"))
            candidates.append(os.path.join(root, "Google", "Chrome", "Application", "chrome.exe"))
    return next((c for c in candidates if os.path.exists(c)), None)


def open_window(url):
    exe = _browser_for_app_window()
    if exe:
        try:
            subprocess.Popen([exe, f"--app={url}", "--window-size=1280,900"],
                             creationflags=getattr(subprocess, "DETACHED_PROCESS", 0))
            return
        except OSError:
            pass
    webbrowser.open(url)


def message_box(text, title="Shorts Uploader", error=False):
    if IS_WIN:
        try:
            import ctypes
            flags = 0x10 if error else 0x40  # MB_ICONERROR / MB_ICONINFORMATION
            ctypes.windll.user32.MessageBoxW(None, text, title, flags | 0x40000)  # topmost
            return
        except Exception:
            pass
    print(f"{title}: {text}", file=sys.stderr)


_tray = None


def start_tray(on_open, on_check, on_quit):
    """Show a tray icon (Windows). Returns True if it started."""
    global _tray
    if not IS_WIN:
        return False
    try:
        import pystray
        from PIL import Image
    except Exception:
        return False
    try:
        image = Image.open(ICON_PNG)
        menu = pystray.Menu(
            pystray.MenuItem("Open Shorts Uploader", lambda icon, item: on_open(), default=True),
            pystray.MenuItem("Check for updates", lambda icon, item: on_check()),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit (stops scheduled posts)", lambda icon, item: on_quit()),
        )
        _tray = pystray.Icon("ShortsUploader", image, "Shorts Uploader — running", menu)
        threading.Thread(target=_tray.run, daemon=True, name="tray").start()
        return True
    except Exception as e:
        print("Tray icon failed:", e, file=sys.stderr)
        return False


def notify(text, title="Shorts Uploader"):
    try:
        if _tray is not None:
            _tray.notify(text, title)
            return True
    except Exception:
        pass
    return False


def stop_tray():
    try:
        if _tray is not None:
            _tray.stop()
    except Exception:
        pass
