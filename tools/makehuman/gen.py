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
rig = HumanService.add_builtin_rig(body, RIG)
rig.name = "Armature"

parts = [("eyes/high-poly/high-poly.mhclo", "Eyes"), ("eyebrows/eyebrow001/eyebrow001.mhclo", "Eyebrows"),
         ("eyelashes/eyelashes01/eyelashes01.mhclo", "Eyelashes"), (f"hair/{HAIR}/{HAIR}.mhclo", "Hair")]
parts += [(f"clothes/{c}/{c}.mhclo", "Clothes") for c in CLOTHES] + [(f"clothes/{SHOES}/{SHOES}.mhclo", "Clothes")]
objs = {}
for p, kind in parts:
    o = HumanService.add_mhclo_asset(A + "/" + p, body, asset_type=kind, subdiv_levels=0, material_type="MAKESKIN")
    objs[os.path.basename(p).split(".")[0]] = o


# MPFB's node materials don't export to glTF: replace each with a plain Principled BSDF + image textures
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
        nm = nt.nodes.new("ShaderNodeNormalMap")
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
