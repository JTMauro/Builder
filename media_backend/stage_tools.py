"""Stage tools: any Poly Haven place as a set, and all ~2,400 CMU motion-capture moves on demand.

- add_set: a set (sky/light + props) from any Poly Haven HDRI, by id or search words (the best match), or
  one of the presets (park, arena, colosseum, ...); writes <stage>/sets/<name>/set.json with sensible ground
  height/radius/exposure. Uses tools/makehuman/add_set.py (also a command-line tool).
- find_moves: search the CMU catalog (title, category, length, whether it retargets cleanly) by words.
- fetch_moves: download CMU clips into a stage (clips/cmu/<label>.json + index.json), so scene beats and
  studio.html can play them by label. scene_video calls ensure_moves for any move it doesn't know yet, so
  a beat can name any CMU id ("02_07-swordplay") or words ("swordplay").
CMU = Carnegie Mellon's free motion-capture library (mocap.cs.cmu.edu, NSF EIA-0196217); GLBs from
huggingface.co/datasets/codepawl/cmu-mocap-glb.
"""

from __future__ import annotations

import base64
import importlib.util
import json
import os
import re
import tempfile
import urllib.request
from functools import lru_cache
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from .config import get_settings
from .ops import OpError, OpResult, operation

TOOLS = Path(__file__).resolve().parents[1] / "tools" / "makehuman"
META_URL = "https://huggingface.co/datasets/codepawl/cmu-mocap-glb/resolve/main/metadata.jsonl"
NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,39}")


def _tool(name: str):
    """Load a tools/makehuman script as a module (tests replace its download functions)."""
    spec = importlib.util.spec_from_file_location(f"builder_tool_{name}", TOOLS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


_mods: dict[str, Any] = {}


def tool(name: str):
    if name not in _mods:
        _mods[name] = _tool(name)
    return _mods[name]


def _stage(name: str) -> Path:
    from .scene import stages_dir

    d = stages_dir() / name
    if not (d / "index.html").exists():
        raise OpError(f"no stage called {name!r}")
    return d


# ---------------------------------------------------------------- sets

class AddSetParams(BaseModel):
    name: str = Field(description="The set's name on the stage (lower-case letters, digits, - or _), or a preset: park, living_room, street, beach, gym, school, cafe, stadium, snow, arena, colosseum, desert, ruins, circus")
    hdri: str | None = Field(None, max_length=80, description="Any Poly Haven HDRI id (polyhaven.com/hdris) or search words such as 'arena' or 'sunset beach' (the best match is used). Leave out for a preset.")
    props: list[dict[str, Any]] = Field(default_factory=list, max_length=12, description='Props: [{"id": "WoodenChair_01" (a Poly Haven model id, or "door"), "at": [x, z], "rot": degrees, "scale": 1}]')
    ground_height: float | None = Field(None, ge=0.5, le=10, description="Camera height the HDRI was shot from (metres); default from the HDRI")
    ground_radius: float | None = Field(None, ge=5, le=500, description="How far away the surroundings are (metres)")
    exposure: float | None = Field(None, ge=0.2, le=4)
    stage: str = "teen"
    resolution: str = Field("2k", pattern=r"^(1k|2k|4k)$", description="HDRI resolution")


@operation("add_set", AddSetParams, "Add a place to a stage as a set for scene_video: any Poly Haven HDRI (by id or search words like 'arena', 'desert', 'night city') as the surroundings and light, plus optional Poly Haven props. Presets: park, living_room, street, beach, gym, school, cafe, stadium, snow, arena (sandy gladiator arena), colosseum, desert, ruins, circus. Downloads (free, CC0).")
def add_set(p: AddSetParams) -> OpResult:
    if not NAME.fullmatch(p.name):
        raise OpError("set name: lower-case letters, digits, - or _ (up to 40)")
    stage = _stage(p.stage)
    m = tool("add_set")
    props = []
    for x in p.props:
        pid = str(x.get("id") or x.get("prop") or "")
        if not re.fullmatch(r"[A-Za-z0-9_]{1,64}", pid):
            raise OpError(f"prop id {pid!r} isn't a Poly Haven model id")
        at = x.get("at") or [0, 0]
        props.append({"id": pid, "at": [float(at[0]), float(at[1])], "rot": float(x.get("rot", 0)), "scale": float(x.get("scale", 1))})
    try:
        if p.hdri or props or p.name not in m.SETS or p.ground_height or p.ground_radius or p.exposure:
            spec = m.custom_spec(p.name, p.hdri or m.SETS.get(p.name, {}).get("hdri") or p.name, props,
                                 p.ground_height, p.ground_radius, p.exposure)
        else:
            spec = None
        data = m.build_set(str(stage), p.name, "1k", p.resolution, spec)
    except SystemExit as e:
        raise OpError(str(e)) from e
    except (OSError, KeyError, ValueError) as e:
        raise OpError(f"couldn't build the set: {e}") from e
    return OpResult(data={"set": p.name, "stage": p.stage, "hdri": data["hdri"], "ground": data["ground"], "exposure": data["exposure"],
                          "props": [x["id"] for x in data["props"]], "uses": {x["id"]: sorted(x.get("use", {})) for x in data["props"] if x.get("use")},
                          "credit": data["credit"], "next": f'scene_video with "set": "{p.name}"'})


class SearchSetsParams(BaseModel):
    words: str = Field(min_length=2, max_length=80)


@operation("find_sets", SearchSetsParams, "Search Poly Haven's ~1,000 free HDRIs (places to use as sets) by words; returns the best ids for add_set, plus the presets.", read_only=True)
def find_sets(p: SearchSetsParams) -> OpResult:
    m = tool("add_set")
    try:
        hits = m.search_hdri(p.words)
    except OSError as e:
        raise OpError(f"couldn't reach Poly Haven: {e}") from e
    return OpResult(data={"matches": [{"id": i, "score": s, "name": n} for i, s, n in hits],
                          "presets": {k: v["hdri"] for k, v in m.SETS.items()}, "good": m.GOOD})


# ---------------------------------------------------------------- moves

def metadata_path() -> Path:
    env = os.environ.get("BUILDER_CMU_METADATA")
    if env:
        return Path(env).expanduser()
    for d in (get_settings().data_dir, Path("~/.builder").expanduser()):
        f = d / "dl" / "cmu" / "metadata.jsonl"
        if f.exists():
            return f
    return get_settings().data_dir / "dl" / "cmu" / "metadata.jsonl"


def _label(title: str) -> str:
    t = re.sub(r"[^a-z0-9]+", " ", title.lower()).strip()
    return re.sub(r"\s+", " ", t)[:60] or "move"


@lru_cache(maxsize=4)
def _catalog(path: str, mtime: float) -> tuple[list[dict], dict[str, dict]]:
    rows, seen = [], {}
    for line in open(path, encoding="utf-8"):
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if not d.get("id"):
            continue
        rows.append({"id": d["id"], "title": d.get("title") or d["id"], "category": d.get("category") or "",
                     "subcategory": d.get("subcategory") or "", "summary": d.get("subject_summary") or "",
                     "seconds": round(float(d.get("seconds") or 0), 2), "retarget_ok": bool(d.get("retarget_ok", True))})
    for r in rows:
        seen.setdefault(_label(r["title"]), []).append(r)
    for lab, rs in seen.items():
        for r in rs:
            r["label"] = lab if len(rs) == 1 else f"{lab} {r['id'].split('-')[0]}"
    return rows, {r["id"]: r for r in rows}


def catalog() -> tuple[list[dict], dict[str, dict]]:
    """All CMU clips: [{id, label, title, category, subcategory, summary, seconds, retarget_ok}], and by id."""
    f = metadata_path()
    if not f.exists():
        f.parent.mkdir(parents=True, exist_ok=True)
        try:
            with urllib.request.urlopen(urllib.request.Request(META_URL, headers={"User-Agent": "Builder/1"}), timeout=120) as r:
                data = r.read()
        except OSError as e:
            raise OpError(f"the CMU move catalog isn't here and couldn't be downloaded: {e}") from e
        f.write_bytes(data)
    return _catalog(str(f), f.stat().st_mtime)


def _words(s: str) -> list[str]:
    return [w[:-1] if len(w) > 4 and w.endswith("s") and not w.endswith("ss") else w for w in re.findall(r"[a-z0-9]+", s.lower())]


def search(words: str, limit: int = 20, include_broken: bool = False, max_seconds: float | None = None) -> list[dict]:
    rows, _ = catalog()
    q = _words(words)
    if not q:
        return []
    out = []
    for r in rows:
        if not include_broken and not r["retarget_ok"]:
            continue
        if max_seconds and r["seconds"] > max_seconds:
            continue
        tw, sub, cat, summ = set(_words(r["title"])), set(_words(r["subcategory"])), set(_words(r["category"])), set(_words(r["summary"]))
        s = 0.0
        for w in q:
            s += 10 if w in tw else 5 if any(x.startswith(w) for x in tw) else 0
            s += 4 if w in sub else 0
            s += 2 if w in cat else 0
            s += 1 if w in summ else 0
        if all(w in tw or any(x.startswith(w) for x in tw) for w in q):
            s += 8   # every word in the title
        if s <= 0:
            continue
        s -= abs(min(r["seconds"], 40) - 6) * 0.12   # short, usable clips first
        s -= 0.4 * max(0, len(tw) - len(q))         # fewer extra words
        out.append((s, r))
    out.sort(key=lambda x: -x[0])
    return [{**r, "score": round(s, 2)} for s, r in out[:limit]]


def stage_index(stage: Path) -> dict[str, str]:
    ix = stage / "clips" / "cmu" / "index.json"
    return json.loads(ix.read_text(encoding="utf-8")) if ix.exists() else {}


def _clip_label_ids(stage: Path) -> dict[str, str]:
    """label -> CMU id for the clips a stage already has."""
    out = {}
    src = json.loads((TOOLS / "cmu_clips.json").read_text(encoding="utf-8"))
    out.update({k: v for k, v in src.items() if not k.startswith("_")})
    for lab, f in stage_index(stage).items():
        try:
            out[lab] = json.loads((stage / "clips" / "cmu" / f).read_text(encoding="utf-8")).get("id", out.get(lab, ""))
        except (OSError, ValueError):
            continue
    return out


def fetch_clip(stage: Path, cid: str, label: str) -> str:
    """Download one CMU clip into the stage as clips/cmu/<label>.json and list it in index.json; returns the file name."""
    d = stage / "clips" / "cmu"
    d.mkdir(parents=True, exist_ok=True)
    fname = "".join(c if c.isalnum() else "-" for c in label)[:80] + ".json"
    index = stage_index(stage)
    if index.get(label) == fname and (d / fname).exists():
        return fname
    cache = get_settings().data_dir / "makehuman" / "cmu"
    cache.mkdir(parents=True, exist_ok=True)
    glb = cache / f"{cid}.glb"
    if not glb.exists():
        try:
            tool("get_cmu").fetch(cid, str(glb))
        except SystemExit as e:
            raise OpError(f"couldn't download CMU clip {cid}: {e}") from e
    (d / fname).write_text(json.dumps({"label": label, "id": cid, "glb": base64.b64encode(glb.read_bytes()).decode()}), encoding="utf-8")
    index = stage_index(stage)   # re-read: another job may have added moves meanwhile
    index[label] = fname
    tmp = Path(tempfile.mktemp(dir=d, suffix=".tmp"))
    tmp.write_text(json.dumps(index, indent=1), encoding="utf-8")
    tmp.replace(d / "index.json")
    return fname


def resolve_move(stage: Path, move: str) -> tuple[str, str] | None:
    """A move a stage doesn't have yet -> (CMU id, label) to fetch: an exact CMU id, a catalog label, or the best search match."""
    from .scene import stage_moves

    if move in stage_moves(stage.name):
        return None
    _, by_id = catalog()
    if move in by_id:
        return move, move
    rows, _ = catalog()
    for r in rows:
        if r["label"] == move.lower() and r["retarget_ok"]:
            return r["id"], move
    hits = search(move, 1, max_seconds=30)
    if not hits:
        raise OpError(f"no move called {move!r}: not on the stage and nothing like it in the CMU catalog (try find_moves)")
    return hits[0]["id"], move


def ensure_moves(stage_name: str, moves: list[str]) -> dict[str, str]:
    """Fetch any moves the stage doesn't have yet; returns {move: CMU id} for those fetched."""
    stage = _stage(stage_name)
    got = {}
    for mv in dict.fromkeys(m for m in moves if m):
        r = resolve_move(stage, mv)
        if r:
            fetch_clip(stage, r[0], r[1])
            got[mv] = r[0]
    return got


class FindMovesParams(BaseModel):
    words: str = Field(min_length=1, max_length=120, description="What the move is, e.g. 'sword fight', 'sit down', 'drunk walk', 'cartwheel'")
    limit: int = Field(15, ge=1, le=100)
    stage: str | None = Field("teen", description="Mark which results the stage already has")
    include_broken: bool = Field(False, description="Also list clips that don't retarget cleanly")
    max_seconds: float | None = Field(None, gt=0, le=300)


@operation("find_moves", FindMovesParams, "Search all ~2,400 CMU motion-capture moves by words (title, category, length). Any result's id or label can be used as a scene beat's move: scene_video fetches it into the stage on demand (or use fetch_moves first).", read_only=True)
def find_moves(p: FindMovesParams) -> OpResult:
    hits = search(p.words, p.limit, p.include_broken, p.max_seconds)
    have = {}
    if p.stage:
        try:
            have = {v: k for k, v in _clip_label_ids(_stage(p.stage)).items()}
        except OpError:
            have = {}
    for h in hits:
        h["on_stage"] = h["id"] in have
        if h["on_stage"]:
            h["stage_label"] = have[h["id"]]
    return OpResult(data={"words": p.words, "count": len(hits), "moves": hits, "catalog_size": len(catalog()[0]),
                          "credit": "CMU Graphics Lab Motion Capture Database (mocap.cs.cmu.edu, NSF EIA-0196217)"})


class FetchMovesParams(BaseModel):
    moves: list[str] = Field(min_length=1, max_length=40, description="CMU ids, catalog labels or search words; each becomes a move on the stage")
    labels: list[str] = Field(default_factory=list, description="Optional names to give them on the stage (same order)")
    stage: str = "teen"


@operation("fetch_moves", FetchMovesParams, "Download CMU motion-capture moves into a stage (by id, label or words), so scene beats and the Studio preview can use them by name.")
def fetch_moves(p: FetchMovesParams) -> OpResult:
    stage = _stage(p.stage)
    done = []
    for i, mv in enumerate(p.moves):
        label = (p.labels[i] if i < len(p.labels) and p.labels[i] else mv).strip()[:80]
        r = resolve_move(stage, mv)
        if r is None:
            done.append({"move": label, "id": _clip_label_ids(stage).get(mv, ""), "fetched": False})
            continue
        fetch_clip(stage, r[0], label)
        done.append({"move": label, "id": r[0], "fetched": True})
    return OpResult(data={"stage": p.stage, "moves": done})
