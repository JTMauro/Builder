"""Game-asset packs for asset_search / asset_import: Kenney (kenney.nl, CC0), Quaternius (quaternius.com, CC0)
and Poly Pizza (poly.pizza, CC0 and CC BY models, many of them Kenney's and Quaternius's).

How each one is fetched (checked November 2026):
- Kenney: no API. A built-in list of the 3D packs (title + keywords) is searched; a pack is downloaded with the
  site's own download button flow (https://kenney.nl/download-link/<hex of "assets/<slug>"> answers a signed
  link to the pack's zip; it needs the pack page as Referer). Packs ship GLB, FBX and OBJ; GLB is used.
- Quaternius: the packs are on Google Drive (anonymous downloads answer "quota exceeded" from servers) and
  itch.io (a purchase-style flow). Quaternius uploads the same packs to OpenGameArt (CC0) as plain zip files,
  so search lists Quaternius's 3D uploads there and import downloads the zip from the item's page, after
  checking the page names Quaternius as the author and CC0 as the licence. Older packs are FBX/OBJ only:
  those are converted to GLB with Blender.
- Poly Pizza: an official API (https://api.poly.pizza/v1.1, header x-auth-token) with a free key from a
  poly.pizza account (POLY_PIZZA_API_KEY). Each model has its own licence (CC0 or CC BY 3.0: credit the
  creator). Its file CDN (static.poly.pizza) blocks many cloud/datacenter addresses; then download the .glb by
  hand from the model page and use asset_import source=url.

A pack is downloaded once into the data folder (cache/asset_packs) and every later import picks single
models out of it by name (item="barrel", or several: "sword, shield_round").
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import urllib.parse
import zipfile
from pathlib import Path
from typing import Any

from .config import NO_WINDOW, get_settings
from .ops import OpError, OpResult

CC0 = {"license": "CC0 1.0", "license_url": "https://creativecommons.org/publicdomain/zero/1.0/"}
CONVERT = Path(__file__).resolve().parents[1] / "tools" / "model3d" / "convert.py"
MODEL_EXT = (".glb", ".gltf", ".fbx", ".obj")
MAX_UNZIP = 1500 * 1024 * 1024

# Kenney's 3D packs (kenney.nl/assets/<slug>). Keywords list some of the models inside each pack.
KENNEY_PACKS: dict[str, tuple[str, str]] = {
    "mini-arena": ("Mini Arena", "arena colosseum gladiator column ruins banner weapon rack spear sword statue trophy wall gate stairs soldier character"),
    "castle-kit": ("Castle Kit", "castle tower wall gate flag banner pennant siege catapult trebuchet ballista ram bridge rocks tree medieval"),
    "mini-characters": ("Mini Characters", "character people knight soldier worker raider zombie robot alien male female animated rigged shield sword bow spear wheelchair luggage"),
    "mini-dungeon": ("Mini Dungeon", "dungeon barrel chest crate column banner torch trap gate wall floor stairs character weapon"),
    "retro-fantasy-kit": ("Retro Fantasy Kit", "fantasy castle tower wall roof medieval village"),
    "fantasy-town-kit": ("Fantasy Town Kit", "town medieval house roof wall fence cart wheel barrel lantern market stall fountain tree"),
    "graveyard-kit": ("Graveyard Kit", "graveyard cemetery grave tombstone coffin fence crypt lantern pumpkin skeleton ghost zombie candle"),
    "pirate-kit": ("Pirate Kit", "pirate ship boat cannon barrel crate chest palm tree flag dock rocks"),
    "survival-kit": ("Survival Kit", "survival camp tent campfire barrel crate box tools axe bucket chest fence log rock tree workbench"),
    "furniture-kit": ("Furniture Kit", "furniture chair table sofa bed lamp cabinet desk kitchen bathroom shelf tv plant"),
    "food-kit": ("Food Kit", "food fruit bread cheese meat drink cup plate pizza cake"),
    "nature-kit": ("Nature Kit", "nature tree rock stone bush flower grass mushroom log cliff path tent campfire"),
    "holiday-kit": ("Holiday Kit", "christmas holiday present gift tree snowman candy lights sled"),
    "blaster-kit": ("Blaster Kit", "blaster gun weapon sci-fi toy"),
    "space-kit": ("Space Kit", "space rocket satellite planet base astronaut alien rover"),
    "space-station-kit": ("Space Station Kit", "space station corridor room sci-fi"),
    "city-kit-commercial": ("City Kit (Commercial)", "city building skyscraper shop office"),
    "city-kit-suburban": ("City Kit (Suburban)", "city house suburban home fence tree driveway"),
    "city-kit-roads": ("City Kit (Roads)", "city road street crossing bridge light sign"),
    "city-kit-industrial": ("City Kit (Industrial)", "city factory industrial chimney warehouse"),
    "car-kit": ("Car Kit", "car vehicle truck van taxi police ambulance firetruck tractor"),
    "toy-car-kit": ("Toy Car Kit", "toy car race track"),
    "racing-kit": ("Racing Kit", "racing race car track barrier tire grandstand flag"),
    "train-kit": ("Train Kit", "train rail locomotive wagon station"),
    "watercraft-kit": ("Watercraft Kit", "boat ship watercraft canoe sailboat"),
    "platformer-kit": ("Platformer Kit", "platformer block coin flag spike tree character"),
    "prototype-kit": ("Prototype Kit", "prototype grey box door wall stairs figure"),
    "tower-defense-kit": ("Tower Defense Kit", "tower defense path enemy cannon ballista"),
    "factory-kit": ("Factory Kit", "factory conveyor machine robot arm box"),
    "modular-dungeon-kit": ("Modular Dungeon Kit", "dungeon modular wall floor corridor room gate"),
    "modular-space-kit": ("Modular Space Kit", "space modular corridor room"),
    "modular-buildings": ("Modular Buildings", "building modular wall window door roof"),
    "building-kit": ("Building Kit", "building wall window door roof stairs"),
    "brick-kit": ("Brick Kit", "brick toy lego block"),
    "marble-kit": ("Marble Kit", "marble run track ball"),
    "minigolf-kit": ("Minigolf Kit", "minigolf golf course hole flag windmill"),
    "coaster-kit": ("Coaster Kit", "roller coaster track cart amusement park"),
    "hexagon-kit": ("Hexagon Kit", "hexagon tile board game terrain"),
    "cube-pets": ("Cube Pets", "pet animal cat dog cube"),
    "blocky-characters": ("Blocky Characters", "character blocky people animated rigged"),
    "animated-characters-protagonists": ("Animated Characters: Protagonists", "character people animated rigged hero"),
    "animated-characters-survivors": ("Animated Characters: Survivors", "character people animated rigged survivor zombie"),
    "animated-characters-retro": ("Animated Characters: Retro", "character people animated rigged retro"),
    "mini-shop": ("Mini Shop", "shop market stall counter shelf"),
    "mini-skate": ("Mini Skate", "skate skateboard ramp park"),
    "mini-arcade": ("Mini Arcade", "arcade machine game cabinet"),
    "retro-urban-kit": ("Retro Urban Kit", "urban city street building retro"),
}

# Extra words for Quaternius's packs on OpenGameArt (their titles are short).
QUATERNIUS_HINTS: dict[str, str] = {
    "lowpoly-medieval-weapons": "sword shield axe bow arrow spear dagger hammer claymore scythe weapon gladiator",
    "fantasy-props-megakit": "barrel crate chest banner torch lantern candle bucket anvil table chair bench cart stall cage dummy weapon rack shield axe coin potion book",
    "medieval-village-megakit": "medieval village house wall roof door window fence cart well market",
    "lowpoly-rpg-characters": "character rigged animated warrior rogue ranger cleric monk wizard sword staff bow",
    "lowpoly-animated-knight": "knight character rigged animated armour sword",
    "animated-characters-pack": "character people rigged animated man woman",
    "lowpoly-posed-humans": "human people posed statue crowd background",
    "lowpoly-rpg": "rpg chest barrel crate sword shield potion",
    "low-poly-rpg-pack": "rpg chest barrel crate sword shield potion",
    "lowpoly-medieval-village-pack": "medieval village house cart barrel well",
    "lowpoly-survival-pack": "survival barrel crate axe tent campfire",
    "universal-animation-library": "animation retarget humanoid mannequin rigged walk run idle",
    "universal-animation-library-2": "animation retarget humanoid mannequin rigged",
    "lowpoly-modular-dungeon-pack": "dungeon wall column torch gate stairs barrel",
    "lowpoly-modular-dungeon": "dungeon wall column torch gate stairs",
}

OGA_LIST = ("https://opengameart.org/art-search-advanced?" + urllib.parse.urlencode(
    {"keys": "", "name": "quaternius", "field_art_type_tid[]": "10", "sort_by": "created", "sort_order": "DESC", "items_per_page": "144"}))
_oga_cache: dict[str, tuple[float, list[tuple[str, str]]]] = {}


def _a():   # assets module (late import: assets imports this module inside functions too)
    from . import assets

    return assets


def cache_dir() -> Path:
    d = Path(os.environ.get("BUILDER_PACK_CACHE") or (get_settings().data_dir / "cache" / "asset_packs")).expanduser()
    d.mkdir(parents=True, exist_ok=True)
    return d


def _known_items(source: str, slug: str) -> list[str]:
    f = cache_dir() / source / slug / "items.json"
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []


# ---------------------------------------------------------------- search


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


def _item_hits(q: str, items: list[str]) -> list[str]:
    words = [w for w in _norm(q).split() if len(w) > 1]
    return [i for i in items if any(w in _norm(i) for w in words)][:30]


def kenney_search(q: str, kind: str, limit: int) -> list[dict[str, Any]]:
    if kind not in ("any", "model"):
        return []
    out = []
    for slug, (title, words) in KENNEY_PACKS.items():
        items = _known_items("kenney", slug)
        hits = _item_hits(q, items)
        score = _a()._match(q, slug, title, words) + (1 if hits else 0) + len(hits) * 0.01
        if score >= 1:
            out.append((score, {"source": "kenney", "id": slug, "kind": "model", "title": f"{title} (Kenney pack)",
                                "preview": None, "page": f"https://kenney.nl/assets/{slug}", "creator": "Kenney", **CC0,
                                "options": {"item": hits or "a model name inside the pack, e.g. " + words.split()[0]},
                                "note": "A pack: asset_import with item= picks single models (GLB) out of it."}))
    out.sort(key=lambda x: -x[0])
    return [r for _, r in out[:limit]]


def _oga_listing() -> list[tuple[str, str]]:
    cached = _oga_cache.get("quaternius")
    if cached and time.time() - cached[0] < 3600:
        return cached[1]
    html, _ = _a()._get_text(OGA_LIST)
    found = re.findall(r'art-preview-title"><a href="/content/([a-z0-9-]+)">([^<]+)<', html)
    items = list(dict.fromkeys((s, t.strip()) for s, t in found))
    _oga_cache["quaternius"] = (time.time(), items)
    return items


def quaternius_search(q: str, kind: str, limit: int) -> list[dict[str, Any]]:
    if kind not in ("any", "model"):
        return []
    out = []
    for slug, title in _oga_listing():
        items = _known_items("quaternius", slug)
        hits = _item_hits(q, items)
        score = _a()._match(q, slug, title, QUATERNIUS_HINTS.get(slug, "")) + (1 if hits else 0) + len(hits) * 0.01
        if score >= 1:
            out.append((score, {"source": "quaternius", "id": slug, "kind": "model", "title": f"{title} (Quaternius pack)",
                                "preview": None, "page": f"https://opengameart.org/content/{slug}", "creator": "Quaternius", **CC0,
                                "options": {"item": hits or "a model name inside the pack"},
                                "note": "A pack (Quaternius's own OpenGameArt upload): asset_import with item= picks single models (GLB)."}))
    out.sort(key=lambda x: -x[0])
    return [r for _, r in out[:limit]]


PP_API = "https://api.poly.pizza/v1.1"


def _pp_key() -> str:
    return os.environ.get("POLY_PIZZA_API_KEY", "")


def _pp_licence(lic: str | None) -> dict[str, str]:
    s = str(lic or "")
    if "0" in s and "CC0" in s.upper().replace(" ", ""):
        return dict(CC0)
    return {"license": "CC BY 3.0", "license_url": "https://creativecommons.org/licenses/by/3.0/"}


def polypizza_search(q: str, kind: str, limit: int) -> list[dict[str, Any]]:
    key = _pp_key()
    if not key or kind not in ("any", "model"):
        return []
    url = f"{PP_API}/search/{urllib.parse.quote(q, safe='')}?" + urllib.parse.urlencode({"Limit": min(limit, 32)})
    out = []
    for m in (_a()._get_json(url, headers={"x-auth-token": key}) or {}).get("results", []):
        creator = (m.get("Creator") or {}).get("Username") if isinstance(m.get("Creator"), dict) else m.get("Creator")
        out.append({"source": "polypizza", "id": str(m.get("ID")), "kind": "model", "title": m.get("Title"),
                    "preview": m.get("Thumbnail"), "page": f"https://poly.pizza/m/{m.get('ID')}", "creator": creator,
                    **_pp_licence(m.get("Licence")), "attribution": m.get("Attribution"),
                    "animated": bool(m.get("Animated")), "triangles": m.get("Tri Count")})
    return out


# ---------------------------------------------------------------- packs: download once, pick models


def _safe_extract(zpath: Path, dest: Path) -> None:
    with zipfile.ZipFile(zpath) as z:
        total = 0
        for info in z.infolist():
            total += info.file_size
            if total > MAX_UNZIP:
                raise OpError("the pack unpacks to more than 1.5 GB")
            name = info.filename.replace("\\", "/")
            if name.startswith("/") or ".." in Path(name).parts or info.is_dir():
                continue
            if name.startswith("__MACOSX/"):
                continue
            target = dest / name
            target.parent.mkdir(parents=True, exist_ok=True)
            with z.open(info) as src, open(target, "wb") as f:
                shutil.copyfileobj(src, f)


def _models_in(folder: Path) -> dict[str, Path]:
    """Model name -> best file (GLB, then glTF, FBX, OBJ); skips previews and Blender sources."""
    best: dict[str, Path] = {}
    for f in sorted(folder.rglob("*")):
        if f.suffix.lower() not in MODEL_EXT or not f.is_file():
            continue
        if re.search(r"preview|blends?/", str(f.relative_to(folder)).lower()):
            continue
        k = f.stem
        cur = best.get(k)
        if cur is None or MODEL_EXT.index(f.suffix.lower()) < MODEL_EXT.index(cur.suffix.lower()):
            best[k] = f
    return best


def _kenney_zip(slug: str, dest: Path) -> dict[str, Any]:
    if slug not in KENNEY_PACKS:
        raise OpError(f"unknown Kenney pack {slug!r} (asset_search source=kenney lists them)")
    hexid = ("assets/" + slug).encode().hex()
    page = f"https://kenney.nl/assets/{slug}"
    link = _a()._get_json(f"https://kenney.nl/download-link/{hexid}", headers={"Referer": page}).get("url", "")
    if not link.startswith("https://kenney.nl/"):
        raise OpError("kenney.nl didn't give a download link for that pack")
    _a()._download(link, dest)
    return {"page": page, "creator": "Kenney (www.kenney.nl)", "attribution": f"{KENNEY_PACKS[slug][0]} by Kenney (www.kenney.nl), CC0"}


def _quaternius_zip(slug: str, dest: Path) -> dict[str, Any]:
    if not re.fullmatch(r"[a-z0-9-]{1,100}", slug):
        raise OpError("bad Quaternius pack id")
    page = f"https://opengameart.org/content/{slug}"
    html, _ = _a()._get_text(page)
    author = re.search(r"Author:.{0,400}?/users/([A-Za-z0-9_-]+)", html, re.S)
    if not author or author.group(1).lower() != "quaternius":
        raise OpError("that OpenGameArt item isn't Quaternius's own upload")
    lic = re.search(r"License\(s\):.{0,1500}?(CC0|CC-BY|GPL|OGA-BY)", html, re.S)
    if not lic or lic.group(1) != "CC0":
        raise OpError("that OpenGameArt item isn't marked CC0")
    zips = re.findall(r'https://opengameart\.org/sites/default/files/[^"\s<>]+\.zip', html)
    zips = sorted(dict.fromkeys(zips), key=lambda u: ("source" in u.lower(), len(u)))
    if not zips:
        raise OpError("no zip on that OpenGameArt page")
    _a()._download(zips[0], dest)
    title = re.search(r"<title>([^|<]+)", html)
    return {"page": page, "creator": "Quaternius (quaternius.com)", "download": zips[0],
            "attribution": f"{(title.group(1).strip() if title else slug)} by Quaternius (quaternius.com), CC0"}


def pack_folder(source: str, slug: str) -> tuple[Path, dict[str, Any]]:
    root = cache_dir() / source / slug
    meta = root / "pack.json"
    if meta.exists() and (root / "files").is_dir():
        return root / "files", json.loads(meta.read_text(encoding="utf-8"))
    shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True)
    try:
        z = root / "pack.zip"
        info = _kenney_zip(slug, z) if source == "kenney" else _quaternius_zip(slug, z)
        _safe_extract(z, root / "files")
        z.unlink()
        names = sorted(_models_in(root / "files"))
        (root / "items.json").write_text(json.dumps(names), encoding="utf-8")
        meta.write_text(json.dumps(info), encoding="utf-8")
        return root / "files", info
    except BaseException:
        shutil.rmtree(root, ignore_errors=True)
        raise


def pick(models: dict[str, Path], want: str) -> str | None:
    w = _norm(want)
    by = {_norm(k): k for k in models}
    if w in by:
        return by[w]
    words = w.split()
    scored = [(sum(x in _norm(k).split() for x in words) * 2 + sum(x in _norm(k) for x in words), -len(k), k) for k in models]
    scored = [s for s in scored if s[0] > 0]
    return max(scored)[2] if scored else None


def blender_python() -> tuple[list[str], dict[str, str]]:
    from .characters import makehuman_dir

    mh = makehuman_dir()
    py = Path(os.environ.get("BUILDER_BLENDER_PYTHON") or (mh / "mhenv" / "bin" / "python"))
    if not py.exists():
        raise OpError("Blender (bpy) isn't installed: run tools/makehuman/setup.sh (it sets up ~/.builder/makehuman/mhenv)")
    env = {**os.environ, "BLENDER_USER_RESOURCES": str(mh / "bhome")}
    return [str(py), "-I"], env


def to_glb(src: Path, out: Path, strip_rig: bool = False, root: Path | None = None) -> None:
    """Any model file -> self-contained GLB (pure Python for glTF/GLB, Blender for FBX/OBJ/others)."""
    from .glbtools import GlbError, pack_glb

    if src.suffix.lower() in (".glb", ".gltf") and not strip_rig:
        try:
            out.write_bytes(pack_glb(src, root))
            return
        except GlbError as e:
            raise OpError(f"couldn't pack {src.name}: {e}") from e
    py, env = blender_python()
    proc = subprocess.run([*py, str(CONVERT), str(src), str(out)] + (["--strip-rig"] if strip_rig else []) + (["--search", str(root)] if root else []),
                          capture_output=True, text=True, timeout=600, env=env, **NO_WINDOW)
    if proc.returncode != 0 or not out.exists():
        raise OpError(f"converting {src.name} to GLB failed:\n" + "\n".join((proc.stderr or proc.stdout).strip().splitlines()[-6:]))


def import_pack_items(source: str, slug: str, item: str | None) -> OpResult:
    a = _a()
    folder, info = pack_folder(source, slug)
    models = _models_in(folder)
    if not item:
        return OpResult(data={"source": source, "pack": slug, "items": sorted(models), **info,
                              "note": "Nothing imported yet: call asset_import again with item= one or more of these names (comma-separated)."})
    res = OpResult(data={"source": source, "pack": slug, "imported": [], **CC0, **info})
    missing = []
    for want in [w.strip() for w in item.split(",") if w.strip()][:20]:
        name = pick(models, want)
        if not name:
            missing.append(want)
            continue
        with tempfile.TemporaryDirectory(prefix="pack-") as tmp:
            glb = Path(tmp) / f"{name}.glb"
            to_glb(models[name], glb, root=folder)
            lic = {"source": source, "id": f"{slug}/{name}", "pack": slug, **CC0, "kind": "model",
                   "page": info.get("page"), "creator": info.get("creator"), "attribution": info.get("attribution"),
                   "original_format": models[name].suffix.lower().lstrip(".")}
            one = a._register(glb, f"{a.storage.safe_name(name)}.glb", lic)
        res.outputs += one.outputs
        res.data["imported"].append({"item": name, "file_id": one.outputs[0].id, "from": models[name].suffix.lower()})
    if missing:
        if not res.outputs:
            near = sorted(models)[:60]
            raise OpError(f"no model like {', '.join(missing)} in {slug} (it has: {', '.join(near)}{' …' if len(models) > 60 else ''})")
        res.data["not_found"] = missing
    return res


def import_polypizza(model_id: str) -> OpResult:
    a = _a()
    key = _pp_key()
    if not key:
        raise OpError("Set POLY_PIZZA_API_KEY in builder.env to use Poly Pizza (free key in your poly.pizza account settings)")
    m = a._get_json(f"{PP_API}/model/{urllib.parse.quote(model_id, safe='')}", headers={"x-auth-token": key})
    url = m.get("Download")
    if not url:
        raise OpError("that Poly Pizza model has no downloadable GLB")
    with tempfile.TemporaryDirectory(prefix="pp-") as tmp:
        f = Path(tmp) / "model.glb"
        try:
            a._download(url, f)
        except OpError as e:
            raise OpError(f"{e}. Poly Pizza's file server blocks many cloud addresses: download the .glb from "
                          f"https://poly.pizza/m/{model_id} by hand and use asset_import source=url") from e
        if f.read_bytes()[:4] != b"glTF":
            raise OpError(f"Poly Pizza's file server answered a web page instead of the model (it blocks many cloud addresses): "
                          f"download it from https://poly.pizza/m/{model_id} and use asset_import source=url")
        creator = (m.get("Creator") or {}).get("Username") if isinstance(m.get("Creator"), dict) else m.get("Creator")
        lic = {"source": "polypizza", "id": model_id, "page": f"https://poly.pizza/m/{model_id}", "creator": creator,
               **_pp_licence(m.get("Licence")), "kind": "model",
               "attribution": m.get("Attribution") or f"{m.get('Title')} by {creator} (poly.pizza)"}
        return a._register(f, a.storage.safe_name(str(m.get("Title") or model_id))[:60] + ".glb", lic)
