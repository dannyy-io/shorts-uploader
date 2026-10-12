"""Shorts Uploader - post one vertical video to YouTube Shorts, Instagram Reels
and TikTok from a single local page.

Run:  python app.py   then open http://127.0.0.1:5005
"""
import os
import re
import secrets
import sys
import threading
import time
import uuid
from urllib.parse import quote

from flask import Flask, abort, jsonify, redirect, render_template, request, send_file
from werkzeug.utils import secure_filename

import downloader
import media
import scheduler
import storage
import updater
import winapp
from platforms import PLATFORMS
from platforms.common import UploadError, new_verifier

HOST = "127.0.0.1"
PORT = int(os.environ.get("SHORTS_UPLOADER_PORT", "5005"))
BASE_URL = f"http://{HOST}:{PORT}"
ALLOWED_EXT = {".mp4", ".mov", ".m4v", ".webm"}

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 4 * 1024 ** 3  # 4 GB

_oauth_pending = {}  # state -> (platform, verifier, created_at)
_jobs = {}
_jobs_lock = threading.Lock()


def _home_error(msg):
    return redirect("/?error=" + quote(msg))


def redirect_uri(platform):
    return f"{BASE_URL}/callback/{platform}"


@app.before_request
def local_only():
    # Block other websites from talking to this server (DNS rebinding / CSRF).
    if request.host.split(":")[0] not in ("127.0.0.1", "localhost"):
        abort(403)
    if request.method == "POST" and request.headers.get("X-Shorts-Uploader") != "1":
        abort(403)


@app.get("/")
def index():
    return render_template("index.html", redirect_uris={p: redirect_uri(p) for p in ("youtube", "tiktok")})


# ---------- settings & accounts ----------

@app.get("/api/status")
def api_status():
    cfg = storage.load("config")
    return jsonify({
        "platforms": {key: {"name": mod.NAME, **mod.status()} for key, mod in PLATFORMS.items()},
        "settings": {
            "youtube_client_id": cfg.get("youtube", {}).get("client_id", ""),
            "youtube_has_secret": bool(cfg.get("youtube", {}).get("client_secret")),
            "tiktok_client_key": cfg.get("tiktok", {}).get("client_key", ""),
            "tiktok_has_secret": bool(cfg.get("tiktok", {}).get("client_secret")),
            "instagram_api_host": cfg.get("instagram", {}).get("api_host", "graph.instagram.com"),
            "presets": get_presets(),
            "ffmpeg": media.available(),
        },
    })


def get_presets():
    """Caption presets (hashtags / credit lines). Migrates the old single 'saved hashtags' box."""
    gen = storage.get_section("config", "general")
    presets = gen.get("presets")
    if presets is None:
        old = (gen.get("footer") or "").strip()
        presets = [{"id": "p1", "name": "My hashtags", "text": old}] if old else []
        storage.update_section("config", "general", presets=presets)
    return presets


def fill_preset(text, title, creator, link):
    """Fill {title} {creator} {link}. Lines whose placeholder has no value are dropped."""
    values = {"creator": (creator or "").strip(), "link": (link or "").strip(), "title": (title or "").strip()}
    lines = []
    for line in (text or "").splitlines():
        keys = re.findall(r"\{(creator|link|title)\}", line)
        if any(not values[k] for k in keys):
            continue
        for k in keys:
            line = line.replace("{" + k + "}", values[k])
        lines.append(line)
    return "\n".join(lines).strip()


def build_texts(form, platforms, title, description, creator, link):
    """Per-platform title + description. A per-platform override replaces the description on
    YouTube, and the whole caption (title + description) on Instagram and TikTok."""
    preset = next((p for p in get_presets() if p["id"] == form.get("preset")), None)
    extra = fill_preset(preset["text"], title, creator, link) if preset else ""
    texts = {}
    for p in platforms:
        override = (form.get(f"desc_{p}") or "").strip()
        if override:
            t, d = (title, override) if p == "youtube" else ("", override)
        else:
            t, d = title, description
        if extra:
            d = f"{d}\n\n{extra}".strip()
        texts[p] = {"title": t, "description": d}
    return texts


@app.post("/api/settings")
def api_settings():
    s = request.get_json(force=True) or {}

    def keep(new, section, key):
        # Blank secret fields mean "leave as is".
        new = (new or "").strip()
        return new if new else storage.get_section("config", section).get(key, "")

    if "youtube_client_id" in s:
        storage.update_section("config", "youtube",
                               client_id=s["youtube_client_id"].strip(),
                               client_secret=keep(s.get("youtube_client_secret"), "youtube", "client_secret"))
    if "tiktok_client_key" in s:
        storage.update_section("config", "tiktok",
                               client_key=s["tiktok_client_key"].strip(),
                               client_secret=keep(s.get("tiktok_client_secret"), "tiktok", "client_secret"))
    if "instagram_api_host" in s:
        host = s["instagram_api_host"]
        if host not in ("graph.instagram.com", "graph.facebook.com"):
            return jsonify(error="Unknown Instagram API host"), 400
        storage.update_section("config", "instagram", api_host=host)
    if "presets" in s:
        cleaned = []
        for i, pr in enumerate((s["presets"] or [])[:30]):
            name, text = str(pr.get("name", "")).strip()[:60], str(pr.get("text", "")).strip()[:2000]
            if name or text:
                cleaned.append({"id": str(pr.get("id") or f"p{int(time.time() * 1000)}{i}")[:40],
                                "name": name or f"Preset {i + 1}", "text": text})
        storage.update_section("config", "general", presets=cleaned)
    if s.get("instagram_token"):
        try:
            PLATFORMS["instagram"].set_token(s["instagram_token"], s.get("instagram_user_id"))
        except UploadError as e:
            return jsonify(error=str(e)), 400
    return api_status()


@app.post("/api/disconnect/<platform>")
def api_disconnect(platform):
    if platform not in PLATFORMS:
        abort(404)
    PLATFORMS[platform].disconnect()
    return api_status()


@app.get("/connect/<platform>")
def connect(platform):
    mod = PLATFORMS.get(platform)
    if not mod or not mod.USES_OAUTH:
        abort(404)
    if not mod.status()["configured"]:
        return _home_error(f"Add your {mod.NAME} app credentials in Settings first.")
    now = time.time()
    for k, v in list(_oauth_pending.items()):
        if now - v[2] > 600:
            _oauth_pending.pop(k, None)
    state, verifier = secrets.token_urlsafe(24), new_verifier()
    _oauth_pending[state] = (platform, verifier, now)
    return redirect(mod.auth_url(redirect_uri(platform), state, verifier))


@app.get("/callback/<platform>")
def callback(platform):
    pending = _oauth_pending.pop(request.args.get("state", ""), None)
    if not pending or pending[0] != platform:
        return _home_error("Sign-in session expired or didn't match. Try Connect again.")
    if request.args.get("error"):
        msg = request.args.get("error_description") or request.args.get("error")
        return _home_error(f"{PLATFORMS[platform].NAME} sign-in was cancelled: {msg}")
    try:
        PLATFORMS[platform].finish_auth(request.args.get("code", ""), redirect_uri(platform), pending[1])
    except UploadError as e:
        return _home_error(str(e))
    return redirect(f"/?connected={platform}")


@app.get("/api/tiktok/creator_info")
def tiktok_creator_info():
    try:
        return jsonify(PLATFORMS["tiktok"].creator_info())
    except UploadError as e:
        return jsonify(error=str(e)), 400


# ---------- uploads ----------

@app.post("/api/upload")
def api_upload():
    video = request.files.get("video")
    local_name = request.form.get("local_file", "")
    if local_name:
        local_path = downloader.safe_path(local_name)
        if not local_path:
            return jsonify(error="That downloaded file isn't there any more."), 400
        filename = local_name
    elif video and video.filename:
        local_path, filename = None, video.filename
    else:
        return jsonify(error="Pick a video file first."), 400
    ext = os.path.splitext(filename)[1].lower()
    if ext not in ALLOWED_EXT:
        return jsonify(error=f"Unsupported file type {ext}. Use MP4, MOV or WebM."), 400

    form = request.form
    selected = [p for p in form.getlist("platforms") if p in PLATFORMS]
    if not selected:
        return jsonify(error="Choose at least one platform."), 400
    title = form.get("title", "").strip()
    description = form.get("description", "").strip()
    src_meta = downloader.meta(local_name) if local_name else {}
    creator = form.get("creator", src_meta.get("creator", ""))
    link = form.get("link", src_meta.get("link", ""))
    texts = build_texts(form, selected, title, description, creator, link)
    fix = {
        "enabled": form.get("fix", "1") == "1",
        "fit": "crop" if form.get("fit") == "crop" else "blur",
        "trim": 180 if form.get("trim") == "1" else 0,
    }

    job_id = uuid.uuid4().hex[:12]
    if local_path:
        path = local_path  # a file from the downloads folder: use it in place, never delete it
    else:
        path = os.path.join(storage.TEMP_DIR, f"{job_id}_{secure_filename(filename) or 'video' + ext}")
        video.save(path)
    if os.path.getsize(path) == 0:
        if not local_path:
            os.remove(path)
        return jsonify(error="That file is empty."), 400

    opts = {
        "youtube": {
            "privacy": form.get("yt_privacy", "public"),
            "made_for_kids": form.get("yt_kids") == "1",
            "add_shorts_tag": form.get("yt_shorts_tag") == "1",
            "notify": form.get("yt_notify", "1") == "1",
        },
        "instagram": {"share_to_feed": form.get("ig_feed", "1") == "1",
                      "thumb_offset": int(float(form["ig_thumb"])) if form.get("ig_thumb") else None},
        "tiktok": {
            "mode": form.get("tt_mode", "draft"),
            "privacy": form.get("tt_privacy", "SELF_ONLY"),
            "disable_comment": form.get("tt_no_comments") == "1",
        },
    }

    mode = form.get("when_mode", "now")
    if mode in ("at", "daily"):
        try:
            if mode == "daily":
                when = scheduler.next_daily_slot()
            else:
                when = float(form.get("when_ts", "0"))
                if when < time.time() - 60:
                    raise ValueError("That time has already passed.")
        except ValueError as e:
            if not local_path:
                os.remove(path)
            return jsonify(error=str(e) or "Pick a valid time."), 400
        # Downloaded files are copied (you keep the original); fresh uploads are moved into the queue.
        item = scheduler.add(path, move=not local_path, filename=filename, title=title, texts=texts,
                             platforms=selected, opts=opts, when=when, fix=fix if fix["enabled"] else None)
        return jsonify(scheduled=item)

    job = _new_job(job_id, filename, title, selected)
    if fix["enabled"]:
        job["prepare"] = {"state": "queued", "progress": 0, "message": "Checking video format"}
    threading.Thread(target=_run_job, args=(job, path, texts, opts, not local_path),
                     kwargs={"fix": fix if fix["enabled"] else None}, daemon=True).start()
    return jsonify(job_id=job_id)


def _new_job(job_id, filename, title, platforms):
    job = {
        "id": job_id,
        "file": filename,
        "title": title,
        "started": time.time(),
        "done": False,
        "platforms": {p: {"state": "queued", "progress": 0, "message": "Waiting", "url": None} for p in platforms},
    }
    with _jobs_lock:
        _jobs[job_id] = job
    return job


def _run_scheduled(item):
    """Called by the scheduler when a queued post comes due. Blocks until done."""
    platforms = scheduler.remaining_platforms(item)
    texts = item.get("texts") or {p: {"title": item.get("title", ""), "description": item.get("description", "")}
                                  for p in item["platforms"]}
    job = _new_job(uuid.uuid4().hex[:12], item["filename"], item["title"], platforms)
    job["scheduled_id"] = item["id"]
    scheduler.set_job(item["id"], job["id"])
    _run_job(job, item["file"], texts, item["opts"], delete_after=False)
    return {p: {"state": e["state"], "url": e["url"], "message": e["message"]} for p, e in job["platforms"].items()}


def _run_job(job, path, texts, opts, delete_after=True, fix=None):
    fixed_path = None
    if fix:
        prep = job["prepare"]

        def prep_report(pct, msg):
            prep.update(state="running", progress=round(pct, 1), message=msg)
        try:
            out = os.path.join(storage.TEMP_DIR, f"{job['id']}_fixed.mp4")
            used, reasons = media.convert(path, out, fix, prep_report)
            if used != path:
                fixed_path = used
                if delete_after:
                    os.remove(path)
                    delete_after = False
                path = used
            prep.update(state="done", progress=100,
                        message="Converted: " + ", ".join(reasons) if reasons else "Format already good")
        except media.MediaError as e:
            prep.update(state="error", message=f"{e} Uploading the original instead.")
        except Exception as e:
            prep.update(state="error", message=f"Auto-fix failed ({e}). Uploading the original instead.")

    def worker(platform):
        entry = job["platforms"][platform]

        def report(pct, msg):
            entry.update(state="running", progress=round(min(max(pct, 0), 100), 1), message=msg)

        try:
            report(0, "Starting")
            t = texts[platform]
            result = PLATFORMS[platform].upload(path, t["title"], t["description"], opts[platform], report)
            entry.update(state="done", progress=100, message=result.get("message", "Done"), url=result.get("url"))
        except UploadError as e:
            entry.update(state="error", message=str(e))
        except Exception as e:  # network errors, unexpected API responses
            entry.update(state="error", message=f"Unexpected error: {type(e).__name__}: {e}")

    threads = [threading.Thread(target=worker, args=(p,), daemon=True) for p in job["platforms"]]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    job["done"] = True
    for leftover in ([path] if delete_after else []) + ([fixed_path] if fixed_path else []):
        try:
            os.remove(leftover)
        except OSError:
            pass
    history = storage.load("history", default=[])
    history.insert(0, {
        "file": job["file"], "title": job["title"], "time": job["started"],
        "results": {p: {"state": e["state"], "url": e["url"], "message": e["message"]}
                    for p, e in job["platforms"].items()},
    })
    storage.save("history", history[:50])


@app.get("/api/jobs/<job_id>")
def api_job(job_id):
    job = _jobs.get(job_id)
    if not job:
        abort(404)
    return jsonify(job)


@app.get("/api/history")
def api_history():
    return jsonify(storage.load("history", default=[]))


# ---------- downloader ----------

@app.get("/api/downloader/status")
def api_downloader_status():
    return jsonify(downloader.status())


@app.post("/api/downloader/install")
def api_downloader_install():
    downloader.install()
    return jsonify(downloader.status())


@app.post("/api/download")
def api_download():
    body = request.get_json(force=True) or {}
    try:
        job_id = downloader.start(body.get("url"), body.get("browser") or None)
    except ValueError as e:
        return jsonify(error=str(e)), 400
    return jsonify(job_id=job_id)


@app.get("/api/download/<job_id>")
def api_download_job(job_id):
    job = downloader.get(job_id)
    if not job:
        abort(404)
    return jsonify(job)


@app.get("/api/downloads")
def api_downloads():
    return jsonify(downloader.list_files())


@app.post("/api/downloads/delete")
def api_downloads_delete():
    if not downloader.delete((request.get_json(force=True) or {}).get("name", "")):
        abort(404)
    return jsonify(downloader.list_files())


@app.post("/api/downloads/open-folder")
def api_downloads_open():
    downloader.open_folder()
    return jsonify(ok=True)


@app.get("/files/<path:name>")
def downloaded_file(name):
    path = downloader.safe_path(name)
    if not path:
        abort(404)
    return send_file(path, conditional=True)


# ---------- scheduled posts ----------

def _queue_payload():
    return jsonify({
        "items": scheduler.items(),
        "next_slot": scheduler.next_daily_slot(),
        "daily_time": scheduler.settings()["daily_time"],
        "autostart": scheduler.autostart_enabled(),
        "autostart_supported": scheduler.autostart_supported(),
    })


@app.get("/api/queue")
def api_queue():
    return _queue_payload()


@app.post("/api/queue/settings")
def api_queue_settings():
    body = request.get_json(force=True) or {}
    try:
        if "daily_time" in body:
            scheduler.set_daily_time(body["daily_time"])
        if "autostart" in body:
            scheduler.set_autostart(bool(body["autostart"]))
    except (ValueError, OSError) as e:
        return jsonify(error=str(e)), 400
    return _queue_payload()


@app.post("/api/queue/<item_id>/<action>")
def api_queue_action(item_id, action):
    body = request.get_json(silent=True) or {}
    try:
        if action == "remove":
            scheduler.remove(item_id)
        elif action == "post-now":
            scheduler.post_now(item_id)
        elif action == "reschedule":
            when = float(body.get("when", 0))
            if when < time.time() - 60:
                raise ValueError("That time has already passed.")
            scheduler.reschedule(item_id, when)
        else:
            abort(404)
    except KeyError:
        return jsonify(error="That scheduled post no longer exists."), 404
    except ValueError as e:
        return jsonify(error=str(e)), 400
    return _queue_payload()


# ---------- app: version, updates, quit ----------

LAUNCHED = os.environ.get("SHORTS_UPLOADER_LAUNCHER") == "1"  # started by launcher.py (can restart itself)
RESTART_CODE = 3
UPDATE_EVERY = 6 * 3600
_app_state = {"restarting": False, "checking": False, "error": None}


def _busy():
    """True while anything is uploading, downloading or converting (don't restart then)."""
    if any(not j["done"] for j in list(_jobs.values())):
        return True
    if any(j["state"] == "running" for j in list(downloader._jobs.values())) or downloader._install["running"]:
        return True
    return any(i["status"] in ("running", "preparing") for i in scheduler.items())


def _exit_soon(code, delay=1.5):
    _app_state["restarting"] = code == RESTART_CODE
    if code == 0:
        winapp.stop_tray()

    def go():
        os._exit(code)
    threading.Timer(delay, go).start()


def _check_updates(install_if_idle):
    """Check GitHub. If newer and allowed, restart so the launcher installs it."""
    if _app_state["checking"]:
        return None
    _app_state["checking"] = True
    try:
        latest = updater.check()
        _app_state["error"] = None
        newer = updater.is_newer(latest) and latest != updater.state().get("skip")
        if newer and install_if_idle and LAUNCHED and not _busy():
            _exit_soon(RESTART_CODE)
            return "restarting"
        return "available" if newer else "current"
    except updater.UpdateError as e:
        _app_state["error"] = str(e)
        return "error"
    finally:
        _app_state["checking"] = False


def _update_loop():
    time.sleep(600)  # first check 10 min after start (the launcher already checked at startup)
    while True:
        if updater.auto_enabled():
            result = _check_updates(install_if_idle=True)
            if result == "available":  # busy right now: try again soon
                time.sleep(300)
                continue
        time.sleep(UPDATE_EVERY)


def _refresh_downloader_daily():
    st = updater.state()
    if time.time() - st.get("extras_checked", 0) > 86400 and downloader._ytdlp_version():
        updater.save_state(extras_checked=time.time())
        downloader.install()


def _changelog_top():
    try:
        with open(os.path.join(storage.BASE_DIR, "CHANGELOG.md"), encoding="utf-8") as f:
            text = f.read()
    except OSError:
        return ""
    parts = re.split(r"^## ", text, flags=re.M)
    return ("## " + parts[1]).strip() if len(parts) > 1 else ""


@app.get("/api/app")
def api_app():
    st = updater.state()
    just = st.get("just_updated")
    if just and just == updater.local_version():
        updater.save_state(just_updated=None)
    latest = st.get("latest")
    return jsonify({
        "version": updater.local_version(),
        "latest": latest,
        "update_available": bool(latest and updater.is_newer(latest) and latest != st.get("skip")),
        "auto": updater.auto_enabled(),
        "last_check": st.get("last_check"),
        "error": _app_state["error"],
        "launched": LAUNCHED,
        "restarting": _app_state["restarting"],
        "just_updated": just if just == updater.local_version() else None,
        "whats_new": _changelog_top() if just else "",
        "rolled_back_from": st.get("rolled_back_from"),
        "repo": updater.REPO,
    })


@app.post("/api/app/check")
def api_app_check():
    result = _check_updates(install_if_idle=bool((request.get_json(silent=True) or {}).get("install")))
    if result == "available" and (request.get_json(silent=True) or {}).get("install"):
        result = "busy" if _busy() else ("manual" if not LAUNCHED else result)
    body = api_app().get_json()
    body["result"] = result
    return jsonify(body)


@app.post("/api/app/settings")
def api_app_settings():
    body = request.get_json(force=True) or {}
    if "auto" in body:
        updater.save_state(auto=bool(body["auto"]))
    return api_app()


@app.post("/api/app/quit")
def api_app_quit():
    _exit_soon(0)
    return jsonify(ok=True)


def _already_running():
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        return sock.connect_ex((HOST, PORT)) == 0


def _cleanup_leftovers():
    for name in os.listdir(storage.TEMP_DIR):
        try:
            os.remove(os.path.join(storage.TEMP_DIR, name))
        except OSError:
            pass


if __name__ == "__main__":
    if _already_running():
        print(f"\n  Shorts Uploader is already running at {BASE_URL}. Opening it.\n")
        if os.environ.get("SHORTS_UPLOADER_NO_BROWSER") != "1":
            winapp.open_window(BASE_URL)
        sys.exit(0)
    _cleanup_leftovers()
    scheduler.start(_run_scheduled)
    if LAUNCHED:
        threading.Thread(target=_update_loop, daemon=True, name="updates").start()
        threading.Timer(30, _refresh_downloader_daily).start()

        def tray_check():
            result = _check_updates(install_if_idle=True)
            msg = {"restarting": "Installing the update — back in a few seconds.",
                   "available": "An update is ready; it'll install once nothing is uploading.",
                   "current": f"You're up to date (version {updater.local_version()}).",
                   }.get(result, f"Couldn't check for updates: {_app_state['error']}")
            if not winapp.notify(msg):
                winapp.message_box(msg)
        winapp.start_tray(on_open=lambda: winapp.open_window(BASE_URL), on_check=tray_check,
                          on_quit=lambda: _exit_soon(0, delay=0.2))
    if os.environ.get("SHORTS_UPLOADER_NO_BROWSER") != "1":
        threading.Timer(1.0, lambda: winapp.open_window(BASE_URL)).start()
    queued = sum(1 for i in scheduler.items() if i["status"] == "scheduled")
    print(f"\n  Shorts Uploader running at {BASE_URL}\n  Close this window to stop it"
          f"{f' (scheduled posts only go out while it runs; {queued} waiting)' if queued else ''}.\n")
    app.run(host=HOST, port=PORT, debug=False, threaded=True)
