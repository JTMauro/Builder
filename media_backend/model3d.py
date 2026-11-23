"""3D model tools: rig a humanoid, bake vertex colours into a texture, put a model on a stage as a prop.

- rig_model: adds a Mixamo-named skeleton (the same names as Builder's MakeHuman rig) to a humanoid mesh and
  skins it with Blender's automatic (bone heat) weights, so tools/makehuman/view/anim.js retarget() and the
  CMU motion-capture moves play on it. The skeleton is fitted from the mesh's shape (tools/model3d/autorig.py);
  works for T-, A- and arms-down poses of one standing person. Optional check renders a walk frame.
- bake_texture: UV-unwraps a vertex-coloured model (e.g. image_to_3d output) with xatlas and bakes the colours
  into a JPEG texture (tools/image3d/bake.py), optionally reducing the triangles first.
- stage_prop: scales a model to real-world size and saves it as props/<id>/<id>_1k.gltf on a studio stage
  (the layout studio.html loads props from), with its licence, and lists it in props/index.json.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from . import storage
from .config import NO_WINDOW
from .ops import OpError, OpResult, _guarded, _src, _stem, operation

TOOLS = Path(__file__).resolve().parents[1] / "tools"
AUTORIG = TOOLS / "model3d" / "autorig.py"
RIGCHECK = TOOLS / "model3d" / "rigcheck.cjs"
BAKE = TOOLS / "image3d" / "bake.py"
MODEL_TYPES = {".glb", ".gltf", ".fbx", ".obj", ".ply", ".stl"}
BONES = ["Hips", "Spine", "Spine1", "Spine2", "Neck", "Head"] + [f"{s}{b}" for s in ("Left", "Right") for b in
                                                                  ("Shoulder", "Arm", "ForeArm", "Hand", "UpLeg", "Leg", "Foot", "ToeBase")]


def _json_line(text: str) -> dict[str, Any]:
    for line in reversed((text or "").strip().splitlines()):
        if line.startswith("{"):
            try:
                return json.loads(line)
            except ValueError:
                continue
    return {}


def _model_src(file_id: str) -> Path:
    src = _src(file_id)
    if src.suffix.lower() not in MODEL_TYPES:
        raise OpError(f"needs a 3D model ({', '.join(sorted(MODEL_TYPES))}), not {src.suffix or 'a file without extension'}")
    return src


# ---------------------------------------------------------------- rig_model


class RigParams(BaseModel):
    file_id: str = Field(description="A humanoid model (GLB/glTF/FBX/OBJ/PLY/STL): one person standing, e.g. an image_to_3d result or a pack character; any existing rig is replaced")
    height_m: float | None = Field(None, gt=0.1, le=20, description="Scale to this height in metres (e.g. 1.75); default keeps the model's size")
    facing: Literal["auto", "front", "back"] = Field("auto", description="Which way the model looks in its file: auto (from the feet), front (+Z, the glTF/three.js convention) or back")
    weights: Literal["heat", "nearest"] = Field("heat", description="heat = Blender's automatic weights (smooth bends); nearest = distance to the bones (for broken meshes)")
    check: bool = Field(True, description="Also render a frame of it walking (three.js + the Xbot walk, needs node + Playwright) as a preview PNG")


def _rig_check(glb: Path, png: Path) -> dict[str, Any]:
    from .characters import makehuman_dir

    node = shutil.which("node")
    xbot = makehuman_dir() / "view" / "Xbot.glb"
    if not node or not xbot.exists():
        return {"skipped": "needs node + Playwright and ~/.builder/makehuman/view/Xbot.glb"}
    with tempfile.TemporaryDirectory(prefix="rigcheck-") as tmp:
        job = Path(tmp) / "job.json"
        job.write_text(json.dumps({"model": str(glb), "xbot": str(xbot), "shots": [["walk", 0.25, "three"]], "out": tmp}), encoding="utf-8")
        try:
            proc = subprocess.run([node, str(RIGCHECK), str(job)], capture_output=True, text=True, timeout=300, **NO_WINDOW)
        except subprocess.TimeoutExpired:
            return {"skipped": "the walk check timed out"}
        info = _json_line(proc.stdout)
        if info.get("files"):
            shutil.copyfile(info["files"][0], png)
        return {"clips": info.get("clips"), "errors": info.get("errors") or ([proc.stderr.strip()[-300:]] if proc.returncode else [])}


@operation("rig_model", RigParams, "Add a Mixamo-named skeleton (mixamorig:Hips, Spine…Head, arms, legs, toes: the same names as Builder's MakeHuman rig) to a humanoid model and skin it with Blender's automatic weights, so it can walk, run and do the CMU motion-capture moves in three.js stages (anim.js retarget). The skeleton is fitted from the body's shape on the CPU (seconds). Returns a rigged GLB and a preview of it walking.")
def rig_model(p: RigParams) -> OpResult:
    from .asset_packs import blender_python

    src = _model_src(p.file_id)
    script = Path(os.environ.get("BUILDER_AUTORIG") or AUTORIG)
    if script.suffix == ".py" and os.environ.get("BUILDER_AUTORIG"):
        py, env = [sys.executable, "-I"], dict(os.environ)   # a stand-in (tests)
    else:
        py, env = blender_python()
    args = [str(src), "OUT", "--facing", {"auto": "auto", "front": "-y", "back": "+y"}[p.facing], "--weights", p.weights]
    if p.height_m:
        args += ["--height", str(p.height_m)]
    report: dict[str, Any] = {}

    def build(out: Path) -> None:
        proc = subprocess.run([*py, str(script), *[str(out) if a == "OUT" else a for a in args]], capture_output=True, text=True,
                              timeout=900, env=env, **NO_WINDOW)
        info = _json_line(proc.stdout)
        if proc.returncode != 0 or not out.exists() or not info:
            raise OpError("rigging failed:\n" + "\n".join((proc.stderr or proc.stdout).strip().splitlines()[-8:]))
        from .glbtools import read_glb

        doc, _ = read_glb(out.read_bytes())
        names = {n.get("name", "") for n in doc.get("nodes", [])}
        missing = [b for b in BONES if f"mixamorig:{b}" not in names]
        if not doc.get("skins") or missing:
            raise OpError(f"the rigged GLB is missing its skin or bones {missing}")
        report.update(info)

    res = _guarded("rig_model", [p.file_id], f"{_stem(p.file_id)}_rigged.glb", build)
    res.data = {"skeleton": "Mixamo names (mixamorig:*), like Builder's MakeHuman rig", "bones": len(BONES),
                "height_m": round(report.get("height", 0), 3), "facing_was": report.get("front"), "turned": bool(report.get("turned")),
                "weights": report.get("weights"), "landmarks": {k: report.get(k) for k in ("crotch", "neck", "left_arm_down_deg", "right_arm_down_deg")},
                "joints_m": report.get("joints_m"),
                "use": "three.js: retarget(XbotOrCmuScene, clips, gltf.scene, { rename: CMU }) from tools/makehuman/view/anim.js; "
                       "Blender/Godot/Unity read it as a normal skinned GLB (Y up, faces +Z).",
                "limits": "One standing humanoid; fingers aren't rigged; clothes/hair follow the nearest bones; very stylised bodies may need height_m and a check of the preview."}
    if p.check:
        with tempfile.TemporaryDirectory(prefix="rigshot-") as tmp:
            png = Path(tmp) / "walk.png"
            chk = _rig_check(storage.path_of(res.outputs[0].id), png)
            res.data["check"] = chk
            if png.exists():
                shot = _guarded("rig_model", [res.outputs[0].id], f"{_stem(p.file_id)}_rigged_walk.png", lambda out: shutil.copyfile(png, out))
                res.outputs.append(shot.outputs[0])
                res.data["preview"] = shot.outputs[0].id
    return res


# ---------------------------------------------------------------- bake_texture


class BakeParams(BaseModel):
    file_id: str = Field(description="A model with vertex colours (GLB/OBJ/PLY), e.g. an image_to_3d result")
    size: int = Field(1024, ge=256, le=4096, description="Texture width/height in pixels")
    max_faces: int | None = Field(None, ge=500, le=2_000_000, description="Reduce to about this many triangles first (e.g. 10000 for web/games); the texture keeps the colour detail")


@operation("bake_texture", BakeParams, "Turn a vertex-coloured model (e.g. from image_to_3d) into a normal textured GLB: UV unwrap with xatlas, colours baked into a JPEG texture, optional triangle reduction first. Smaller and game/engine-friendly. CPU, seconds.")
def bake_texture(p: BakeParams) -> OpResult:
    import importlib.util

    src = _model_src(p.file_id)
    missing = [m for m in ("xatlas", "trimesh", "numpy", "PIL", "scipy") if importlib.util.find_spec(m) is None]
    if missing:
        raise OpError(f"bake_texture needs {', '.join(missing)}: pip install xatlas trimesh pillow scipy fast-simplification")
    stats: dict[str, Any] = {}
    args = [sys.executable, "-I", str(BAKE), str(src), "OUT", "--size", str(p.size)] + (["--max-faces", str(p.max_faces)] if p.max_faces else [])

    def build(out: Path) -> None:
        proc = subprocess.run([str(out) if a == "OUT" else a for a in args], capture_output=True, text=True, timeout=1800, **NO_WINDOW)
        info = _json_line(proc.stdout)
        if proc.returncode == 4:
            raise OpError(info.get("error") or "the model has no vertex colours to bake")
        if proc.returncode != 0 or not out.exists():
            raise OpError("baking failed:\n" + "\n".join((proc.stderr or proc.stdout).strip().splitlines()[-8:]))
        stats.update(info)

    res = _guarded("bake_texture", [p.file_id], f"{_stem(p.file_id)}_textured.glb", build)
    res.data = {**stats, "size_bytes": res.outputs[0].size, "input_bytes": src.stat().st_size, "unwrap": "xatlas (MIT)"}
    return res


# ---------------------------------------------------------------- stage_prop


class StagePropParams(BaseModel):
    file_id: str = Field(description="The model (GLB/glTF, or FBX/OBJ via Blender)")
    prop_id: str = Field(pattern=r"^[A-Za-z0-9][\w-]{0,63}$", description="Name on the stage, e.g. gladiator_sword (letters, digits, _ and -)")
    stage: str = Field("teen", description="Studio stage folder")
    height_m: float | None = Field(None, gt=0.005, le=200, description="Real-world height in metres (e.g. 0.9 for a barrel)")
    length_m: float | None = Field(None, gt=0.005, le=200, description="Or: real-world size of its longest side in metres (e.g. 0.95 for a sword)")
    max_texture: int = Field(1024, ge=128, le=4096, description="Shrink textures to at most this many pixels (props load fast)")
    overwrite: bool = False


@operation("stage_prop", StagePropParams, "Put a 3D model on a studio stage as a prop at real-world size: scaled (height_m or length_m), feet on the floor, textures shrunk, saved as props/<id>/<id>_1k.gltf with its licence and listed in props/index.json (studio.html / scene_video can then place it or have a character hold it).")
def stage_prop(p: StagePropParams) -> OpResult:
    from .asset_packs import to_glb
    from .assets import license_of
    from .glbtools import read_glb, shrink_textures, split_to_gltf, wrap_scale
    from .scene import list_stages, stages_dir

    src = _model_src(p.file_id)
    stage = stages_dir() / p.stage
    if not (stage / "index.html").exists():
        raise OpError(f"no stage called {p.stage!r} (have: {', '.join(list_stages()) or 'none'})")
    folder = stage / "props" / p.prop_id
    if folder.exists() and not p.overwrite:
        raise OpError(f"the stage already has a prop {p.prop_id!r} (overwrite=true replaces it)")
    if bool(p.height_m) == bool(p.length_m):
        raise OpError("give height_m or length_m (the real-world size), not both")
    info: dict[str, Any] = {}

    def build(out: Path) -> None:
        import trimesh

        with tempfile.TemporaryDirectory(prefix="prop-") as tmp:
            glb = Path(tmp) / "in.glb"
            to_glb(src, glb)
            data = glb.read_bytes()
            scene = trimesh.load(glb, force="scene")
            ext = scene.extents
            if ext is None or max(ext) <= 0:
                raise OpError("the model is empty")
            s = (p.height_m / ext[1]) if p.height_m else (p.length_m / max(ext))
            lift = -float(scene.bounds[0][1]) * s          # feet (its lowest point) on the floor
            data = shrink_textures(wrap_scale(data, float(s), lift), p.max_texture)
            read_glb(data)
            out.write_bytes(data)
            info.update({"scale": round(float(s), 5), "size_m": [round(float(x * s), 3) for x in ext]})

    res = _guarded("stage_prop", [p.file_id], f"{p.prop_id}.glb", build)
    if folder.exists():
        shutil.rmtree(folder)
    split_to_gltf(storage.path_of(res.outputs[0].id).read_bytes(), folder, f"{p.prop_id}_1k")
    lic = license_of(p.file_id)
    if lic:
        (folder / "license.json").write_text(json.dumps(lic, indent=1), encoding="utf-8")
    index = stage / "props" / "index.json"
    try:
        ids = json.loads(index.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        ids = []
    if p.prop_id not in ids:
        ids.append(p.prop_id)
        index.write_text(json.dumps(ids), encoding="utf-8")
    res.data = {"stage": p.stage, "prop": p.prop_id, "path": f"props/{p.prop_id}/{p.prop_id}_1k.gltf", **info,
                "license": (lic or {}).get("license"), "credit": (lic or {}).get("attribution"),
                "hold": f'scene_video beat: "prop": {{"prop": "{p.prop_id}", "hand": "right"}} (add a GRIP entry in studio.html for a better grip)'}
    return res
