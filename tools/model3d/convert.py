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


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("src")
    ap.add_argument("out")
    ap.add_argument("--strip-rig", action="store_true", help="remove armatures and keep the meshes in their current pose")
    ap.add_argument("--no-anim", action="store_true")
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
    bpy.ops.export_scene.gltf(filepath=a.out, export_format="GLB", export_animations=not a.no_anim, export_yup=True)
    print(json.dumps({"meshes": len(meshes), "armatures": sum(o.type == "ARMATURE" for o in bpy.data.objects),
                      "triangles": sum(sum(len(p.vertices) - 2 for p in o.data.polygons) for o in meshes)}))


if __name__ == "__main__":
    main()
