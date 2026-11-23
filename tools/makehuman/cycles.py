"""Photoreal still of a MakeHuman character with Blender Cycles (headless, CPU).

    S=~/.builder/makehuman
    BLENDER_USER_RESOURCES=$S/bhome $S/mhenv/bin/python -I tools/makehuman/cycles.py \\
        $S/mhgen/teen.blend $S/shots/teen.png --shot closeup --pose relaxed --percent 50

Input is a .blend from gen.py (materials Skin, Eye, Hair, Brows, Lashes, Clothes_*, Shoes, Teeth and
the `mixamorig:*` rig) or a .glb exported from it. The skin gets subsurface scattering, the eyes a wet
coat, hair/brows/lashes a clipped alpha (Cycles stochastic transparency), the clothes keep their textures.
Light: an HDRI (default ~/.builder/dl/hdri/studio.hdr) plus an optional soft key light, on a curved
studio backdrop (grey, white), a shadow catcher (transparent PNG) or nothing (the HDRI shows).
The camera is framed from the posed mesh bounds and the head bone. Prints timings.
"""
import argparse
import math
import os
import sys
import time
import warnings

import bpy  # noqa: I001  (bpy must come first: it provides bmesh and mathutils)
import bmesh
from mathutils import Matrix, Vector

T0 = time.time()
warnings.filterwarnings("ignore", category=DeprecationWarning)  # use_nodes is going away in Blender 6
HOME = os.path.expanduser("~")
DEFAULT_HDRI = os.path.join(HOME, ".builder", "dl", "hdri", "studio.hdr")

# gen.py's smile = these expression units * SMILE (keys named "smile-<unit>")
SMILE_UNITS = {"mouth-corner-puller": 0.95, "mouth-upward-retraction": 0.35, "mouth-open": 0.18,
               "mouth-parling": 0.25, "eye-left-slit": 0.18, "eye-right-slit": 0.18}

# shot -> (lens mm, lower edge, upper edge, view angle around the body in degrees, default f-stop)
# edges: "feet" | "hips" | "chest" | "neck" (heights from the rig), upper edge is always above the head
SHOTS = {
    "full": (50, "feet", 0.07, 0, 4.0),
    "medium": (70, "hips", 0.10, 0, 2.8),
    "closeup": (85, "neck", 0.10, 0, 2.0),
    "portrait3q": (85, "chest", 0.13, 32, 2.2),
}


def log(*a):
    print(f"[cycles {time.time() - T0:6.1f}s]", *a, flush=True)


def parse_args(argv):
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("input", help=".blend (from gen.py) or .glb")
    p.add_argument("output", help="output .png")
    p.add_argument("--shot", choices=sorted(SHOTS), default="full")
    p.add_argument("--lens", type=float, help="focal length in mm (default per shot, 50-85)")
    p.add_argument("--angle", type=float, help="camera angle around the person in degrees (+ = their right side)")
    p.add_argument("--dof", action="store_true", help="depth of field, focused on the eyes")
    p.add_argument("--fstop", type=float, help="aperture f-stop for --dof (default per shot)")
    p.add_argument("--pose", choices=["none", "relaxed"], default="none",
                   help="relaxed: lower the arms from the A-pose to a natural standing pose")
    p.add_argument("--smile", type=float, help="0..1, scales gen.py's smile shape keys (if present)")
    p.add_argument("--shape-key", action="append", default=[], metavar="NAME=VALUE",
                   help="set a body shape key, or load a MakeHuman target onto a .blend body: an expression unit "
                        "(mouth-open, eye-left-closure, eyebrows-left-up ...) or group/target (repeatable)")
    p.add_argument("--hdri", default=DEFAULT_HDRI, help="equirectangular .hdr/.exr for the world light")
    p.add_argument("--hdri-strength", type=float, default=1.0)
    p.add_argument("--hdri-rotate", type=float, default=0.0, help="degrees around the vertical axis")
    p.add_argument("--key", type=float, default=80.0, help="soft key light power in W (0 = off)")
    p.add_argument("--rim", type=float, default=60.0, help="rim light power in W behind the person (0 = off)")
    p.add_argument("--backdrop", choices=["grey", "white", "none", "shadow"], default="grey",
                   help="curved studio backdrop, transparent shadow catcher, or none (HDRI visible)")
    p.add_argument("--samples", type=int, default=64)
    p.add_argument("--no-denoise", action="store_true")
    p.add_argument("--res", default="1920x1080", help="WxH, e.g. 1920x1080, 3840x2160, 1080x1350")
    p.add_argument("--percent", type=int, default=100, help="resolution scale for quick previews")
    p.add_argument("--view", default="AgX", help="view transform: AgX | Filmic | Khronos PBR Neutral | Standard")
    p.add_argument("--look", default="Medium High Contrast", help="AgX look, e.g. 'Base Contrast', 'Punchy'")
    p.add_argument("--exposure", type=float, default=-0.5, help="stops (the studio HDRI + key are bright)")
    p.add_argument("--skin-even", type=float, default=0.35,
                   help="0..1, blend the skin texture toward its average tone (hides pale texture patches)")
    p.add_argument("--subdiv", type=int, default=1, help="render subdivision of the body (0 = off)")
    p.add_argument("--threads", type=int, default=0, help="CPU threads (0 = all)")
    p.add_argument("--save-blend", help="also save the lit scene as a .blend (for checking in Blender)")
    return p.parse_args(argv)


# ---------------------------------------------------------------- loading
def load(path):
    if path.lower().endswith(".blend"):
        bpy.ops.wm.open_mainfile(filepath=os.path.abspath(path))
    elif path.lower().endswith((".glb", ".gltf")):
        bpy.ops.wm.read_factory_settings(use_empty=True)
        bpy.ops.import_scene.gltf(filepath=os.path.abspath(path))
    else:
        sys.exit(f"unsupported input {path} (use .blend or .glb)")
    # drop anything that is not the character (cameras, lights from an earlier session)
    for o in list(bpy.data.objects):
        if o.type in {"CAMERA", "LIGHT"}:
            bpy.data.objects.remove(o, do_unlink=True)
    arm = next((o for o in bpy.data.objects if o.type == "ARMATURE"), None)
    meshes = [o for o in bpy.data.objects if o.type == "MESH" and o.visible_get()]
    body = max(meshes, key=lambda o: len(o.data.vertices) if mat_kind(o) == "skin" else 0)
    return arm, body, meshes


def mat_kind(o_or_m):
    m = o_or_m
    if isinstance(o_or_m, bpy.types.Object):
        m = o_or_m.active_material
    if m is None:
        return ""
    n = m.name.lower()
    for k in ("skin", "eye", "hair", "brows", "lashes", "clothes", "shoes", "teeth"):
        if n.startswith(k):
            return k
    return ""


# ---------------------------------------------------------------- pose and shape keys
def bone(arm, name):
    if arm is None:
        return None
    for n in (name, "mixamorig:" + name, "mixamorig_" + name):
        if n in arm.pose.bones:
            return arm.pose.bones[n]
    return None


def aim_bone(arm, pb, direction):
    """Rotate a pose bone (in world space, about its head) so it points along `direction`."""
    mw = arm.matrix_world
    head = mw @ pb.head
    cur = (mw @ pb.tail) - head
    rot = cur.rotation_difference(Vector(direction).normalized()).to_matrix().to_4x4()
    world = Matrix.Translation(head) @ rot @ Matrix.Translation(-head) @ mw @ pb.matrix
    pb.matrix = mw.inverted() @ world
    bpy.context.view_layer.update()


def relaxed_pose(arm):
    # MakeHuman faces -Y (Blender Z up): arms hang down, slightly out from the hips, elbows a little bent
    for side, sx in (("Left", 1), ("Right", -1)):
        up, fore, hand = bone(arm, side + "Arm"), bone(arm, side + "ForeArm"), bone(arm, side + "Hand")
        if not (up and fore):
            log("relaxed pose: no", side, "arm bones, skipped")
            continue
        aim_bone(arm, up, (sx * 0.20, 0.03, -1.0))
        aim_bone(arm, fore, (sx * 0.10, -0.22, -1.0))
        if hand:
            aim_bone(arm, hand, (sx * 0.06, -0.12, -1.0))
    log("pose: relaxed arms")


def mpfb_targets():
    """MPFB's target folder (needs the add-on, i.e. BLENDER_USER_RESOURCES=$S/bhome), or None."""
    try:
        bpy.ops.preferences.addon_enable(module="bl_ext.user_default.mpfb")
        import bl_ext.user_default.mpfb as mpfb
        from bl_ext.user_default.mpfb.services import TargetService
        return os.path.join(os.path.dirname(mpfb.__file__), "data", "targets"), TargetService
    except Exception as e:  # noqa: BLE001
        log("MPFB add-on not available:", e)
        return None, None


def load_target(body, name, key_name):
    """Load a MakeHuman target (expression unit like `mouth-open`, or `group/target`) as a shape key.
    Only for an MPFB body from a .blend (a .glb has lost the helper vertices the targets index)."""
    if not any(m.type == "MASK" and m.name == "Hide helpers" for m in body.modifiers):
        return None
    td, ts = mpfb_targets()
    if td is None:
        return None
    for f in (os.path.join(td, "expression", "units", "caucasian", name + ".target.gz"),
              os.path.join(td, name + ".target.gz")):
        if os.path.exists(f):
            return ts.load_target(body, f, weight=0.0, name=key_name)
    return None


def set_shape_keys(body, meshes, smile, pairs):
    keys = lambda: body.data.shape_keys.key_blocks if body.data.shape_keys else {}  # noqa: E731
    changed = False

    def setkey(k, v):
        k.slider_min = min(k.slider_min, v)
        k.slider_max = max(k.slider_max, v)
        k.value = v

    if smile is not None:
        smile = max(0.0, min(1.0, smile))
        for unit, w in SMILE_UNITS.items():
            k = keys().get("smile-" + unit) or (load_target(body, unit, "smile-" + unit) if smile > 0 else None)
            if k is not None:
                setkey(k, w * smile)
                changed = True
        if not changed:
            log("--smile: no smile shape keys and no MPFB body to load them on (use the .blend)")
        elif smile > 0 and not any(mat_kind(o) == "teeth" for o in meshes):
            log("--smile: this character has no teeth (make it with SMILE or MORPHS=talk in gen.py)")
    for item in pairs:
        name, _, val = item.partition("=")
        k = keys().get(name) or load_target(body, name, "x-" + os.path.basename(name))
        if k is None:
            log("no shape key or MakeHuman target", name)
            continue
        setkey(k, float(val))
        changed = True
    if changed:
        # refit teeth, lashes and brows to the new face (as gen.py does for its expressions)
        try:
            mpfb_targets()  # enables the add-on
            from bl_ext.user_default.mpfb.services import ClothesService
            for o in meshes:
                if o is not body and mat_kind(o) in {"teeth", "lashes", "brows"}:
                    ClothesService.fit_clothes_to_human(o, body, set_parent=False)
            log("shape keys set, proxies refitted")
        except Exception as e:  # noqa: BLE001
            log("shape keys set (proxies not refitted:", e, ")")


# ---------------------------------------------------------------- materials
def nodes_of(m):
    m.use_nodes = True
    nt = m.node_tree
    bsdf = next((n for n in nt.nodes if n.type == "BSDF_PRINCIPLED"), None)
    if bsdf is None:
        bsdf = nt.nodes.new("ShaderNodeBsdfPrincipled")
        out = next(n for n in nt.nodes if n.type == "OUTPUT_MATERIAL")
        nt.links.new(bsdf.outputs[0], out.inputs["Surface"])
    return nt, bsdf


def linked_image(nt, socket):
    for lk in nt.links:
        if lk.to_socket == socket and lk.from_node.type == "TEX_IMAGE":
            return lk.from_node
    return None


def setin(bsdf, name, v):
    if name in bsdf.inputs:
        bsdf.inputs[name].default_value = v


def alpha_clip(nt, bsdf, lo, hi, backface_cull=False):
    """Base-colour texture alpha -> clipped (slightly soft) Principled Alpha. Cycles treats alpha as
    stochastic transparency, so a hard-ish clip keeps strands crisp instead of a grey haze."""
    tex = linked_image(nt, bsdf.inputs["Base Color"])
    if tex is None:
        return
    tex.image.alpha_mode = "STRAIGHT"
    tex.interpolation = "Cubic"
    for lk in list(nt.links):
        if lk.to_socket == bsdf.inputs["Alpha"]:
            nt.links.remove(lk)
    mr = nt.nodes.new("ShaderNodeMapRange")
    mr.inputs["From Min"].default_value = lo
    mr.inputs["From Max"].default_value = hi
    mr.clamp = True
    nt.links.new(tex.outputs["Alpha"], mr.inputs["Value"])
    out = mr.outputs["Result"]
    if backface_cull:
        geo = nt.nodes.new("ShaderNodeNewGeometry")
        inv = nt.nodes.new("ShaderNodeMath"); inv.operation = "SUBTRACT"; inv.inputs[0].default_value = 1.0
        nt.links.new(geo.outputs["Backfacing"], inv.inputs[1])
        mul = nt.nodes.new("ShaderNodeMath"); mul.operation = "MULTIPLY"
        nt.links.new(out, mul.inputs[0]); nt.links.new(inv.outputs[0], mul.inputs[1])
        out = mul.outputs[0]
    nt.links.new(out, bsdf.inputs["Alpha"])


def even_skin(nt, b, amount):
    """MakeHuman's skin textures have pale, grey cheek patches that subsurface light makes obvious:
    blend the texture part-way toward its own average tone."""
    tex = linked_image(nt, b.inputs["Base Color"])
    if tex is None or amount <= 0:
        return
    try:
        import numpy as np
        im = tex.image
        px = np.empty(im.size[0] * im.size[1] * 4, dtype=np.float32)
        im.pixels.foreach_get(px)
        avg = px.reshape(-1, 4)[::7, :3].mean(0)   # the texture's fill colour is the base skin tone
        if not im.is_float:                         # 8-bit pixels are sRGB-encoded; shader colours are linear
            avg = np.where(avg <= 0.04045, avg / 12.92, ((avg + 0.055) / 1.055) ** 2.4)
    except Exception as e:  # noqa: BLE001
        log("skin evening skipped:", e)
        return
    mix = nt.nodes.new("ShaderNodeMix")
    mix.data_type = "RGBA"
    mix.blend_type = "MIX"
    mix.inputs["Factor"].default_value = amount
    mix.inputs[7].default_value = (*avg.tolist(), 1.0)      # B (colour)
    nt.links.new(tex.outputs["Color"], mix.inputs[6])         # A (colour)
    nt.links.new(mix.outputs[2], b.inputs["Base Color"])


def skin_material(m, even=0.0):
    nt, b = nodes_of(m)
    even_skin(nt, b, even)
    b.subsurface_method = "RANDOM_WALK_SKIN"
    setin(b, "Subsurface Weight", 1.0)
    setin(b, "Subsurface Radius", (1.0, 0.38, 0.2))
    setin(b, "Subsurface Scale", 0.004)       # metres: MakeHuman is real scale
    setin(b, "Subsurface IOR", 1.4)
    setin(b, "Subsurface Anisotropy", 0.8)
    setin(b, "Specular IOR Level", 0.5)
    setin(b, "IOR", 1.4)
    setin(b, "Coat Weight", 0.08)            # faint oily sheen
    setin(b, "Coat Roughness", 0.35)
    setin(b, "Sheen Weight", 0.08)           # peach fuzz at grazing angles
    setin(b, "Sheen Roughness", 0.4)
    # roughness: the painted map (green, ~0.62) scaled toward ~0.5, or a flat 0.5
    rough_src = None
    for lk in nt.links:
        if lk.to_socket == b.inputs["Roughness"]:
            rough_src = lk.from_socket
    if rough_src is not None:
        mul = nt.nodes.new("ShaderNodeMath"); mul.operation = "MULTIPLY_ADD"
        mul.inputs[1].default_value = 0.65; mul.inputs[2].default_value = 0.1
        nt.links.new(rough_src, mul.inputs[0]); nt.links.new(mul.outputs[0], b.inputs["Roughness"])
    else:
        setin(b, "Roughness", 0.5)
    # pores: fine noise bump in object space (the skin texture has no normal map)
    if not b.inputs["Normal"].is_linked:
        tc = nt.nodes.new("ShaderNodeTexCoord")
        nz = nt.nodes.new("ShaderNodeTexNoise"); nz.inputs["Scale"].default_value = 900.0
        nz.inputs["Detail"].default_value = 3.0
        nt.links.new(tc.outputs["Object"], nz.inputs["Vector"])
        bump = nt.nodes.new("ShaderNodeBump"); bump.inputs["Strength"].default_value = 0.12
        bump.inputs["Distance"].default_value = 0.0004
        nt.links.new(nz.outputs["Fac"], bump.inputs["Height"]); nt.links.new(bump.outputs["Normal"], b.inputs["Normal"])


def eye_material(m):
    nt, b = nodes_of(m)
    setin(b, "Roughness", 0.25)
    setin(b, "Coat Weight", 1.0)             # wet cornea highlight
    setin(b, "Coat Roughness", 0.02)
    setin(b, "Coat IOR", 1.376)
    setin(b, "Specular IOR Level", 0.5)
    b.subsurface_method = "RANDOM_WALK"
    setin(b, "Subsurface Weight", 0.15)       # sclera isn't chalky
    setin(b, "Subsurface Radius", (1.0, 0.5, 0.4))
    setin(b, "Subsurface Scale", 0.002)


def hair_material(m):
    nt, b = nodes_of(m)
    setin(b, "Roughness", 0.55)
    setin(b, "Specular IOR Level", 0.4)
    setin(b, "Sheen Weight", 0.15)
    setin(b, "Sheen Roughness", 0.3)
    setin(b, "Anisotropic", 0.0)
    alpha_clip(nt, b, 0.25, 0.55)
    # strand relief from the texture's own light/dark streaks (when there is no normal map)
    tex = linked_image(nt, b.inputs["Base Color"])
    if tex is not None and not b.inputs["Normal"].is_linked:
        bump = nt.nodes.new("ShaderNodeBump")
        bump.inputs["Strength"].default_value = 0.35
        bump.inputs["Distance"].default_value = 0.0015
        nt.links.new(tex.outputs["Color"], bump.inputs["Height"])
        nt.links.new(bump.outputs["Normal"], b.inputs["Normal"])


def fine_alpha(m, rough, lo, hi):
    nt, b = nodes_of(m)
    setin(b, "Roughness", rough)
    setin(b, "Specular IOR Level", 0.35)
    alpha_clip(nt, b, lo, hi)


def cloth_material(m):
    nt, b = nodes_of(m)
    if not b.inputs["Roughness"].is_linked:
        setin(b, "Roughness", 0.85)
    setin(b, "Specular IOR Level", 0.3)
    setin(b, "Sheen Weight", 0.35)            # soft fibre sheen
    setin(b, "Sheen Roughness", 0.5)
    for lk in list(nt.links):                 # opaque cloth: don't let a texture alpha punch holes
        if lk.to_socket == b.inputs["Alpha"]:
            nt.links.remove(lk)
    setin(b, "Alpha", 1.0)


def teeth_material(m):
    nt, b = nodes_of(m)
    setin(b, "Roughness", 0.25)
    b.subsurface_method = "RANDOM_WALK"
    setin(b, "Subsurface Weight", 0.4)
    setin(b, "Subsurface Radius", (1.0, 0.9, 0.7))
    setin(b, "Subsurface Scale", 0.001)


def setup_materials(even):
    done = []
    for m in bpy.data.materials:
        if m.users == 0:
            continue
        k = mat_kind(m)
        {"skin": lambda m: skin_material(m, even), "eye": eye_material, "hair": hair_material,
         "brows": lambda m: fine_alpha(m, 0.6, 0.2, 0.85), "lashes": lambda m: fine_alpha(m, 0.5, 0.12, 0.45),
         "clothes": cloth_material, "shoes": cloth_material, "teeth": teeth_material}.get(k, lambda m: None)(m)
        if k:
            done.append(m.name)
    log("materials:", ", ".join(done))


# ---------------------------------------------------------------- scene: world, lights, backdrop
def world(path, strength, rotate):
    w = bpy.data.worlds.new("Studio")
    bpy.context.scene.world = w
    w.use_nodes = True
    nt = w.node_tree
    bg = nt.nodes["Background"]
    bg.inputs["Strength"].default_value = strength
    if path and os.path.exists(path):
        env = nt.nodes.new("ShaderNodeTexEnvironment")
        env.image = bpy.data.images.load(os.path.abspath(path), check_existing=True)
        tc = nt.nodes.new("ShaderNodeTexCoord")
        mp = nt.nodes.new("ShaderNodeMapping")
        mp.inputs["Rotation"].default_value = (0, 0, math.radians(rotate))
        nt.links.new(tc.outputs["Generated"], mp.inputs["Vector"])
        nt.links.new(mp.outputs["Vector"], env.inputs["Vector"])
        nt.links.new(env.outputs["Color"], bg.inputs["Color"])
        log("hdri:", path)
    else:
        bg.inputs["Color"].default_value = (0.5, 0.5, 0.5, 1)
        log("no HDRI found, flat grey world:", path)


def area_light(name, power, loc, target, size, color=(1, 1, 1)):
    ld = bpy.data.lights.new(name, "AREA")
    ld.energy = power
    ld.shape = "DISK"
    ld.size = size
    ld.color = color
    o = bpy.data.objects.new(name, ld)
    bpy.context.scene.collection.objects.link(o)
    o.location = loc
    o.rotation_euler = (Vector(target) - Vector(loc)).to_track_quat("-Z", "Y").to_euler()
    return o


def backdrop(kind, b):
    """Curved cyclorama behind the person (who faces -Y), or a shadow-catcher floor."""
    if kind == "none":
        return
    floor_z = b["zmin"]
    me = bpy.data.meshes.new("Backdrop")
    bm = bmesh.new()
    if kind == "shadow":
        prof = [(-8.0, floor_z), (8.0, floor_z)]
    else:
        wall_y, r = b["ymax"] + 2.2, 1.6
        prof = [(-12.0, floor_z), (wall_y - r, floor_z)]
        for i in range(1, 12):
            a = math.pi / 2 * i / 12
            prof.append((wall_y - r + r * math.sin(a), floor_z + r - r * math.cos(a)))
        prof += [(wall_y, floor_z + r), (wall_y, floor_z + 8.0)]
    w = 14.0
    rows = [[bm.verts.new((x, y, z)) for (y, z) in prof] for x in (-w / 2, w / 2)]
    for i in range(len(prof) - 1):
        bm.faces.new((rows[0][i], rows[1][i], rows[1][i + 1], rows[0][i + 1]))
    bm.normal_update()
    bm.to_mesh(me)
    bm.free()
    for p in me.polygons:
        p.use_smooth = True
    o = bpy.data.objects.new("Backdrop", me)
    bpy.context.scene.collection.objects.link(o)
    m = bpy.data.materials.new("Backdrop")
    m.use_nodes = True
    bs = m.node_tree.nodes["Principled BSDF"]
    bs.inputs["Base Color"].default_value = (0.18, 0.18, 0.185, 1) if kind == "grey" else (0.75, 0.75, 0.75, 1)
    bs.inputs["Roughness"].default_value = 0.9
    setin(bs, "Specular IOR Level", 0.2)
    me.materials.append(m)
    if kind == "shadow":
        o.is_shadow_catcher = True
        bpy.context.scene.render.film_transparent = True


# ---------------------------------------------------------------- bounds and camera
def bounds(meshes, arm):
    dg = bpy.context.evaluated_depsgraph_get()
    pts = []
    for o in meshes:
        if mat_kind(o) in {"", "teeth"}:
            continue
        ev = o.evaluated_get(dg)
        me = ev.to_mesh()
        mw = ev.matrix_world
        pts += [mw @ v.co for v in me.vertices]
        ev.to_mesh_clear()
    b = {"pts": pts, "zmin": min(p.z for p in pts), "zmax": max(p.z for p in pts),
         "ymin": min(p.y for p in pts), "ymax": max(p.y for p in pts)}
    H = b["zmax"] - b["zmin"]

    def bz(name, frac):
        pb = bone(arm, name)
        return (arm.matrix_world @ pb.head) if pb else Vector((0, 0, b["zmin"] + frac * H))

    head = bz("Head", 0.88)
    b["head"] = head
    b["levels"] = {"feet": b["zmin"], "hips": bz("Hips", 0.55).z - 0.03 * H,
                   "chest": bz("Spine2", 0.72).z + 0.03 * H, "neck": bz("Neck", 0.84).z - 0.035 * H}
    # eyes: the eye meshes if present, else in front of the head bone
    eyes = [o for o in meshes if mat_kind(o) == "eye"]
    if eyes:
        ev = eyes[0].evaluated_get(dg)
        me = ev.to_mesh()
        ep = [ev.matrix_world @ v.co for v in me.vertices]
        ev.to_mesh_clear()
        b["eyes"] = sum(ep, Vector()) / len(ep)
    else:
        b["eyes"] = head + Vector((0, -0.09, 0.09))
    return b


def camera(b, shot, lens, angle, res, dof, fstop):
    sc = bpy.context.scene
    lens0, lower, top_margin, angle0, f0 = SHOTS[shot]
    lens = lens or lens0
    angle = angle0 if angle is None else angle
    zlo = b["levels"][lower]
    zhi = b["zmax"] + top_margin * (b["zmax"] - zlo) * (0.6 if shot == "full" else 1.0)
    if shot == "full":
        zlo -= 0.04 * (b["zmax"] - b["zmin"])
    band = [p for p in b["pts"] if zlo <= p.z <= zhi]
    a = math.radians(angle)
    # view direction (camera -> person) and the screen's horizontal axis
    fwd = Vector((math.sin(a), math.cos(a), 0.0))      # angle 0: camera on -Y looking +Y
    side = Vector((math.cos(a), -math.sin(a), 0.0))
    xs = [p.dot(side) for p in band]
    ds = [p.dot(fwd) for p in band]
    W, H = res
    sensor = 36.0
    cd = bpy.data.cameras.new("Cam")
    cd.lens = lens
    cd.sensor_width = sensor
    cd.sensor_fit = "AUTO"
    long_fov = 2 * math.atan(sensor / 2 / lens)
    if W >= H:
        hfov, vfov = long_fov, 2 * math.atan(math.tan(long_fov / 2) * H / W)
    else:
        vfov, hfov = long_fov, 2 * math.atan(math.tan(long_fov / 2) * W / H)
    h = zhi - zlo
    wdt = (max(xs) - min(xs)) * 1.12
    front = min(ds)
    dist = max(h / 2 / math.tan(vfov / 2), wdt / 2 / math.tan(hfov / 2))
    xc = (max(xs) + min(xs)) / 2
    zc = (zhi + zlo) / 2
    # camera height: eye level for head shots, chest height for full/medium (slight downward look)
    cam_z = b["eyes"].z if shot in {"closeup", "portrait3q"} else min(zc + 0.15 * h, b["eyes"].z)
    cam = bpy.data.objects.new("Cam", cd)
    sc.collection.objects.link(cam)
    centre = side * xc + fwd * (front + 0.0) + Vector((0, 0, zc))
    pos = side * xc + fwd * (front - dist) + Vector((0, 0, cam_z))
    # aim so the frame is centred on the band (shift instead of tilting keeps verticals straight)
    look = Vector((centre.x, centre.y, cam_z))
    cam.location = pos
    cam.rotation_euler = (look - pos).to_track_quat("-Z", "Y").to_euler()
    shift = (zc - cam_z) / (2 * dist * math.tan(vfov / 2))
    cd.shift_y = shift * (H / max(W, H))
    cd.clip_start, cd.clip_end = 0.05, 200
    sc.camera = cam
    if dof:
        cd.dof.use_dof = True
        cd.dof.focus_distance = (b["eyes"] - pos).length
        cd.dof.aperture_fstop = fstop or f0
        cd.dof.aperture_blades = 7
    log(f"camera: {shot} {lens:.0f}mm, {dist:.2f} m from the body, angle {angle:.0f}°"
        + (f", f/{cd.dof.aperture_fstop:.1f} focus {cd.dof.focus_distance:.2f} m" if dof else ""))
    return cam, pos


# ---------------------------------------------------------------- render settings
def render_settings(a):
    sc = bpy.context.scene
    sc.render.engine = "CYCLES"
    cy = sc.cycles
    cy.device = "CPU"
    cy.samples = a.samples
    cy.use_adaptive_sampling = True
    cy.adaptive_threshold = 0.02
    cy.max_bounces = 8
    cy.diffuse_bounces = 3
    cy.glossy_bounces = 3
    cy.transmission_bounces = 4
    cy.transparent_max_bounces = 64      # stacked hair cards and lashes need many
    cy.caustics_reflective = cy.caustics_refractive = False
    cy.sample_clamp_indirect = 8.0
    cy.blur_glossy = 1.0
    cy.use_denoising = False
    if not a.no_denoise:
        try:
            cy.denoiser = "OPENIMAGEDENOISE"
            cy.denoising_input_passes = "RGB_ALBEDO_NORMAL"
            cy.denoising_prefilter = "ACCURATE"
            cy.use_denoising = True
        except Exception as e:  # noqa: BLE001
            log("OpenImageDenoise not available, rendering without denoise:", e)
    W, H = (int(x) for x in a.res.lower().split("x"))
    sc.render.resolution_x, sc.render.resolution_y = W, H
    sc.render.resolution_percentage = a.percent
    if a.threads:
        sc.render.threads_mode = "FIXED"
        sc.render.threads = a.threads
    vs = sc.view_settings
    try:
        vs.view_transform = a.view
    except TypeError:
        log("unknown view transform", a.view, "- using AgX")
        vs.view_transform = "AgX"
    look = a.look
    if look and look != "None":
        for cand in (look, f"{vs.view_transform} - {look}"):
            try:
                vs.look = cand
                break
            except TypeError:
                pass
    vs.exposure = a.exposure
    vs.gamma = 1.0
    sc.render.image_settings.file_format = "PNG"
    sc.render.image_settings.color_depth = "8"
    return W, H


def main():
    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else sys.argv[1:]
    a = parse_args(argv)
    arm, body, meshes = load(a.input)
    log("loaded", a.input, f"({len(meshes)} meshes, rig: {arm.name if arm else 'none'})")
    if a.pose == "relaxed":
        if arm is None:
            log("--pose relaxed: no armature, skipped")
        else:
            relaxed_pose(arm)
    if a.smile is not None or a.shape_key:
        set_shape_keys(body, meshes, a.smile, a.shape_key)
    setup_materials(a.skin_even)
    if a.subdiv > 0:
        for o in meshes:
            if mat_kind(o) == "skin":  # body only: subdividing the clothes balloons them
                md = o.modifiers.new("Render subdiv", "SUBSURF")
                md.levels = 0
                md.render_levels = a.subdiv
                md.quality = 2
    W, H = render_settings(a)
    b = bounds(meshes, arm)
    cam, cam_pos = camera(b, a.shot, a.lens, a.angle, (W, H), a.dof, a.fstop)
    world(a.hdri, a.hdri_strength, a.hdri_rotate)
    backdrop(a.backdrop, b)
    bpy.context.scene.render.image_settings.color_mode = "RGBA" if bpy.context.scene.render.film_transparent else "RGB"
    # key: front-left (camera left), above eye level; rim: behind on the other side
    tgt = b["eyes"] if a.shot in {"closeup", "portrait3q"} else Vector((0, 0, (b["zmin"] + b["zmax"]) * 0.55))
    if a.key > 0:
        area_light("Key", a.key, (tgt.x - 2.4, tgt.y - 1.9, b["eyes"].z + 1.1), tgt, 1.5, (1.0, 0.96, 0.92))
    if a.rim > 0:
        area_light("Rim", a.rim, (tgt.x + 1.8, b["ymax"] + 1.6, b["eyes"].z + 0.8), b["eyes"], 1.0, (0.92, 0.96, 1.0))
    out = os.path.abspath(a.output)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    sc = bpy.context.scene
    sc.render.filepath = out
    if a.save_blend:
        bpy.ops.wm.save_as_mainfile(filepath=os.path.abspath(a.save_blend), copy=True)
    t_setup = time.time() - T0
    log(f"rendering {W * a.percent // 100}x{H * a.percent // 100}, {a.samples} samples"
        f", denoise {'on' if sc.cycles.use_denoising else 'off'}, {sc.view_settings.view_transform}"
        f" / {sc.view_settings.look}")
    t1 = time.time()
    bpy.ops.render.render(write_still=True)
    t_render = time.time() - t1
    log(f"wrote {out}")
    print(f"TIMING setup {t_setup:.1f}s render {t_render:.1f}s total {time.time() - T0:.1f}s", flush=True)


if __name__ == "__main__":
    main()
