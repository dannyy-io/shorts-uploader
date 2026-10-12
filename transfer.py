"""Export / import your setup (accounts, presets, history, queue) as one .zip file,
e.g. to move from your PC to the home server.

The export contains your account tokens, so it should be kept private.
Downloaded videos (the downloads folder) aren't included.
"""
import json
import os
import shutil
import time
import uuid
import zipfile

import scheduler
import storage
import updater

DATA_FILES = ["config.json", "tokens.json", "history.json", "cookies.txt"]
MERGED_JSON = {"config.json": "config", "tokens.json": "tokens"}  # merged section by section


class TransferError(Exception):
    pass


def export(include_queue=True, move_queue=False):
    """Write an export zip into the temp folder. Returns (path, number_of_queued_posts)."""
    path = os.path.join(storage.TEMP_DIR, f"export_{uuid.uuid4().hex[:8]}.zip")
    items = []
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for name in DATA_FILES:
            src = os.path.join(storage.DATA_DIR, name)
            if os.path.exists(src):
                z.write(src, "data/" + name)
        if include_queue:
            for it in scheduler.items():
                if it["status"] not in ("scheduled", "error", "preparing"):
                    continue  # skip posts that are uploading right now
                src = scheduler.resolve_file(it)
                if not os.path.exists(src):
                    continue
                arc = "queue/" + os.path.basename(src)
                z.write(src, arc, compress_type=zipfile.ZIP_STORED)  # videos are already compressed
                copy = dict(it, file=arc, job_id=None)
                items.append(copy)
            z.writestr("data/queue.json", json.dumps(items, indent=2))
        z.writestr("manifest.json", json.dumps({
            "app": "shorts-uploader", "version": updater.local_version(),
            "exported_at": time.time(), "queue": len(items),
        }, indent=2))
    if move_queue:
        for it in items:
            try:
                scheduler.remove(it["id"])
            except (KeyError, ValueError):
                pass
    return path, len(items)


def _safe_names(z):
    for name in z.namelist():
        norm = os.path.normpath(name)
        if name.startswith("/") or norm.startswith("..") or ":" in name.split("/")[0]:
            raise TransferError("That file looks unsafe; not importing it.")


def import_zip(fileobj):
    try:
        z = zipfile.ZipFile(fileobj)
    except zipfile.BadZipFile:
        raise TransferError("That isn't a Shorts Uploader export file.")
    with z:
        _safe_names(z)
        names = set(z.namelist())
        if "manifest.json" not in names:
            raise TransferError("That isn't a Shorts Uploader export file.")
        manifest = json.loads(z.read("manifest.json"))
        if manifest.get("app") != "shorts-uploader":
            raise TransferError("That isn't a Shorts Uploader export file.")
        summary = {"from_version": manifest.get("version"), "accounts": [], "queue": 0, "presets": 0}

        for name in DATA_FILES:
            arc = "data/" + name
            if arc not in names:
                continue
            raw = z.read(arc)
            if name in MERGED_JSON:
                incoming = json.loads(raw or b"{}")
                key = MERGED_JSON[name]
                current = storage.load(key)
                current.update(incoming)  # imported sections replace the same sections here
                storage.save(key, current)
                if key == "tokens":
                    summary["accounts"] = sorted(k for k, v in incoming.items() if v)
                if key == "config":
                    summary["presets"] = len(incoming.get("general", {}).get("presets") or [])
            elif name == "history.json":
                incoming = json.loads(raw or b"[]")
                current = storage.load("history", default=[])
                seen = {(h.get("time"), h.get("title")) for h in current}
                merged = current + [h for h in incoming if (h.get("time"), h.get("title")) not in seen]
                merged.sort(key=lambda h: h.get("time", 0), reverse=True)
                storage.save("history", merged[:50])
            else:
                with open(os.path.join(storage.DATA_DIR, name), "wb") as f:
                    f.write(raw)

        if "data/queue.json" in names:
            new_items = []
            for it in json.loads(z.read("data/queue.json") or b"[]"):
                arc = it.get("file", "")
                if arc not in names:
                    continue
                new_id = uuid.uuid4().hex[:12]
                base = os.path.basename(arc).split("_", 1)[-1]
                dest = os.path.join(scheduler.QUEUE_DIR, f"{new_id}_{base}")
                with z.open(arc) as src, open(dest, "wb") as out:
                    shutil.copyfileobj(src, out, 1024 * 1024)
                it.update(id=new_id, file=dest, size=os.path.getsize(dest), job_id=None)
                if it.get("status") not in ("scheduled", "error", "preparing"):
                    it["status"] = "scheduled"
                new_items.append(it)
            scheduler.import_items(new_items)
            summary["queue"] = len(new_items)
    return summary
