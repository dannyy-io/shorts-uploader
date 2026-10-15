"""Shorts Uploader - post one vertical video to YouTube Shorts, Instagram Reels
and TikTok from a single local page.

Run:  python app.py   then open http://127.0.0.1:5005
"""
import datetime
import hmac
import os
import re
import secrets
import sys
import threading
import time
import uuid
from urllib.parse import quote

from flask import Flask, Response, abort, jsonify, redirect, render_template, request, send_file, session
from werkzeug.datastructures import MultiDict
from werkzeug.utils import secure_filename

import downloader
import media
import scheduler
import storage
import summarizer
import updater
import winapp
from platforms import PLATFORMS
from platforms.common import UploadError, new_verifier

SERVER = os.environ.get("SHORTS_UPLOADER_SERVER") == "1"  # running on a home server / in Docker
HOST = "0.0.0.0" if SERVER else "127.0.0.1"
PORT = int(os.environ.get("SHORTS_UPLOADER_PORT", "5005"))
BASE_URL = f"http://127.0.0.1:{PORT}"
# Sign-in redirects must match what's registered with Google/TikTok, so they always point at 127.0.0.1:5005.
REDIRECT_BASE = os.environ.get("SHORTS_UPLOADER_REDIRECT_BASE", "http://127.0.0.1:5005")
PASSWORD = os.environ.get("SHORTS_UPLOADER_PASSWORD", "")
PLACEHOLDER_PASSWORDS = {"", "change-me", "changeme", "password"}
ALLOWED_EXT = {".mp4", ".mov", ".m4v", ".webm"}

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 4 * 1024 ** 3  # 4 GB


def _secret_key():
    path = os.path.join(storage.DATA_DIR, "secret_key")
    try:
        with open(path, "rb") as f:
            key = f.read()
            if len(key) >= 32:
                return key
    except OSError:
        pass
    key = os.urandom(32)
    with open(path, "wb") as f:
        f.write(key)
    return key


app.secret_key = _secret_key()
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
                  PERMANENT_SESSION_LIFETIME=datetime.timedelta(days=30))
_login_failures = {}  # ip -> [timestamps]
STAGING_DIR = os.path.join(storage.DATA_DIR, "staging")  # videos sent from the browser once, reused for AI + posting
os.makedirs(STAGING_DIR, exist_ok=True)


def staged_path(token):
    """Path of a staged upload by its token, or None (refuses anything outside the staging folder)."""
    if not token or not re.fullmatch(r"[0-9a-f]{16}", token):
        return None
    for name in os.listdir(STAGING_DIR):
        if name.startswith(token + "_"):
            return os.path.join(STAGING_DIR, name)
    return None

_oauth_pending = {}  # state -> (platform, verifier, created_at)
_jobs = {}
_jobs_lock = threading.Lock()


def _home_error(msg):
    return redirect("/?error=" + quote(msg))


def redirect_uri(platform):
    return f"{REDIRECT_BASE}/callback/{platform}"


OPEN_ENDPOINTS = {"login", "login_post", "static"}


@app.before_request
def guard():
    if SERVER:
        # Reachable from the network: everything needs the password.
        if PASSWORD in PLACEHOLDER_PASSWORDS:
            return render_template("login.html", setup_needed=True), 503
        if request.endpoint in OPEN_ENDPOINTS:
            return None
        if not session.get("ok"):
            if request.path.startswith("/api/") or request.path.startswith("/files/"):
                return jsonify(error="Please log in again."), 401
            return redirect("/login")
    elif request.host.split(":")[0] not in ("127.0.0.1", "localhost"):
        abort(403)  # PC app: only this computer may talk to it (blocks DNS-rebinding tricks)
    # Other websites can't send this header, so they can't make the app do anything.
    if request.method == "POST" and request.headers.get("X-Shorts-Uploader") != "1":
        abort(403)


@app.get("/login")
def login():
    if not SERVER:
        return redirect("/")
    return render_template("login.html", setup_needed=False, error=None)


@app.post("/login")
def login_post():
    if not SERVER:
        abort(404)
    ip = request.remote_addr or "?"
    now = time.time()
    recent = [t for t in _login_failures.get(ip, []) if now - t < 900]
    if len(recent) >= 8:
        return render_template("login.html", setup_needed=False,
                               error="Too many wrong passwords. Wait 15 minutes and try again."), 429
    given = request.form.get("password", "")
    if hmac.compare_digest(given.encode(), PASSWORD.encode()):
        _login_failures.pop(ip, None)
        session.clear()
        session.permanent = True
        session["ok"] = True
        return redirect("/")
    recent.append(now)
    _login_failures[ip] = recent
    time.sleep(1)
    return render_template("login.html", setup_needed=False, error="That password isn't right."), 401


@app.post("/api/logout")
def api_logout():
    session.clear()
    return jsonify(ok=True)


@app.get("/")
def index():
    return render_template("index.html", redirect_uris={p: redirect_uri(p) for p in ("youtube", "tiktok")},
                           server=SERVER)


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
            "ai": summarizer.public_settings(),
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


def parse_tags(raw):
    """'a, b,#c' -> ['a', 'b', 'c'] (deduplicated, no '#')."""
    out, seen = [], set()
    for t in re.split(r"[,\n]", raw or ""):
        t = re.sub(r"\s+", " ", t.replace("#", "")).strip()
        if t and t.lower() not in seen:
            seen.add(t.lower())
            out.append(t)
    return out


def hashtags(tags, limit):
    out = []
    for t in tags:
        h = "#" + re.sub(r"[^\w]", "", t)
        if len(h) > 1 and h.lower() not in (x.lower() for x in out):
            out.append(h)
        if len(out) >= limit:
            break
    return " ".join(out)


def build_texts(form, platforms, title, description, creator, link):
    """Per-platform title + description. A per-platform override replaces the description on
    YouTube, and the whole caption (title + description) on Instagram and TikTok."""
    preset = next((p for p in get_presets() if p["id"] == form.get("preset")), None)
    extra = fill_preset(preset["text"], title, creator, link) if preset else ""
    tags = parse_tags(form.get("tags", ""))
    as_hashtags = form.get("tags_to_captions") == "1"
    texts = {}
    for p in platforms:
        override = (form.get(f"desc_{p}") or "").strip()
        if override:
            t, d = (title, override) if p == "youtube" else ("", override)
        else:
            t, d = title, description
        if extra:
            d = f"{d}\n\n{extra}".strip()
        if as_hashtags and tags:
            # YouTube shows the first 3 hashtags above the title (and ignores them all past 15)
            d = f"{d}\n\n{hashtags(tags, 3 if p == 'youtube' else 8)}".strip()
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


def _start_oauth(platform):
    mod = PLATFORMS.get(platform)
    if not mod or not mod.USES_OAUTH:
        abort(404)
    if not mod.status()["configured"]:
        raise UploadError(f"Add your {mod.NAME} app credentials in Settings first.")
    now = time.time()
    for k, v in list(_oauth_pending.items()):
        if now - v[2] > 1800:
            _oauth_pending.pop(k, None)
    state, verifier = secrets.token_urlsafe(24), new_verifier()
    _oauth_pending[state] = (platform, verifier, now)
    return mod.auth_url(redirect_uri(platform), state, verifier)


@app.get("/connect/<platform>")
def connect(platform):
    try:
        return redirect(_start_oauth(platform))
    except UploadError as e:
        return _home_error(str(e))


@app.post("/api/oauth/start/<platform>")
def api_oauth_start(platform):
    try:
        return jsonify(url=_start_oauth(platform))
    except UploadError as e:
        return jsonify(error=str(e)), 400


@app.post("/api/oauth/finish")
def api_oauth_finish():
    """Finish a sign-in from the address the browser landed on (used when the app runs on a server)."""
    from urllib.parse import parse_qs, urlparse
    raw = ((request.get_json(force=True) or {}).get("url") or "").strip()
    query = urlparse(raw).query if "://" in raw else raw.lstrip("?")
    params = {k: v[0] for k, v in parse_qs(query).items()}
    pending = _oauth_pending.get(params.get("state", ""))
    if not params.get("code") and not params.get("error"):
        return jsonify(error="That doesn't look like the sign-in address. Copy the whole address from the "
                             "page you landed on after signing in (it starts with http://127.0.0.1)."), 400
    if not pending:
        return jsonify(error="That sign-in has expired or was already used. Click Connect again."), 400
    platform = pending[0]
    if params.get("error"):
        _oauth_pending.pop(params["state"], None)
        return jsonify(error=f"{PLATFORMS[platform].NAME} sign-in was cancelled: "
                             f"{params.get('error_description') or params['error']}"), 400
    try:
        PLATFORMS[platform].finish_auth(params["code"], redirect_uri(platform), pending[1])
    except UploadError as e:
        return jsonify(error=str(e)), 400
    _oauth_pending.pop(params["state"], None)
    return jsonify(connected=platform, name=PLATFORMS[platform].NAME)


@app.get("/callback/<platform>")
def callback(platform):
    pending = _oauth_pending.pop(request.args.get("state", ""), None)
    if not pending or pending[0] != platform:
        if request.args.get("code") and request.args.get("state"):
            # Probably a sign-in started on the home-server copy that landed on this PC's copy:
            # show the address so it can be pasted into the server.
            from markupsafe import escape
            url = escape(request.url)
            return (f"""<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Shorts Uploader</title><body style="font:15px/1.5 system-ui;max-width:640px;margin:40px auto;padding:0 16px">
<h2>Finish connecting on your other copy</h2>
<p>This sign-in was started from another copy of Shorts Uploader (for example your home server).
Copy the address below and paste it into the <b>Connect</b> box there.</p>
<textarea readonly onclick="this.select()" style="width:100%;height:110px;font:13px monospace">{url}</textarea>
<p><button onclick="navigator.clipboard.writeText(document.querySelector('textarea').value);this.textContent='Copied'">Copy</button></p>
</body>""", 200)
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
    body, code = _submit_post(request.form, request.files.get("video"))
    return jsonify(body), code


_slot_lock = threading.Lock()  # two posts added at once must not take the same post time


def _submit_post(form, video=None):
    """Post now or add to the queue, from upload-form fields. Returns (json body, status code)."""
    local_name = form.get("local_file", "")
    staged = staged_path(form.get("staged", ""))
    if local_name:
        local_path = downloader.safe_path(local_name)
        if not local_path:
            return dict(error="That downloaded file isn't there any more."), 400
        filename = local_name
    elif staged:
        local_path, filename = None, os.path.basename(staged).split("_", 1)[1]
    elif form.get("staged"):
        return dict(error="That video expired from the app's temporary storage. Add it again."), 400
    elif video and video.filename:
        local_path, filename = None, video.filename
    else:
        return dict(error="Pick a video file first."), 400
    ext = os.path.splitext(filename)[1].lower()
    if ext not in ALLOWED_EXT:
        return dict(error=f"Unsupported file type {ext}. Use MP4, MOV or WebM."), 400

    selected = [p for p in form.getlist("platforms") if p in PLATFORMS]
    if not selected:
        return dict(error="Choose at least one platform."), 400
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
    hook = media.hook_settings({"hook": {"text": form.get("hook_text", ""), "style": form.get("hook_style"),
                                         "pos": form.get("hook_pos"), "secs": form.get("hook_secs") or 0}})
    if hook:
        if not media.available():
            return dict(error="Hook text needs ffmpeg, which isn't installed yet. Restart the app to install it."), 400
        fix.update(enabled=True, hook=hook)  # drawing text on the video always needs a conversion

    job_id = uuid.uuid4().hex[:12]
    if local_path:
        path = local_path  # a file from the downloads folder: use it in place, never delete it
    elif staged:
        path = os.path.join(storage.TEMP_DIR, f"{job_id}_{secure_filename(filename) or 'video' + ext}")
        os.replace(staged, path)  # already on the server: no second upload
    else:
        path = os.path.join(storage.TEMP_DIR, f"{job_id}_{secure_filename(filename) or 'video' + ext}")
        video.save(path)
    if os.path.getsize(path) == 0:
        if not local_path:
            os.remove(path)
        return dict(error="That file is empty."), 400

    opts = _platform_opts(form)

    mode = form.get("when_mode", "now")
    if mode in ("at", "daily"):
        with _slot_lock:
            return _schedule_post(path, local_path, mode, form, filename, title, texts, selected, opts, fix,
                                  local_name)
    return _post_now(job_id, filename, title, selected, local_name, fix, path, texts, opts, local_path)


def _platform_opts(form):
    """Each platform's upload settings (privacy, feed, drafts...) from the upload form's fields."""
    return {
        "youtube": {
            "privacy": form.get("yt_privacy", "public"),
            "made_for_kids": form.get("yt_kids") == "1",
            "add_shorts_tag": form.get("yt_shorts_tag") == "1",
            "notify": form.get("yt_notify", "1") == "1",
            "tags": parse_tags(form.get("tags", "")),
        },
        "instagram": {"share_to_feed": form.get("ig_feed", "1") == "1",
                      "thumb_offset": int(float(form["ig_thumb"])) if form.get("ig_thumb") else None},
        "tiktok": {
            "mode": form.get("tt_mode", "draft"),
            "privacy": form.get("tt_privacy", "SELF_ONLY"),
            "disable_comment": form.get("tt_no_comments") == "1",
        },
    }


def _post_now(job_id, filename, title, selected, local_name, fix, path, texts, opts, local_path):
    job = _new_job(job_id, filename, title, selected)
    job["source_download"] = local_name or None
    if fix["enabled"]:
        job["prepare"] = {"state": "queued", "progress": 0, "message": "Checking video format"}
    threading.Thread(target=_run_job, args=(job, path, texts, opts, not local_path),
                     kwargs={"fix": fix if fix["enabled"] else None}, daemon=True).start()
    return dict(job_id=job_id), 200


def _schedule_post(path, local_path, mode, form, filename, title, texts, selected, opts, fix, local_name):
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
        return dict(error=str(e) or "Pick a valid time."), 400
    # Downloaded files are copied (you keep the original); fresh uploads are moved into the queue.
    item = scheduler.add(path, move=not local_path, filename=filename, title=title, texts=texts,
                         platforms=selected, opts=opts, when=when, fix=fix if fix["enabled"] else None,
                         source=local_name or None)
    return dict(scheduled=item), 200


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
    if job.get("source_download") and any(e["state"] == "done" for e in job["platforms"].values()):
        downloader.mark_posted(job["source_download"])


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


def _queue_source(item):
    """Which download a queued post came from (posts queued before 1.8.0 only have the file name)."""
    if "source_download" in item:
        return item["source_download"]
    return item.get("filename") if downloader.safe_path(item.get("filename", "")) else None


def _on_posted(item):
    name = _queue_source(item)
    if name:
        downloader.mark_posted(name)


def _downloads_with_status():
    """Each download plus where it stands: new, queued, posted or skipped."""
    queued = {}
    for item in scheduler.items():
        name = _queue_source(item)
        if name and item["status"] != "done":
            queued.setdefault(name, item)
    last_posted = {}  # file name -> when it last went out, from upload history
    for h in storage.load("history", default=[]):
        if any(r.get("state") == "done" for r in (h.get("results") or {}).values()):
            last_posted[h.get("file")] = max(last_posted.get(h.get("file"), 0), h.get("time") or 0)
    with _auto_lock:
        working = {i["local_file"]: i["state"] for i in _auto["items"] if i["local_file"] and i["state"] in AUTO_ACTIVE}
    files = downloader.list_files()
    for f in files:
        if f["name"] in working:
            f["auto"] = working[f["name"]]
        q = queued.get(f["name"])
        cleared = f.pop("cleared_at", None) or 0  # "Clear mark" forgets posts from before it
        posted = max(f.pop("posted_at", None) or 0, last_posted.get(f["name"], 0))
        if q:
            f["status"] = "queued"
            f["queue"] = {"when": q["when"], "state": q["status"]}
        elif f["mark"] == "posted" or posted > cleared:
            f["status"] = "posted"
        elif f["mark"] == "skip":
            f["status"] = "skipped"
        else:
            f["status"] = "new"
    return files


@app.get("/api/downloads")
def api_downloads():
    return jsonify(_downloads_with_status())


@app.post("/api/downloads/mark")
def api_downloads_mark():
    """Mark downloads by hand: "posted", "skip", or null to clear the mark."""
    body = request.get_json(force=True) or {}
    mark = body.get("mark")
    if mark not in (*downloader.MARKS, None):
        return jsonify(error="Unknown mark."), 400
    names = [n for n in body.get("names") or [] if isinstance(n, str)]
    for name in names[:1000]:
        downloader.set_mark(name, mark)
        if mark is None:
            downloader.update_meta(name, cleared_at=time.time())
    return jsonify(_downloads_with_status())


@app.post("/api/downloads/delete")
def api_downloads_delete():
    if not downloader.delete((request.get_json(force=True) or {}).get("name", "")):
        abort(404)
    return jsonify(_downloads_with_status())


@app.post("/api/downloads/open-folder")
def api_downloads_open():
    if SERVER:
        return jsonify(error="The downloads folder is on the server."), 400
    downloader.open_folder()
    return jsonify(ok=True)


@app.post("/api/downloader/cookies")
def api_downloader_cookies():
    """Upload a cookies.txt (lets downloads use your logins, e.g. for Instagram)."""
    f = request.files.get("cookies")
    if not f:
        return jsonify(error="Choose a cookies.txt file."), 400
    data = f.read(5 * 1024 * 1024)
    text = data.decode("utf-8", "replace")
    if "\t" not in text or not any(d in text for d in (".instagram.com", ".youtube.com", ".tiktok.com", "Netscape")):
        return jsonify(error="That doesn't look like a cookies.txt export."), 400
    with open(downloader.COOKIES_FILE, "wb") as out:
        out.write(data)
    return jsonify(downloader.status())


@app.post("/api/downloader/cookies/delete")
def api_downloader_cookies_delete():
    try:
        os.remove(downloader.COOKIES_FILE)
    except OSError:
        pass
    return jsonify(downloader.status())


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
        "daily_times": scheduler.settings()["daily_times"],
        "autostart": scheduler.autostart_enabled(),
        "autostart_supported": scheduler.autostart_supported(),
    })


@app.get("/api/queue")
def api_queue():
    return _queue_payload()


@app.post("/api/queue/platforms")
def api_queue_platforms():
    """Also post already-queued videos to more platforms. Body: {platforms: [...], base: [[field, value], ...]}
    (base = the Upload tab's settings, for each platform's options). Times, titles and captions stay the same."""
    body = request.get_json(force=True) or {}
    wanted = [p for p in body.get("platforms") or [] if p in PLATFORMS]
    if not wanted:
        return jsonify(error="Pick a platform to add."), 400
    missing = [PLATFORMS[p].NAME for p in wanted if not PLATFORMS[p].status().get("connected")]
    if missing:
        return jsonify(error=f"Connect {', '.join(missing)} in Settings first."), 400
    opts = _platform_opts(MultiDict([(str(k), str(v)) for k, v in body.get("base") or []]))
    changed = scheduler.add_platforms(wanted, {p: opts[p] for p in wanted})
    return jsonify({**_queue_payload().get_json(), "changed": changed})


@app.post("/api/queue/settings")
def api_queue_settings():
    body = request.get_json(force=True) or {}
    try:
        if "daily_times" in body:
            scheduler.set_daily_times(body["daily_times"] or [])
        elif "daily_time" in body:
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
    if any(not j["done"] for j in list(_jobs.values())) or _auto["workers"]:
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


def _changelog_for(version):
    """The CHANGELOG section for this version (falls back to the newest one)."""
    try:
        with open(os.path.join(storage.BASE_DIR, "CHANGELOG.md"), encoding="utf-8") as f:
            text = f.read()
    except OSError:
        return ""
    sections = ["## " + p.strip() for p in re.split(r"^## ", text, flags=re.M)[1:]]
    match = next((sec for sec in sections if sec.splitlines()[0].strip("# ").strip() == version), None)
    return match or (sections[0] if sections else "")


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
        "server": SERVER,
        "restarting": _app_state["restarting"],
        "just_updated": just if just == updater.local_version() else None,
        "whats_new": _changelog_for(updater.local_version()) if just else "",
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
    if SERVER:
        return jsonify(error="On the server, stop it from Dockge instead."), 400
    _exit_soon(0)
    return jsonify(ok=True)


# ---------- AI summaries ----------

@app.post("/api/stage")
def api_stage():
    """Send a video from the browser once; the token can then be summarized and posted."""
    video = request.files.get("video")
    if not video or not video.filename:
        return jsonify(error="Pick a video file first."), 400
    ext = os.path.splitext(video.filename)[1].lower()
    if ext not in ALLOWED_EXT:
        return jsonify(error=f"Unsupported file type {ext}. Use MP4, MOV or WebM."), 400
    token = secrets.token_hex(8)
    path = os.path.join(STAGING_DIR, f"{token}_{secure_filename(video.filename) or 'video' + ext}")
    video.save(path)
    return jsonify(token=token, name=video.filename, size=os.path.getsize(path))


@app.post("/api/summarize")
def api_summarize():
    body = request.get_json(force=True) or {}
    source = {}
    if body.get("local_file"):
        path = downloader.safe_path(body["local_file"])
        source = downloader.meta(body["local_file"])
    else:
        path = staged_path(body.get("staged", ""))
    if not path:
        return jsonify(error="That video isn't on the app any more. Add it again."), 400
    if not summarizer.settings()["key"]:
        return jsonify(error="Add a Gemini API key in Settings → AI summaries first (it's free)."), 400
    hint = str(body.get("hint", ""))[:300]
    return jsonify(job_id=summarizer.start(path, hint=hint, source=source))


@app.get("/api/summarize/<job_id>")
def api_summarize_job(job_id):
    job = summarizer.get(job_id)
    if not job:
        abort(404)
    return jsonify(job)


@app.get("/api/hook.png")
def api_hook_png():
    """The hook text exactly as it will be drawn on the video (for the preview on the Upload tab)."""
    a = request.args
    try:
        img = media.render_hook(a.get("text", ""), 540, 960, a.get("style", "outline"), a.get("pos", "top"))
    except media.MediaError as e:
        return jsonify(error=str(e)), 400
    import io
    buf = io.BytesIO()
    img.save(buf, "PNG")
    buf.seek(0)
    return send_file(buf, mimetype="image/png", max_age=3600)


# ---------- auto-queue: AI writes each title + description, then it's queued (runs in the background) ----------

AUTO_WORKERS = 2  # the summarizer only runs 2 at once anyway (free-tier rate limits)
AUTO_RETRIES = 3  # when Gemini is overloaded, a video goes back in line this many times
AUTO_RETRY_AFTER = 60  # seconds before it's tried again
AUTO_ACTIVE = ("waiting", "ai", "queueing")
_FORM_ONLY = {"local_file", "staged", "video", "title", "description", "tags", "creator", "link",
              "when_mode", "when_ts", "ig_thumb", "hook_text"}
_auto = {"items": [], "workers": 0, "stop": False}
_auto_lock = threading.Lock()


def title_from_name(name):
    """'Video by someone [AbC123].mp4' -> 'Video by someone' (same as the page does)."""
    t = re.sub(r"\s*\[[^\]]+\]\.[^.]+$", "", name or "")
    t = re.sub(r"\.[^.]+$", "", t)
    return re.sub(r"\s+", " ", t.replace("_", " ")).strip()[:100]


def _auto_status():
    with _auto_lock:
        items = [{k: it.get(k) for k in ("key", "name", "local_file", "state", "message", "title", "when")}
                 for it in _auto["items"]]
        running = _auto["workers"] > 0
    count = lambda *st: sum(1 for i in items if i["state"] in st)
    return {"running": running, "items": items, "total": len(items), "queued": count("queued"),
            "failed": count("error"), "left": count(*AUTO_ACTIVE), "stopped": count("stopped")}


def _auto_one(it):
    path = downloader.safe_path(it["local_file"]) if it["local_file"] else staged_path(it["staged"])
    if not path:
        raise ValueError("That video isn't there any more.")
    ai = it.get("ai")
    if not ai:
        source = downloader.meta(it["local_file"]) if it["local_file"] else {}
        hint = f"Originally by {it['creator']}" if it["creator"] else ""
        r = summarizer.summarize(path, hint=hint, source=source, report=lambda m: it.update(message=m))
        ai = {"title": (r["titles"] or [""])[0], "description": r["description"], "hook": r.get("hook", ""),
              "tags": r["tags"]["post"] + r["tags"]["niche"] + r["tags"]["broad"]}
    shared = it["shared"]
    title = (it["title"] or ai.get("title") or title_from_name(it["name"])).strip()[:100]
    desc = "\n\n".join(x for x in [ai.get("description", "").strip() if shared["ai_desc"] else "",
                                    shared["description"].strip()] if x)
    tags = parse_tags(", ".join([it["tags"], ", ".join(ai.get("tags") or []), shared["tags"]]))
    fields = list(it["base"]) + [("title", title), ("description", desc), ("tags", ", ".join(tags)),
                                 ("link", it["link"]), ("when_mode", "daily")]
    if it["creator"]:
        fields.append(("creator", it["creator"]))
    hook = it["hook"] if it["hook_locked"] else (ai.get("hook", "") if shared["ai_hook"] else "")
    if hook:
        fields.append(("hook_text", hook))
    fields.append(("local_file", it["local_file"]) if it["local_file"] else ("staged", it["staged"]))
    it.update(state="queueing", message="Adding to the queue", title=title)
    body, code = _submit_post(MultiDict(fields))
    if code != 200:
        raise ValueError(body.get("error") or "Couldn't add it to the queue.")
    it.update(state="queued", message="Queued", when=body["scheduled"]["when"])


def _auto_worker():
    while True:
        with _auto_lock:
            if _auto["stop"]:
                for i in _auto["items"]:
                    if i["state"] == "waiting":
                        i.update(state="stopped", message="Stopped")
            waiting = [i for i in _auto["items"] if i["state"] == "waiting"]
            it = next((i for i in waiting if i.get("retry_at", 0) <= time.time()), None)
            if not waiting:
                _auto["workers"] -= 1
                return
            if it:
                it.update(state="ai", message="Starting")
        if not it:  # only ones waiting to retry later are left
            time.sleep(2)
            continue
        try:
            _auto_one(it)
        except summarizer.SummaryError as e:
            it["tries"] = it.get("tries", 0) + 1
            if e.temporary and it["tries"] <= AUTO_RETRIES and not _auto["stop"]:
                it.update(state="waiting", retry_at=time.time() + AUTO_RETRY_AFTER * it["tries"],
                          message=f"Gemini is busy, will try again in {AUTO_RETRY_AFTER * it['tries'] // 60} min")
            else:
                it.update(state="error", message=str(e))
        except ValueError as e:
            it.update(state="error", message=str(e))
        except Exception as e:  # keep going with the rest
            it.update(state="error", message=f"Unexpected error: {type(e).__name__}: {e}")


def _auto_kick():
    """Start workers for waiting items (call with _auto_lock held)."""
    _auto["stop"] = False
    while _auto["workers"] < AUTO_WORKERS:
        _auto["workers"] += 1
        threading.Thread(target=_auto_worker, daemon=True).start()


@app.get("/api/autoqueue")
def api_autoqueue_status():
    return jsonify(_auto_status())


@app.post("/api/autoqueue")
def api_autoqueue():
    """Queue many videos: the AI writes each one's title, description and tags, then it's added to the queue.
    Body: {items: [{local_file | staged, name, creator, link, tags, title?, title_locked?, ai?}],
           base: [[field, value], ...] (the Upload tab's settings), description, tags, ai_description}"""
    body = request.get_json(force=True) or {}
    base = [(str(k), str(v)) for k, v in (body.get("base") or []) if str(k) not in _FORM_ONLY]
    if not any(k == "platforms" and v in PLATFORMS for k, v in base):
        return jsonify(error="Turn on at least one platform on the Upload tab first."), 400
    shared = {"description": str(body.get("description") or ""), "tags": str(body.get("tags") or ""),
              "ai_desc": bool(body.get("ai_description", True)), "ai_hook": bool(body.get("ai_hook", False))}
    new = []
    for raw in (body.get("items") or [])[:500]:
        local, staged = str(raw.get("local_file") or ""), str(raw.get("staged") or "")
        if not local and not staged:
            continue
        ai = raw.get("ai") if isinstance(raw.get("ai"), dict) else None
        if not ai and not summarizer.settings()["key"]:
            return jsonify(error="Add a Gemini API key in Settings → AI summaries first (it's free)."), 400
        new.append({
            "key": ("dl:" + local) if local else ("st:" + staged), "local_file": local, "staged": staged,
            "name": str(raw.get("name") or local or "video")[:200], "creator": str(raw.get("creator") or "")[:100],
            "link": str(raw.get("link") or "")[:500], "tags": str(raw.get("tags") or "")[:1000],
            "title": str(raw.get("title") or "")[:100] if raw.get("title_locked") else "", "ai": ai,
            "hook": str(raw.get("hook") or "")[:120], "hook_locked": bool(raw.get("hook_locked")),
            "base": base, "shared": shared, "state": "waiting", "message": "Waiting", "when": None,
        })
    if not new:
        return jsonify(error="Pick some videos first."), 400
    with _auto_lock:
        if not _auto["workers"]:  # a fresh run: forget the last one's finished list
            _auto["items"] = [i for i in _auto["items"] if i["state"] in AUTO_ACTIVE]
        busy = {i["key"] for i in _auto["items"] if i["state"] in AUTO_ACTIVE}
        _auto["items"] = [i for i in _auto["items"] if i["key"] not in {n["key"] for n in new} or i["key"] in busy]
        _auto["items"] += [n for n in new if n["key"] not in busy]
        _auto_kick()
    return jsonify(_auto_status())


@app.post("/api/autoqueue/stop")
def api_autoqueue_stop():
    """Don't start any more (ones the AI is already on still finish)."""
    with _auto_lock:
        _auto["stop"] = True
        for i in _auto["items"]:  # (also ones waiting to retry later)
            if i["state"] == "waiting":
                i.update(state="stopped", message="Stopped")
    return jsonify(_auto_status())


@app.post("/api/autoqueue/retry")
def api_autoqueue_retry():
    """Try the failed and stopped ones again."""
    with _auto_lock:
        for i in _auto["items"]:
            if i["state"] in ("error", "stopped"):
                i.update(state="waiting", message="Waiting", retry_at=0, tries=0)
        if any(i["state"] == "waiting" for i in _auto["items"]):
            _auto_kick()
    return jsonify(_auto_status())


@app.post("/api/autoqueue/clear")
def api_autoqueue_clear():
    """Hide the finished list."""
    with _auto_lock:
        _auto["items"] = [i for i in _auto["items"] if i["state"] in AUTO_ACTIVE]
    return jsonify(_auto_status())


@app.post("/api/ai/settings")
def api_ai_settings():
    body = request.get_json(force=True) or {}
    if body.get("clear_key"):
        summarizer.clear_key()
    summarizer.save_settings(key=body.get("key"), model=body.get("model"), niche=body.get("niche"),
                             language=body.get("language"))
    result = summarizer.public_settings()
    if body.get("test") or body.get("key"):
        try:
            result["resolved_model"] = summarizer.test_key()
        except summarizer.SummaryError as e:
            return jsonify(error=str(e), **result), 400
    return jsonify(result)


# ---------- export / import (move to another computer or the home server) ----------

@app.post("/api/transfer/export")
def api_transfer_export():
    import transfer
    body = request.get_json(silent=True) or {}
    for name in os.listdir(storage.TEMP_DIR):  # tidy up any earlier exports that weren't removed
        old = os.path.join(storage.TEMP_DIR, name)
        if name.startswith("export_") and time.time() - os.path.getmtime(old) > 600:
            try:
                os.remove(old)
            except OSError:
                pass
    path, count = transfer.export(include_queue=bool(body.get("queue", True)),
                                  move_queue=bool(body.get("move", False)))
    def stream():  # send the file, then delete it (also if the download is cancelled)
        try:
            with open(path, "rb") as f:
                while True:
                    chunk = f.read(1024 * 1024)
                    if not chunk:
                        break
                    yield chunk
        finally:
            try:
                os.remove(path)
            except OSError:
                pass

    name = f"shorts-uploader-backup-{time.strftime('%Y-%m-%d')}.zip"
    return Response(stream(), mimetype="application/zip", headers={
        "Content-Disposition": f'attachment; filename="{name}"',
        "Content-Length": str(os.path.getsize(path)),
        "X-Queue-Count": str(count),
    })


@app.post("/api/transfer/import")
def api_transfer_import():
    import transfer
    f = request.files.get("backup")
    if not f:
        return jsonify(error="Choose the backup .zip file."), 400
    tmp = os.path.join(storage.TEMP_DIR, f"import_{uuid.uuid4().hex[:8]}.zip")
    f.save(tmp)
    try:
        with open(tmp, "rb") as fh:
            summary = transfer.import_zip(fh)
    except transfer.TransferError as e:
        return jsonify(error=str(e)), 400
    except (ValueError, KeyError) as e:
        return jsonify(error=f"That backup couldn't be read ({e})."), 400
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass
    return jsonify(summary)


def _already_running():
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        return sock.connect_ex((HOST, PORT)) == 0


def _cleanup_leftovers():
    for name in os.listdir(STAGING_DIR):  # staged videos nobody posted within a day
        p = os.path.join(STAGING_DIR, name)
        if time.time() - os.path.getmtime(p) > 86400:
            try:
                os.remove(p)
            except OSError:
                pass
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
    scheduler.on_posted = _on_posted
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
    if SERVER:
        print(f"\n  Shorts Uploader (server mode) listening on port {PORT}. Open http://<this-server>:{PORT}"
              f"{f' — {queued} scheduled post(s) waiting' if queued else ''}\n", flush=True)
        if PASSWORD in PLACEHOLDER_PASSWORDS:
            print("  !! Set SHORTS_UPLOADER_PASSWORD in the compose file before using it. !!\n", flush=True)
    else:
        print(f"\n  Shorts Uploader running at {BASE_URL}\n  Close this window to stop it"
              f"{f' (scheduled posts only go out while it runs; {queued} waiting)' if queued else ''}.\n")
    app.run(host=HOST, port=PORT, debug=False, threaded=True)
