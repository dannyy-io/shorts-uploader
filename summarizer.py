"""AI video summaries with Google Gemini: a summary, title ideas, a description and tags.

Tags follow a three-part formula:
  post-specific  - what this exact video is
  niche          - the channel's niche (from Settings, plus the AI's best guess)
  broad          - general reach tags (shorts, viral, ...)

The video is shrunk to a small low-res copy first (the AI only needs to see and hear it),
then sent inline, or through Gemini's Files API if it's still big. Results are cached per
file so re-running bulk summaries is free.
"""
import base64
import hashlib
import json
import os
import re
import subprocess
import threading
import time
import uuid

import requests

import storage

API = os.environ.get("SHORTS_UPLOADER_GEMINI_BASE", "https://generativelanguage.googleapis.com")  # override = tests only
INLINE_LIMIT = 14 * 1024 * 1024  # keep inline requests well under Gemini's 20 MB request cap
MAX_SECONDS = 180
CACHE_FILE = "summaries"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
SKIP_WORDS = ("image", "tts", "live", "audio", "embedding", "computer", "thinking-exp", "learnlm", "veo", "imagen")

_jobs = {}
_model_cache = {"key": None, "model": None, "at": 0}
_sem = threading.Semaphore(2)  # at most 2 summaries at once (free-tier rate limits)


class SummaryError(Exception):
    pass


# ---------- settings ----------

def settings():
    cfg = storage.get_section("config", "ai")
    return {
        "key": cfg.get("gemini_key", ""),
        "model": cfg.get("model", ""),          # empty = pick automatically
        "niche": cfg.get("niche", ""),          # e.g. "anime edits, AMV, demon slayer"
        "language": cfg.get("language", ""),    # empty = English
    }


def public_settings():
    s = settings()
    return {"has_key": bool(s["key"]), "model": s["model"], "niche": s["niche"], "language": s["language"],
            "resolved_model": _model_cache["model"] if _model_cache["key"] == s["key"] else None}


def save_settings(key=None, model=None, niche=None, language=None):
    cur = storage.get_section("config", "ai")
    if key is not None and key.strip():
        cur["gemini_key"] = key.strip()
    if model is not None:
        cur["model"] = model.strip()
    if niche is not None:
        cur["niche"] = niche.strip()[:200]
    if language is not None:
        cur["language"] = language.strip()[:40]
    storage.set_section("config", "ai", cur)
    _model_cache.update(key=None, model=None, at=0)


def clear_key():
    cur = storage.get_section("config", "ai")
    cur.pop("gemini_key", None)
    storage.set_section("config", "ai", cur)


# ---------- talking to Gemini ----------

def _headers(key, extra=None):
    return {"x-goog-api-key": key, **(extra or {})}


def _err(resp):
    try:
        body = resp.json()
        msg = (body.get("error") or {}).get("message") or resp.text[:200]
    except ValueError:
        msg = resp.text[:200]
    if resp.status_code in (400, 403) and ("API key" in msg or "API_KEY" in msg or "permission" in msg.lower()):
        return "Gemini didn't accept the API key. Check it in Settings → AI summaries."
    if resp.status_code == 429:
        return "Gemini's free-tier limit was hit. Wait a minute and try again (bulk runs slow down automatically)."
    if resp.status_code >= 500:
        return "Gemini is having trouble right now. Try again in a bit."
    return f"Gemini error ({resp.status_code}): {msg}"


def _version(name):
    m = re.search(r"gemini-(\d+(?:\.\d+)?)", name)
    return float(m.group(1)) if m else 0.0


def pick_model(key, force=False):
    """Choose the newest Flash model this key can use for generateContent (video capable)."""
    wanted = settings()["model"]
    if wanted:
        return wanted
    if not force and _model_cache["key"] == key and time.time() - _model_cache["at"] < 86400:
        return _model_cache["model"]
    models, token = [], None
    for _ in range(5):
        r = requests.get(f"{API}/v1beta/models", params={"pageSize": 200, **({"pageToken": token} if token else {})},
                         headers=_headers(key), timeout=20)
        if not r.ok:
            raise SummaryError(_err(r))
        body = r.json()
        models += body.get("models", [])
        token = body.get("nextPageToken")
        if not token:
            break
    candidates = []
    for m in models:
        name = m.get("name", "").split("/")[-1]
        if "generateContent" not in m.get("supportedGenerationMethods", []):
            continue
        if "flash" not in name or any(w in name for w in SKIP_WORDS):
            continue
        lite = "lite" in name
        preview = "preview" in name or "exp" in name
        dated = bool(re.search(r"-\d{2}-\d{4}$|-\d{3}$", name))  # pinned snapshots: prefer the plain alias
        candidates.append(((not lite), _version(name), (not preview), (not dated), name))
    if not candidates:
        raise SummaryError("No Gemini Flash model is available for this key.")
    best = max(candidates)[-1]
    _model_cache.update(key=key, model=best, at=time.time())
    return best


def _ffmpeg():
    import media
    exe = media.ffmpeg_exe()
    if not exe:
        raise SummaryError("ffmpeg isn't installed yet (restart the app to install it).")
    return exe


def make_proxy(src):
    """A small low-res copy (≈1–3 MB for a Short) that's quick to send."""
    out = os.path.join(storage.TEMP_DIR, f"ai_{uuid.uuid4().hex[:8]}.mp4")
    vf = "scale='if(gt(iw,ih),640,-2)':'if(gt(iw,ih),-2,640)',fps=2"
    args = [_ffmpeg(), "-hide_banner", "-loglevel", "error", "-y", "-i", src, "-t", str(MAX_SECONDS),
            "-vf", vf, "-c:v", "libx264", "-preset", "veryfast", "-crf", "32", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "48k", "-ac", "1", "-movflags", "+faststart", out]
    res = subprocess.run(args, capture_output=True, text=True, errors="replace", creationflags=NO_WINDOW)
    if res.returncode != 0 or not os.path.exists(out):
        raise SummaryError("Couldn't read that video: " + (res.stderr.strip().splitlines() or ["unknown error"])[-1][:160])
    return out


def _upload_file(key, path):
    """Gemini Files API (for videos too big to send inline). Returns (file_name, file_uri)."""
    size = os.path.getsize(path)
    start = requests.post(f"{API}/upload/v1beta/files", timeout=30, headers=_headers(key, {
        "X-Goog-Upload-Protocol": "resumable", "X-Goog-Upload-Command": "start",
        "X-Goog-Upload-Header-Content-Length": str(size), "X-Goog-Upload-Header-Content-Type": "video/mp4",
        "Content-Type": "application/json"}), json={"file": {"display_name": "shorts-uploader-clip"}})
    if not start.ok:
        raise SummaryError(_err(start))
    url = start.headers.get("x-goog-upload-url") or start.headers.get("X-Goog-Upload-URL")
    with open(path, "rb") as f:
        up = requests.post(url, data=f, timeout=300, headers={
            "Content-Length": str(size), "X-Goog-Upload-Offset": "0", "X-Goog-Upload-Command": "upload, finalize"})
    if not up.ok:
        raise SummaryError(_err(up))
    info = up.json().get("file", {})
    name, uri = info.get("name"), info.get("uri")
    deadline = time.time() + 180
    while info.get("state") not in (None, "ACTIVE"):
        if info.get("state") == "FAILED":
            raise SummaryError("Gemini couldn't process that video.")
        if time.time() > deadline:
            raise SummaryError("Gemini took too long to process the video.")
        time.sleep(2)
        g = requests.get(f"{API}/v1beta/{name}", headers=_headers(key), timeout=20)
        if not g.ok:
            raise SummaryError(_err(g))
        info = g.json()
    return name, uri


def _delete_file(key, name):
    try:
        requests.delete(f"{API}/v1beta/{name}", headers=_headers(key), timeout=15)
    except requests.RequestException:
        pass


SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "summary": {"type": "STRING"},
        "titles": {"type": "ARRAY", "items": {"type": "STRING"}},
        "description": {"type": "STRING"},
        "tags_post": {"type": "ARRAY", "items": {"type": "STRING"}},
        "tags_niche": {"type": "ARRAY", "items": {"type": "STRING"}},
        "tags_broad": {"type": "ARRAY", "items": {"type": "STRING"}},
    },
    "required": ["summary", "titles", "description", "tags_post", "tags_niche", "tags_broad"],
}


def _prompt(hint, source, niche, language):
    lines = [
        "You're helping a creator post this short vertical video to YouTube Shorts, Instagram Reels and TikTok.",
        "Watch and listen to it, then return JSON with:",
        "- summary: 2-3 plain sentences describing what actually happens in the video.",
        "- titles: 3 short, catchy title options (max 70 characters each, no hashtags, no ALL CAPS, "
        "nothing the video doesn't deliver).",
        "- description: a 1-2 sentence caption for the post (no hashtags).",
        "- tags_post: 4-6 post-specific tags that say exactly what this video is "
        "(e.g. for a bicep workout: bicep workout, bicep curl, best bicep exercise).",
        "- tags_niche: 3-5 tags for the channel's niche/category.",
        "- tags_broad: 3-4 broad reach tags such as shorts, viral, fyp.",
        "Tags are plain lowercase words or short phrases without '#'.",
        "Only name people, characters, songs, games or shows if you're confident; otherwise describe them generally.",
    ]
    if niche:
        lines.append(f"The channel's niche is: {niche}. Use it for tags_niche.")
    if hint:
        lines.append(f"The creator says about this video: {hint}")
    if source.get("source_title") or source.get("creator"):
        lines.append(f"It was originally posted by {source.get('creator') or 'someone else'} with the title: "
                     f"{source.get('source_title') or '(unknown)'}. Use that only as a hint.")
    lines.append(f"Write everything in {language or 'English'}.")
    return "\n".join(lines)


def _clean_tags(items, limit):
    out, seen = [], set()
    for t in items or []:
        t = re.sub(r"[#<>\"]", "", str(t)).strip().lower()
        t = re.sub(r"\s+", " ", t)[:60]
        if t and t not in seen:
            seen.add(t)
            out.append(t)
        if len(out) >= limit:
            break
    return out


def _parse(text):
    try:
        return json.loads(text)
    except ValueError:
        m = re.search(r"\{.*\}", text or "", re.S)
        if m:
            try:
                return json.loads(m.group(0))
            except ValueError:
                pass
    raise SummaryError("Gemini's answer couldn't be read. Try again.")


def _cache_key(path):
    st = os.stat(path)
    return hashlib.sha256(f"{os.path.basename(path)}|{st.st_size}|{int(st.st_mtime)}".encode()).hexdigest()[:24]


def summarize(path, hint="", source=None, report=lambda msg: None, use_cache=True):
    s = settings()
    if not s["key"]:
        raise SummaryError("Add a Gemini API key in Settings → AI summaries first (it's free).")
    source = source or {}
    ck = _cache_key(path) + hashlib.sha256(f"{hint}|{s['niche']}|{s['language']}".encode()).hexdigest()[:8]
    cache = storage.load(CACHE_FILE)
    if use_cache and ck in cache:
        return cache[ck]

    with _sem:
        report("Preparing video")
        proxy = make_proxy(path)
        uploaded = None
        try:
            model = pick_model(s["key"])
            if os.path.getsize(proxy) <= INLINE_LIMIT:
                with open(proxy, "rb") as f:
                    video_part = {"inlineData": {"mimeType": "video/mp4", "data": base64.b64encode(f.read()).decode()}}
            else:
                report("Uploading to Gemini")
                uploaded, uri = _upload_file(s["key"], proxy)
                video_part = {"fileData": {"mimeType": "video/mp4", "fileUri": uri}}
            report("Watching the video")
            body = {
                "contents": [{"role": "user", "parts": [video_part,
                                                        {"text": _prompt(hint, source, s["niche"], s["language"])}]}],
                "generationConfig": {"responseMimeType": "application/json", "responseSchema": SCHEMA,
                                     "temperature": 0.7},
            }
            for attempt in range(3):
                r = requests.post(f"{API}/v1beta/models/{model}:generateContent", json=body,
                                  headers=_headers(s["key"]), timeout=180)
                if r.status_code == 429 and attempt < 2:
                    report("Rate-limited by Gemini, waiting a moment")
                    time.sleep(20 * (attempt + 1))
                    continue
                if r.status_code == 404 and not s["model"] and attempt < 2:
                    model = pick_model(s["key"], force=True)  # model retired since we cached it
                    continue
                break
            if not r.ok:
                raise SummaryError(_err(r))
            data = r.json()
            cand = (data.get("candidates") or [{}])[0]
            text = "".join(p.get("text", "") for p in (cand.get("content") or {}).get("parts", []))
            if not text:
                reason = cand.get("finishReason") or (data.get("promptFeedback") or {}).get("blockReason") or "empty answer"
                raise SummaryError(f"Gemini didn't describe this video ({reason}).")
            raw = _parse(text)
        except requests.RequestException as e:
            raise SummaryError(f"Couldn't reach Gemini ({type(e).__name__}).")
        finally:
            if uploaded:
                _delete_file(s["key"], uploaded)
            try:
                os.remove(proxy)
            except OSError:
                pass

    niche_fixed = [t.strip() for t in s["niche"].split(",") if t.strip()]
    result = {
        "summary": str(raw.get("summary", "")).strip(),
        "titles": [str(t).strip().strip('"')[:100] for t in raw.get("titles", []) if str(t).strip()][:3],
        "description": str(raw.get("description", "")).strip(),
        "tags": {
            "post": _clean_tags(raw.get("tags_post"), 6),
            "niche": _clean_tags(niche_fixed + list(raw.get("tags_niche") or []), 5),
            "broad": _clean_tags(raw.get("tags_broad"), 4),
        },
        "model": model,
    }
    if not result["titles"] and not result["summary"]:
        raise SummaryError("Gemini's answer was empty. Try again.")
    cache[ck] = result
    if len(cache) > 300:  # keep the cache small
        for k in list(cache)[:-300]:
            cache.pop(k, None)
    storage.save(CACHE_FILE, cache)
    return result


# ---------- background jobs ----------

def start(path, hint="", source=None, cleanup=False):
    job = {"id": uuid.uuid4().hex[:12], "state": "running", "message": "Starting", "result": None}
    _jobs[job["id"]] = job

    def run():
        try:
            job["result"] = summarize(path, hint, source, report=lambda m: job.update(message=m))
            job.update(state="done", message="Done")
        except SummaryError as e:
            job.update(state="error", message=str(e))
        except Exception as e:
            job.update(state="error", message=f"Unexpected error: {type(e).__name__}: {e}")
        finally:
            if cleanup:
                try:
                    os.remove(path)
                except OSError:
                    pass
    threading.Thread(target=run, daemon=True).start()
    return job["id"]


def get(job_id):
    return _jobs.get(job_id)


def test_key(key=None):
    key = key or settings()["key"]
    if not key:
        raise SummaryError("Paste a Gemini API key first.")
    return pick_model(key, force=True)
