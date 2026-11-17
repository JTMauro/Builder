"""File store: every uploaded or produced media file gets a short id and a JSON metadata record."""

from __future__ import annotations

import json
import mimetypes
import re
import shutil
import threading
import time
import uuid
from pathlib import Path
from typing import BinaryIO

from pydantic import BaseModel

from .config import get_settings

_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")
_lock = threading.Lock()


class FileInfo(BaseModel):
    id: str
    name: str
    size: int
    mime_type: str
    created_at: float
    source: str = "upload"  # "upload", "import", "url", or the operation that produced it
    parent_ids: list[str] = []


class FileNotFound(KeyError):
    pass


def safe_name(name: str) -> str:
    name = Path(name).name or "file"
    cleaned = _SAFE_NAME.sub("_", name).strip("._") or "file"
    return cleaned[:120]


def _dir(file_id: str) -> Path:
    if not re.fullmatch(r"[a-f0-9]{12}", file_id or ""):
        raise FileNotFound(file_id)
    return get_settings().files_dir / file_id


def _new_id() -> str:
    return uuid.uuid4().hex[:12]


def _write_meta(info: FileInfo) -> None:
    (_dir(info.id) / "meta.json").write_text(info.model_dump_json(indent=2))


def new_output_path(name: str) -> tuple[str, Path]:
    """Reserve an id and return the path an operation should write its output to."""
    file_id = _new_id()
    d = _dir(file_id)
    d.mkdir(parents=True, exist_ok=False)
    return file_id, d / safe_name(name)


def register(file_id: str, path: Path, source: str, parent_ids: list[str] | None = None) -> FileInfo:
    """Record metadata for a file already written to the path returned by new_output_path."""
    info = FileInfo(
        id=file_id,
        name=path.name,
        size=path.stat().st_size,
        mime_type=mimetypes.guess_type(path.name)[0] or "application/octet-stream",
        created_at=time.time(),
        source=source,
        parent_ids=parent_ids or [],
    )
    _write_meta(info)
    return info


def save_stream(name: str, stream: BinaryIO, source: str = "upload") -> FileInfo:
    max_bytes = get_settings().max_upload_mb * 1024 * 1024
    file_id, path = new_output_path(name)
    written = 0
    try:
        with path.open("wb") as out:
            while chunk := stream.read(1024 * 1024):
                written += len(chunk)
                if written > max_bytes:
                    raise ValueError(f"File is larger than the {get_settings().max_upload_mb} MB limit")
                out.write(chunk)
    except BaseException:
        shutil.rmtree(path.parent, ignore_errors=True)
        raise
    return register(file_id, path, source)


def save_bytes(name: str, data: bytes, source: str = "upload") -> FileInfo:
    import io

    return save_stream(name, io.BytesIO(data), source)


def import_path(src: Path, source: str = "import") -> FileInfo:
    file_id, path = new_output_path(src.name)
    shutil.copyfile(src, path)
    return register(file_id, path, source)


def get(file_id: str) -> FileInfo:
    meta = _dir(file_id) / "meta.json"
    if not meta.exists():
        raise FileNotFound(file_id)
    return FileInfo.model_validate_json(meta.read_text())


def path_of(file_id: str) -> Path:
    info = get(file_id)
    return _dir(file_id) / info.name


def list_files() -> list[FileInfo]:
    root = get_settings().files_dir
    if not root.exists():
        return []
    out = []
    for meta in root.glob("*/meta.json"):
        try:
            out.append(FileInfo.model_validate(json.loads(meta.read_text())))
        except Exception:
            continue
    return sorted(out, key=lambda f: f.created_at, reverse=True)


def delete(file_id: str) -> None:
    d = _dir(file_id)
    if not (d / "meta.json").exists():
        raise FileNotFound(file_id)
    with _lock:
        shutil.rmtree(d)
