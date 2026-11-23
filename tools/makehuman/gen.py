"""Generate a realistic, rigged MakeHuman person headless with Blender (bpy) + MPFB.

    BLENDER_USER_RESOURCES=$S/bhome $S/mhenv/bin/python -I gen.py $S

$S is the work folder made by setup.sh ($S/mhassets, $S/mhtex from tex.py, output in $S/mhgen).
Settings (environment variables):
  OUT       output name                      default person
  AGE       age in years (1..90)             default 25
  GENDER    0 female .. 1 male               default 1
  HEIGHT, WEIGHT, MUSCLE, PROPORTIONS  0..1  default .5 .5 .5 .5
  RACE      "caucasian,african,asian"         default .92,.05,.03
  HAIR      hair asset                        default short02
  CLOTHES   clothes asset(s), comma separated default male_casualsuit06
  SHOES     shoes asset                       default shoes05
  RIG       builtin rig: mixamo | game_engine | default   default mixamo
  SMILE     0 neutral .. 1 broad smile with teeth          default 0
  SHAPE     extra shape targets "group/target:weight,..." (see mpfb/data/targets), e.g. a longer neck
  FRINGE    brow (lift hair that falls below the eyebrows) | keep   default brow
  HAIR_SNUG 0..1: pull hair that stands off the head in towards the scalp (0 = as fitted, default 0)
  MORPHS    talk: also export expression variants (speech mouth shapes, blinks, smile, brows) for morphs.py
Writes $S/mhgen/<OUT>.glb (and .blend). Convert for three.js with glb2json.py.
"""
import json
import os
import subprocess
import sys

import bpy  # noqa: I001  (bpy must come first: it provides bmesh and mathutils)
import bmesh
from mathutils.bvhtree import BVHTree

S = sys.argv[-1]
E = os.environ.get
OUT, HAIR, SHOES, RIG = E("OUT", "person"), E("HAIR", "short02"), E("SHOES", "shoes05"), E("RIG", "mixamo")
CLOTHES = [c for c in E("CLOTHES", "male_casualsuit06").split(",") if c]
A, T, G = S + "/mhassets", S + "/mhtex", S + "/mhgen"
HERE = os.path.dirname(os.path.abspath(__file__))


def age_macro(years):
    # MakeHuman: 0 = 1 year, 0.1875 = 11, 0.5 = 25, 1 = 90
    y = max(1.0, min(90.0, years))
    if y <= 11:
        return (y - 1) / 10 * 0.1875
    if y <= 25:
        return 0.1875 + (y - 11) / 14 * 0.3125
    return 0.5 + (y - 25) / 65 * 0.5


bpy.ops.preferences.addon_enable(module="bl_ext.user_default.mpfb")
from bl_ext.user_default.mpfb.services import HumanService  # noqa: E402

for o in list(bpy.data.objects):
    bpy.data.objects.remove(o, do_unlink=True)
race = [float(x) for x in E("RACE", ".92,.05,.03").split(",")]
macro = {"gender": float(E("GENDER", "1")), "age": age_macro(float(E("AGE", "25"))),
         "muscle": float(E("MUSCLE", ".5")), "weight": float(E("WEIGHT", ".5")),
         "proportions": float(E("PROPORTIONS", ".5")), "height": float(E("HEIGHT", ".5")),
         "cupsize": 0.5, "firmness": 0.5, "race": {"caucasian": race[0], "african": race[1], "asian": race[2]}}
body = HumanService.create_human(macro_detail_dict=macro)
body.name = "Body"
# extra MakeHuman shape targets, before the rig and clothes are fitted: SHAPE="neck/measure-neck-height-incr:0.6,torso/torso-scale-depth-decr:0.3"
if E("SHAPE"):
    import bl_ext.user_default.mpfb as _mpfb0
    from bl_ext.user_default.mpfb.services import TargetService as _TS
    TD = os.path.join(os.path.dirname(_mpfb0.__file__), "data", "targets")
    for item in E("SHAPE").split(","):
        if ":" not in item:
            continue
        nm, w = item.rsplit(":", 1)
        f = os.path.join(TD, nm.strip() + ".target.gz")
        if os.path.exists(f):
            _TS.load_target(body, f, weight=float(w), name=os.path.basename(nm.strip()))
        else:
            print("no such target:", nm)
rig = HumanService.add_builtin_rig(body, RIG)
rig.name = "Armature"

parts = [("eyes/high-poly/high-poly.mhclo", "Eyes"), ("eyebrows/eyebrow001/eyebrow001.mhclo", "Eyebrows"),
         ("eyelashes/eyelashes01/eyelashes01.mhclo", "Eyelashes"), (f"hair/{HAIR}/{HAIR}.mhclo", "Hair")]
parts += [(f"clothes/{c}/{c}.mhclo", "Clothes") for c in CLOTHES] + [(f"clothes/{SHOES}/{SHOES}.mhclo", "Clothes")]
objs = {}
for p, kind in parts:
    o = HumanService.add_mhclo_asset(A + "/" + p, body, asset_type=kind, subdiv_levels=0, material_type="MAKESKIN")
    objs[os.path.basename(p).split(".")[0]] = o


# a smile from MakeHuman's expression units (0 = neutral, 1 = broad smile showing teeth)
SMILE = float(E("SMILE", "0"))
if SMILE > 0:
    import bl_ext.user_default.mpfb as _mpfb
    from bl_ext.user_default.mpfb.services import TargetService
    U = os.path.join(os.path.dirname(_mpfb.__file__), "data", "targets", "expression", "units", "caucasian")
    for unit, w in [("mouth-corner-puller", 0.95), ("mouth-upward-retraction", 0.35), ("mouth-open", 0.18),
                    ("mouth-parling", 0.25), ("eye-left-slit", 0.18), ("eye-right-slit", 0.18)]:
        f = os.path.join(U, unit + ".target.gz")
        if os.path.exists(f):
            TargetService.load_target(body, f, weight=w * SMILE, name="smile-" + unit)


# teeth: for a smile, or for talking (the mouth opens)
if (SMILE > 0 or E("MORPHS", "") == "talk") and os.path.exists(A + "/teeth/teeth_shape01/teeth_shape01.mhclo"):
    objs["teeth"] = HumanService.add_mhclo_asset(A + "/teeth/teeth_shape01/teeth_shape01.mhclo", body, asset_type="Teeth", subdiv_levels=0, material_type="MAKESKIN")

# MPFB's node materials don't export to glTF: replace each with a plain Principled BSDF + image textures
# clothes normal maps are strong; softer creases look more like real cotton (exported as normalTexture.scale)
NORMAL_STRENGTH = {"Clothes_" + c: float(E("CLOTHES_NORMAL", "0.45")) for c in CLOTHES}


def mat(name, img, alpha=False, rough=0.6, normal=None):
    m = bpy.data.materials.new(name)
    m.use_nodes = True
    nt = m.node_tree
    b = nt.nodes["Principled BSDF"]
    tx = nt.nodes.new("ShaderNodeTexImage")
    tx.image = bpy.data.images.load(T + "/" + img)
    nt.links.new(tx.outputs["Color"], b.inputs["Base Color"])
    if alpha:
        nt.links.new(tx.outputs["Alpha"], b.inputs["Alpha"])
    if normal and os.path.exists(T + "/" + normal):
        nx = nt.nodes.new("ShaderNodeTexImage")
        nx.image = bpy.data.images.load(T + "/" + normal)
        nx.image.colorspace_settings.name = "Non-Color"
        nm = nt.nodes.new("ShaderNodeNormalMap"); nm.inputs["Strength"].default_value = NORMAL_STRENGTH.get(name, 1.0)
        nt.links.new(nx.outputs["Color"], nm.inputs["Color"])
        nt.links.new(nm.outputs["Normal"], b.inputs["Normal"])
    b.inputs["Roughness"].default_value = rough
    if img == "skin.jpg":
        rx = nt.nodes.new("ShaderNodeTexImage")
        rx.image = bpy.data.images.load(T + "/skin_rough.png")
        rx.image.colorspace_settings.name = "Non-Color"
        sp = nt.nodes.new("ShaderNodeSeparateColor")
        nt.links.new(rx.outputs["Color"], sp.inputs["Color"])
        nt.links.new(sp.outputs["Green"], b.inputs["Roughness"])
    b.inputs["Metallic"].default_value = 0
    return m


def setmat(o, m):
    o.data.materials.clear()
    o.data.materials.append(m)


# the skin roughness map is written by paint.py; start with a flat one so the first load works
if not os.path.exists(T + "/skin_rough.png"):
    from PIL import Image
    Image.new("RGB", (8, 8), (0, 158, 0)).save(T + "/skin_rough.png")
setmat(body, mat("Skin", "skin.jpg", rough=0.55))
setmat(objs["high-poly"], mat("Eye", "eye.jpg", False, rough=0.12))
# the high-poly eye's outer cornea shell maps to a transparent (black) area: remove it
e = objs["high-poly"]
bm_e = bmesh.new()
bm_e.from_mesh(e.data)
uv_e = bm_e.loops.layers.uv.active
dead = {lp.vert for f in bm_e.faces for lp in f.loops if lp[uv_e].uv.x > 0.87}
bmesh.ops.delete(bm_e, geom=list(dead), context="VERTS")
bm_e.to_mesh(e.data)
bm_e.free()
setmat(objs["eyebrow001"], mat("Brows", "brows.png", True, 0.9))
setmat(objs["eyelashes01"], mat("Lashes", "lashes.png", True, 0.9))
# only short02's normal map is clean; others have empty areas that shade black
setmat(objs[HAIR], mat("Hair", "hair.png", True, 0.8, "hair_n.jpg" if HAIR == "short02" else None))
# single-sided hair: the inner sides of the hair layers otherwise show as black patches
objs[HAIR].data.materials[0].use_backface_culling = True
for c in CLOTHES:
    setmat(objs[c], mat("Clothes_" + c, "clothes.jpg", False, 0.9, "clothes_n.jpg"))
setmat(objs[SHOES], mat("Shoes", "shoes.jpg", False, 0.6))
# the clothes meshes have deep sculpted folds that read as ribs and abs on a fitted tee: smooth them
# (volume-preserving), placed first so it bakes into the rest shape before the armature
CS = float(E("CLOTHES_SMOOTH", "0.7"))
if CS > 0:
    for c in CLOTHES:
        o = objs[c]
        md = o.modifiers.new("Smooth folds", "LAPLACIANSMOOTH")
        md.lambda_factor = CS; md.iterations = 6; md.use_volume_preserve = True; md.use_normalized = True
        while o.modifiers.find("Smooth folds") > 0:
            bpy.context.view_layer.objects.active = o
            bpy.ops.object.modifier_move_up(modifier="Smooth folds")
if "teeth" in objs:
    tm = bpy.data.materials.new("Teeth"); tm.use_nodes = True
    tb = tm.node_tree.nodes["Principled BSDF"]; tb.inputs["Base Color"].default_value = (0.86, 0.83, 0.76, 1); tb.inputs["Roughness"].default_value = 0.3
    setmat(objs["teeth"], tm)

# keep the eyes clear: lift any fringe that falls below the eyebrows (MakeHuman faces -Y in Blender)
if E("FRINGE", "brow") == "brow":
    eye = objs["high-poly"]
    ez = [(eye.matrix_world @ v.co) for v in eye.data.vertices]
    brow_z = max(p.z for p in ez) + 0.012
    eye_front = min(p.y for p in ez)
    hair = objs[HAIR]
    inv = hair.matrix_world.inverted()
    for v in hair.data.vertices:
        w = hair.matrix_world @ v.co
        if w.z < brow_z and w.z > brow_z - 0.09 and w.y < eye_front + 0.035:
            w.z = brow_z - (brow_z - w.z) * 0.22
            v.co = inv @ w

# snug hair: hair standing off the head (a helmet look in close-ups) is pulled towards the scalp,
# keeping a few millimetres of thickness; SNUG=1 hugs the head, 0.5 halves the gap
SNUG = float(E("HAIR_SNUG", "0") or 0)
if SNUG > 0:
    dg0 = bpy.context.evaluated_depsgraph_get()
    bev = body.evaluated_get(dg0); bmesh0 = bev.to_mesh()
    bbvh = BVHTree.FromPolygons([bev.matrix_world @ v.co for v in bmesh0.vertices], [p.vertices[:] for p in bmesh0.polygons])
    bev.to_mesh_clear()
    hair = objs[HAIR]; inv = hair.matrix_world.inverted(); moved = 0
    for v in hair.data.vertices:
        w = hair.matrix_world @ v.co
        near, nrm, _, d = bbvh.find_nearest(w)
        if near is None or d is None or d < 0.007:
            continue
        out = (w - near).normalized()
        v.co = inv @ (near + out * (0.007 + (d - 0.007) * (1 - SNUG)))
        moved += 1
    print(f"snug hair: moved {moved} of {len(hair.data.vertices)} vertices")

# scalp: skin covered by the hair (a ray along the normal hits the hair) -> UV triangles for paint.py
dg = bpy.context.evaluated_depsgraph_get()
hobj = objs[HAIR].evaluated_get(dg)
hm = hobj.to_mesh()
bvh = BVHTree.FromPolygons([hobj.matrix_world @ v.co for v in hm.vertices], [p.vertices[:] for p in hm.polygons])
hobj.to_mesh_clear()
bm = bmesh.new()
bm.from_object(body, dg)
bm.transform(body.matrix_world)
bm.normal_update()
hp = rig.matrix_world @ rig.pose.bones[("mixamorig:Head" if RIG == "mixamo" else "head")].head
flag = {}
for v in bm.verts:
    if v.co.z < hp.z - 0.03:
        continue
    hit = bvh.ray_cast(v.co + v.normal * 0.0005, v.normal, 0.045)
    flag[v.index] = 1.0 if hit[0] is not None else 0.0
uvl = bm.loops.layers.uv.active
tris = []
for f in bm.faces:
    fl = [flag.get(lp.vert.index, 0.0) for lp in f.loops]
    if max(fl) == 0:
        continue
    tris.append([[lp[uvl].uv.x, lp[uvl].uv.y, flag.get(lp.vert.index, 0.0)] for lp in f.loops])
os.makedirs(G, exist_ok=True)
json.dump(tris, open(G + "/scalp_tris.json", "w"))
bm.free()
print(subprocess.run(["python3", "-I", HERE + "/paint.py", T, G + "/scalp_tris.json"], capture_output=True, text=True).stdout)
for im in bpy.data.images:
    if im.filepath.endswith("skin.jpg") or im.filepath.endswith("skin_rough.png"):
        im.reload()

bpy.ops.wm.save_as_mainfile(filepath=f"{G}/{OUT}.blend")
bpy.ops.object.select_all(action="DESELECT")
# export_apply bakes MPFB's "hide helpers" mask and the clothes' delete-vertex masks
bpy.ops.export_scene.gltf(filepath=f"{G}/{OUT}.glb", export_format="GLB", export_apply=True,
                          export_animations=False, export_morph=False, export_yup=True)
print("wrote", f"{G}/{OUT}.glb")

# ---------------------------------------------------------------- expressions as morph targets
# MORPHS=talk exports one extra GLB per expression (mouth shapes for speech, blinks, smile, brows) with the
# proxies (lashes, brows, teeth) refitted; morphs.py then merges them into the main GLB as morph targets.
EXPRESSIONS = {
    "viseme_A": {"mouth-compression": 0.7},                                   # M B P
    "viseme_B": {"mouth-open": 0.12, "mouth-retraction": 0.35},               # most consonants, EE
    "viseme_C": {"mouth-open": 0.3, "mouth-retraction": 0.25},                # EH AE
    "viseme_D": {"mouth-open": 0.68},                                         # AA
    "viseme_E": {"mouth-open": 0.28, "mouth-pursing": 0.35},                  # AO ER
    "viseme_F": {"mouth-pursing": 0.85, "mouth-open": 0.08},                  # OO W
    "viseme_G": {"mouth-open": 0.06, "mouth-retraction": 0.2, "mouth-compression": 0.3},   # F V
    "viseme_H": {"mouth-open": 0.34, "mouth-retraction": 0.1},                # L
    "blink_L": {"eye-left-closure": 1.0},
    "blink_R": {"eye-right-closure": 1.0},
    "smile": {"mouth-corner-puller": 0.95, "mouth-upward-retraction": 0.35, "mouth-open": 0.18, "mouth-parling": 0.25,
              "eye-left-slit": 0.18, "eye-right-slit": 0.18},
    "brows_up": {"eyebrows-left-up": 0.7, "eyebrows-right-up": 0.7},
    "brows_frown": {"eyebrows-left-down": 0.6, "eyebrows-right-down": 0.6, "eyebrows-left-inner-up": 0.0},
}
if E("MORPHS", "") == "talk":
    import bl_ext.user_default.mpfb as _mpfb2
    from bl_ext.user_default.mpfb.services import ClothesService, TargetService as _TS2

    UD = os.path.join(os.path.dirname(_mpfb2.__file__), "data", "targets", "expression", "units", "caucasian")
    units = sorted({u for e in EXPRESSIONS.values() for u in e})
    keys = {}
    for u in units:
        f = os.path.join(UD, u + ".target.gz")
        if os.path.exists(f):
            keys[u] = _TS2.load_target(body, f, weight=0.0, name="x-" + u).name
    # only the face's own pieces follow an expression; refitting hair or clothes would undo their fixes
    proxies = [o for k, o in objs.items() if k.lower().startswith(("eyebrow", "eyelash", "teeth"))]
    print("refit per expression:", [o.name for o in proxies])
    variants = {}
    for name, weights in EXPRESSIONS.items():
        for u, kname in keys.items():
            body.data.shape_keys.key_blocks[kname].value = weights.get(u, 0.0)
        for o in proxies:
            ClothesService.fit_clothes_to_human(o, body, set_parent=False)
        path = f"{G}/{OUT}__{name}.glb"
        bpy.ops.object.select_all(action="DESELECT")
        bpy.ops.export_scene.gltf(filepath=path, export_format="GLB", export_apply=True,
                                  export_animations=False, export_morph=False, export_yup=True)
        variants[name] = path
    json.dump(variants, open(f"{G}/{OUT}__variants.json", "w"), indent=1)
    print("expression variants:", ", ".join(variants))
