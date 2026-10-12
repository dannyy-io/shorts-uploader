"""YouTube Shorts via the YouTube Data API v3 (resumable upload).

A Short is just a normal upload that is vertical (or square) and 3 minutes or
less; YouTube classifies it automatically.
"""
import os
import time

import requests

import storage
from .common import UploadError, error_text, mime_for, s256_base64url

NAME = "YouTube"
USES_OAUTH = True

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
UPLOAD_URL = "https://www.googleapis.com/upload/youtube/v3/videos"
CHANNEL_URL = "https://www.googleapis.com/youtube/v3/channels"
SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.readonly",  # only to show your channel name
]
CHUNK = 8 * 1024 * 1024  # must be a multiple of 256 KiB
TITLE_LIMIT = 100
DESCRIPTION_LIMIT = 5000


def _client():
    cfg = storage.get_section("config", "youtube")
    return cfg.get("client_id", "").strip(), cfg.get("client_secret", "").strip()


def status():
    cid, secret = _client()
    tok = storage.get_section("tokens", "youtube")
    return {
        "configured": bool(cid and secret),
        "connected": bool(tok.get("refresh_token")),
        "account": tok.get("account"),
    }


def auth_url(redirect_uri, state, verifier):
    cid, _ = _client()
    params = {
        "client_id": cid,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(SCOPES),
        "access_type": "offline",
        "prompt": "consent",
        "state": state,
        "code_challenge": s256_base64url(verifier),
        "code_challenge_method": "S256",
    }
    return requests.Request("GET", AUTH_URL, params=params).prepare().url


def finish_auth(code, redirect_uri, verifier):
    cid, secret = _client()
    r = requests.post(TOKEN_URL, data={
        "code": code,
        "client_id": cid,
        "client_secret": secret,
        "redirect_uri": redirect_uri,
        "grant_type": "authorization_code",
        "code_verifier": verifier,
    }, timeout=30)
    if not r.ok:
        raise UploadError(f"Google sign-in failed: {error_text(r)}")
    t = r.json()
    if not t.get("refresh_token"):
        raise UploadError("Google didn't return a refresh token. Remove the app's access at "
                          "myaccount.google.com/permissions and connect again.")
    storage.set_section("tokens", "youtube", {
        "access_token": t["access_token"],
        "refresh_token": t["refresh_token"],
        "expires_at": time.time() + int(t.get("expires_in", 3600)),
    })
    try:
        ch = requests.get(CHANNEL_URL, params={"part": "snippet", "mine": "true"},
                          headers={"Authorization": f"Bearer {t['access_token']}"}, timeout=20)
        items = ch.json().get("items", []) if ch.ok else []
        if items:
            storage.update_section("tokens", "youtube", account=items[0]["snippet"]["title"])
    except requests.RequestException:
        pass


def disconnect():
    storage.clear_section("tokens", "youtube")


def _access_token(force=False):
    tok = storage.get_section("tokens", "youtube")
    if not tok.get("refresh_token"):
        raise UploadError("YouTube isn't connected. Open Settings and click Connect.")
    if not force and tok.get("expires_at", 0) > time.time() + 120:
        return tok["access_token"]
    cid, secret = _client()
    r = requests.post(TOKEN_URL, data={
        "client_id": cid,
        "client_secret": secret,
        "refresh_token": tok["refresh_token"],
        "grant_type": "refresh_token",
    }, timeout=30)
    if not r.ok:
        if "invalid_grant" in r.text:
            raise UploadError("YouTube sign-in expired. Reconnect in Settings. (If this happens "
                              "every week, set your Google OAuth app to 'In production' — see README.)")
        raise UploadError(f"Couldn't refresh YouTube sign-in: {error_text(r)}")
    t = r.json()
    storage.update_section("tokens", "youtube", access_token=t["access_token"],
                           expires_at=time.time() + int(t.get("expires_in", 3600)))
    return t["access_token"]


def upload(path, title, description, opts, report):
    title = (title or "").strip()
    if not title:
        raise UploadError("YouTube needs a title.")
    if len(title) > TITLE_LIMIT:
        raise UploadError(f"YouTube titles max out at {TITLE_LIMIT} characters.")
    if opts.get("add_shorts_tag") and "#shorts" not in (description or "").lower():
        description = ((description or "").rstrip() + "\n\n#Shorts").strip()
    if len(description or "") > DESCRIPTION_LIMIT:
        raise UploadError(f"YouTube descriptions max out at {DESCRIPTION_LIMIT} characters.")
    for ch in "<>":
        if ch in title or ch in (description or ""):
            raise UploadError("YouTube doesn't allow < or > in titles or descriptions.")

    size = os.path.getsize(path)
    mime = mime_for(path)
    body = {
        "snippet": {
            "title": title,
            "description": description or "",
            "categoryId": str(opts.get("category_id") or "22"),
        },
        "status": {
            "privacyStatus": opts.get("privacy", "public"),
            "selfDeclaredMadeForKids": bool(opts.get("made_for_kids")),
        },
    }

    report(0, "Starting upload")
    token = _access_token()
    init = requests.post(
        UPLOAD_URL,
        params={"uploadType": "resumable", "part": "snippet,status",
                "notifySubscribers": "true" if opts.get("notify", True) else "false"},
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=UTF-8",
            "X-Upload-Content-Type": mime,
            "X-Upload-Content-Length": str(size),
        },
        json=body,
        timeout=60,
    )
    if not init.ok:
        raise UploadError(error_text(init))
    session_url = init.headers["Location"]

    offset, failures = 0, 0
    with open(path, "rb") as f:
        while True:
            f.seek(offset)
            chunk = f.read(CHUNK)
            end = offset + len(chunk) - 1
            resp = None
            try:
                resp = requests.put(session_url, data=chunk, timeout=(30, 300), headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Length": str(len(chunk)),
                    "Content-Range": f"bytes {offset}-{end}/{size}",
                })
            except requests.RequestException:
                pass

            if resp is not None and resp.status_code in (200, 201):
                video = resp.json()
                vid = video["id"]
                report(100, "Uploaded")
                return {
                    "url": f"https://youtube.com/shorts/{vid}",
                    "message": f"Uploaded ({video.get('status', {}).get('privacyStatus', 'unknown')}). "
                               "YouTube may still be processing it.",
                }
            if resp is not None and resp.status_code == 308:
                offset = _next_offset(resp)
                failures = 0
                report(100 * offset / size, "Uploading")
                continue
            if resp is not None and resp.status_code == 401:
                token = _access_token(force=True)
                continue
            if resp is None or resp.status_code in (429, 500, 502, 503, 504):
                failures += 1
                if failures > 6:
                    raise UploadError("YouTube upload kept failing; check your connection and try again.")
                report(100 * offset / size, f"Connection hiccup, retrying ({failures}/6)")
                time.sleep(min(2 ** failures, 60))
                offset = _query_offset(session_url, token, size, offset)
                continue
            raise UploadError(error_text(resp))


def _next_offset(resp):
    rng = resp.headers.get("Range")  # e.g. "bytes=0-8388607"
    return int(rng.rsplit("-", 1)[1]) + 1 if rng else 0


def _query_offset(session_url, token, size, fallback):
    try:
        r = requests.put(session_url, timeout=30, headers={
            "Authorization": f"Bearer {token}",
            "Content-Length": "0",
            "Content-Range": f"bytes */{size}",
        })
        if r.status_code == 308:
            return _next_offset(r)
    except requests.RequestException:
        pass
    return fallback
