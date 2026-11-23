"""Small glTF helpers in plain Python (no Blender): pack a .gltf or a .glb with outside files into one
self-contained .glb, read a .glb's JSON, scale a model, and split a .glb into the .gltf + .bin + textures
layout the studio stages load props from (props/<id>/<id>_1k.gltf).
"""

from __future__ import annotations

import base64
import json
import mimetypes
import struct
import urllib.parse
from pathlib import Path
from typing import Any

GLB_MAGIC, JSON_CHUNK, BIN_CHUNK = 0x46546C67, 0x4E4F534A, 0x004E4942


class GlbError(ValueError):
    pass


def read_glb(data: bytes) -> tuple[dict[str, Any], bytes]:
    if len(data) < 20 or struct.unpack_from("<I", data)[0] != GLB_MAGIC:
        raise GlbError("not a binary glTF (.glb)")
    off, doc, bin_ = 12, None, b""
    while off + 8 <= len(data):
        ln, kind = struct.unpack_from("<II", data, off)
        chunk = data[off + 8: off + 8 + ln]
        if kind == JSON_CHUNK:
            doc = json.loads(chunk)
        elif kind == BIN_CHUNK:
            bin_ = bytes(chunk)
        off += 8 + ln
    if doc is None:
        raise GlbError("the .glb has no JSON chunk")
    return doc, bin_


def write_glb(doc: dict[str, Any], bin_: bytes) -> bytes:
    if bin_:
        doc.setdefault("buffers", [{}])
        doc["buffers"][0] = {"byteLength": len(bin_)}
    js = json.dumps(doc, separators=(",", ":")).encode()
    js += b" " * (-len(js) % 4)
    bin_ = bin_ + b"\0" * (-len(bin_) % 4)
    out = struct.pack("<II", len(js), JSON_CHUNK) + js
    if bin_:
        out += struct.pack("<II", len(bin_), BIN_CHUNK) + bin_
    return struct.pack("<III", GLB_MAGIC, 2, 12 + len(out)) + out


def _load_uri(uri: str, base: Path, root: Path | None = None) -> bytes:
    if uri.startswith("data:"):
        return base64.b64decode(uri.split(",", 1)[1])
    rel = urllib.parse.unquote(uri)
    root = (root or base.parent).resolve()   # the model's folder and around it (e.g. ../Textures), never further up
    p = (base / rel).resolve()
    if root not in p.parents:
        raise GlbError(f"glTF points outside its folder: {uri}")
    if not p.exists():   # packs often keep textures in a shared folder the model doesn't name exactly
        p = min(root.rglob(Path(rel).name), key=lambda q: (len(q.parts), str(q)), default=None)
        if not p:
            raise GlbError(f"missing file {uri} next to the model")
    return p.read_bytes()


def pack_glb(path: Path, root: Path | None = None) -> bytes:
    """A .gltf (with .bin/textures beside it) or a .glb that points at outside images -> one self-contained .glb.
    Outside files are looked for under `root` (default: the folder above the model's)."""
    path = Path(path)
    raw = path.read_bytes()
    if path.suffix.lower() == ".glb":
        doc, bin_ = read_glb(raw)
        buffers = [bin_]
        for b in doc.get("buffers", [])[1:]:
            buffers.append(_load_uri(b["uri"], path.parent, root))
    else:
        doc = json.loads(raw)
        buffers = [_load_uri(b["uri"], path.parent, root) if b.get("uri") else b"" for b in doc.get("buffers", [])]
    # one buffer: every buffer view moves into a single binary chunk
    blob = bytearray()
    starts = []
    for b in buffers:
        blob += b"\0" * (-len(blob) % 8)
        starts.append(len(blob))
        blob += b
    for bv in doc.get("bufferViews", []):
        bv["byteOffset"] = bv.get("byteOffset", 0) + starts[bv.get("buffer", 0)]
        bv["buffer"] = 0
    for img in doc.get("images", []):
        if "uri" not in img:
            continue
        data = _load_uri(img.pop("uri"), path.parent, root)
        blob += b"\0" * (-len(blob) % 8)
        doc.setdefault("bufferViews", []).append({"buffer": 0, "byteOffset": len(blob), "byteLength": len(data)})
        img["bufferView"] = len(doc["bufferViews"]) - 1
        img.setdefault("mimeType", "image/jpeg" if data[:3] == b"\xff\xd8\xff" else "image/png")
        blob += data
    doc["buffers"] = [{"byteLength": len(blob)}] if blob else []
    return write_glb(doc, bytes(blob))


def wrap_scale(data: bytes, scale: float, lift_to_floor: float | None = None) -> bytes:
    """Put every scene root under one node scaled by `scale` (and moved up by lift_to_floor metres after scaling)."""
    doc, bin_ = read_glb(data)
    sc = doc.get("scene", 0)
    scenes = doc.get("scenes") or [{"nodes": list(range(len(doc.get("nodes", []))))}]
    roots = scenes[sc].get("nodes", [])
    node = {"name": "scaled", "children": roots, "scale": [scale] * 3}
    if lift_to_floor:
        node["translation"] = [0.0, lift_to_floor, 0.0]
    doc.setdefault("nodes", []).append(node)
    scenes[sc]["nodes"] = [len(doc["nodes"]) - 1]
    doc["scenes"] = scenes
    return write_glb(doc, bin_)


def split_to_gltf(data: bytes, folder: Path, name: str) -> Path:
    """Write <folder>/<name>.gltf + <name>.bin + textures/ (the layout of the stages' Poly Haven props)."""
    doc, bin_ = read_glb(data)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "textures").mkdir(exist_ok=True)
    keep = bytearray()
    remap: dict[int, int] = {}
    image_views = {img["bufferView"]: i for i, img in enumerate(doc.get("images", [])) if "bufferView" in img}
    views = doc.get("bufferViews", [])
    new_views = []
    for i, bv in enumerate(views):
        chunk = bin_[bv.get("byteOffset", 0): bv.get("byteOffset", 0) + bv["byteLength"]]
        if i in image_views:
            img = doc["images"][image_views[i]]
            ext = mimetypes.guess_extension(img.get("mimeType", "image/png")) or ".png"
            fname = f"textures/{name}_{image_views[i]}{'.jpg' if ext in ('.jpe', '.jpeg') else ext}"
            (folder / fname).write_bytes(chunk)
            img.pop("bufferView", None)
            img.pop("mimeType", None)
            img["uri"] = fname
            continue
        keep += b"\0" * (-len(keep) % 4)
        remap[i] = len(new_views)
        new_views.append({**bv, "buffer": 0, "byteOffset": len(keep)})
        keep += chunk
    doc["bufferViews"] = new_views
    for acc in doc.get("accessors", []):
        if "bufferView" in acc:
            acc["bufferView"] = remap[acc["bufferView"]]
        sp = acc.get("sparse")
        if sp:
            sp["indices"]["bufferView"] = remap[sp["indices"]["bufferView"]]
            sp["values"]["bufferView"] = remap[sp["values"]["bufferView"]]
    for m in doc.get("meshes", []):   # Draco and similar extensions keep bufferView references too
        for prim in m.get("primitives", []):
            for ext in (prim.get("extensions") or {}).values():
                if isinstance(ext, dict) and "bufferView" in ext:
                    ext["bufferView"] = remap[ext["bufferView"]]
    doc["buffers"] = [{"uri": f"{name}.bin", "byteLength": len(keep)}] if keep else []
    (folder / f"{name}.bin").write_bytes(bytes(keep))
    out = folder / f"{name}.gltf"
    out.write_text(json.dumps(doc, indent=1), encoding="utf-8")
    return out


def summary(data: bytes) -> dict[str, Any]:
    doc, _ = read_glb(data)
    return {"meshes": len(doc.get("meshes", [])), "skins": len(doc.get("skins", [])), "animations": [a.get("name") for a in doc.get("animations", [])],
            "images": len(doc.get("images", [])), "bones": sorted({doc["nodes"][j].get("name", "") for s in doc.get("skins", []) for j in s.get("joints", [])})}


def shrink_textures(data: bytes, max_px: int = 1024, jpeg_quality: int = 88) -> bytes:
    """Scale every embedded image down to at most max_px on its longest side (PNG stays PNG, so alpha survives;
    big opaque PNGs become JPEG). Needs Pillow."""
    import io

    from PIL import Image

    doc, bin_ = read_glb(data)
    imgs = [i for i in doc.get("images", []) if "bufferView" in i]
    if not imgs:
        return data
    blob = bytearray()
    views = doc.get("bufferViews", [])
    image_view = {i["bufferView"]: i for i in imgs}
    new_views = []
    for k, bv in enumerate(views):
        chunk = bin_[bv.get("byteOffset", 0): bv.get("byteOffset", 0) + bv["byteLength"]]
        if k in image_view:
            img = image_view[k]
            try:
                im = Image.open(io.BytesIO(chunk))
                im.load()
            except Exception:   # unreadable image: keep it as it is
                im = None
            if im is not None and max(im.size) > max_px:
                im.thumbnail((max_px, max_px), Image.LANCZOS)
                buf = io.BytesIO()
                opaque = im.mode in ("RGB", "L") or (im.mode in ("RGBA", "LA") and im.getchannel("A").getextrema()[0] == 255)
                if opaque:
                    im.convert("RGB").save(buf, "JPEG", quality=jpeg_quality)
                    img["mimeType"] = "image/jpeg"
                else:
                    im.save(buf, "PNG", optimize=True)
                    img["mimeType"] = "image/png"
                chunk = buf.getvalue()
        blob += b"\0" * (-len(blob) % 4)
        new_views.append({**bv, "buffer": 0, "byteOffset": len(blob), "byteLength": len(chunk)})
        blob += chunk
    doc["bufferViews"] = new_views
    return write_glb(doc, bytes(blob))
