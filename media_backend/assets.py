"""Asset finder: free, properly licensed assets for builds, pulled into Builder's library.

- asset_search: search Poly Haven (HDRI skies, textures, 3D models; CC0), ambientCG (materials;
  CC0), Openverse (Creative Commons images and sounds; commercial-use licences by default) and,
  with FREESOUND_API_KEY set, Freesound (sound effects).
- asset_import: download one result into the library, with its licence and credit line saved in a
  license.json beside the file.
- page_references: collect the images on one web page (for mood boards and look references).

Uses each library's public API (no scraping) and fetches public http(s) addresses only.
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
import shutil
import socket
import tempfile
import time
import urllib.parse
import urllib.request
import zipfile
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from . import storage
from .ops import OpError, OpResult, _guarded, operation

UA = "Builder-asset-finder/0.3"
MAX_BYTES = 600 * 1024 * 1024
SOURCES = ("polyhaven", "ambientcg", "openverse", "freesound")
CC0 = {"license": "CC0 1.0", "license_url": "https://creativecommons.org/publicdomain/zero/1.0/"}


# ---------------------------------------------------------------- network (public addresses only)


def _check_public(url: str) -> None:
    u = urllib.parse.urlparse(url)
    if u.scheme not in ("http", "https") or not u.hostname:
        raise OpError(f"Only http(s) web addresses can be fetched: {url}")
    try:
        infos = socket.getaddrinfo(u.hostname, None)
    except socket.gaierror as e:
        raise OpError(f"Can't find {u.hostname}") from e
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split("%")[0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast or ip.is_unspecified:
            raise OpError(f"{u.hostname} is a private or local address; only public sites can be fetched")


def _open(url: str, timeout: int = 30):
    _check_public(url)
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    try:
        return urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as e:
        raise OpError(f"{urllib.parse.urlparse(url).hostname} answered {e.code} for {url}") from e
    except (urllib.error.URLError, TimeoutError) as e:
        raise OpError(f"Couldn't reach {urllib.parse.urlparse(url).hostname}: {getattr(e, 'reason', e)}") from e


def _get_json(url: str, headers: dict[str, str] | None = None) -> Any:
    if headers:
        _check_public(url)
        req = urllib.request.Request(url, headers={"User-Agent": UA, **headers})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            raise OpError(f"{urllib.parse.urlparse(url).hostname} answered {e.code}") from e
    with _open(url) as r:
        return json.loads(r.read())


def _get_text(url: str, limit: int = 3_000_000) -> tuple[str, str]:
    with _open(url) as r:
        ctype = r.headers.get("Content-Type", "")
        raw = r.read(limit)
        final = r.geturl()
    charset = re.search(r"charset=([\w-]+)", ctype)
    return raw.decode(charset.group(1) if charset else "utf-8", errors="replace"), final


def _download(url: str, dest: Path) -> None:
    with _open(url, timeout=120) as r, open(dest, "wb") as f:
        total = 0
        while chunk := r.read(1 << 20):
            total += len(chunk)
            if total > MAX_BYTES:
                raise OpError(f"{url} is larger than {MAX_BYTES >> 20} MB")
            f.write(chunk)


# ---------------------------------------------------------------- search


def _match(q: str, *texts: Any) -> int:
    words = [w for w in re.findall(r"\w+", q.lower()) if len(w) > 1]
    hay = " ".join(str(t).lower() for t in texts)
    return sum(1 for w in words if w in hay) if words else 1


_PH_TYPES = {"hdri": "hdris", "texture": "textures", "model": "models"}
_ph_cache: dict[str, tuple[float, dict]] = {}


def _polyhaven(q: str, kind: str, limit: int) -> list[dict[str, Any]]:
    kinds = [kind] if kind in _PH_TYPES else ([] if kind in ("image", "sound") else list(_PH_TYPES))
    out = []
    for k in kinds:
        t = _PH_TYPES[k]
        cached = _ph_cache.get(t)
        if not cached or time.time() - cached[0] > 3600:
            cached = (time.time(), _get_json(f"https://api.polyhaven.com/assets?t={t}"))
            _ph_cache[t] = cached
        for aid, a in cached[1].items():
            score = _match(q, aid, a.get("name"), a.get("categories"), a.get("tags"))
            if score:
                out.append((score, a.get("download_count", 0), {
                    "source": "polyhaven", "id": aid, "kind": k, "title": a.get("name", aid),
                    "preview": f"https://cdn.polyhaven.com/asset_img/thumbs/{aid}.png?width=256",
                    "page": f"https://polyhaven.com/a/{aid}", "creator": ", ".join(a.get("authors", {})), **CC0,
                    "options": {"resolution": ["1k", "2k", "4k", "8k"]}}))
    out.sort(key=lambda x: (-x[0], -x[1]))
    return [r for *_, r in out[:limit]]


def _ambientcg(q: str, kind: str, limit: int) -> list[dict[str, Any]]:
    if kind not in ("any", "texture"):
        return []
    url = "https://ambientcg.com/api/v2/full_json?" + urllib.parse.urlencode(
        {"q": q, "limit": limit, "type": "Material", "include": "downloadData,imageData,tagData", "sort": "Popular"})
    out = []
    for a in _get_json(url).get("foundAssets", []):
        downloads = a.get("downloadFolders", {}).get("default", {}).get("downloadFiletypeCategories", {}).get("zip", {}).get("downloads", [])
        out.append({"source": "ambientcg", "id": a["assetId"], "kind": "texture", "title": a.get("displayName") or a["assetId"],
                    "preview": (a.get("previewImage") or {}).get("256-PNG") or (a.get("previewImage") or {}).get("128-PNG"),
                    "page": f"https://ambientcg.com/view?id={a['assetId']}", "creator": "ambientCG", **CC0,
                    "options": {"resolution": [d["attribute"] for d in downloads]}})
    return out


def _openverse(q: str, kind: str, limit: int, commercial: bool) -> list[dict[str, Any]]:
    kinds = {"image": "images", "sound": "audio"}
    targets = [kinds[kind]] if kind in kinds else (["images", "audio"] if kind == "any" else [])
    out = []
    for t in targets:
        params = {"q": q, "page_size": min(limit, 20)}
        if commercial:
            params["license_type"] = "commercial"
        for r in _get_json(f"https://api.openverse.org/v1/{t}/?" + urllib.parse.urlencode(params)).get("results", []):
            out.append({"source": "openverse", "id": r["id"], "kind": "image" if t == "images" else "sound",
                        "title": r.get("title") or r["id"], "preview": r.get("thumbnail") or r.get("url"),
                        "page": r.get("foreign_landing_url"), "creator": r.get("creator"),
                        "license": f"CC {r.get('license', '').upper()} {r.get('license_version', '')}".strip() if r.get("license") not in ("cc0", "pdm") else ("CC0 1.0" if r.get("license") == "cc0" else "Public Domain Mark"),
                        "license_url": r.get("license_url"), "attribution": r.get("attribution"), "url": r.get("url")})
    return out


def _freesound(q: str, kind: str, limit: int) -> list[dict[str, Any]]:
    key = os.environ.get("FREESOUND_API_KEY", "")
    if not key or kind not in ("any", "sound"):
        return []
    url = "https://freesound.org/apiv2/search/text/?" + urllib.parse.urlencode(
        {"query": q, "page_size": min(limit, 20), "fields": "id,name,username,license,previews,url,duration", "token": key})
    out = []
    for r in _get_json(url).get("results", []):
        out.append({"source": "freesound", "id": str(r["id"]), "kind": "sound", "title": r.get("name"), "creator": r.get("username"),
                    "license": r.get("license"), "license_url": r.get("license"), "page": r.get("url"),
                    "preview": (r.get("previews") or {}).get("preview-hq-mp3"), "duration_s": r.get("duration")})
    return out


class AssetSearchParams(BaseModel):
    query: str = Field(min_length=1, max_length=200, description="What you need, e.g. 'sunset sky', 'brick wall', 'wooden chair', 'rain'")
    kind: Literal["any", "hdri", "texture", "model", "image", "sound"] = Field("any", description="hdri = sky/studio lighting; texture = PBR material; model = 3D model; image = photo/illustration; sound = audio")
    source: Literal["any", "polyhaven", "ambientcg", "openverse", "freesound"] = "any"
    limit: int = Field(12, ge=1, le=40)
    commercial_only: bool = Field(True, description="Only licences that allow commercial use (CC0, CC BY, CC BY-SA)")


@operation("asset_search", AssetSearchParams, "Find free, licensed assets for a build: HDRI skies, PBR textures and 3D models (Poly Haven, ambientCG, CC0), Creative Commons images and sounds (Openverse), sound effects (Freesound, with a key). Returns ids to pass to asset_import, with licence and credit.", read_only=True)
def asset_search(p: AssetSearchParams) -> OpResult:
    results, errors = [], []
    plan = [s for s in SOURCES if p.source in ("any", s)]
    for s in plan:
        try:
            if s == "polyhaven":
                results += _polyhaven(p.query, p.kind, p.limit)
            elif s == "ambientcg":
                results += _ambientcg(p.query, p.kind, p.limit)
            elif s == "openverse":
                results += _openverse(p.query, p.kind, p.limit, p.commercial_only)
            elif s == "freesound":
                results += _freesound(p.query, p.kind, p.limit)
        except OpError as e:
            errors.append(f"{s}: {e}")
    note = "" if os.environ.get("FREESOUND_API_KEY") else "Freesound is off (set FREESOUND_API_KEY, free at freesound.org/apiv2/apply)."
    return OpResult(data={"results": results[: p.limit * 2], "errors": errors, "note": note})


# ---------------------------------------------------------------- import


def _save_license(file_id: str, info: dict[str, Any]) -> None:
    (storage.path_of(file_id).parent / "license.json").write_text(json.dumps(info, indent=2), encoding="utf-8")


def license_of(file_id: str) -> dict[str, Any] | None:
    try:
        return json.loads((storage.path_of(file_id).parent / "license.json").read_text(encoding="utf-8"))
    except (OSError, ValueError, storage.FileNotFound):
        return None


def _ext_from(url: str, default: str) -> str:
    ext = Path(urllib.parse.urlparse(url).path).suffix.lower()
    return ext if re.fullmatch(r"\.[a-z0-9]{2,5}", ext or "") else default


class AssetImportParams(BaseModel):
    source: Literal["polyhaven", "ambientcg", "openverse", "freesound", "url"] = Field(description="Where the asset is from (as asset_search returned it), or url for a direct link")
    asset_id: str | None = Field(None, max_length=200, description="The result's id (Poly Haven, ambientCG, Openverse, Freesound)")
    url: str | None = Field(None, max_length=2000, description="With source=url: a direct link to an image, sound or video on a public site")
    kind: Literal["hdri", "texture", "model", "image", "sound"] | None = Field(None, description="Poly Haven: which kind the id is (from asset_search)")
    resolution: str = Field("2k", max_length=12, description="Poly Haven: 1k/2k/4k/8k; ambientCG: e.g. 2K-JPG, 4K-PNG")
    license_note: str | None = Field(None, max_length=500, description="With source=url: the licence/credit you found on the page")


@operation("asset_import", AssetImportParams, "Download an asset found with asset_search (or a direct link) into Builder's library, with its licence and credit line saved beside it. HDRIs come as .hdr, Poly Haven textures as a zip of PBR maps, models as a zip of glTF + textures.")
def asset_import(p: AssetImportParams) -> OpResult:
    work = Path(tempfile.mkdtemp(prefix="asset-"))
    try:
        return _import(p, work)
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _register(src: Path, name: str, lic: dict[str, Any]) -> OpResult:
    res = _guarded("asset_import", [], name, lambda out: shutil.copyfile(src, out), data=lic)
    _save_license(res.outputs[0].id, lic)
    return res


def _zip_dir(folder: Path, dest: Path) -> None:
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as z:
        for f in sorted(folder.rglob("*")):
            if f.is_file():
                z.write(f, f.relative_to(folder))


def _import(p: AssetImportParams, work: Path) -> OpResult:
    if p.source == "url":
        if not p.url:
            raise OpError("Give url with source=url")
        f = work / ("download" + _ext_from(p.url, ".bin"))
        _download(p.url, f)
        name = storage.safe_name(Path(urllib.parse.urlparse(p.url).path).name or f.name)
        lic = {"source": "url", "page": p.url, "license": p.license_note or "unknown: check the page before using it",
               "attribution": p.license_note or ""}
        return _register(f, name, lic)
    if not p.asset_id:
        raise OpError("Give asset_id (from asset_search)")
    if p.source == "polyhaven":
        files = _get_json(f"https://api.polyhaven.com/files/{urllib.parse.quote(p.asset_id)}")
        res = p.resolution.lower()
        authors = ", ".join(_get_json(f"https://api.polyhaven.com/info/{urllib.parse.quote(p.asset_id)}").get("authors", {}))
        lic = {"source": "polyhaven", "id": p.asset_id, "page": f"https://polyhaven.com/a/{p.asset_id}", "creator": authors, **CC0,
               "attribution": f"{p.asset_id} by {authors} (Poly Haven), CC0"}
        if "hdri" in files:
            pick = files["hdri"].get(res) or next(iter(files["hdri"].values()))
            f = work / f"{p.asset_id}_{res}.hdr"
            _download(pick["hdr"]["url"], f)
            return _register(f, f.name, {**lic, "kind": "hdri"})
        if "gltf" in files:
            g = files["gltf"].get(res) or next(iter(files["gltf"].values()))
            folder = work / p.asset_id
            main = g["gltf"]
            folder.mkdir()
            _download(main["url"], folder / Path(urllib.parse.urlparse(main["url"]).path).name)
            for rel, inc in (main.get("include") or {}).items():
                dest = folder / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                _download(inc["url"], dest)
            kind = "model" if p.kind == "model" or "Diffuse" not in files else "texture"
            z = work / f"{p.asset_id}_{res}.zip"
            _zip_dir(folder, z)
            return _register(z, z.name, {**lic, "kind": kind, "contents": "glTF + textures"})
        raise OpError(f"Poly Haven has no downloadable files for {p.asset_id}")
    if p.source == "ambientcg":
        data = _get_json("https://ambientcg.com/api/v2/full_json?" + urllib.parse.urlencode({"id": p.asset_id, "include": "downloadData"}))
        assets = data.get("foundAssets") or []
        if not assets:
            raise OpError(f"ambientCG has no asset {p.asset_id}")
        downloads = assets[0]["downloadFolders"]["default"]["downloadFiletypeCategories"]["zip"]["downloads"]
        want = p.resolution.upper() if "-" in p.resolution else f"{p.resolution.upper()}-JPG"
        pick = next((d for d in downloads if d["attribute"] == want), downloads[0])
        f = work / pick["fileName"]
        _download(pick["downloadLink"], f)
        return _register(f, f.name, {"source": "ambientcg", "id": p.asset_id, "page": f"https://ambientcg.com/view?id={p.asset_id}",
                                     "creator": "ambientCG", **CC0, "kind": "texture", "attribution": f"{p.asset_id} from ambientCG.com, CC0"})
    if p.source == "openverse":
        meta = None
        for t in ("images", "audio"):
            try:
                meta = _get_json(f"https://api.openverse.org/v1/{t}/{urllib.parse.quote(p.asset_id)}/")
                break
            except OpError:
                continue
        if not meta:
            raise OpError(f"Openverse has no item {p.asset_id}")
        url = meta["url"]
        f = work / ("item" + _ext_from(url, ".jpg"))
        _download(url, f)
        name = storage.safe_name((meta.get("title") or p.asset_id)[:60]) + f.suffix
        return _register(f, name, {"source": "openverse", "id": p.asset_id, "page": meta.get("foreign_landing_url"),
                                   "creator": meta.get("creator"), "license": f"CC {meta.get('license', '').upper()} {meta.get('license_version', '')}".strip(),
                                   "license_url": meta.get("license_url"), "attribution": meta.get("attribution")})
    if p.source == "freesound":
        key = os.environ.get("FREESOUND_API_KEY", "")
        if not key:
            raise OpError("Set FREESOUND_API_KEY in builder.env to use Freesound (free key at freesound.org/apiv2/apply)")
        meta = _get_json(f"https://freesound.org/apiv2/sounds/{urllib.parse.quote(p.asset_id)}/?" + urllib.parse.urlencode({"token": key}))
        url = (meta.get("previews") or {}).get("preview-hq-mp3")
        if not url:
            raise OpError("This sound has no downloadable preview")
        f = work / f"freesound_{p.asset_id}.mp3"
        _download(url, f)
        return _register(f, storage.safe_name(meta.get("name", p.asset_id))[:60] + ".mp3",
                         {"source": "freesound", "id": p.asset_id, "page": meta.get("url"), "creator": meta.get("username"),
                          "license": meta.get("license"), "license_url": meta.get("license"),
                          "attribution": f"\"{meta.get('name')}\" by {meta.get('username')} (freesound.org), {meta.get('license')}",
                          "note": "High-quality preview (MP3). The original file needs a Freesound login."})
    raise OpError(f"Unknown source {p.source}")


# ---------------------------------------------------------------- page references


class _Imgs(HTMLParser):
    def __init__(self):
        super().__init__()
        self.found: list[str] = []
        self.title = ""
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "img":
            src = a.get("src") or a.get("data-src")
            if a.get("srcset"):
                src = a["srcset"].split(",")[-1].strip().split(" ")[0] or src
            if src:
                self.found.append(src)
        elif tag == "meta" and (a.get("property") in ("og:image", "twitter:image") or a.get("name") in ("og:image", "twitter:image")):
            if a.get("content"):
                self.found.insert(0, a["content"])
        elif tag == "title":
            self._in_title = True

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False

    def handle_data(self, data):
        if self._in_title:
            self.title += data


class PageRefsParams(BaseModel):
    url: str = Field(max_length=2000, description="A public web page to collect reference images from")
    limit: int = Field(30, ge=1, le=100)


@operation("page_references", PageRefsParams, "Collect the images on one web page (title, share image and pictures) as references for a build's look. Import any with asset_import source=url; check each image's licence on the page first.", read_only=True)
def page_references(p: PageRefsParams) -> OpResult:
    html, final = _get_text(p.url)
    parser = _Imgs()
    parser.feed(html)
    seen, images = set(), []
    for src in parser.found:
        if src.startswith("data:"):
            continue
        absu = urllib.parse.urljoin(final, src)
        if absu in seen or not absu.startswith(("http://", "https://")):
            continue
        if re.search(r"(sprite|pixel|spacer|tracking|1x1|logo)\b", absu, re.I):
            continue
        seen.add(absu)
        images.append(absu)
    return OpResult(data={"url": final, "title": parser.title.strip(), "images": images[: p.limit],
                          "note": "Images on web pages usually belong to their owners: use them as references, or import only ones whose licence allows it."})
