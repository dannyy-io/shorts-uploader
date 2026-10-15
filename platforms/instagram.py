"""Instagram Reels via the Instagram API (resumable upload of a local file).

Needs an Instagram Professional account (Business or Creator). Sign-in is a
token you generate once in the Meta developer dashboard and paste into Settings;
the app refreshes it automatically before it expires (they last 60 days).
"""
import os
import time

import requests

import storage
from .common import ProgressFile, UploadError, build_caption, error_text

NAME = "Instagram"
USES_OAUTH = False

VERSION = "v23.0"
CAPTION_LIMIT = 2200
REFRESH_AFTER = 7 * 86400  # refresh the 60-day token weekly


def _host():
    return storage.get_section("config", "instagram").get("api_host") or "graph.instagram.com"


def status():
    tok = storage.get_section("tokens", "instagram")
    return {"configured": True, "connected": bool(tok.get("access_token")), "account": tok.get("username")}


def set_token(token, user_id=None):
    token = token.strip()
    host = _host()
    if host == "graph.instagram.com":
        r = requests.get(f"https://{host}/{VERSION}/me",
                         params={"fields": "user_id,username", "access_token": token}, timeout=20)
        if not r.ok:
            raise UploadError(_err(r, "Instagram didn't accept that token: "))
        me = r.json()
        user_id, username = me.get("user_id") or me.get("id"), me.get("username")
    else:
        if not user_id:
            raise UploadError("With Facebook Login you also need your Instagram user ID.")
        r = requests.get(f"https://{host}/{VERSION}/{user_id}",
                         params={"fields": "username", "access_token": token}, timeout=20)
        if not r.ok:
            raise UploadError(_err(r, "Instagram didn't accept that token: "))
        username = r.json().get("username")
    storage.set_section("tokens", "instagram", {
        "access_token": token, "user_id": str(user_id), "username": username, "saved_at": time.time(),
    })


def disconnect():
    storage.clear_section("tokens", "instagram")


def _token():
    tok = storage.get_section("tokens", "instagram")
    if not tok.get("access_token"):
        raise UploadError("Instagram isn't connected. Paste an access token in Settings.")
    if _host() == "graph.instagram.com" and time.time() - tok.get("saved_at", 0) > REFRESH_AFTER:
        r = requests.get("https://graph.instagram.com/refresh_access_token",
                         params={"grant_type": "ig_refresh_token", "access_token": tok["access_token"]},
                         timeout=20)
        if r.ok and r.json().get("access_token"):
            tok = storage.update_section("tokens", "instagram",
                                         access_token=r.json()["access_token"], saved_at=time.time())
        elif time.time() - tok.get("saved_at", 0) > 59 * 86400:
            raise UploadError("Instagram token expired. Generate a new one and paste it in Settings.")
    return tok["access_token"], tok["user_id"]


BLOCKED_HINT = (" Meta has locked API access for your developer account or app (common with new accounts). "
                "Open developers.facebook.com, complete any verification it asks for (look for a banner or "
                "notification), check the app's dashboard for restriction alerts, then hit Retry.")


def _err(resp, prefix=""):
    text = error_text(resp)
    if "api access blocked" in text.lower():
        text += BLOCKED_HINT
    return prefix + text


def upload(path, title, description, opts, report):
    caption = build_caption(title, description, CAPTION_LIMIT)
    token, user_id = _token()
    host = _host()
    base = f"https://{host}/{VERSION}"
    size = os.path.getsize(path)

    report(0, "Creating Reel")
    data = {
        "media_type": "REELS",
        "upload_type": "resumable",
        "caption": caption,
        "share_to_feed": "true" if opts.get("share_to_feed", True) else "false",
        "access_token": token,
    }
    if opts.get("thumb_offset") is not None:
        data["thumb_offset"] = str(int(opts["thumb_offset"]))  # cover frame, in milliseconds
    r = requests.post(f"{base}/{user_id}/media", data=data, timeout=60)
    if not r.ok:
        raise UploadError(_err(r))
    container = r.json()
    cid = container["id"]
    upload_uri = container.get("uri") or f"https://rupload.facebook.com/ig-api-upload/{VERSION}/{cid}"

    with ProgressFile(path, report, 0, 80) as body:
        up = requests.post(upload_uri, data=body, timeout=(30, 900), headers={
            "Authorization": f"OAuth {token}",
            "offset": "0",
            "file_size": str(size),
        })
    if not up.ok:
        raise UploadError(_err(up, "Instagram rejected the file: "))

    report(82, "Instagram is processing")
    deadline = time.time() + 15 * 60
    while True:
        time.sleep(5)
        s = requests.get(f"{base}/{cid}", params={"fields": "status_code,status", "access_token": token},
                         timeout=30)
        if s.ok:
            code = s.json().get("status_code")
            if code == "FINISHED":
                break
            if code in ("ERROR", "EXPIRED"):
                raise UploadError(f"Instagram couldn't process the video: {s.json().get('status', code)}")
        if time.time() > deadline:
            raise UploadError("Instagram is taking unusually long to process the video.")

    report(95, "Publishing")
    pub = requests.post(f"{base}/{user_id}/media_publish",
                        data={"creation_id": cid, "access_token": token}, timeout=60)
    if not pub.ok:
        raise UploadError(_err(pub, "Instagram processed the video but publishing failed: "))
    media_id = pub.json()["id"]

    url = None
    try:
        p = requests.get(f"{base}/{media_id}", params={"fields": "permalink", "access_token": token}, timeout=20)
        url = p.json().get("permalink") if p.ok else None
    except requests.RequestException:
        pass
    report(100, "Posted")
    return {"url": url, "message": "Posted to Instagram Reels."}
