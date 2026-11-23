"""Convert a 3D model (FBX, OBJ, glTF, GLB, DAE, PLY, STL) to one self-contained GLB with Blender (bpy).

    BLENDER_USER_RESOURCES=... MHENV/bin/python -I convert.py IN OUT.glb [--strip-rig] [--no-anim]

Run by media_backend/asset_packs.py and model3d.py. Prints one JSON line with stats.
"""
import argparse
import json
import sys

import bpy  # noqa: E402  (bpy before mathutils)


def load(path: str) -> None:
    low = path.lower()
    bpy.ops.wm.read_factory_settings(use_empty=True)
    if low.endswith(".fbx"):
        bpy.ops.import_scene.fbx(filepath=path, automatic_bone_orientation=True)
    elif low.endswith(".obj"):
        bpy.ops.wm.obj_import(filepath=path)
    elif low.endswith((".glb", ".gltf")):
        bpy.ops.import_scene.gltf(filepath=path)
    elif low.endswith(".dae"):
        bpy.ops.wm.collada_import(filepath=path)
    elif low.endswith(".ply"):
        bpy.ops.wm.ply_import(filepath=path)
    elif low.endswith(".stl"):
        bpy.ops.wm.stl_import(filepath=path)
    else:
        raise SystemExit(f"can't read {path}")


COLOURS = {"red": (0.62, 0.08, 0.06), "blue": (0.1, 0.22, 0.6), "green": (0.15, 0.42, 0.12), "yellow": (0.85, 0.7, 0.1),
           "gold": (0.83, 0.62, 0.18), "silver": (0.62, 0.64, 0.67), "steel": (0.45, 0.48, 0.52), "metal": (0.45, 0.47, 0.5),
           "iron": (0.35, 0.36, 0.38), "black": (0.04, 0.04, 0.04), "white": (0.9, 0.9, 0.9), "brown": (0.3, 0.16, 0.07),
           "wood": (0.38, 0.22, 0.1), "leather": (0.32, 0.18, 0.09), "grey": (0.4, 0.4, 0.4), "gray": (0.4, 0.4, 0.4),
           "orange": (0.85, 0.38, 0.06), "purple": (0.35, 0.12, 0.5), "pink": (0.9, 0.45, 0.6), "skin": (0.8, 0.58, 0.45),
           "eye": (0.05, 0.05, 0.06), "rope": (0.6, 0.5, 0.32), "stone": (0.5, 0.48, 0.45)}


def fix_materials(search: str = "") -> list[str]:
    """Old FBX exports read back with alpha 0 (invisible) or lose their colours (plain 0.8 grey). Make them
    opaque, and give a grey material named after a colour ("Red", "DarkWood", "Silver") that colour."""
    fixed = []
    for m in bpy.data.materials:
        if not m.use_nodes:
            continue
        bsdf = next((n for n in m.node_tree.nodes if n.type == "BSDF_PRINCIPLED"), None)
        if not bsdf:
            continue
        alpha = bsdf.inputs["Alpha"]
        if not alpha.is_linked and alpha.default_value < 0.02:
            alpha.default_value = 1.0
            if hasattr(m, "blend_method"):
                m.blend_method = "OPAQUE"
            if hasattr(m, "surface_render_method"):
                m.surface_render_method = "DITHERED"
            fixed.append(f"{m.name}: opaque")
        base = bsdf.inputs["Base Color"]
        name = m.name.lower()
        spec = bsdf.inputs.get("Specular IOR Level")
        if spec is not None and not spec.is_linked and spec.default_value > 0.9:   # FBX specular 1.0 reads back as a chrome look
            spec.default_value = 0.5
        if search and not base.is_linked:   # a material named after its texture file ("Warrior_Texture" -> Warrior_Texture.png)
            import os

            want = {m.name.lower() + e for e in (".png", ".jpg", ".jpeg")}
            hits = sorted((os.path.join(d, f) for d, _, fs in os.walk(search) for f in fs if f.lower() in want), key=len)
            if hits:
                tex = m.node_tree.nodes.new("ShaderNodeTexImage")
                tex.image = bpy.data.images.load(hits[0])
                m.node_tree.links.new(tex.outputs["Color"], base)
                fixed.append(f"{m.name}: texture {os.path.basename(hits[0])}")
                continue
        if not base.is_linked and all(abs(c - 0.8) < 0.01 for c in base.default_value[:3]):
            word = next((w for w in COLOURS if w in name), None)
            if word:
                k = 0.55 if "dark" in name else 1.3 if "light" in name else 1.0
                base.default_value = (*[min(1.0, c * k) for c in COLOURS[word]], 1.0)
                if word in ("silver", "steel", "metal", "iron", "gold"):
                    bsdf.inputs["Metallic"].default_value = 0.6
                fixed.append(f"{m.name}: {word}")
    return fixed


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("src")
    ap.add_argument("out")
    ap.add_argument("--strip-rig", action="store_true", help="remove armatures and keep the meshes in their current pose")
    ap.add_argument("--no-anim", action="store_true")
    ap.add_argument("--search", default="", help="folder to look in for textures the file names but Blender can't find")
    a = ap.parse_args(sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else sys.argv[1:])
    load(a.src)
    if a.strip_rig:
        for o in list(bpy.data.objects):
            if o.type == "MESH":
                for m in list(o.modifiers):
                    if m.type == "ARMATURE":
                        bpy.context.view_layer.objects.active = o
                        bpy.ops.object.modifier_apply(modifier=m.name)
                o.vertex_groups.clear()
        for o in list(bpy.data.objects):
            if o.type == "ARMATURE":
                for ch in o.children:
                    mw = ch.matrix_world.copy()
                    ch.parent = None
                    ch.matrix_world = mw
                bpy.data.objects.remove(o)
    meshes = [o for o in bpy.data.objects if o.type == "MESH"]
    if not meshes:
        raise SystemExit("no meshes in the file")
    if a.search:
        import os

        for img in bpy.data.images:
            path = bpy.path.abspath(img.filepath) if img.filepath else ""
            if img.source == "FILE" and path and not os.path.exists(path):
                base = os.path.basename(path.replace("\\", "/"))
                hits = sorted((os.path.join(d, f) for d, _, fs in os.walk(a.search) for f in fs if f.lower() == base.lower()), key=len)
                if hits:
                    img.filepath = hits[0]
                    img.reload()
    fixed = fix_materials(a.search)
    bpy.ops.export_scene.gltf(filepath=a.out, export_format="GLB", export_animations=not a.no_anim, export_yup=True)
    print(json.dumps({"meshes": len(meshes), "armatures": sum(o.type == "ARMATURE" for o in bpy.data.objects),
                      "triangles": sum(sum(len(p.vertices) - 2 for p in o.data.polygons) for o in meshes), "material_fixes": fixed}))


if __name__ == "__main__":
    main()
