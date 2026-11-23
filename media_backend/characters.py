"""Characters: make a new MakeHuman person from plain words, keep a library, export game-ready GLBs.

- make_character: Claude turns a description ("a grumpy grandpa in a work suit") into MakeHuman
  settings limited to the assets that are installed, plus a Kokoro voice and a short persona
  (or pass `params` to skip Claude). tools/makehuman/run_job.py builds the person (about 30 s),
  the one-file glTF goes onto a stage as models/<name>.json, and the character is saved to the library.
- The library (<data>/library or BUILDER_LIBRARY_DIR): characters/<name>.json (settings, voice,
  persona, which stages have the model) and scenes/<name>.json (scene_video templates). The repo's
  studio/characters/*.json are the seed. A template's cast can name library characters as their
  model; resolve_cast() fills in each one's voice.
- export_character: a stage character as a binary glTF with its moves baked in (idle, walk, run,
  gestures, CMU motion capture) for the Craft engine, Godot or three.js games
  (tools/makehuman/view/export_glb.cjs: three.js retarget + GLTFExporter in headless Chromium).

Real people and famous characters are never recreated: descriptions naming them are refused.
"""

from __future__ import annotations

import difflib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from .config import NO_WINDOW, get_settings
from .ops import OpError, OpResult, _guarded, operation
from .voices import VOICES

ROOT = Path(__file__).resolve().parents[1]
RUN_JOB = ROOT / "tools" / "makehuman" / "run_job.py"
EXPORTER = ROOT / "tools" / "makehuman" / "view" / "export_glb.cjs"
SEEDS = ROOT / "studio" / "characters"
NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
RESERVED = {"light"}   # models/light.json is the stage's HDRI

# used when the MakeHuman assets folder isn't there (tests, a machine without the studio set up)
KNOWN = {
    "hair": ["afro01", "bob01", "bob02", "braid01", "long01", "ponytail01", "short01", "short02", "short03", "short04"],
    "outfits": ["female_casualsuit01", "female_casualsuit02", "female_elegantsuit01", "female_sportsuit01", "male_casualsuit01",
                "male_casualsuit02", "male_casualsuit03", "male_casualsuit04", "male_casualsuit05", "male_casualsuit06",
                "male_elegantsuit01", "male_worksuit01"],
    "shoes": ["shoes01", "shoes02", "shoes03", "shoes04", "shoes05", "shoes06"],
    "eyes": ["blue_eye", "bluegreen_eye", "brown_eye", "brownlight_eye", "deepblue_eye", "green_eye", "grey_eye", "ice_eye", "lightblue_eye"],
}
HAIR_WORDS = {"braid": "braid01", "pony": "ponytail01", "afro": "afro01", "curl": "afro01", "bob": "bob01", "long": "long01",
              "crew": "short01", "buzz": "short01", "bald": "short01", "short": "short02"}
NUMS = {"age": (1, 90, 25), "gender": (0, 1, 1), "height": (0, 1, 0.5), "weight": (0, 1, 0.5), "muscle": (0, 1, 0.5),
        "smile": (0, 1, 0), "hairSnug": (0, 1, 0), "jeansLight": (0.5, 2, None)}
RGBS = ("skinRGB", "hairRGB", "topRGB", "shoesRGB")

# "looks like Tom Hanks", "as Harry Potter", "based on Taylor Swift": a cue followed by a capitalised full name
_NAMED = re.compile(r"\b(?:look(?:s|ing)?\s+(?:just\s+)?like|lookalike(?:\s+of)?|resembl\w*|version\s+of|based\s+on|dressed\s+as|"
                    r"impression\s+of|copy\s+of|clone\s+of|double\s+of|as|like)\s+"
                    r"((?:[A-Z][\w'.-]+)(?:\s+(?:[A-Z][\w'.-]+|de|van|von|da|del|bin)){1,3})")
_FAMOUS = re.compile(r"\b(celebrity|famous|real person|real-life|movie star|pop star|president|prime minister|the actor|the singer|"
                     r"the footballer|the rapper|the youtuber|lookalike|doppelg[aä]nger|deepfake)\b", re.I)


# ---------------------------------------------------------------- places


def makehuman_dir() -> Path:
    return Path(os.environ.get("BUILDER_MAKEHUMAN_DIR") or (get_settings().data_dir / "makehuman")).expanduser()


def library_dir() -> Path:
    return Path(os.environ.get("BUILDER_LIBRARY_DIR") or (get_settings().data_dir / "library")).expanduser()


def _stages_dir() -> Path:
    from .scene import stages_dir   # late: scene.py may import this module

    return stages_dir()


def _check_name(name: str, what: str = "name") -> str:
    if not NAME_RE.match(name or "") or name in RESERVED:
        raise OpError(f"{what} {name!r} must be 1-32 lower-case letters, digits, - or _ (starting with a letter)")
    return name


# ---------------------------------------------------------------- assets and validation


def assets() -> dict[str, list[str]]:
    """Hair, outfits, shoes and eye colours installed in the MakeHuman assets folder (or the known set)."""
    a = makehuman_dir() / "mhassets"
    if not (a / "hair").is_dir():
        return {k: list(v) for k, v in KNOWN.items()}
    clothes = sorted(p.name for p in (a / "clothes").iterdir() if p.is_dir()) if (a / "clothes").is_dir() else []
    eyes = sorted(p.stem for p in (a / "eyes" / "materials").glob("*_eye.png"))
    return {"hair": sorted(p.name for p in (a / "hair").iterdir() if (p / f"{p.name}.mhclo").exists()),
            # one suit per person (tex.py paints one clothes texture); hats aren't supported yet
            "outfits": [c for c in clothes if re.search(r"suit\d+$", c)],
            "shoes": [c for c in clothes if c.startswith("shoes")],
            "eyes": eyes or list(KNOWN["eyes"])}


def _targets_dir() -> Path:
    return makehuman_dir() / "bhome" / "extensions" / "user_default" / "mpfb" / "data" / "targets"


def _pick(value: Any, have: list[str], words: dict[str, str] | None = None) -> str | None:
    v = str(value or "").strip().lower().replace(" ", "")
    if not v:
        return None
    if v in have:
        return v
    close = difflib.get_close_matches(v, have, n=1, cutoff=0.75)
    if close:
        return close[0]
    for w, asset in (words or {}).items():
        if w in v and asset in have:
            return asset
    return None


def _outfit(value: Any, male: bool, have: list[str]) -> str | None:
    v = str(value or "").lower()
    if v in have:
        return v
    sex = "male" if male else "female"
    kinds = [("sport", "sportsuit"), ("athlet", "sportsuit"), ("gym", "sportsuit"), ("track", "sportsuit"), ("work", "worksuit"),
             ("overall", "worksuit"), ("elegant", "elegantsuit"), ("formal", "elegantsuit"), ("business", "elegantsuit"),
             ("suit", "elegantsuit"), ("casual", "casualsuit")]
    for word, kind in kinds:
        if word in v:
            mine = [o for o in have if o.startswith(sex + "_") and kind in o]
            other = [o for o in have if kind in o]
            if mine or other:
                return (mine or other)[0]
    return None


def validate_params(p: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """MakeHuman settings limited to installed assets and sane ranges. Returns (clean settings, notes on what changed)."""
    have, notes, out = assets(), [], {}
    for k, (lo, hi, default) in NUMS.items():
        if k in p and p[k] is not None:
            try:
                v = float(p[k])
            except (TypeError, ValueError):
                notes.append(f"{k} {p[k]!r} is not a number: left out")
                continue
            if not lo <= v <= hi:
                notes.append(f"{k} {v} out of range: clamped to {lo}..{hi}")
            out[k] = round(min(hi, max(lo, v)), 3)
        elif default is not None and k in ("age", "gender"):
            out[k] = default
    male = out.get("gender", 1) >= 0.5
    for k in RGBS:
        v = p.get(k)
        if v is None:
            continue
        try:
            rgb = [round(min(1.0, max(0.0, float(x))), 3) for x in v]
            if len(rgb) != 3:
                raise ValueError
            out[k] = rgb
        except (TypeError, ValueError):
            notes.append(f"{k} {v!r} is not three numbers 0..1: left out")

    hair = _pick(p.get("hair"), have["hair"], HAIR_WORDS)
    fallback_hair = next((h for h in (["short02", "short03"] if male else ["long01", "ponytail01", "bob01"]) if h in have["hair"]), have["hair"][0])
    if p.get("hair") and not hair:
        notes.append(f"no hair called {p.get('hair')!r}: used {fallback_hair} (have: {', '.join(have['hair'])})")
    elif hair and hair != p.get("hair"):
        notes.append(f"hair {p.get('hair')!r} -> {hair}")
    out["hair"] = hair or fallback_hair

    eye_in = str(p.get("eyes") or "").lower().replace(" ", "")
    eyes = _pick(eye_in if eye_in.endswith("_eye") or not eye_in else eye_in + "_eye", have["eyes"])
    if eye_in and not eyes:
        notes.append(f"no eye colour {p.get('eyes')!r}: used brown_eye")
    out["eyes"] = eyes or ("brown_eye" if "brown_eye" in have["eyes"] else have["eyes"][0])

    default_outfit = next((o for o in (["male_casualsuit06"] if male else ["female_casualsuit01"]) if o in have["outfits"]), have["outfits"][0])
    outfit = _outfit(p.get("outfit"), male, have["outfits"])
    if p.get("outfit") and not outfit:
        notes.append(f"no outfit {p.get('outfit')!r}: used {default_outfit} (have: {', '.join(have['outfits'])})")
    out["outfit"] = outfit or default_outfit
    if out.get("topRGB") and out["outfit"] != "male_casualsuit06":
        out.pop("topRGB")   # tex.py recolours the top of the casual tee's texture only
        notes.append("topRGB only recolours the plain tee (male_casualsuit06): left out")
    if "jeansLight" in out and "casualsuit" not in out["outfit"]:
        out.pop("jeansLight")

    shoes = _pick(p.get("shoes"), have["shoes"])
    if p.get("shoes") and not shoes:
        notes.append(f"no shoes {p.get('shoes')!r}: used shoes05")
    out["shoes"] = shoes or ("shoes05" if "shoes05" in have["shoes"] else have["shoes"][0])

    shape, tdir = {}, _targets_dir()
    for k, w in (p.get("shape") or {}).items() if isinstance(p.get("shape"), dict) else []:
        if not re.match(r"^[a-z0-9_-]+/[a-z0-9_.-]+$", str(k)):
            notes.append(f"shape target {k!r} left out (expected group/target)")
            continue
        if tdir.is_dir() and not (tdir / f"{k}.target.gz").exists():
            notes.append(f"no shape target {k!r}: left out")
            continue
        try:
            shape[k] = round(min(1.0, max(-1.0, float(w))), 3)
        except (TypeError, ValueError):
            notes.append(f"shape {k} weight {w!r} left out")
    if shape:
        out["shape"] = shape
    out["morphs"] = "talk" if p.get("morphs", "talk") == "talk" else ""
    return out, notes


def default_voice(params: dict[str, Any]) -> str:
    male, age = params.get("gender", 1) >= 0.5, params.get("age", 25)
    if male:
        return "am_puck" if age < 20 else "bm_george" if age >= 60 else "am_liam"
    return "af_bella" if age < 20 else "bf_emma" if age >= 60 else "af_heart"


def refuses(text: str | None) -> str | None:
    """Why a description can't be made (it names or asks for a real person or famous character), or None."""
    if not text:
        return None
    m = _NAMED.search(text)
    if m:
        return f"it names {m.group(1)!r}: Builder doesn't recreate real people or famous characters. Describe an original person instead."
    m = _FAMOUS.search(text)
    if m:
        return f"it asks for someone real or famous ({m.group(1)!r}): Builder doesn't recreate real people or famous characters. Describe an original person instead."
    return None


# ---------------------------------------------------------------- the library


def _seed() -> dict[str, dict]:
    out = {}
    for f in sorted(SEEDS.glob("*.json")) if SEEDS.is_dir() else []:
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        params = d.get("params") if isinstance(d.get("params"), dict) else d
        out[f.stem] = {"name": f.stem, "params": params, "voice": d.get("voice") or default_voice(params),
                       "persona": d.get("persona") or "", "model": f.stem, "source": f"studio/characters/{f.name}"}
    return out


def _lib_file(kind: str, name: str) -> Path:
    return library_dir() / kind / f"{name}.json"


def characters() -> dict[str, dict]:
    """All library characters: the repo seed, overridden by the data folder's definitions."""
    out = _seed()
    d = library_dir() / "characters"
    for f in sorted(d.glob("*.json")) if d.is_dir() else []:
        try:
            out[f.stem] = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
    return out


def scene_templates() -> dict[str, dict]:
    d, out = library_dir() / "scenes", {}
    for f in sorted(d.glob("*.json")) if d.is_dir() else []:
        try:
            out[f.stem] = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
    return out


def _stage_info() -> dict[str, dict]:
    sd, out = _stages_dir(), {}
    if not sd.is_dir():
        return out
    for s in sorted(p for p in sd.iterdir() if (p / "index.html").exists()):
        models = sorted(f.stem for f in (s / "models").glob("*.json") if f.stem not in RESERVED) if (s / "models").is_dir() else []
        sets = sorted(x.name for x in (s / "sets").iterdir() if (x / "set.json").exists()) if (s / "sets").is_dir() else []
        out[s.name] = {"models": models, "sets": sets, "studio": (s / "studio.html").exists()}
    return out


def save_character(entry: dict[str, Any]) -> Path:
    f = _lib_file("characters", _check_name(entry["name"]))
    f.parent.mkdir(parents=True, exist_ok=True)
    tmp = f.with_suffix(".tmp")
    tmp.write_text(json.dumps(entry, indent=1), encoding="utf-8")
    tmp.replace(f)
    return f


def resolve_cast(cast: list) -> list:
    """Fill in each cast member's voice from the library (by its model name, or a "character" key).

    Takes scene_video cast entries (dicts or CastMember models) and returns the same kind; members with a
    voice already, or whose model isn't a library character, are unchanged. scene.py calls this first.
    """
    lib = characters()
    out = []
    for c in cast or []:
        is_model = hasattr(c, "model_dump")
        d = c.model_dump() if is_model else dict(c)
        if not d.get("model") and d.get("character"):
            d["model"] = d["character"]
        ch = lib.get(str(d.get("model") or ""))
        update = {}
        if ch and not d.get("voice") and ch.get("voice") in VOICES:
            update["voice"] = ch["voice"]
        if is_model:
            out.append(c.model_copy(update=update) if update else c)
        else:
            d.pop("character", None)
            out.append({**d, **update})
    return out


class LibraryListParams(BaseModel):
    pass


@operation("library_list", LibraryListParams, "The character library: characters (voice, persona, MakeHuman settings, which stages have the model), the stages with their models and sets, the voices, and saved scene templates.", read_only=True)
def library_list(p: LibraryListParams) -> OpResult:
    stages = _stage_info()
    chars = []
    for name, c in characters().items():
        chars.append({"name": name, "voice": c.get("voice"), "persona": c.get("persona", ""), "description": c.get("description", ""),
                      "model": c.get("model", name), "on_stages": [s for s, i in stages.items() if c.get("model", name) in i["models"]],
                      "source": c.get("source", "library")})
    known = {c["model"] for c in chars}
    stage_only = sorted({m for i in stages.values() for m in i["models"]} - known)
    return OpResult(data={"characters": chars, "stage_only_models": stage_only, "stages": stages, "voices": VOICES,
                          "scenes": [{"name": n, "description": s.get("description", ""), "stage": s.get("params", {}).get("stage", "teen"),
                                      "cast": [c.get("model") for c in s.get("params", {}).get("cast", [])]} for n, s in scene_templates().items()],
                          "assets": assets(), "library_dir": str(library_dir())})


class LibraryGetParams(BaseModel):
    name: str = Field(description="A library character or scene template name")
    kind: str = Field("any", description="character, scene or any")


@operation("library_get", LibraryGetParams, "One library entry: a character (settings, voice, persona) or a scene template. A template comes back as scene_video_params ready to pass to scene_video, with each cast member's voice filled in from the library.", read_only=True)
def library_get(p: LibraryGetParams) -> OpResult:
    if p.kind in ("any", "character"):
        c = characters().get(p.name)
        if c:
            return OpResult(data={"kind": "character", **c})
    if p.kind in ("any", "scene"):
        s = scene_templates().get(p.name)
        if s:
            params = dict(s.get("params") or {})
            params["cast"] = resolve_cast(params.get("cast") or [])
            return OpResult(data={"kind": "scene", **s, "scene_video_params": params})
    have = sorted(characters()) + sorted(scene_templates())
    raise OpError(f"nothing in the library called {p.name!r} (have: {', '.join(have) or 'none'})")


class SaveSceneParams(BaseModel):
    name: str = Field(description="Template name (lower-case letters, digits, - or _)")
    params: dict[str, Any] = Field(description="scene_video params; cast members can name library characters as their model (their voice is filled in)")
    description: str = Field("", max_length=500)
    overwrite: bool = False


@operation("library_save_scene", SaveSceneParams, "Save scene_video params under a name as a reusable scene template (library_get returns it ready for scene_video, with library characters' voices filled in).")
def library_save_scene(p: SaveSceneParams) -> OpResult:
    from pydantic import ValidationError

    from .scene import SceneParams

    _check_name(p.name, "template name")
    try:
        parsed = SceneParams.model_validate(p.params)
    except ValidationError as e:
        err = e.errors()[0]
        raise OpError(f"not valid scene_video params: {'.'.join(str(x) for x in err['loc'])}: {err['msg']}") from e
    lib, on_stage = characters(), _stage_info().get(parsed.stage, {}).get("models", [])
    unknown = [c.model for c in parsed.cast if c.model not in lib and c.model not in on_stage]
    f = _lib_file("scenes", p.name)
    if f.exists() and not p.overwrite:
        raise OpError(f"a scene template called {p.name!r} exists (overwrite=true to replace it)")
    # keep only what was given: cast voices left out are filled from the library each time the template is used
    clean = parsed.model_dump(mode="json", exclude_unset=True)
    entry = {"name": p.name, "description": p.description, "params": clean, "saved": time.strftime("%Y-%m-%dT%H:%M:%S")}
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(entry, indent=1), encoding="utf-8")
    return OpResult(data={"saved": str(f), "template": entry, "warnings": [f"cast model {m!r} is not in the library or on the stage" for m in unknown]})


# ---------------------------------------------------------------- make a character


def _ask(system: str, content: list[dict[str, Any]] | str, max_tokens: int = 2000) -> str:
    """One Claude call; returns the text. (Tests replace this.)"""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise OpError("making a character from a description needs ANTHROPIC_API_KEY (the same key Rivet uses), or pass params")
    import anthropic

    resp = anthropic.Anthropic().messages.create(model=get_settings().model, max_tokens=max_tokens, system=system,
                                                 messages=[{"role": "user", "content": content}])
    return "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")


def _design_prompt() -> str:
    a = assets()
    return f"""You design original 3D people for MakeHuman. Turn the description into settings.
Answer only JSON: {{"name": short lower-case id (letters, digits, _), "params": {{
 "age": years 1..90, "gender": 0 (female) .. 1 (male), "height", "weight", "muscle": 0..1 (0.5 = average),
 "skinRGB", "hairRGB": [r, g, b] sRGB 0..1 (natural tones; desaturated for light brown hair, e.g. [0.40, 0.315, 0.235]),
 "topRGB"?: [r, g, b] (only with outfit male_casualsuit06, a plain tee), "shoesRGB"?: [r, g, b],
 "hair": one of {a['hair']}, "eyes": one of {a['eyes']}, "outfit": one of {a['outfits']}, "shoes": one of {a['shoes']},
 "smile"?: 0..1, "hairSnug"?: 0..1 (pulls bulky hair onto the head), "shape"?: {{"group/target": weight}} (optional MakeHuman
 targets, e.g. "neck/measure-neck-height-incr", "torso/measure-waist-circ-decr", "stomach/stomach-pregnant-incr")}},
 "voice": one of {list(VOICES)}, "persona": one or two sentences (personality, how they talk)}}
Pick the asset nearest to what's described (outfits: casualsuit = everyday, sportsuit = athletic, worksuit = overalls, elegantsuit = formal).
Never recreate a real person (famous or not) or a famous fictional character. If the description names or asks for one,
answer only {{"refuse": "why"}}."""


def _design(description: str) -> dict[str, Any]:
    text = _ask(_design_prompt(), f"Description: {description}")
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise OpError("Claude's answer had no character settings in it")
    try:
        d = json.loads(m.group(0))
    except ValueError as e:
        raise OpError("Claude's answer wasn't valid JSON") from e
    if d.get("refuse"):
        raise OpError(f"can't make that character: {d['refuse']}")
    if not isinstance(d.get("params"), dict):
        raise OpError("Claude's answer had no params")
    return d


def _run_job(params: dict[str, Any], name: str) -> Path:
    """Build the person with tools/makehuman/run_job.py; returns the one-file glTF (.json) it wrote."""
    mh = makehuman_dir()
    job = Path(os.environ.get("BUILDER_MAKEHUMAN_JOB") or RUN_JOB)
    if job == RUN_JOB and not (mh / "mhenv").exists():
        raise OpError("Blender + MakeHuman aren't installed (run tools/makehuman/setup.sh or tools/setup_studio.sh)")
    with tempfile.TemporaryDirectory(prefix="mhjob-") as tmp:
        pf = Path(tmp) / "params.json"
        pf.write_text(json.dumps(params), encoding="utf-8")
        try:
            proc = subprocess.run([sys.executable, "-I", str(job), str(pf), name, str(mh)], capture_output=True, text=True,
                                  timeout=1800, **NO_WINDOW)
        except subprocess.TimeoutExpired as e:
            raise OpError("building the character timed out (30 minutes)") from e
    if proc.returncode != 0:
        raise OpError("building the character failed:\n" + "\n".join((proc.stderr or proc.stdout).strip().splitlines()[-10:]))
    out = mh / "mhgen" / f"{name}.json"
    if not out.exists():
        raise OpError(f"the MakeHuman job finished without writing {out}")
    return out


class MakeCharacterParams(BaseModel):
    description: str | None = Field(None, max_length=1000, description="Plain words, e.g. 'a sporty 20-year-old woman with braids' (Claude designs the settings)")
    params: dict[str, Any] | None = Field(None, description="MakeHuman settings to use instead of Claude: age, gender, height, weight, muscle, skinRGB, hairRGB, topRGB, shoesRGB, hair, eyes, outfit, shoes, smile, hairSnug, shape, morphs")
    name: str | None = Field(None, description="Character name = the stage model name (models/<name>.json); Claude suggests one if left out")
    voice: str | None = Field(None, description="Kokoro voice (default: Claude's pick, or one that fits age and gender)")
    persona: str | None = Field(None, max_length=600)
    stage: str | None = Field("teen", description="Stage to add the model to (empty = don't add)")
    build: bool = Field(True, description="false = only save the definition to the library")
    overwrite: bool = Field(False, description="Replace an existing character or stage model with this name")


@operation("make_character", MakeCharacterParams, "Make a new realistic 3D person from a description (Claude picks MakeHuman settings from the installed hair, outfits, shoes and eye colours, a voice and a persona) or from explicit params; builds it with MakeHuman (about 30 s), adds it to a stage as models/<name>.json for scene_video, and saves it to the character library. Refuses real people and famous characters.")
def make_character(p: MakeCharacterParams) -> OpResult:
    if not p.description and not p.params:
        raise OpError("give a description or params")
    why = refuses(p.description) or refuses(p.persona)
    if why:
        raise OpError(f"can't make that character: {why}")
    if p.voice and p.voice not in VOICES:
        raise OpError(f"unknown voice {p.voice!r} (have: {', '.join(VOICES)})")
    design: dict[str, Any] = {}
    if p.params is None:
        design = _design(p.description or "")
    raw = p.params if p.params is not None else design["params"]
    params, notes = validate_params(raw)
    name = p.name or re.sub(r"[^a-z0-9_-]", "", str(design.get("name") or "").lower().replace(" ", "_"))[:32]
    if not name:
        raise OpError("give the character a name")
    _check_name(name)
    voice = p.voice or (design.get("voice") if design.get("voice") in VOICES else None) or default_voice(params)
    persona = (p.persona or str(design.get("persona") or "")).strip()[:600]
    stage_dir = None
    if p.stage:
        stage_dir = _stages_dir() / p.stage
        if not (stage_dir / "index.html").exists():
            raise OpError(f"no stage called {p.stage!r} (have: {', '.join(_stage_info()) or 'none'})")
    exists = name in characters() or (stage_dir is not None and (stage_dir / "models" / f"{name}.json").exists())
    if exists and not p.overwrite:
        raise OpError(f"there is already a character called {name!r} (overwrite=true to replace it, or pick another name)")

    entry = {"name": name, "description": p.description or "", "params": params, "voice": voice, "persona": persona, "model": name,
             "stages": [], "made": time.strftime("%Y-%m-%dT%H:%M:%S")}
    if not p.build:
        f = save_character(entry)
        return OpResult(data={**entry, "library_file": str(f), "notes": notes, "built": False})

    model_json = _run_job(params, name)
    if stage_dir is not None:
        (stage_dir / "models").mkdir(exist_ok=True)
        shutil.copyfile(model_json, stage_dir / "models" / f"{name}.json")
        entry["stages"] = [p.stage]
    old = characters().get(name, {})
    entry["stages"] = sorted(set(entry["stages"]) | (set(old.get("stages", [])) if p.overwrite else set()))
    f = save_character(entry)
    glb = model_json.with_suffix(".glb")
    data = {**entry, "library_file": str(f), "notes": notes, "built": True,
            "stage_model": str(stage_dir / "models" / f"{name}.json") if stage_dir is not None else None,
            "next": f'scene_video with cast [{{"id": "a", "model": "{name}"}}] on stage {p.stage or "<a stage>"} (voice {voice} is picked up from the library)'}
    if glb.exists():
        return _guarded("make_character", [], f"{name}.glb", lambda out: shutil.copyfile(glb, out), data=data)
    return OpResult(data=data)


# ---------------------------------------------------------------- export for games

DEFAULT_MOVES = ["idle", "walk", "run", "wave", "point", "thumbs up", "agree", "headShake"]


def _node() -> str:
    node = shutil.which("node")
    if not node:
        raise OpError("export_character needs Node.js with Playwright (see `builder doctor`)")
    return node


def _exporter(job: dict[str, Any], stage: Path) -> dict[str, Any]:
    driver = Path(os.environ.get("CHARACTER_EXPORTER") or EXPORTER)
    if not driver.exists():
        raise OpError(f"character exporter not found at {driver}")
    with tempfile.TemporaryDirectory(prefix="export-") as tmp:
        jf, rf = Path(tmp) / "job.json", Path(tmp) / "report.json"
        jf.write_text(json.dumps({**job, "report": str(rf)}), encoding="utf-8")
        runner = [sys.executable] if driver.suffix == ".py" else [_node()]   # .py: a stand-in (tests)
        try:
            proc = subprocess.run([*runner, str(driver), str(stage), str(jf)], capture_output=True, text=True, timeout=1800,
                                  cwd=str(driver.parent), **NO_WINDOW)
        except subprocess.TimeoutExpired as e:
            raise OpError("the export timed out") from e
        if proc.returncode != 0 or not rf.exists():
            raise OpError("export failed:\n" + "\n".join((proc.stderr or proc.stdout or "no output").strip().splitlines()[-8:]))
        return json.loads(rf.read_text(encoding="utf-8"))


class ExportParams(BaseModel):
    character: str = Field("teen", description="Model on the stage (models/<name>.json)")
    stage: str = "teen"
    moves: list[str] = Field(default_factory=lambda: list(DEFAULT_MOVES), min_length=1, max_length=80,
                             description="Moves to bake in: idle, walk, run, agree, headShake, wave, point, thumbs up, arms crossed, the stage's CMU moves (jump, dance, ...), or 'all'")
    morphs: bool = Field(True, description="Keep the face morph targets (mouth shapes, blinks, smile, brows)")
    in_place: bool = Field(True, description="Walk and run cycles stay in place (the game moves the character)")
    preview: str | None = Field("auto", description="Load the GLB back in three.js and render one frame of this move as a check ('auto' = the second move, null = skip)")


@operation("export_character", ExportParams, "Export a stage character as a game-ready binary glTF (.glb) with its moves baked in as named animations (idle, walk, run, gestures, CMU motion capture) and optional face morphs, for the Craft engine, Godot or three.js. Loads the file back to check the clips and renders a preview frame.")
def export_character(p: ExportParams) -> OpResult:
    from .scene import list_stages, stage_moves

    stage = _stages_dir() / p.stage
    if not (stage / "index.html").exists():
        raise OpError(f"no stage called {p.stage!r} (have: {', '.join(list_stages()) or 'none'})")
    models = sorted(f.stem for f in (stage / "models").glob("*.json") if f.stem not in RESERVED) if (stage / "models").is_dir() else []
    if p.character not in models:
        raise OpError(f"no character {p.character!r} on stage {p.stage!r} (have: {', '.join(models) or 'none'})")
    known = stage_moves(p.stage)
    moves = known if [m.lower() for m in p.moves] == ["all"] else list(dict.fromkeys(p.moves))
    bad = [m for m in moves if m not in known]
    if bad:
        raise OpError(f"unknown moves {bad} (this stage has: {', '.join(known)})")
    if "idle" not in moves:
        moves = ["idle", *moves]   # gestures are layered over the idle; the idle is also the rest state in a game
    preview = None
    if p.preview:
        preview = (moves[1] if len(moves) > 1 else moves[0]) if p.preview == "auto" else p.preview
        if preview not in moves:
            raise OpError(f"preview move {preview!r} is not one of the exported moves")
    report: dict[str, Any] = {}
    shot_dir = Path(tempfile.mkdtemp(prefix="export-shot-"))

    def build(out: Path) -> None:
        job = {"model": p.character, "moves": moves, "morphs": p.morphs, "inPlace": p.in_place, "out": str(out),
               "check": {"clip": preview, "png": str(shot_dir / "preview.png"), "width": 640, "height": 640} if preview else None}
        report.update(_exporter(job, stage))
        if not out.exists() or out.stat().st_size < 1000:
            raise OpError("export wrote no GLB")
        loaded = report.get("loaded") or {}
        missing = [m for m in moves if m not in {c["name"] for c in loaded.get("clips", [])}]
        if missing:
            raise OpError(f"the exported GLB loaded back without the moves {missing}")

    try:
        res = _guarded("export_character", [], f"{p.character}_game.glb", build)
        res.data.update({"character": p.character, "stage": p.stage, "moves": moves, "morphs": p.morphs, "in_place": p.in_place,
                         "clips": report.get("loaded", {}).get("clips", []), "morph_targets": report.get("loaded", {}).get("morphTargets", 0),
                         "size_bytes": res.outputs[0].size,
                         "use": "three.js: GLTFLoader -> new AnimationMixer(gltf.scene).clipAction(THREE.AnimationClip.findByName(gltf.animations, 'walk')).play(); "
                                "Godot: import the .glb, the moves are in its AnimationPlayer. Scale: real MakeHuman metres (decimetres in some engines: check).",
                         "credits": "MakeHuman assets CC0; CMU motion capture (mocap.cs.cmu.edu, NSF EIA-0196217); three.js Xbot moves (MIT)."})
        png = shot_dir / "preview.png"
        if preview and png.exists():
            shot = _guarded("export_character", [res.outputs[0].id], f"{p.character}_{preview.replace(' ', '_')}_preview.png",
                            lambda out: shutil.copyfile(png, out))
            res.outputs.append(shot.outputs[0])
            res.data["preview"] = {"move": preview, "file_id": shot.outputs[0].id}
        return res
    finally:
        shutil.rmtree(shot_dir, ignore_errors=True)
