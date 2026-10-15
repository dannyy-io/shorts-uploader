"""Scheduled posting: a persistent queue of videos, posted by a background loop.

Posts go out while the app is running. Anything that came due while the PC was
off or the app was closed is posted as soon as the app starts again.
"""
import datetime as dt
import os
import queue as _queue
import shutil
import sys
import threading
import time
import uuid

from werkzeug.utils import secure_filename

import storage

QUEUE_DIR = os.path.join(storage.DATA_DIR, "queue")
os.makedirs(QUEUE_DIR, exist_ok=True)

CHECK_EVERY = 15  # seconds
_lock = threading.RLock()
_runner = None  # set by app.py: runner(item) -> {platform: result}
on_posted = None  # set by app.py: on_posted(item), called after a post goes out
_wake = threading.Event()
_prep_queue = _queue.Queue()


# ---------- settings ----------

def settings():
    cfg = storage.get_section("config", "schedule")
    times = cfg.get("daily_times") or [cfg.get("daily_time", "18:00")]  # older versions had one time
    return {"daily_times": sorted(set(times))}


def _clean_time(value):
    try:
        hh, mm = str(value).strip().split(":")[:2]
        hh, mm = int(hh), int(mm)
    except ValueError:
        raise ValueError(f"'{value}' isn't a valid time")
    if not (0 <= hh <= 23 and 0 <= mm <= 59):
        raise ValueError(f"'{value}' isn't a valid time")
    return f"{hh:02d}:{mm:02d}"


def set_daily_times(values):
    times = sorted({_clean_time(v) for v in values})
    if not times:
        raise ValueError("Keep at least one post time.")
    if len(times) > 24:
        raise ValueError("That's a lot of posts per day; the limit here is 24.")
    storage.update_section("config", "schedule", daily_times=times, daily_time=times[0])


def set_daily_time(value):  # kept for older callers
    set_daily_times([value])


# ---------- queue storage ----------

def items():
    return storage.load("queue", default=[])


def _save(lst):
    lst.sort(key=lambda i: i["when"])
    storage.save("queue", lst)


def _edit(item_id, fn):
    with _lock:
        lst = items()
        for item in lst:
            if item["id"] == item_id:
                result = fn(item, lst)
                _save(lst)
                return result
    raise KeyError(item_id)


def next_daily_slot(now=None):
    """The next post time (from the daily times list) that nothing is scheduled for yet."""
    times = [tuple(map(int, t.split(":"))) for t in settings()["daily_times"]]
    now = now or time.time()
    # every post still in the queue holds its slot, including ones converting or waiting for a retry
    taken = {int(i["when"] // 60) for i in items() if i["status"] != "done"}
    day = dt.date.fromtimestamp(now)
    for _ in range(3660):
        for hh, mm in times:
            slot = dt.datetime.combine(day, dt.time(hh, mm)).timestamp()
            if slot > now + 60 and int(slot // 60) not in taken:
                return slot
        day += dt.timedelta(days=1)
    raise ValueError("Queue is full")


def add(src_path, move, filename, title, texts, platforms, opts, when, fix=None, source=None):
    item_id = uuid.uuid4().hex[:12]
    dest = os.path.join(QUEUE_DIR, f"{item_id}_{secure_filename(filename) or 'video.mp4'}")
    (shutil.move if move else shutil.copy2)(src_path, dest)
    item = {
        "id": item_id,
        "file": dest,
        "filename": filename,
        "size": os.path.getsize(dest),
        "title": title,
        "texts": texts,
        "platforms": platforms,
        "opts": opts,
        "when": float(when),
        "status": "preparing" if fix else "scheduled",
        "fix": fix,
        "prep": None,
        "results": {},
        "job_id": None,
        "created": time.time(),
        "source_download": source,  # name in the Downloads folder it came from, if any
    }
    with _lock:
        lst = items()
        lst.append(item)
        _save(lst)
    if fix:
        _prep_queue.put(item_id)
    _wake.set()
    return item


def remove(item_id):
    def fn(item, lst):
        if item["status"] == "running":
            raise ValueError("It's uploading right now; wait for it to finish.")
        # (removing while it's converting is fine: the converter cleans up after itself)
        lst.remove(item)
        try:
            os.remove(resolve_file(item))
        except OSError:
            pass
    return _edit(item_id, fn)


def reschedule(item_id, when):
    def fn(item, lst):
        if item["status"] in ("running", "preparing"):
            raise ValueError("It's being worked on right now; try again in a moment.")
        if item["status"] == "done":
            raise ValueError("That one's already posted.")
        item["file"] = resolve_file(item)
        if not os.path.exists(item["file"]):
            raise ValueError("The video file for this post is missing.")
        if item.get("prep_error"):  # conversion failed earlier: try it again first
            item.update(when=float(when), status="preparing", prep_error=None)
            _prep_queue.put(item["id"])
        else:
            item.update(when=float(when), status="scheduled")
        return item
    result = _edit(item_id, fn)
    _wake.set()
    return result


def add_platforms(platforms, opts):
    """Add platforms to every queued post that isn't posting right now. Returns the ids of the posts changed.
    The new platforms get the same title and caption as the post's first platform."""
    changed = []
    with _lock:
        lst = items()
        for item in lst:
            if item["status"] in ("running", "done"):
                continue
            new = [p for p in platforms if p not in item["platforms"]]
            if not new:
                continue
            texts = item.get("texts") or {}
            src = next((texts[p] for p in item["platforms"] if p in texts), None) or {
                "title": item.get("title", ""), "description": item.get("description", "")}
            for p in new:  # (Instagram and TikTok join title + description into one caption when posting)
                texts[p] = {"title": src.get("title") or item.get("title", ""), "description": src.get("description", "")}
                item.setdefault("opts", {})[p] = opts.get(p, {})
            item["texts"] = texts
            item["platforms"] = item["platforms"] + new
            item.pop("tailor", None)  # its captions haven't been tailored for the new platform yet
            changed.append(item["id"])
        _save(lst)
    return changed


def post_now(item_id):
    return reschedule(item_id, time.time())


def resolve_file(item):
    """The queued video's path, even if the app folder was moved since it was queued."""
    path = item.get("file", "")
    if os.path.exists(path):
        return path
    alt = os.path.join(QUEUE_DIR, os.path.basename(path.replace("\\", "/")))
    return alt if os.path.exists(alt) else path


def remaining_platforms(item):
    """Platforms that still need posting (a retry skips ones that already succeeded)."""
    return [p for p in item["platforms"] if item["results"].get(p, {}).get("state") != "done"]


# ---------- background video preparation (auto-fix) ----------

def _prepare(item_id):
    import media
    item = next((i for i in items() if i["id"] == item_id), None)
    if not item or item["status"] != "preparing":
        return

    last = [0.0]

    def report(pct, msg):
        if time.time() - last[0] > 1 or pct >= 100:  # don't rewrite the queue file on every frame
            last[0] = time.time()
            try:
                _edit(item_id, lambda it, lst: it.update(prep={"progress": round(pct), "message": msg}))
            except KeyError:
                pass

    src = resolve_file(item)
    out = os.path.splitext(src)[0] + "_fixed.mp4"

    def discard(*paths):
        for path in paths:
            try:
                os.remove(path)
            except OSError:
                pass

    try:
        used, reasons = media.convert(src, out, item.get("fix") or {}, report)
    except Exception as e:
        discard(out)
        msg = str(e)

        def failed(it, lst):
            it.update(status="error", prep_error=msg, prep={"progress": 0, "message": msg})
        try:
            _edit(item_id, failed)
        except KeyError:  # removed from the queue while converting
            discard(src)
        _wake.set()
        return

    def done(it, lst):
        if used != src:
            discard(src)
        it.update(file=used, size=os.path.getsize(used), status="scheduled", fix=None,
                  prep={"progress": 100, "message": ("Converted: " + ", ".join(reasons)) if reasons
                        else "Format already good"})
    try:
        _edit(item_id, done)
    except KeyError:  # removed from the queue while converting
        discard(src, out)
    _wake.set()


def _prep_loop():
    while True:
        item_id = _prep_queue.get()
        try:
            _prepare(item_id)
        except Exception as e:
            print("Prepare error:", e, file=sys.stderr)


# ---------- background loop ----------

def _process(item_id):
    with _lock:
        lst = items()
        item = next((i for i in lst if i["id"] == item_id), None)
        if not item or item["status"] != "scheduled":
            return
        item["status"] = "running"
        _save(lst)
    try:
        item["file"] = resolve_file(item)
        results = _runner(item) if os.path.exists(item["file"]) else {
            p: {"state": "error", "message": "The video file for this post is missing.", "url": None}
            for p in remaining_platforms(item)}
    except Exception as e:  # never let one bad item kill the loop
        results = {p: {"state": "error", "message": f"Unexpected error: {e}", "url": None}
                   for p in remaining_platforms(item)}

    posted = []

    def fn(it, lst):
        it["results"].update(results)
        it["job_id"] = None
        all_done = all(it["results"].get(p, {}).get("state") == "done" for p in it["platforms"])
        it["status"] = "done" if all_done else "error"
        it["finished"] = time.time()
        if any(r.get("state") == "done" for r in it["results"].values()):
            posted.append(dict(it))
        if all_done:
            try:
                os.remove(it["file"])
            except OSError:
                pass
            lst.remove(it)  # finished posts live on in upload history
    _edit(item_id, fn)
    if posted and on_posted:
        try:
            on_posted(posted[0])
        except Exception as e:
            print("Couldn't record the post:", e, file=sys.stderr)


def _loop():
    while True:
        try:
            now = time.time()
            due = [i["id"] for i in items() if i["status"] == "scheduled" and i["when"] <= now]
            for item_id in due:
                _process(item_id)
        except Exception as e:  # keep the scheduler alive no matter what
            print("Scheduler error:", e, file=sys.stderr)
        _wake.wait(CHECK_EVERY)
        _wake.clear()


def start(runner):
    global _runner
    _runner = runner
    # If the app was closed mid-upload, put those back in the queue (finished platforms are skipped),
    # and restart any conversions that were interrupted.
    with _lock:
        lst = items()
        for item in lst:
            if item["status"] == "running":
                item["status"] = "scheduled"
            if item["status"] == "preparing":
                _prep_queue.put(item["id"])
        _save(lst)
    threading.Thread(target=_loop, daemon=True, name="scheduler").start()
    threading.Thread(target=_prep_loop, daemon=True, name="prepare").start()


def import_items(new_items):
    """Add posts brought over from another computer (see transfer.py)."""
    with _lock:
        lst = items()
        lst.extend(new_items)
        _save(lst)
    for it in new_items:
        if it.get("status") == "preparing":
            _prep_queue.put(it["id"])
    _wake.set()


def set_job(item_id, job_id):
    def fn(item, lst):
        item["job_id"] = job_id
    try:
        _edit(item_id, fn)
    except KeyError:
        pass


# ---------- start with Windows ----------

def _startup_file():
    if os.name != "nt":
        return None
    appdata = os.environ.get("APPDATA")
    if not appdata:
        return None
    return os.path.join(appdata, "Microsoft", "Windows", "Start Menu", "Programs", "Startup", "Shorts Uploader.vbs")


def autostart_supported():
    return _startup_file() is not None


def autostart_enabled():
    path = _startup_file()
    return bool(path and os.path.exists(path))


def set_autostart(enabled):
    path = _startup_file()
    if not path:
        raise ValueError("Starting with the computer is only set up for Windows.")
    if not enabled:
        if os.path.exists(path):
            os.remove(path)
        return
    base = storage.BASE_DIR.replace('"', '""')
    pythonw = os.path.join(storage.BASE_DIR, ".venv", "Scripts", "pythonw.exe")
    if os.path.exists(pythonw):  # installed app: start silently into the tray
        pw = pythonw.replace('"', '""')
        run = f'sh.Run """{pw}"" ""{base}\\launcher.py"" --background", 0, False\r\n'
    else:  # older start.bat setup: start minimized in the taskbar
        run = f'sh.Run """{base}\\start.bat""", 7, False\r\n'
    script = (
        'Set sh = CreateObject("WScript.Shell")\r\n'
        'sh.Environment("Process")("SHORTS_UPLOADER_NO_BROWSER") = "1"\r\n'
        f'sh.CurrentDirectory = "{base}"\r\n'
        + run
    )
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(script)
