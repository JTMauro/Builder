"""Watch folders: Builder picks up videos that land in a folder and makes the versions you asked for.

Point one at DaVinci Resolve's render folder (or any export folder). When a new video has finished
writing, Builder imports it and runs a preset, saving the results beside it in "Builder output":

- social: vertical 9:16, square 1:1, a 1080p web copy, a thumbnail and a short GIF
- web:    a 1080p web copy and a thumbnail
- import: just bring it into Builder's media library

Folders are kept in watch.json in Builder's data folder. Inputs are never changed.
"""

from __future__ import annotations

import json
import shutil
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from . import ops, storage
from .config import get_settings

VIDEO_EXT = {".mp4", ".mov", ".mkv", ".webm", ".mxf", ".avi", ".m4v"}
OUTPUT_DIR = "Builder output"
POLL_S = 5.0
SETTLE_S = 4.0

PRESETS: dict[str, list[tuple[str, dict[str, Any], str]]] = {
    "social": [
        ("reframe", {"aspect": "9:16", "mode": "crop", "height": 1920}, "vertical"),
        ("reframe", {"aspect": "1:1", "mode": "crop", "height": 1080}, "square"),
        ("resize", {"height": 1080}, "web"),
        ("thumbnail", {"time": 1.0, "width": 1280}, "thumbnail"),
        ("make_gif", {"start": 0, "duration": 4, "width": 480}, "preview"),
    ],
    "web": [
        ("resize", {"height": 1080}, "web"),
        ("thumbnail", {"time": 1.0, "width": 1280}, "thumbnail"),
    ],
    "import": [],
}

_lock = threading.Lock()
_thread: threading.Thread | None = None
_stop = threading.Event()
_pending: dict[str, tuple[int, float, float]] = {}   # path -> (size, mtime, first seen with this size)
log: list[dict[str, Any]] = []                         # recent results, newest last


class WatchError(ValueError):
    pass


def _state_path() -> Path:
    return get_settings().data_dir / "watch.json"


def _load() -> dict[str, Any]:
    try:
        data = json.loads(_state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    data.setdefault("folders", [])
    data.setdefault("done", {})
    return data


def _save(data: dict[str, Any]) -> None:
    p = _state_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(p)


def _allowed(path: Path) -> Path:
    resolved = path.expanduser().resolve()
    for root in get_settings().allowed_dirs:
        if resolved == root or root in resolved.parents:
            return resolved
    roots = ", ".join(str(r) for r in get_settings().allowed_dirs)
    raise WatchError(f"{resolved} is outside the folders Builder may use ({roots}). Add it to MEDIA_BACKEND_ALLOWED_DIRS.")


def list_folders(owner: str | None = None) -> list[dict[str, Any]]:
    with _lock:
        folders = _load()["folders"]
    return [f for f in folders if owner is None or f["owner"] == owner]


def add_folder(path: str, preset: str = "social", owner: str = "local") -> dict[str, Any]:
    if preset not in PRESETS:
        raise WatchError(f"preset must be one of: {', '.join(PRESETS)}")
    folder = _allowed(Path(path))
    if not folder.is_dir():
        raise WatchError(f"{folder} is not a folder")
    with _lock:
        data = _load()
        for f in data["folders"]:
            if Path(f["path"]) == folder:
                f.update(preset=preset, owner=owner, enabled=True)
                _save(data)
                return f
        entry = {"id": uuid.uuid4().hex[:8], "path": str(folder), "preset": preset, "owner": owner,
                 "enabled": True, "created_at": time.time()}
        data["folders"].append(entry)
        # files already there when the folder is added are left alone
        for v in _videos(folder):
            data["done"][str(v)] = _sig(v)
        _save(data)
    ensure_running()
    return entry


def remove_folder(folder_id: str, owner: str | None = None) -> bool:
    with _lock:
        data = _load()
        keep = [f for f in data["folders"] if not (f["id"] == folder_id and (owner is None or f["owner"] == owner))]
        removed = len(keep) != len(data["folders"])
        data["folders"] = keep
        _save(data)
    return removed


def _videos(folder: Path) -> list[Path]:
    try:
        return sorted(p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in VIDEO_EXT and not p.name.startswith("."))
    except OSError:
        return []


def _sig(p: Path) -> str:
    st = p.stat()
    return f"{st.st_size}:{int(st.st_mtime)}"


def process(path: Path, preset: str) -> dict[str, Any]:
    """Import one video and run a preset on it; results go to <folder>/Builder output/."""
    info = storage.import_path(path, source="watch")
    out_dir = path.parent / OUTPUT_DIR
    out_dir.mkdir(exist_ok=True)
    made, errors = [], []
    for op_name, params, label in PRESETS[preset]:
        try:
            res = ops.run_operation(op_name, {"file_id": info.id, **params})
            for o in res.outputs:
                dest = out_dir / f"{path.stem}_{label}{Path(o.name).suffix}"
                shutil.copyfile(storage.path_of(o.id), dest)
                made.append(str(dest))
        except Exception as e:  # one failed version shouldn't stop the others
            errors.append(f"{label}: {e}")
    return {"file": str(path), "file_id": info.id, "preset": preset, "made": made, "errors": errors, "at": time.time()}


def scan_once(settle_s: float = SETTLE_S) -> list[dict[str, Any]]:
    """Check every watched folder once; process videos that have stopped growing."""
    results = []
    with _lock:
        data = _load()
    now = time.time()
    for f in data["folders"]:
        if not f.get("enabled", True):
            continue
        folder = Path(f["path"])
        for v in _videos(folder):
            key = str(v)
            try:
                sig = _sig(v)
                size, mtime = v.stat().st_size, v.stat().st_mtime
            except OSError:
                continue
            if data["done"].get(key) == sig:
                continue
            seen = _pending.get(key)
            if seen is None or seen[:2] != (size, mtime):
                _pending[key] = (size, mtime, now)
                if settle_s > 0:
                    continue
            elif now - seen[2] < settle_s:
                continue
            _pending.pop(key, None)
            result = process(v, f["preset"])
            results.append(result)
            with _lock:
                d2 = _load()
                d2["done"][key] = sig
                _save(d2)
            log.append(result)
            del log[:-50]
    return results


def _loop() -> None:
    while not _stop.wait(POLL_S):
        try:
            if list_folders():
                scan_once()
        except Exception as e:  # keep watching even if one pass fails
            log.append({"error": str(e), "at": time.time()})
            del log[:-50]


def ensure_running() -> None:
    global _thread
    if _thread and _thread.is_alive():
        return
    _stop.clear()
    _thread = threading.Thread(target=_loop, daemon=True, name="watch-folders")
    _thread.start()


def stop() -> None:
    _stop.set()
