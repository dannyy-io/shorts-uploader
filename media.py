"""Auto-fix: make any clip upload-ready for Shorts / Reels / TikTok.

Target: 1080x1920 (9:16), H.264 + AAC in MP4, 60 fps max, "fast start".
Horizontal or square clips get either a blurred copy of themselves behind them
(default) or are cropped to fill. Files that are already fine are left alone,
and files that only need a different container are remuxed without re-encoding.
"""
import os
import re
import shutil
import subprocess

TARGET_W, TARGET_H = 1080, 1920
ASPECT = TARGET_W / TARGET_H
FONT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "fonts", "Inter-Black.otf")
HOOK_STYLES = ("outline", "box")
HOOK_POSITIONS = ("top", "middle")


class MediaError(Exception):
    pass


def ffmpeg_exe():
    import downloader  # shares the ffmpeg that the downloader sets up
    folder = downloader.ffmpeg_dir()
    if not folder:
        return None
    exe = os.path.join(folder, "ffmpeg.exe" if os.name == "nt" else "ffmpeg")
    return exe if os.path.exists(exe) else shutil.which("ffmpeg")


def available():
    return ffmpeg_exe() is not None


def _run(args, **kw):
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    return subprocess.run(args, capture_output=True, text=True, errors="replace", creationflags=flags, **kw)


def probe(path):
    """Read basic stream info by parsing `ffmpeg -i` (no ffprobe needed)."""
    exe = ffmpeg_exe()
    if not exe:
        raise MediaError("ffmpeg isn't installed (restart the app with start.bat to install it).")
    out = _run([exe, "-hide_banner", "-i", path]).stderr
    info = {"duration": 0.0, "width": 0, "height": 0, "vcodec": None, "pix_fmt": None,
            "fps": 0.0, "acodec": None, "rotation": 0}
    m = re.search(r"Duration: (\d+):(\d+):(\d+(?:\.\d+)?)", out)
    if m:
        info["duration"] = int(m[1]) * 3600 + int(m[2]) * 60 + float(m[3])
    vline = next((ln for ln in out.splitlines() if re.search(r"Stream #\S+.*: Video: ", ln)
                  and "attached pic" not in ln), None)
    if not vline:
        raise MediaError("Couldn't find a video track in that file.")
    info["vcodec"] = re.search(r"Video: (\w+)", vline)[1]
    pm = re.search(r"Video: [^,]+, (\w+)", vline)
    info["pix_fmt"] = pm[1] if pm else None
    wh = re.search(r", (\d{2,5})x(\d{2,5})", vline)
    if wh:
        info["width"], info["height"] = int(wh[1]), int(wh[2])
    fm = re.search(r"([\d.]+) fps", vline)
    info["fps"] = float(fm[1]) if fm else 0.0
    am = re.search(r"Stream #\S+.*: Audio: (\w+)", out)
    info["acodec"] = am[1] if am else None
    rm = re.search(r"rotation of (-?[\d.]+) degrees", out) or re.search(r"rotate\s*:\s*(-?\d+)", out)
    if rm:
        info["rotation"] = int(float(rm[1])) % 360
    if info["rotation"] in (90, 270):  # phone videos stored sideways: ffmpeg rotates them on encode
        info["width"], info["height"] = info["height"], info["width"]
    return info


def plan(info, path, fix):
    """Return (mode, reasons). mode is None (leave as is), 'remux' or 'encode'."""
    reasons, encode = [], False
    w, h = info["width"], info["height"]
    if w and h and abs(w / h - ASPECT) > 0.01:
        reasons.append("horizontal → vertical" if w > h else "reframed to 9:16")
        encode = True
    elif h > TARGET_H:
        reasons.append(f"scaled down to {TARGET_W}×{TARGET_H}")
        encode = True
    if info["vcodec"] != "h264" or info["pix_fmt"] not in ("yuv420p", "yuvj420p"):
        reasons.append(f"{info['vcodec']} → H.264")
        encode = True
    if info["acodec"] and info["acodec"] != "aac":
        reasons.append(f"{info['acodec']} audio → AAC")
        encode = True
    if info["fps"] > 60.5:
        reasons.append("frame rate capped at 60")
        encode = True
    if hook_settings(fix):
        reasons.append("hook text added")
        encode = True
    trim = float(fix.get("trim") or 0)
    if trim and info["duration"] > trim + 0.5:
        reasons.append(f"trimmed to {int(trim // 60)}:{int(trim % 60):02d}")
        encode = True
    if encode:
        return "encode", reasons
    if os.path.splitext(path)[1].lower() not in (".mp4", ".m4v"):
        return "remux", ["repackaged as MP4"]
    return None, []


def output_size(info):
    """Width and height the converted video will have."""
    w, h = info["width"], info["height"]
    if not (w and h) or abs(w / h - ASPECT) > 0.01 or h > TARGET_H:
        return TARGET_W, TARGET_H
    return w // 2 * 2, h // 2 * 2


def _filter(info, fit, hook=None):
    """ffmpeg filter graph ending in [v]. With hook, input 1 is the hook text image to lay on top."""
    base = _base_filter(info, fit)
    if not hook:
        return base
    secs = float(hook.get("secs") or 0)
    enable = f":enable='lte(t,{secs:g})'" if secs else ""
    return base[:-3] + "[base];[base][1:v]overlay=0:0:format=auto" + enable + "[v]"


def _base_filter(info, fit):
    w, h = info["width"], info["height"]
    tail = ",fps=60" if info["fps"] > 60.5 else ""
    if w and h and abs(w / h - ASPECT) > 0.01:
        if fit == "crop":
            return (f"[0:v]scale={TARGET_W}:{TARGET_H}:force_original_aspect_ratio=increase,"
                    f"crop={TARGET_W}:{TARGET_H},setsar=1{tail}[v]")
        return (f"[0:v]split=2[a][b];"
                f"[a]scale={TARGET_W}:{TARGET_H}:force_original_aspect_ratio=increase,crop={TARGET_W}:{TARGET_H},"
                f"boxblur=24:2,eq=brightness=-0.06[bg];"
                f"[b]scale={TARGET_W}:{TARGET_H}:force_original_aspect_ratio=decrease[fg];"
                f"[bg][fg]overlay=(W-w)/2:(H-h)/2,setsar=1{tail}[v]")
    if h > TARGET_H:
        return f"[0:v]scale={TARGET_W}:{TARGET_H},setsar=1{tail}[v]"
    # keep size; make dimensions even for H.264
    return f"[0:v]scale=trunc(iw/2)*2:trunc(ih/2)*2,setsar=1{tail}[v]"


def convert(src, dst, fix, report=lambda pct, msg: None):
    """Write a fixed copy of src to dst. Returns (path_to_use, reasons).
    If nothing needs changing, returns (src, []) and writes nothing."""
    info = probe(src)
    mode, reasons = plan(info, src, fix)
    if mode is None:
        return src, []
    exe = ffmpeg_exe()
    trim = float(fix.get("trim") or 0)
    total = min(info["duration"], trim) if trim else info["duration"]
    if mode == "remux":
        args = [exe, "-hide_banner", "-y", "-i", src, "-map", "0:v:0", "-map", "0:a:0?", "-c", "copy",
                "-movflags", "+faststart", dst]
        report(0, "Repackaging video")
        res = _run(args)
        if res.returncode != 0:
            raise MediaError("Couldn't repackage the video: " + res.stderr.strip().splitlines()[-1][:200])
        return dst, reasons

    hook = hook_settings(fix)
    hook_png = None
    args = [exe, "-hide_banner", "-y", "-i", src]
    if hook:
        hook_png = os.path.splitext(dst)[0] + "_hook.png"
        ow, oh = output_size(info)
        render_hook(hook["text"], ow, oh, hook["style"], hook["pos"]).save(hook_png)
        args += ["-i", hook_png]
    if trim and info["duration"] > trim:
        args += ["-t", str(trim)]
    args += ["-filter_complex", _filter(info, fix.get("fit", "blur"), hook),
             "-map", "[v]", "-map", "0:a:0?",
             "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-profile:v", "high",
             "-pix_fmt", "yuv420p",
             "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
             "-movflags", "+faststart", "-progress", "pipe:1", "-nostats", dst]
    report(0, "Converting video (" + ", ".join(reasons) + ")")
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            errors="replace", creationflags=flags)
    tail = []

    def drain_stderr():
        for line in proc.stderr:
            tail.append(line)
            del tail[:-30]

    import threading
    t = threading.Thread(target=drain_stderr, daemon=True)
    t.start()
    for line in proc.stdout:
        if line.startswith("out_time_us=") or line.startswith("out_time_ms="):
            try:
                secs = int(line.split("=")[1]) / 1e6
            except ValueError:
                continue
            if total:
                report(min(99.0, 100 * secs / total), "Converting video")
    proc.wait()
    t.join(timeout=5)
    if hook_png:
        try:
            os.remove(hook_png)
        except OSError:
            pass
    if proc.returncode != 0:
        msg = next((ln.strip() for ln in reversed(tail) if ln.strip()), "unknown error")
        raise MediaError("Couldn't convert the video: " + msg[:200])
    report(100, "Video ready")
    return dst, reasons


# ---------- hook text: big caption on top of the video ----------

_EMOJI = re.compile("[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF\uFE0F\u200D\u20E3"
                    "\U0001F3FB-\U0001F3FF\U0000E000-\U0000F8FF\U000E0000-\U000E007F]")


def clean_hook(text):
    """One tidy line of hook text (no emoji: the font can't draw them), max 120 characters."""
    text = _EMOJI.sub("", str(text or ""))
    text = re.sub(r"[ \t]+", " ", text.replace("\r", "")).strip()
    lines = [ln.strip() for ln in text.split("\n") if ln.strip()]
    return "\n".join(lines)[:120].strip()


def hook_settings(fix):
    """The hook part of auto-fix settings, cleaned up, or None if there's no hook text."""
    hook = (fix or {}).get("hook") or {}
    text = clean_hook(hook.get("text"))
    if not text:
        return None
    secs = float(hook.get("secs") or 0)
    return {"text": text,
            "style": hook.get("style") if hook.get("style") in HOOK_STYLES else "outline",
            "pos": hook.get("pos") if hook.get("pos") in HOOK_POSITIONS else "top",
            "secs": secs if 0 < secs <= 60 else 0}


def _wrap(draw, font, text, max_w):
    lines = []
    for para in text.split("\n"):
        cur = ""
        for word in para.split(" "):
            test = f"{cur} {word}".strip()
            if draw.textlength(test, font=font) <= max_w or not cur:
                cur = test
            else:
                lines.append(cur)
                cur = word
        lines.append(cur)
    return lines


def render_hook(text, width=TARGET_W, height=TARGET_H, style="outline", pos="top"):
    """A transparent image the size of the video with the hook text drawn on it (Pillow)."""
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        raise MediaError("Hook text needs the Pillow package. Restart the app so it can install it.")
    if not os.path.exists(FONT):
        raise MediaError("The hook text font is missing. Reinstall the app.")
    text = clean_hook(text)
    img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    if not text:
        return img
    draw = ImageDraw.Draw(img)
    unit = width / TARGET_W
    max_w = int(width * 0.84)
    # biggest size where it fits in 3 lines (long words shrink it further)
    for size in range(int(96 * unit), int(40 * unit) - 1, -int(4 * unit) or -1):
        font = ImageFont.truetype(FONT, size)
        lines = _wrap(draw, font, text, max_w)
        if len(lines) <= 3 and all(draw.textlength(ln, font=font) <= max_w for ln in lines):
            break
    lines = lines[:4]
    line_h = int(size * 1.18)
    block_h = line_h * len(lines)
    top = int(height * 0.14) if pos == "top" else (height - block_h) // 2
    if style == "box":
        pad_x, pad_y = int(size * 0.38), int(size * 0.16)
        for i, ln in enumerate(lines):
            lw = draw.textlength(ln, font=font)
            x0, y0 = (width - lw) / 2 - pad_x, top + i * line_h - pad_y
            draw.rounded_rectangle([x0, y0, x0 + lw + 2 * pad_x, y0 + line_h + pad_y * 0.6],
                                   radius=int(size * 0.22), fill=(255, 255, 255, 255))
        for i, ln in enumerate(lines):
            draw.text((width / 2, top + i * line_h + line_h / 2), ln, font=font, anchor="mm", fill=(0, 0, 0, 255))
    else:
        stroke = max(3, int(size * 0.11))
        for i, ln in enumerate(lines):
            xy = (width / 2, top + i * line_h + line_h / 2)
            draw.text((xy[0] + 0, xy[1] + int(size * 0.05)), ln, font=font, anchor="mm", fill=(0, 0, 0, 110),
                      stroke_width=stroke, stroke_fill=(0, 0, 0, 110))  # soft drop shadow
            draw.text(xy, ln, font=font, anchor="mm", fill=(255, 255, 255, 255),
                      stroke_width=stroke, stroke_fill=(0, 0, 0, 255))
    return img
