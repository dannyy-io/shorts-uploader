"""Temporary public links for videos (Instagram Login can only post a video from a public URL).

Ways to get a link, tried in order:
  1. Our own short-lived Cloudflare quick tunnel (free, no account): a tiny web server that serves only
     this one video under a random path, reachable at https://<random>.trycloudflare.com while the
     post is being made, then shut down.
  2. Free temporary file hosts (litterbox.catbox.moe, tmpfiles.org) as backups.

Each way is a context manager that yields the URL and cleans up afterwards.
"""
import contextlib
import http.server
import os
import platform
import re
import secrets
import shutil
import socketserver
import subprocess
import sys
import threading
import time
import urllib.request
import uuid

import requests

import storage

TIMEOUT = (20, 600)
UA = {"User-Agent": "ShortsUploader/1.0 (+https://github.com/dannyy-io/shorts-uploader)"}
BIN_DIR = os.path.join(storage.DATA_DIR, "bin")
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_tunnel_lock = threading.Lock()  # one tunnel at a time


class HostError(Exception):
    pass


# ---------- 1. own Cloudflare quick tunnel ----------

def _cloudflared_asset():
    machine = platform.machine().lower()
    arch = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64",
            "armv7l": "arm", "armv6l": "arm", "i386": "386", "i686": "386"}.get(machine)
    if not arch:
        return None
    if os.name == "nt":
        return f"cloudflared-windows-{arch}.exe"
    if sys.platform.startswith("linux"):
        return f"cloudflared-linux-{arch}"
    return None  # macOS ships as an archive; the backup hosts are used there


def cloudflared_exe():
    """Path to cloudflared, downloading Cloudflare's official build the first time."""
    found = shutil.which("cloudflared")
    if found:
        return found
    asset = _cloudflared_asset()
    if not asset:
        raise HostError("no cloudflared build for this computer")
    exe = os.path.join(BIN_DIR, "cloudflared.exe" if os.name == "nt" else "cloudflared")
    if os.path.exists(exe) and os.path.getsize(exe) > 1_000_000:
        return exe
    os.makedirs(BIN_DIR, exist_ok=True)
    url = f"https://github.com/cloudflare/cloudflared/releases/latest/download/{asset}"
    tmp = exe + ".part"
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=120) as r, open(tmp, "wb") as f:
            shutil.copyfileobj(r, f)
        os.replace(tmp, exe)
    except OSError as e:
        with contextlib.suppress(OSError):
            os.remove(tmp)
        raise HostError(f"couldn't download cloudflared ({e})")
    if os.name != "nt":
        os.chmod(exe, 0o755)
    return exe


def _file_server(path, secret):
    """A local web server that serves only `path`, at /<secret>/video.mp4 (with Range support)."""
    want = f"/{secret}/video.mp4"
    size = os.path.getsize(path)

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, head_only):
            if self.path.split("?")[0] != want:
                self.send_error(404)
                return
            start, end = 0, size - 1
            m = re.match(r"bytes=(\d*)-(\d*)$", self.headers.get("Range", ""))
            if m and (m[1] or m[2]):
                if m[1]:
                    start = int(m[1])
                    end = min(int(m[2]), size - 1) if m[2] else size - 1
                else:  # last N bytes
                    start = max(0, size - int(m[2]))
                if start > end or start >= size:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.end_headers()
                    return
                self.send_response(206)
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            else:
                self.send_response(200)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(end - start + 1))
            self.end_headers()
            if head_only:
                return
            with open(path, "rb") as f:
                f.seek(start)
                left = end - start + 1
                while left > 0:
                    chunk = f.read(min(1 << 20, left))
                    if not chunk:
                        break
                    try:
                        self.wfile.write(chunk)
                    except OSError:
                        return
                    left -= len(chunk)

        def do_GET(self):
            self._send(False)

        def do_HEAD(self):
            self._send(True)

    class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
        daemon_threads = True

    srv = Server(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


@contextlib.contextmanager
def quick_tunnel(path, wait=90):
    """Yield a public https URL for `path` through a temporary Cloudflare tunnel."""
    exe = cloudflared_exe()
    with _tunnel_lock:
        secret = secrets.token_urlsafe(24)
        srv = _file_server(path, secret)
        port = srv.server_address[1]
        proc = subprocess.Popen([exe, "tunnel", "--no-autoupdate", "--url", f"http://127.0.0.1:{port}"],
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace",
                                creationflags=NO_WINDOW)
        found, lines = [], []

        def read():
            for line in proc.stdout:
                lines.append(line.strip())
                del lines[:-20]
                m = re.search(r"https://[a-z0-9-]+\.trycloudflare\.com", line)
                if m and not found:
                    found.append(m.group(0))
        threading.Thread(target=read, daemon=True).start()
        try:
            deadline = time.time() + wait
            while not found:
                if proc.poll() is not None:
                    raise HostError("cloudflared stopped: " + (lines[-1] if lines else "no output"))
                if time.time() > deadline:
                    raise HostError("Cloudflare didn't give a link in time")
                time.sleep(0.5)
            url = f"{found[0]}/{secret}/video.mp4"
            # a new quick tunnel takes a few seconds before it answers; check it ourselves first
            while True:
                try:
                    r = requests.head(url, headers=UA, timeout=15)
                    if r.status_code == 200:
                        break
                except requests.RequestException:
                    pass
                if time.time() > deadline:
                    raise HostError("the link never became reachable")
                time.sleep(2)
            yield url
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
            srv.shutdown()
            srv.server_close()


# ---------- 2. free temporary file hosts (backups) ----------

def _open(path):
    return (f"{uuid.uuid4().hex}.mp4", open(path, "rb"), "video/mp4")


def _check(url):
    url = (url or "").strip()
    if not url.startswith("https://"):
        raise HostError(f"unexpected answer: {url[:120] or 'empty'}")
    return url


@contextlib.contextmanager
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
    yield _check(r.text)


@contextlib.contextmanager
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
    yield _check(page.replace("https://tmpfiles.org/", "https://tmpfiles.org/dl/", 1))


HOSTS = [("Cloudflare tunnel", quick_tunnel), ("litterbox.catbox.moe", litterbox), ("tmpfiles.org", tmpfiles)]
