"""TikTok via the Content Posting API (Login Kit for Desktop + PKCE).

Two modes:
  * "draft"  - video lands in your TikTok inbox; you tap the notification in the
               TikTok app and hit Post. Works publicly without TikTok's app audit.
  * "direct" - posts straight to your profile. Until your developer app passes
               TikTok's audit, these posts are forced to "Only me" and your
               account must be set to private.
"""
import math
import os
import time

import requests

import storage
from .common import UploadError, build_caption, error_text, mime_for, s256_hex

NAME = "TikTok"
USES_OAUTH = True

AUTH_URL = "https://www.tiktok.com/v2/auth/authorize/"
TOKEN_URL = "https://open.tiktokapis.com/v2/oauth/token/"
API = "https://open.tiktokapis.com/v2"
SCOPES = "user.info.basic,video.upload,video.publish"
CAPTION_LIMIT = 2200
SINGLE_CHUNK_MAX = 64 * 1000 * 1000  # TikTok allows one chunk up to 64 MB
CHUNK = 10 * 1000 * 1000


def _client():
    cfg = storage.get_section("config", "tiktok")
    return cfg.get("client_key", "").strip(), cfg.get("client_secret", "").strip()


def status():
    key, secret = _client()
    tok = storage.get_section("tokens", "tiktok")
    return {
        "configured": bool(key and secret),
        "connected": bool(tok.get("refresh_token")) and tok.get("refresh_expires_at", 0) > time.time(),
        "account": tok.get("account"),
    }


def auth_url(redirect_uri, state, verifier):
    key, _ = _client()
    params = {
        "client_key": key,
        "response_type": "code",
        "scope": SCOPES,
        "redirect_uri": redirect_uri,
        "state": state,
        "code_challenge": s256_hex(verifier),  # TikTok desktop wants HEX, not base64url
        "code_challenge_method": "S256",
    }
    return requests.Request("GET", AUTH_URL, params=params).prepare().url


def _save_tokens(t):
    now = time.time()
    storage.update_section(
        "tokens", "tiktok",
        access_token=t["access_token"],
        refresh_token=t["refresh_token"],
        open_id=t.get("open_id"),
        expires_at=now + int(t.get("expires_in", 86400)),
        refresh_expires_at=now + int(t.get("refresh_expires_in", 31536000)),
    )


def _token_request(data):
    key, secret = _client()
    r = requests.post(TOKEN_URL, data={"client_key": key, "client_secret": secret, **data},
                      headers={"Content-Type": "application/x-www-form-urlencoded"}, timeout=30)
    body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    if not r.ok or "access_token" not in body:
        raise UploadError(f"TikTok sign-in failed: {body.get('error_description') or error_text(r)}")
    return body


def finish_auth(code, redirect_uri, verifier):
    t = _token_request({
        "code": code,
        "grant_type": "authorization_code",
        "redirect_uri": redirect_uri,
        "code_verifier": verifier,
    })
    storage.clear_section("tokens", "tiktok")
    _save_tokens(t)
    try:
        r = requests.get(f"{API}/user/info/", params={"fields": "display_name"},
                         headers={"Authorization": f"Bearer {t['access_token']}"}, timeout=20)
        name = r.json().get("data", {}).get("user", {}).get("display_name")
        if name:
            storage.update_section("tokens", "tiktok", account=name)
    except (requests.RequestException, ValueError):
        pass


def disconnect():
    storage.clear_section("tokens", "tiktok")


def _access_token():
    tok = storage.get_section("tokens", "tiktok")
    if not tok.get("refresh_token"):
        raise UploadError("TikTok isn't connected. Open Settings and click Connect.")
    if tok.get("expires_at", 0) > time.time() + 300:
        return tok["access_token"]
    if tok.get("refresh_expires_at", 0) < time.time():
        raise UploadError("TikTok sign-in expired. Reconnect in Settings.")
    t = _token_request({"grant_type": "refresh_token", "refresh_token": tok["refresh_token"]})
    _save_tokens(t)
    return t["access_token"]


def _api(path, token, payload):
    r = requests.post(f"{API}{path}", json=payload, timeout=60, headers={
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json; charset=UTF-8",
    })
    try:
        body = r.json()
    except ValueError:
        raise UploadError(error_text(r))
    err = body.get("error", {})
    if err.get("code") not in (None, "ok"):
        raise UploadError(_friendly(err.get("code"), err.get("message")))
    if not r.ok:
        raise UploadError(error_text(r))
    return body.get("data", {})


def _friendly(code, message):
    hints = {
        "unaudited_client_can_only_post_to_private_accounts":
            "Your TikTok app isn't audited yet, so direct posting only works if your TikTok "
            "account is set to private. Switch TikTok to 'Send to drafts' mode, or make the account private.",
        "spam_risk_too_many_posts": "TikTok's daily API post limit for this account was reached.",
        "spam_risk_too_many_pending_share":
            "Too many videos are waiting in your TikTok inbox. Post or delete some in the app first.",
        "privacy_level_option_mismatch": "That TikTok privacy option isn't available for your account.",
        "access_token_invalid": "TikTok sign-in is no longer valid. Reconnect in Settings.",
        "scope_not_authorized": "Your TikTok app is missing the video.upload / video.publish scope.",
    }
    return hints.get(code, f"TikTok error: {code} - {message}")


def creator_info():
    return _api("/post/publish/creator_info/query/", _access_token(), {})


def upload(path, title, description, opts, report):
    size = os.path.getsize(path)
    mode = opts.get("mode", "draft")
    token = _access_token()

    if size <= SINGLE_CHUNK_MAX:
        chunk_size, chunks = size, 1
    else:
        chunk_size = CHUNK
        chunks = math.floor(size / chunk_size)  # last chunk absorbs the remainder
    source = {"source": "FILE_UPLOAD", "video_size": size,
              "chunk_size": chunk_size, "total_chunk_count": chunks}

    report(0, "Starting upload")
    if mode == "direct":
        info = creator_info()
        privacy = opts.get("privacy") or "SELF_ONLY"
        if privacy not in info.get("privacy_level_options", [privacy]):
            raise UploadError(f"TikTok privacy '{privacy}' isn't allowed for this account.")
        post_info = {
            "title": build_caption(title, description, CAPTION_LIMIT),
            "privacy_level": privacy,
            "disable_comment": bool(opts.get("disable_comment")) or bool(info.get("comment_disabled")),
            "disable_duet": bool(info.get("duet_disabled")),
            "disable_stitch": bool(info.get("stitch_disabled")),
        }
        data = _api("/post/publish/video/init/", token, {"post_info": post_info, "source_info": source})
    else:
        data = _api("/post/publish/inbox/video/init/", token, {"source_info": source})

    publish_id, upload_url = data["publish_id"], data["upload_url"]
    mime = mime_for(path)
    with open(path, "rb") as f:
        for i in range(chunks):
            start = i * chunk_size
            end = size - 1 if i == chunks - 1 else start + chunk_size - 1
            f.seek(start)
            body = f.read(end - start + 1)
            for attempt in range(4):
                try:
                    r = requests.put(upload_url, data=body, timeout=(30, 300), headers={
                        "Content-Type": mime,
                        "Content-Length": str(len(body)),
                        "Content-Range": f"bytes {start}-{end}/{size}",
                    })
                    if r.status_code in (200, 201, 206):
                        break
                    if r.status_code < 500:
                        raise UploadError(f"TikTok rejected the file: {error_text(r)}")
                except requests.RequestException:
                    pass
                time.sleep(2 ** (attempt + 1))
            else:
                raise UploadError("TikTok upload kept failing; check your connection and try again.")
            report(90 * (i + 1) / chunks, "Uploading")

    report(92, "TikTok is processing")
    deadline = time.time() + 15 * 60
    while time.time() < deadline:
        time.sleep(4)
        st = _api("/post/publish/status/fetch/", token, {"publish_id": publish_id})
        state = st.get("status")
        if state == "SEND_TO_USER_INBOX":
            report(100, "In your TikTok inbox")
            return {"url": None, "message": "Sent to TikTok. Open the TikTok app, tap the inbox "
                                            "notification, add your caption and post."}
        if state == "PUBLISH_COMPLETE":
            report(100, "Posted")
            return {"url": None, "message": f"Posted to TikTok ({opts.get('privacy', 'SELF_ONLY')})."}
        if state == "FAILED":
            raise UploadError(f"TikTok processing failed: {st.get('fail_reason', 'unknown reason')}")
    raise UploadError("TikTok is taking unusually long to process; check the app in a few minutes.")
