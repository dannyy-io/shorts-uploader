import base64
import hashlib
import os
import secrets


class UploadError(Exception):
    """An error with a message that's safe and useful to show in the UI."""


MIME_TYPES = {
    ".mp4": "video/mp4",
    ".m4v": "video/mp4",
    ".mov": "video/quicktime",
    ".webm": "video/webm",
}


def mime_for(path):
    return MIME_TYPES.get(os.path.splitext(path)[1].lower(), "video/mp4")


def new_verifier():
    return secrets.token_urlsafe(64)[:96]


def s256_base64url(verifier):
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def s256_hex(verifier):
    return hashlib.sha256(verifier.encode("ascii")).hexdigest()


def error_text(resp):
    """Pull the most human-readable error out of an API response."""
    try:
        body = resp.json()
    except ValueError:
        return f"HTTP {resp.status_code}: {resp.text[:300]}"
    err = body.get("error")
    if isinstance(err, dict):
        msg = err.get("message") or err.get("error_user_msg") or err.get("code")
        if isinstance(err.get("errors"), list) and err["errors"]:
            msg = err["errors"][0].get("message", msg)
        return f"HTTP {resp.status_code}: {msg}"
    if isinstance(err, str):
        return f"HTTP {resp.status_code}: {body.get('error_description') or err}"
    return f"HTTP {resp.status_code}: {str(body)[:300]}"


def build_caption(title, description, limit):
    parts = [p.strip() for p in (title, description) if p and p.strip()]
    caption = "\n\n".join(parts)
    if len(caption) > limit:
        raise UploadError(f"Caption is {len(caption)} characters; the limit is {limit}.")
    return caption


class ProgressFile:
    """File wrapper that reports read progress and tells requests its length,
    so the body is streamed with a proper Content-Length."""

    def __init__(self, path, report, start_pct=0.0, end_pct=100.0):
        self._f = open(path, "rb")
        self._size = os.path.getsize(path)
        self._read = 0
        self._report = report
        self._start = start_pct
        self._span = end_pct - start_pct

    def __len__(self):
        return self._size

    def read(self, n=-1):
        chunk = self._f.read(n if n and n > 0 else 1024 * 1024)
        self._read += len(chunk)
        if self._size:
            self._report(self._start + self._span * self._read / self._size, "Uploading")
        return chunk

    def close(self):
        self._f.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
