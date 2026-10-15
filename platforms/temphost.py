"""Temporary public links for videos (Instagram Login can only post a video from a public URL).

The file goes to a free temporary file host under a random name and expires there on its own
(an hour or less). Hosts are tried in order; if one is down, the next one is used.
"""
import os
import uuid

import requests

TIMEOUT = (20, 600)
UA = {"User-Agent": "ShortsUploader/1.0 (+https://github.com/dannyy-io/shorts-uploader)"}


class HostError(Exception):
    pass


def _open(path):
    return (f"{uuid.uuid4().hex}.mp4", open(path, "rb"), "video/mp4")


def _check(url):
    url = (url or "").strip()
    if not url.startswith("https://"):
        raise HostError(f"unexpected answer: {url[:120] or 'empty'}")
    return url


def litterbox(path):
    """litterbox.catbox.moe: up to 1 GB, deleted after 1 hour."""
    name, f, mime = _open(path)
    with f:
        try:
            r = requests.post("https://litterbox.catbox.moe/resources/internals/api.php", headers=UA,
                              data={"reqtype": "fileupload", "time": "1h"},
                              files={"fileToUpload": (name, f, mime)}, timeout=TIMEOUT)
        except requests.RequestException as e:
            raise HostError(f"couldn't reach it ({type(e).__name__})")
    if not r.ok:
        raise HostError(f"HTTP {r.status_code}")
    return _check(r.text)


def tmpfiles(path):
    """tmpfiles.org: up to 100 MB, deleted after 60 minutes."""
    if os.path.getsize(path) > 100 * 1024 * 1024:
        raise HostError("video is over its 100 MB limit")
    name, f, mime = _open(path)
    with f:
        try:
            r = requests.post("https://tmpfiles.org/api/v1/upload", headers=UA,
                              files={"file": (name, f, mime)}, timeout=TIMEOUT)
        except requests.RequestException as e:
            raise HostError(f"couldn't reach it ({type(e).__name__})")
    if not r.ok:
        raise HostError(f"HTTP {r.status_code}")
    try:
        page = r.json()["data"]["url"]
    except (ValueError, KeyError, TypeError):
        raise HostError(f"unexpected answer: {r.text[:120]}")
    # the API returns the download page; the file itself is under /dl/
    page = page.replace("http://", "https://", 1)
    return _check(page.replace("https://tmpfiles.org/", "https://tmpfiles.org/dl/", 1))


HOSTS = [("litterbox.catbox.moe", litterbox), ("tmpfiles.org", tmpfiles)]
