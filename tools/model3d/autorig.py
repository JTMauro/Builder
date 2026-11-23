"""Add a Mixamo-named skeleton to a humanoid mesh and skin it, with Blender (bpy), on the CPU.

    BLENDER_USER_RESOURCES=... MHENV/bin/python -I autorig.py IN OUT.glb [--height 1.75] [--facing auto|-y|+y]
                                                       [--weights heat|nearest]

The skeleton is fitted from the mesh's own shape (no learned model): height, the crotch (where the legs
split), the neck (the narrowest slice between chest and head), the fingertips (the points furthest out on
each side above the hips), the legs' centres and the toes. Bones are named like Builder's MakeHuman rig and
Mixamo (mixamorig:Hips, Spine, Spine1, Spine2, Neck, Head, Left/RightShoulder/Arm/ForeArm/Hand,
Left/RightUpLeg/Leg/Foot/ToeBase), so tools/makehuman/view/anim.js retarget() and the CMU moves work.
Skin weights: Blender's automatic (bone heat) weights; vertices heat can't reach (separate pieces, holes)
get distance-based weights from the nearest bones, on their own side of the body. Any existing rig is
removed first (the mesh keeps its current pose). Prints one JSON line with the fitted landmarks.
"""
import argparse
import json
import sys

import bpy  # noqa: E402  (bpy before bmesh/mathutils)
import bmesh  # noqa: E402
import numpy as np  # noqa: E402
from mathutils import Matrix, Vector  # noqa: E402

LOADERS = {".fbx": lambda p: bpy.ops.import_scene.fbx(filepath=p, automatic_bone_orientation=True),
           ".obj": lambda p: bpy.ops.wm.obj_import(filepath=p),
           ".glb": lambda p: bpy.ops.import_scene.gltf(filepath=p),
           ".gltf": lambda p: bpy.ops.import_scene.gltf(filepath=p),
           ".ply": lambda p: bpy.ops.wm.ply_import(filepath=p),
           ".stl": lambda p: bpy.ops.wm.stl_import(filepath=p)}
SIDES = ("Left", "Right")


def load_mesh(path: str):
    bpy.ops.wm.read_factory_settings(use_empty=True)
    ext = "." + path.rsplit(".", 1)[-1].lower()
    if ext not in LOADERS:
        raise SystemExit(f"can't read {ext} files")
    LOADERS[ext](path)
    for o in list(bpy.data.objects):   # bake any rig's pose into the meshes, then drop the rig
        if o.type == "MESH":
            bpy.context.view_layer.objects.active = o
            for m in list(o.modifiers):
                if m.type == "ARMATURE":
                    bpy.ops.object.modifier_apply(modifier=m.name)
            o.vertex_groups.clear()
            if o.data.shape_keys:
                o.shape_key_clear()
            o.animation_data_clear()
    for o in list(bpy.data.objects):
        if o.type != "MESH":
            for ch in o.children:
                mw = ch.matrix_world.copy()
                ch.parent = None
                ch.matrix_world = mw
            bpy.data.objects.remove(o)
    meshes = [o for o in bpy.data.objects if o.type == "MESH"]
    if not meshes:
        raise SystemExit("no mesh in the file")
    bpy.ops.object.select_all(action="DESELECT")
    for o in meshes:
        o.select_set(True)
    bpy.context.view_layer.objects.active = meshes[0]
    if len(meshes) > 1:
        bpy.ops.object.join()
    obj = bpy.context.view_layer.objects.active
    obj.name = "Body"
    bpy.ops.object.transform_apply(location=True, rotation=True, scale=True)
    return obj


def surface_points(obj, n=60000, seed=0):
    """Points spread evenly over the surface (low-poly meshes have too few vertices to measure)."""
    me = obj.data
    me.calc_loop_triangles()
    v = np.array([x.co for x in me.vertices], dtype=np.float64)
    tris = np.array([t.vertices for t in me.loop_triangles], dtype=np.int64)
    a, b, c = v[tris[:, 0]], v[tris[:, 1]], v[tris[:, 2]]
    area = 0.5 * np.linalg.norm(np.cross(b - a, c - a), axis=1)
    rng = np.random.default_rng(seed)
    pick = rng.choice(len(tris), size=n, p=area / area.sum())
    r1, r2 = rng.random(n), rng.random(n)
    s = np.sqrt(r1)
    pts = (1 - s)[:, None] * a[pick] + (s * (1 - r2))[:, None] * b[pick] + (s * r2)[:, None] * c[pick]
    return np.concatenate([pts, v])


def central_width(sl, cx, H, bins=200):
    """Width of the connected run of occupied x-bins around cx in one slice of points."""
    if len(sl) == 0:
        return 0.0, cx, cx
    lo, hi = cx - H, cx + H
    occ = np.zeros(bins, bool)
    idx = np.clip(((sl[:, 0] - lo) / (hi - lo) * bins).astype(int), 0, bins - 1)
    occ[idx] = True
    gap = max(1, int(0.012 * bins / 2))   # tolerate tiny gaps (sparse sampling)
    c = bins // 2
    if not occ[max(0, c - 3): c + 4].any():
        return 0.0, cx, cx
    left = right = c
    while left > 0 and occ[max(0, left - gap): left].any():
        left -= 1
    while right < bins - 1 and occ[right + 1: right + 1 + gap].any():
        right += 1
    step = (hi - lo) / bins
    return (right - left + 1) * step, lo + left * step, lo + (right + 1) * step


def fit(pts, height_hint=None):
    zmin, zmax = pts[:, 2].min(), pts[:, 2].max()
    H = zmax - zmin
    z = (pts[:, 2] - zmin) / H
    top = pts[z > 0.9]
    cx = float((top[:, 0].min() + top[:, 0].max()) / 2) if len(top) else float(np.median(pts[:, 0]))   # heads are symmetric

    def sl(f0, f1):
        return pts[(z >= f0) & (z < f1)]

    # facing: toes stick out to the front; compare the feet's y spread with the shins'
    feet, shins = sl(0.0, 0.05), sl(0.12, 0.22)
    front_sign = -1.0   # Blender glTF convention: the character looks toward -Y
    if len(feet) > 20 and len(shins) > 20:
        d = np.median(feet[:, 1]) - np.median(shins[:, 1])
        if abs(d) > 0.01 * H:
            front_sign = float(np.sign(d))
    # crotch: lowest slice above the knees with surface on the centre line
    crotch = None
    for f in np.arange(0.25, 0.62, 0.005):
        s = sl(f, f + 0.01)
        if len(s) and (np.abs(s[:, 0] - cx) < 0.018 * H).sum() >= 3:
            crotch = f
            break
    crotch = float(np.clip(crotch if crotch is not None else 0.46, 0.30, 0.58))
    # neck: the narrowest connected slice between the chest and the head
    prof = []
    for f in np.arange(crotch + 0.12, 0.96, 0.01):
        w, _, _ = central_width(sl(f, f + 0.012), cx, H)
        prof.append((f, w))
    prof = np.array(prof)
    upper = prof[(prof[:, 0] > crotch + 0.15) & (prof[:, 0] < 0.93) & (prof[:, 1] > 0)]
    neck = float(upper[np.argmin(upper[:, 1]), 0]) if len(upper) else 0.84   # big cartoon heads: the neck is lower
    neck = float(np.clip(neck, crotch + 0.18, 0.92))
    # shoulders: going down from the neck, where the body first gets much wider than the neck
    nw = float(upper[np.argmin(upper[:, 1]), 1]) if len(upper) else 0.1 * H
    below = prof[(prof[:, 0] < neck - 0.015) & (prof[:, 0] > neck - 0.14)][::-1]
    wide = below[below[:, 1] > 1.6 * nw]
    shoulder_f = float(np.clip(wide[0, 0] if len(wide) else neck - 0.035, neck - 0.10, neck - 0.025))
    # torso half-width under the armpits (arms in an A/T-pose stand apart from the chest there)
    chest_z = crotch + 0.55 * (neck - crotch)
    w, l, r = central_width(sl(chest_z, chest_z + 0.02), cx, H)
    half = float(np.clip(w / 2 if w else 0.1 * H, 0.06 * H, 0.16 * H))

    def ymid(f0, f1, xsel=None):
        s = sl(f0, f1)
        if xsel is not None:
            s = s[xsel(s)]
        return float(np.median(s[:, 1])) if len(s) else float(np.median(pts[:, 1]))

    P = {}
    zf = lambda f: zmin + f * H   # noqa: E731
    hips_f = crotch + 0.06
    yc = ymid(hips_f - 0.03, hips_f + 0.03, lambda s: np.abs(s[:, 0] - cx) < half)
    P["Hips"] = (cx, yc, zf(hips_f))
    for name, k in (("Spine", 0.18), ("Spine1", 0.42), ("Spine2", 0.66)):
        f = hips_f + k * (neck - 0.02 - hips_f)
        P[name] = (cx, ymid(f - 0.02, f + 0.02, lambda s: np.abs(s[:, 0] - cx) < half), zf(f))
    P["Neck"] = (cx, ymid(neck - 0.03, neck - 0.01, lambda s: np.abs(s[:, 0] - cx) < half * 0.6), zf(neck - 0.02))
    P["Head"] = (cx, ymid(neck + 0.01, neck + 0.04, lambda s: np.abs(s[:, 0] - cx) < half * 0.6), zf(neck + 0.025))
    P["HeadTop"] = (cx, P["Head"][1], zmax)
    info = {"height": H, "crotch": crotch, "neck": neck, "shoulder": shoulder_f, "front": "-y" if front_sign < 0 else "+y", "half_chest": half / H}
    for side, sx in (("Left", 1.0), ("Right", -1.0)):   # character's left is +X when it looks toward -Y
        sx = sx * -front_sign   # facing +Y mirrors left and right
        # leg centre (x, y) at the thigh and knee
        legs = sl(crotch - 0.12, crotch - 0.06)
        legs = legs[(legs[:, 0] - cx) * sx > 0.005 * H]
        lx = float(np.median(legs[:, 0])) if len(legs) else cx + sx * 0.09 * H
        P[f"{side}UpLeg"] = (lx, yc, zf(crotch + 0.02))
        knee_f = crotch * 0.53
        kn = sl(knee_f - 0.03, knee_f + 0.03)
        kn = kn[(kn[:, 0] - cx) * sx > 0]
        kx = float(np.median(kn[:, 0])) if len(kn) else lx
        P[f"{side}Leg"] = (kx, ymid(knee_f - 0.03, knee_f + 0.03, lambda s: (s[:, 0] - cx) * sx > 0) + front_sign * 0.008 * H, zf(knee_f))
        an = sl(0.03, 0.08)
        an = an[(an[:, 0] - cx) * sx > 0]
        ax = float(np.median(an[:, 0])) if len(an) else kx
        ay = float(np.median(an[:, 1])) if len(an) else P[f"{side}Leg"][1]
        P[f"{side}Foot"] = (ax, ay, zf(0.05))
        ft = sl(0.0, 0.04)
        ft = ft[(ft[:, 0] - cx) * sx > 0]
        tip_y = float(ft[:, 1].min() if front_sign < 0 else ft[:, 1].max()) if len(ft) else ay + front_sign * 0.12 * H
        P[f"{side}ToeBase"] = (ax, ay + 0.6 * (tip_y - ay), zf(0.015))
        P[f"{side}ToeEnd"] = (ax, tip_y, zf(0.015))
        # arm: the shoulder joint sits a little wider than the hip joint; the fingertip is the point on this
        # side, beyond the shoulder, furthest from it (works for T-, A- and arms-down poses)
        sh_off = float(np.clip(max(1.5 * abs(lx - cx), 0.085 * H), 0.06 * H, max(half * 0.9, 0.07 * H)))
        sh = np.array([cx + sx * sh_off, P["Spine2"][1], zf(shoulder_f)])
        root = pts[(z > shoulder_f - 0.2) & (z < shoulder_f + 0.03) & ((pts[:, 0] - cx) * sx > sh_off * 1.05) & ((pts[:, 0] - cx) * sx < sh_off * 1.6)]
        if len(root) > 20:   # an arm held out sideways: the shoulder joint is level with the arm's middle
            zr, xr = np.percentile(root[:, 2], [5, 95]), np.percentile(root[:, 0], [5, 95])
            if (xr[1] - xr[0]) >= (zr[1] - zr[0]) * 0.8:
                sh[2] = min(sh[2], (zr[0] + zr[1]) / 2)
        region = pts[(z > crotch - 0.12) & (z < shoulder_f + 0.06) & ((pts[:, 0] - cx) * sx > sh_off * 1.05)]
        dist = np.linalg.norm(region - sh, axis=1) if len(region) else np.zeros(0)

        def ring(r0, r1):   # centre of the arm where a sphere round the shoulder cuts it (a thin held weapon barely moves it)
            m = (dist > r0 * H) & (dist < r1 * H)
            return region[m].mean(0) if m.sum() >= 8 else None

        near = dist[dist < 0.55 * H]
        R = float(np.clip(np.percentile(near, 95), 0.2 * H, 0.46 * H)) / H if len(near) > 20 else 0.44   # arm reach
        el, wr = ring(0.36 * R, 0.5 * R), ring(0.68 * R, 0.84 * R)
        if el is None or wr is None:
            d0 = np.array([sx * 0.7, 0, -0.7])
            el, wr = sh + d0 / np.linalg.norm(d0) * 0.19 * H, sh + d0 / np.linalg.norm(d0) * 0.34 * H
        u1, u2 = (el - sh) / np.linalg.norm(el - sh), (wr - el) / np.linalg.norm(wr - el)
        elbow = sh + u1 * 0.41 * R * H - np.array([0, front_sign * 0.01 * H, 0])   # a hint of bend
        wrist = elbow + u2 * 0.375 * R * H
        d = u2
        P[f"{side}Shoulder"] = (cx + sx * 0.02 * H, P["Spine2"][1], zf(shoulder_f - 0.01))
        P[f"{side}Arm"] = tuple(sh)
        P[f"{side}ForeArm"] = tuple(elbow)
        P[f"{side}Hand"] = tuple(wrist)
        P[f"{side}HandEnd"] = tuple(wrist + d * 0.2 * R * H)
        u = (wrist - sh) / np.linalg.norm(wrist - sh)
        info[f"{side.lower()}_arm_reach"] = round(R, 3)
        info[f"{side.lower()}_arm_down_deg"] = round(float(np.degrees(np.arcsin(np.clip(-u[2], -1, 1)))), 1)
    return P, info, front_sign


CHAIN = [("Hips", None, "Spine"), ("Spine", "Hips", "Spine1"), ("Spine1", "Spine", "Spine2"), ("Spine2", "Spine1", "Neck"),
         ("Neck", "Spine2", "Head"), ("Head", "Neck", "HeadTop")]
for _s in SIDES:
    CHAIN += [(f"{_s}Shoulder", "Spine2", f"{_s}Arm"), (f"{_s}Arm", f"{_s}Shoulder", f"{_s}ForeArm"),
              (f"{_s}ForeArm", f"{_s}Arm", f"{_s}Hand"), (f"{_s}Hand", f"{_s}ForeArm", f"{_s}HandEnd"),
              (f"{_s}UpLeg", "Hips", f"{_s}Leg"), (f"{_s}Leg", f"{_s}UpLeg", f"{_s}Foot"),
              (f"{_s}Foot", f"{_s}Leg", f"{_s}ToeBase"), (f"{_s}ToeBase", f"{_s}Foot", f"{_s}ToeEnd")]


def build_armature(P, front_sign):
    arm_data = bpy.data.armatures.new("Armature")
    rig = bpy.data.objects.new("Armature", arm_data)
    bpy.context.collection.objects.link(rig)
    bpy.context.view_layer.objects.active = rig
    bpy.ops.object.mode_set(mode="EDIT")
    eb = arm_data.edit_bones
    made = {}
    for name, parent, tail in CHAIN:
        b = eb.new("mixamorig:" + name)
        b.head = Vector(P[name])
        b.tail = Vector(P[tail])
        if (b.tail - b.head).length < 1e-4:
            b.tail = b.head + Vector((0, 0, 0.01))
        if name == "Hips":   # Mixamo's hips bone points up
            b.tail = b.head + Vector((0, 0, max(0.05, (Vector(P["Spine"]) - b.head).length)))
        if parent:
            b.parent = made[parent]
            b.use_connect = name not in ("Spine", "LeftShoulder", "RightShoulder", "LeftUpLeg", "RightUpLeg") and (b.head - made[parent].tail).length < 1e-4
        b.align_roll(Vector((0, front_sign, 0)) if "Arm" in name or "Hand" in name or "Shoulder" in name else Vector((0, -front_sign, 0)))
        made[name] = b
    bpy.ops.object.mode_set(mode="OBJECT")
    return rig


def segment_weights(verts, rig, H, cx):
    """Distance-based weights: each vertex follows its nearest bones (inverse 4th power of the distance to the
    bone segment, 3 bones at most), never a bone of the other side when it is clearly off the centre line."""
    bones = [b for b in rig.data.bones]
    heads = np.array([rig.matrix_world @ b.head_local for b in bones])
    tails = np.array([rig.matrix_world @ b.tail_local for b in bones])
    seg = tails - heads
    L2 = np.maximum((seg ** 2).sum(1), 1e-12)
    out = np.zeros((len(verts), len(bones)))
    side = np.array([1 if "Left" in b.name else -1 if "Right" in b.name else 0 for b in bones])
    for i0 in range(0, len(verts), 4000):
        v = verts[i0: i0 + 4000]
        t = np.clip((((v[:, None, :] - heads[None]) * seg[None]).sum(2)) / L2[None], 0, 1)
        d = np.linalg.norm(v[:, None, :] - (heads[None] + t[..., None] * seg[None]), axis=2) + 0.01 * H
        w = d ** -4
        vs = np.sign(v[:, 0] - cx) * (np.abs(v[:, 0] - cx) > 0.03 * H)
        w[(vs[:, None] * side[None]) < 0] = 0   # wrong side
        keep = np.argsort(-w, axis=1)[:, :3]
        mask = np.zeros_like(w, bool)
        np.put_along_axis(mask, keep, True, 1)
        w = np.where(mask, w, 0)
        out[i0: i0 + 4000] = w / np.maximum(w.sum(1, keepdims=True), 1e-12)
    return bones, out


def skin(obj, rig, method, H, cx):
    bm = bmesh.new()
    bm.from_mesh(obj.data)
    bmesh.ops.remove_doubles(bm, verts=bm.verts, dist=1e-4 * H)   # glue split seams so heat can flow across
    bm.to_mesh(obj.data)
    bm.free()
    bpy.ops.object.select_all(action="DESELECT")
    obj.select_set(True)
    rig.select_set(True)
    bpy.context.view_layer.objects.active = rig
    used = method
    if method == "heat":
        bpy.ops.object.parent_set(type="ARMATURE_AUTO")
    else:
        bpy.ops.object.parent_set(type="ARMATURE_NAME")
    verts = np.array([v.co for v in obj.data.vertices])
    vg = {g.index: g.name for g in obj.vertex_groups}
    total = np.zeros(len(verts))
    for v in obj.data.vertices:
        total[v.index] = sum(g.weight for g in v.groups if g.group in vg)
    empty = np.nonzero(total < 1e-3)[0]
    if len(empty):
        bones, W = segment_weights(verts[empty], rig, H, cx)
        groups = {b.name: obj.vertex_groups.get(b.name) or obj.vertex_groups.new(name=b.name) for b in bones}
        for row, vi in enumerate(empty):
            for j in np.nonzero(W[row])[0]:
                groups[bones[j].name].add([int(vi)], float(W[row, j]), "REPLACE")
        if method == "heat" and len(empty) > 0.5 * len(verts):
            used = "nearest (heat weighting failed)"
        elif method == "heat":
            used = f"heat (+nearest for {len(empty)} vertices)"
    return used, int(len(empty)), len(verts)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("src")
    ap.add_argument("out")
    ap.add_argument("--height", type=float, default=0.0, help="scale to this height in metres (0 = keep)")
    ap.add_argument("--facing", default="auto", choices=["auto", "-y", "+y"])
    ap.add_argument("--weights", default="heat", choices=["heat", "nearest"])
    ap.add_argument("--blend", default="")
    a = ap.parse_args(sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else sys.argv[1:])
    obj = load_mesh(a.src)
    pts = surface_points(obj)
    zmin, zmax = pts[:, 2].min(), pts[:, 2].max()
    if zmax - zmin < 1e-6:
        raise SystemExit("the mesh is flat")
    # put the feet on the floor, centred, and scale if asked
    s = a.height / (zmax - zmin) if a.height > 0 else 1.0
    c = np.median(pts, axis=0)
    obj.data.transform(Matrix.Scale(s, 4) @ Matrix.Translation(Vector((-c[0], -c[1], -zmin))))
    obj.data.update()
    pts = (pts - np.array([c[0], c[1], zmin])) * s
    P, info, front = fit(pts)
    if a.facing != "auto":
        front = -1.0 if a.facing == "-y" else 1.0
    if front > 0:   # turn it round so it looks toward -Y (glTF +Z, three.js forward)
        obj.data.transform(Matrix.Rotation(np.pi, 4, "Z"))
        obj.data.update()
        pts = pts * np.array([-1, -1, 1])
        P, info, _ = fit(pts)
        info["turned"] = True
        front = -1.0
    H = info["height"]
    rig = build_armature(P, front)
    method, filled, nverts = skin(obj, rig, a.weights, H, 0.0)
    bpy.ops.object.select_all(action="SELECT")
    if a.blend:
        bpy.ops.wm.save_as_mainfile(filepath=a.blend)
    bpy.ops.export_scene.gltf(filepath=a.out, export_format="GLB", export_skins=True, export_animations=False, export_yup=True,
                              export_rest_position_armature=True)
    info.update({"weights": method, "vertices": nverts, "filled_vertices": filled, "scale": s, "bones": len(CHAIN),
                 "joints_m": {k: [round(float(x), 3) for x in v] for k, v in P.items() if not k.endswith(("End", "Top"))}})
    print(json.dumps(info))


if __name__ == "__main__":
    main()
