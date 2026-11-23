"""Bake a mesh's colours into a UV texture (xatlas UV unwrap + numpy rasterisation; no GPU or OpenGL).

    python -I bake.py IN.glb|obj|ply OUT.glb [--size 1024] [--max-faces N]

As a module (TripoSR's worker uses it): bake(vertices, faces, size, vertex_colors=..., colour_fn=...) returns
(vertices, faces, uv, image) for the unwrapped mesh. colour_fn(points) -> RGB 0..1 samples the colour at each
texel's 3D point (sharper than vertex colours); without it, vertex colours are interpolated across each triangle.
Texels just outside the charts are filled from their neighbours (padding) so mipmaps don't bleed black seams.
Prints one JSON line with stats.
"""
from __future__ import annotations

import argparse
import json
import sys
import time


def unwrap(vertices, faces, size: int, padding: int = 2):
    import numpy as np
    import xatlas

    atlas = xatlas.Atlas()
    atlas.add_mesh(np.ascontiguousarray(vertices, dtype=np.float32), np.ascontiguousarray(faces, dtype=np.uint32))
    po = xatlas.PackOptions()
    po.resolution = size
    po.padding = padding
    po.bilinear = True
    po.bruteForce = False
    co = xatlas.ChartOptions()
    atlas.generate(chart_options=co, pack_options=po)
    vmap, idx, uv = atlas[0]
    return vmap, idx, uv


def rasterise(uv, faces, size: int, chunk: int = 4_000_000):
    """Every texel centre inside a UV triangle: (texel rows, cols, triangle ids, barycentric weights)."""
    import numpy as np

    P = uv[faces] * size - 0.5                                  # (T, 3, 2), texel centres at integers
    lo = np.floor(P.min(1)).astype(np.int64).clip(0, size - 1)
    hi = np.ceil(P.max(1)).astype(np.int64).clip(0, size - 1)
    wh = hi - lo + 1
    counts = wh[:, 0] * wh[:, 1]
    rows, cols, tids, bary = [], [], [], []
    start = 0
    while start < len(faces):
        end = start + max(1, int(np.searchsorted(np.cumsum(counts[start:]), chunk)))
        sel = np.arange(start, min(end, len(faces)))
        c = counts[sel]
        t = np.repeat(sel, c)
        k = np.arange(c.sum()) - np.repeat(np.cumsum(c) - c, c)
        x = lo[t, 0] + k % wh[t, 0]
        y = lo[t, 1] + k // wh[t, 0]
        a, b, cc = P[t, 0], P[t, 1], P[t, 2]
        v0, v1 = b - a, cc - a
        v2 = np.stack([x, y], 1) - a
        den = v0[:, 0] * v1[:, 1] - v1[:, 0] * v0[:, 1]
        ok = np.abs(den) > 1e-12
        den = np.where(ok, den, 1)
        w1 = (v2[:, 0] * v1[:, 1] - v1[:, 0] * v2[:, 1]) / den
        w2 = (v0[:, 0] * v2[:, 1] - v2[:, 0] * v0[:, 1]) / den
        w0 = 1 - w1 - w2
        inside = ok & (w0 >= -1e-3) & (w1 >= -1e-3) & (w2 >= -1e-3)
        rows.append(y[inside])
        cols.append(x[inside])
        tids.append(t[inside])
        bary.append(np.stack([w0, w1, w2], 1)[inside].clip(0, 1))
        start = end
    return np.concatenate(rows), np.concatenate(cols), np.concatenate(tids), np.concatenate(bary)


def dilate(img, filled, steps: int = 8):
    """Grow the charts' colours into the empty texels around them (several texels of padding)."""
    import numpy as np

    img = img.astype(np.float32)
    filled = filled.copy()
    for _ in range(steps):
        acc = np.zeros_like(img)
        cnt = np.zeros(filled.shape, np.float32)
        for dy, dx in ((-1, 0), (1, 0), (0, -1), (0, 1), (-1, -1), (-1, 1), (1, -1), (1, 1)):
            sh_f = np.roll(np.roll(filled, dy, 0), dx, 1)
            sh_i = np.roll(np.roll(img, dy, 0), dx, 1)
            acc += sh_i * sh_f[..., None]
            cnt += sh_f
        grow = (~filled) & (cnt > 0)
        img[grow] = acc[grow] / cnt[grow][:, None]
        filled |= grow
    if (~filled).any():   # whatever is left: the mean colour (keeps the atlas from showing black)
        img[~filled] = img[filled].mean(0) if filled.any() else 128
    return img


def bake(vertices, faces, size: int = 1024, vertex_colors=None, colour_fn=None):
    import numpy as np

    vmap, idx, uv = unwrap(vertices, faces, size)
    nv = np.asarray(vertices)[vmap]
    r, c, t, w = rasterise(uv, idx, size)
    if colour_fn is not None:
        pts = (nv[idx[t]] * w[..., None]).sum(1)
        rgb = np.asarray(colour_fn(pts), dtype=np.float32).reshape(-1, 3)[:, :3] * 255.0
    else:
        vc = np.asarray(vertex_colors, dtype=np.float32)[vmap][:, :3]
        rgb = (vc[idx[t]] * w[..., None]).sum(1)
    img = np.zeros((size, size, 3), np.float32)
    filled = np.zeros((size, size), bool)
    img[r, c] = rgb
    filled[r, c] = True
    coverage = float(filled.mean())
    img = dilate(img, filled)
    img = np.flipud(img)   # row 0 of the picture is v = 1
    return nv, idx, uv, np.clip(img, 0, 255).astype(np.uint8), coverage


def textured_mesh(vertices, faces, uv, image, quality: int = 90):
    import io

    import trimesh
    from PIL import Image

    buf = io.BytesIO()
    Image.fromarray(image).save(buf, "JPEG", quality=quality)   # JPEG keeps the GLB small (colour maps need no alpha)
    pil = Image.open(io.BytesIO(buf.getvalue()))
    mat = trimesh.visual.material.PBRMaterial(baseColorTexture=pil, metallicFactor=0.0, roughnessFactor=0.85, doubleSided=False)
    return trimesh.Trimesh(vertices, faces, visual=trimesh.visual.TextureVisuals(uv=uv, image=pil, material=mat), process=False)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("src")
    ap.add_argument("out")
    ap.add_argument("--size", type=int, default=1024)
    ap.add_argument("--max-faces", type=int, default=0)
    a = ap.parse_args()
    import numpy as np
    import trimesh

    t0 = time.time()
    mesh = trimesh.load(a.src, force="mesh", process=False)
    vc = getattr(mesh.visual, "vertex_colors", None)
    if mesh.visual.kind != "vertex" or vc is None or len(vc) != len(mesh.vertices):
        if mesh.visual.kind == "face":
            mesh.visual.vertex_colors = mesh.visual.vertex_colors   # trimesh averages face colours onto vertices
            vc = mesh.visual.vertex_colors
        else:
            print(json.dumps({"error": "the model has no vertex colours to bake"}))
            sys.exit(4)
    if a.max_faces and len(mesh.faces) > a.max_faces:
        cols = np.asarray(vc)[:, :3].astype(np.float32)
        before = mesh.vertices.copy()
        mesh = mesh.simplify_quadric_decimation(face_count=a.max_faces)
        from scipy.spatial import cKDTree   # colours for the new vertices from the nearest old ones

        _, near = cKDTree(before).query(mesh.vertices)
        vc = cols[near]
    faces_in = len(mesh.faces)
    v, f, uv, img, cov = bake(mesh.vertices, mesh.faces, a.size, vertex_colors=np.asarray(vc, dtype=np.float32))
    textured_mesh(v, f, uv, img).export(a.out)
    print(json.dumps({"faces": int(len(f)), "faces_in": faces_in, "vertices": int(len(v)), "texture": a.size,
                      "coverage": round(cov, 3), "total_s": round(time.time() - t0, 1)}))


if __name__ == "__main__":
    main()
