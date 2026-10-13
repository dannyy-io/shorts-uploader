"""Download a video from a link (YouTube, Instagram, TikTok and most other sites)
using yt-dlp, saving it to ./downloads.

yt-dlp, ffmpeg (for merging HD video + audio) and Deno (which YouTube needs) are
installed into the app's own Python environment with one click from the app.
"""
import importlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import sysconfig
import threading
import time
import uuid

import storage

DOWNLOAD_DIR = os.path.join(storage.BASE_DIR, "downloads")
BIN_DIR = os.path.join(storage.DATA_DIR, "bin")
META_DIR = os.path.join(DOWNLOAD_DIR, ".meta")  # creator + source link for each download, for credits
COOKIES_FILE = os.path.join(storage.DATA_DIR, "cookies.txt")
VIDEO_EXT = {".mp4", ".mov", ".m4v", ".webm", ".mkv"}
BROWSERS = ("firefox", "chrome", "edge", "brave", "opera", "vivaldi", "chromium")
PACKAGES = ["yt-dlp[default]", "imageio-ffmpeg"]
OPTIONAL_PACKAGES = ["deno"]  # JS runtime YouTube needs; tried separately so a failure doesn't block the rest

os.makedirs(DOWNLOAD_DIR, exist_ok=True)
os.makedirs(BIN_DIR, exist_ok=True)
os.makedirs(META_DIR, exist_ok=True)

_jobs = {}
_install = {"running": False, "log": "", "ok": None, "needs_restart": False}


def _use_venv_tools():
    """Tools pip installs (like deno) land in the venv's Scripts/bin folder, which
    isn't on PATH when start.bat runs the venv's python directly. Add it."""
    scripts = sysconfig.get_path("scripts")
    if scripts and scripts not in os.environ.get("PATH", "").split(os.pathsep):
        os.environ["PATH"] = scripts + os.pathsep + os.environ.get("PATH", "")


_use_venv_tools()


# ---------- tool detection ----------

def _ytdlp_version():
    if importlib.util.find_spec("yt_dlp") is None:
        return None
    try:
        from yt_dlp.version import __version__
        return __version__
    except Exception:
        return "unknown"


def ffmpeg_dir():
    """Folder containing an ffmpeg binary, or None."""
    found = shutil.which("ffmpeg")
    if found:
        return os.path.dirname(found)
    name = "ffmpeg.exe" if os.name == "nt" else "ffmpeg"
    local = os.path.join(BIN_DIR, name)
    if os.path.exists(local):
        return BIN_DIR
    try:
        import imageio_ffmpeg
        src = imageio_ffmpeg.get_ffmpeg_exe()
        # yt-dlp looks for a file literally named "ffmpeg", so copy it under that name.
        shutil.copy2(src, local)
        if os.name != "nt":
            os.chmod(local, 0o755)
        return BIN_DIR
    except Exception:
        return None


def status():
    return {
        "ytdlp": _ytdlp_version(),
        "ffmpeg": bool(ffmpeg_dir()) if _ytdlp_version() else bool(shutil.which("ffmpeg")),
        "deno": bool(shutil.which("deno")),
        "ejs": importlib.util.find_spec("yt_dlp_ejs") is not None,
        "cookies_file": os.path.exists(COOKIES_FILE),
        "install": dict(_install),
        "folder": DOWNLOAD_DIR,
    }


# ---------- install / update ----------

def install():
    if _install["running"]:
        return
    was_loaded = "yt_dlp" in sys.modules
    _install.update(running=True, log="", ok=None)

    def pip(pkgs):
        proc = subprocess.run([sys.executable, "-m", "pip", "install", "-U", "--disable-pip-version-check", *pkgs],
                              capture_output=True, text=True)
        _install["log"] += (proc.stdout[-1500:] + proc.stderr[-1500:])
        return proc.returncode == 0

    def run():
        try:
            ok = pip(PACKAGES)
            pip(OPTIONAL_PACKAGES)  # best effort
            importlib.invalidate_caches()
            ffmpeg_dir()
            _install.update(ok=ok, needs_restart=ok and was_loaded)
        except Exception as e:
            _install.update(ok=False, log=_install["log"] + f"\n{e}")
        finally:
            _install["running"] = False

    threading.Thread(target=run, daemon=True).start()


# ---------- downloading ----------

_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _friendly_error(msg, url):
    msg = _ANSI.sub("", str(msg)).replace("ERROR: ", "").strip()
    low = msg.lower()
    if "instagram" in url.lower() and ("login" in low or "cookies" in low or "rate-limit" in low
                                       or "empty media response" in low):
        return ("Instagram wants you to be logged in to download this. Pick the browser you're "
                "logged into Instagram with under 'Use my browser login' and try again.")
    if "sign in to confirm" in low or "not a bot" in low:
        return ("YouTube asked to confirm you're not a bot. Pick the browser you're logged into YouTube "
                "with under 'Use my browser login' and try again.")
    if ("could not copy" in low and "cookie" in low) or "failed to decrypt" in low or "dpapi" in low:
        return ("Couldn't read cookies from that browser (Chrome-based browsers lock them on Windows). "
                "Close the browser completely and retry, use Firefox, or export a cookies.txt file — see README.")
    if "unsupported url" in low:
        return "That link isn't a video page the downloader recognizes. Copy the link straight from the video."
    if "private" in low:
        return "That video is private or you don't have access to it."
    if "requested format is not available" in low or "n challenge" in low or "signature" in low:
        return msg + "  — Try 'Update downloader'; YouTube changes often."
    return msg


def start(url, browser=None):
    url = (url or "").strip()
    if not re.match(r"^https?://", url):
        raise ValueError("Paste a full link starting with https://")
    if _ytdlp_version() is None:
        raise ValueError("The downloader isn't installed yet. Click 'Install downloader' first.")
    if browser and browser not in BROWSERS:
        raise ValueError("Unknown browser")
    job = {"id": uuid.uuid4().hex[:12], "url": url, "state": "running", "progress": 0,
           "message": "Looking up video", "files": [], "started": time.time()}
    _jobs[job["id"]] = job
    threading.Thread(target=_run, args=(job, browser), daemon=True).start()
    return job["id"]


def get(job_id):
    return _jobs.get(job_id)


def _run(job, browser):
    try:
        import yt_dlp
    except ImportError:
        job.update(state="error", message="The downloader isn't installed. Click 'Install downloader'.")
        return

    def on_progress(d):
        if d.get("status") == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            done = d.get("downloaded_bytes") or 0
            info = d.get("info_dict") or {}
            kind = "audio" if info.get("vcodec") == "none" else "video"
            job["progress"] = round(100 * done / total, 1) if total else job["progress"]
            job["message"] = f"Downloading {kind}"
        elif d.get("status") == "finished":
            job["message"] = "Finishing up"

    def on_post(d):
        if d.get("status") == "started" and d.get("postprocessor") in ("Merger", "FFmpegMerger"):
            job["message"] = "Merging video and audio"

    ff = ffmpeg_dir()
    opts = {
        "outtmpl": os.path.join(DOWNLOAD_DIR, "%(title).70B [%(id)s].%(ext)s"),
        "windowsfilenames": True,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "progress_hooks": [on_progress],
        "postprocessor_hooks": [on_post],
    }
    if ff:
        # Best quality up to 1080p, preferring H.264 + AAC in MP4 so every platform accepts it.
        opts.update({
            "format": "bv*+ba/b",
            "format_sort": ["res:1080", "vcodec:h264", "acodec:aac", "ext:mp4:m4a"],
            "merge_output_format": "mp4",
            "ffmpeg_location": ff,
        })
    else:
        opts["format"] = "b[ext=mp4]/b"  # single-file formats only (lower quality on YouTube)
    if browser:
        opts["cookiesfrombrowser"] = (browser,)
    elif os.path.exists(COOKIES_FILE):
        opts["cookiefile"] = COOKIES_FILE

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(job["url"], download=True)
        entries = info.get("entries") if info.get("_type") == "playlist" else [info]
        files = []
        for entry in entries or []:
            if not entry:
                continue
            for rd in entry.get("requested_downloads") or []:
                path = rd.get("filepath") or rd.get("_filename")
                if path and os.path.exists(path):
                    files.append(os.path.basename(path))
                    _save_meta(os.path.basename(path), entry, info, job["url"])
        if not files:
            raise RuntimeError("Download finished but no video file was found (was it a photo post?).")
        job.update(state="done", progress=100, files=files, title=info.get("title"),
                   message="Saved " + (files[0] if len(files) == 1 else f"{len(files)} videos"))
    except Exception as e:
        job.update(state="error", message=_friendly_error(e, job["url"]))


# ---------- creator info (for credits) ----------

def _creator_handle(entry, parent):
    """Best guess at the original creator's @handle (or name) from yt-dlp's info."""
    def first(*keys):
        for src in (entry, parent):
            for k in keys:
                v = (src or {}).get(k)
                if v and isinstance(v, str):
                    return v.strip()
        return ""
    site = (entry.get("extractor_key") or parent.get("extractor_key") or "").lower()
    if "instagram" in site:
        handle = first("channel", "uploader_id", "uploader")
    elif "youtube" in site:
        handle = first("uploader_id", "channel_handle", "uploader", "channel")
    elif "tiktok" in site:
        handle = first("uploader", "channel", "creator")
    else:
        handle = first("uploader_id", "uploader", "channel", "creator")
    if handle and not handle.startswith("@") and re.fullmatch(r"[\w.]+", handle) and not handle.isdigit():
        handle = "@" + handle
    return handle


def _save_meta(name, entry, parent, url):
    parent = parent or {}
    meta = {
        "creator": _creator_handle(entry, parent),
        "link": entry.get("webpage_url") or parent.get("webpage_url") or url,
        "source_title": entry.get("title") or parent.get("title") or "",
        "site": entry.get("extractor_key") or parent.get("extractor_key") or "",
    }
    _write_meta(name, meta)


def _write_meta(name, data):
    try:
        with open(os.path.join(META_DIR, name + ".json"), "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except OSError:
        pass


_meta_lock = threading.Lock()


def update_meta(name, **values):
    """Change some fields of a download's info file (None removes a field)."""
    if not safe_path(name):
        return False
    with _meta_lock:
        data = meta(name)
        for k, v in values.items():
            if v is None:
                data.pop(k, None)
            else:
                data[k] = v
        _write_meta(name, data)
    return True


MARKS = ("posted", "skip")  # set by hand in the Downloads tab


def set_mark(name, mark):
    return update_meta(name, mark=mark if mark in MARKS else None)


def mark_posted(name):
    """Remember that this download went out (it then shows as Posted)."""
    return update_meta(name, posted_at=time.time())


def meta(name):
    try:
        with open(os.path.join(META_DIR, os.path.basename(name) + ".json"), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def delete(name):
    path = safe_path(name)
    if not path:
        return False
    os.remove(path)
    try:
        os.remove(os.path.join(META_DIR, name + ".json"))
    except OSError:
        pass
    return True


# ---------- the downloads folder ----------

def safe_path(name):
    """Resolve a file name inside the downloads folder, refusing anything outside it."""
    if not name or os.path.basename(name) != name:
        return None
    path = os.path.join(DOWNLOAD_DIR, name)
    if os.path.splitext(name)[1].lower() not in VIDEO_EXT or not os.path.isfile(path):
        return None
    return path


def list_files(limit=500):
    items = []
    for name in os.listdir(DOWNLOAD_DIR):
        path = os.path.join(DOWNLOAD_DIR, name)
        if os.path.isfile(path) and os.path.splitext(name)[1].lower() in VIDEO_EXT:
            st = os.stat(path)
            m = meta(name)
            items.append({"name": name, "size": st.st_size, "time": st.st_mtime,
                          "creator": m.get("creator", ""), "link": m.get("link", ""),
                          "source_title": m.get("source_title", ""),
                          "mark": m.get("mark") if m.get("mark") in MARKS else None,
                          "posted_at": m.get("posted_at"), "cleared_at": m.get("cleared_at")})
    items.sort(key=lambda x: x["time"], reverse=True)
    return items[:limit]


def open_folder():
    if os.name == "nt":
        os.startfile(DOWNLOAD_DIR)  # noqa: S606 - local folder only
    elif sys.platform == "darwin":
        subprocess.Popen(["open", DOWNLOAD_DIR])
    else:
        subprocess.Popen(["xdg-open", DOWNLOAD_DIR])
