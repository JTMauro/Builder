"""Procedural, game-ready tree models (GLB) for the game "Unhappy Woks": woody and conifer trees.

    /root/.builder/makehuman/mhenv/bin/python -I tools/gamesfx/trees_woody.py OUT_DIR [--only name,name] [--work DIR]

Runs inside Blender's Python module (bpy). Every tree is grown here from code: a branching skeleton
(recursive growth with gravity/noise, or whorled conifer branches), meshed as generalized cylinders
with continuous UVs, plus alpha cards for needles, leaves, twigs, moss and vines. Leaf, needle, moss,
vine and twig textures are painted procedurally (PIL/numpy, original). Bark colour + normal maps are
CC0 materials from ambientCG (downloaded on first run, 1K-JPG, cached in WORK/acg).

Output per tree: OUT_DIR/<name>.glb (glTF 2.0 binary, Y-up, metres, origin at the base of the trunk,
roots sink ~0.3 m below 0), and OUT_DIR/woody_manifest.json with height, triangles, bytes and sources.
Budgets: <= 15k triangles, <= 600 KB, textures <= 1024 px (foliage 512 px, PNG with alpha, MASK).
"""
from __future__ import annotations

import argparse
import io
import json
import math
import struct
import sys
import urllib.request
import zipfile
from pathlib import Path

import bpy  # noqa: F401  (must come before mathutils/bmesh)
import numpy as np
from PIL import Image, ImageDraw, ImageFilter

UP = np.array([0.0, 0.0, 1.0])
ACG_URL = "https://ambientcg.com/get?file={id}_1K-JPG.zip"
ACG_PAGE = "https://ambientcg.com/view?id={id}"


# ----------------------------------------------------------------------------------------------
# small vector helpers
def nrm(v):
    v = np.asarray(v, float)
    n = np.linalg.norm(v)
    return v / n if n > 1e-12 else v


def perp(v):
    v = nrm(v)
    a = np.array([1.0, 0, 0]) if abs(v[0]) < 0.9 else np.array([0, 1.0, 0])
    return nrm(np.cross(v, a))


def rot(v, axis, ang):
    k = nrm(axis)
    v = np.asarray(v, float)
    c, s = math.cos(ang), math.sin(ang)
    return v * c + np.cross(k, v) * s + k * np.dot(k, v) * (1 - c)


def horiz(v):
    h = np.array([v[0], v[1], 0.0])
    return nrm(h) if np.linalg.norm(h) > 1e-6 else np.array([1.0, 0, 0])


# ----------------------------------------------------------------------------------------------
# mesh accumulator: one vertex list, faces with per-loop UVs and a material key
class Model:
    def __init__(self):
        self.V, self.N, self.F, self.UV, self.M = [], [], [], [], []

    def v(self, p, n=None):
        self.V.append(np.asarray(p, float))
        self.N.append(np.zeros(3) if n is None else nrm(n))
        return len(self.V) - 1

    def f(self, idx, uv, mat):
        self.F.append(tuple(idx))
        self.UV.append(tuple(uv))
        self.M.append(mat)

    def tris(self):
        return sum(len(f) - 2 for f in self.F)

    def transform(self, scale=1.0):
        self.V = [p * scale for p in self.V]

    def zmax(self):
        return max(p[2] for p in self.V)

    def retag_up_faces(self, src, dst, thr, rng=None, jitter=0.0):
        """Faces of material src whose normal points up (nz > thr) become material dst (frost)."""
        for i, (f, m) in enumerate(zip(self.F, self.M)):
            if m != src:
                continue
            p = [self.V[k] for k in f]
            n = nrm(np.cross(p[1] - p[0], p[2] - p[0]))
            t = thr + (rng.uniform(-jitter, jitter) if rng is not None else 0)
            if n[2] > t:
                self.M[i] = dst


def frames(P):
    P = np.asarray(P, float)
    T = np.zeros_like(P)
    T[0] = P[1] - P[0]
    T[-1] = P[-1] - P[-2]
    if len(P) > 2:
        T[1:-1] = P[2:] - P[:-2]
    T = np.array([nrm(t) for t in T])
    N = np.zeros_like(P)
    N[0] = perp(T[0])
    for i in range(1, len(P)):
        n = N[i - 1] - np.dot(N[i - 1], T[i]) * T[i]
        N[i] = nrm(n) if np.linalg.norm(n) > 1e-6 else perp(T[i])
    B = np.cross(T, N)
    return T, N, B


def arclen(P):
    P = np.asarray(P, float)
    d = np.linalg.norm(np.diff(P, axis=0), axis=1)
    return np.concatenate([[0], np.cumsum(d)])


def tube(m, P, R, sides, mat, tex=0.8, texv=None, cap=False, shape=None, twist=0.0, u_tiles=None,
         v0=0.0, jag=0.0, rng=None):
    """Generalized cylinder along polyline P with radii R. UV: u around (tiles), v along (metres/texv)."""
    P = np.asarray(P, float)
    T, N, B = frames(P)
    L = arclen(P)
    texv = texv or tex
    if u_tiles is None:
        u_tiles = max(1, int(round(2 * math.pi * R[0] / tex)))
    rings = []
    for i, p in enumerate(P):
        ring = []
        for j in range(sides):
            a = 2 * math.pi * j / sides
            r = R[i] * (shape(a, i, L[i], p) if shape else 1.0)
            ring.append(m.v(p + r * (math.cos(a) * N[i] + math.sin(a) * B[i])))
        rings.append(ring)
    for i in range(len(P) - 1):
        va, vb = v0 + L[i] / texv, v0 + L[i + 1] / texv
        for j in range(sides):
            j2 = (j + 1) % sides
            ua, ub = j / sides * u_tiles, (j + 1) / sides * u_tiles
            m.f((rings[i][j], rings[i][j2], rings[i + 1][j2], rings[i + 1][j]),
                ((ua + twist * L[i], va), (ub + twist * L[i], va), (ub + twist * L[i + 1], vb),
                 (ua + twist * L[i + 1], vb)), mat)
    if cap:
        c = P[-1] + T[-1] * R[-1] * 0.3
        if jag and rng is not None:  # broken, splintered end: zig-zag rim + recessed centre
            for j, k in enumerate(rings[-1]):
                m.V[k] = m.V[k] + T[-1] * R[-1] * jag * rng.uniform(0, 1.6) * (1 if j % 2 else 0.3)
            c = P[-1] - T[-1] * R[-1] * 0.2 * jag
        ci = m.v(c)
        for j in range(sides):
            j2 = (j + 1) % sides
            a, b = 2 * math.pi * j / sides, 2 * math.pi * (j + 1) / sides
            m.f((rings[-1][j], rings[-1][j2], ci),
                ((0.5 + 0.4 * math.cos(a), 0.5 + 0.4 * math.sin(a)),
                 (0.5 + 0.4 * math.cos(b), 0.5 + 0.4 * math.sin(b)), (0.5, 0.5)), mat)
    return rings


def sample(P, R, t):
    """Point, tangent and radius at arc-length fraction t of polyline P."""
    P = np.asarray(P, float)
    L = arclen(P)
    s = t * L[-1]
    i = int(np.clip(np.searchsorted(L, s) - 1, 0, len(P) - 2))
    f = (s - L[i]) / max(L[i + 1] - L[i], 1e-9)
    return P[i] + (P[i + 1] - P[i]) * f, nrm(P[i + 1] - P[i]), R[i] + (R[i + 1] - R[i]) * f


def grow(p, d, length, nseg, rng, gravity=0.0, noise=0.0, curl=0.0, curl_axis=None):
    pts = [np.asarray(p, float)]
    d = nrm(d)
    step = length / nseg
    ax = curl_axis if curl_axis is not None else perp(d)
    for _ in range(nseg):
        d = nrm(d + UP * gravity * step + rng.normal(size=3) * noise * math.sqrt(step))
        if curl:
            d = rot(d, ax, curl * step)
        pts.append(pts[-1] + d * step)
    return pts


def ico():
    t = (1 + 5 ** 0.5) / 2
    V = np.array([[-1, t, 0], [1, t, 0], [-1, -t, 0], [1, -t, 0], [0, -1, t], [0, 1, t], [0, -1, -t],
                  [0, 1, -t], [t, 0, -1], [t, 0, 1], [-t, 0, -1], [-t, 0, 1]], float)
    V /= np.linalg.norm(V, axis=1)[:, None]
    F = [(0, 11, 5), (0, 5, 1), (0, 1, 7), (0, 7, 10), (0, 10, 11), (1, 5, 9), (5, 11, 4), (11, 10, 2),
         (10, 7, 6), (7, 1, 8), (3, 9, 4), (3, 4, 2), (3, 2, 6), (3, 6, 8), (3, 8, 9), (4, 9, 5),
         (2, 4, 11), (6, 2, 10), (8, 6, 7), (9, 8, 1)]
    return V, F


def blob(m, c, ax, size, mat, rng, flat_bottom=0.35):
    """Snow clump: a squashed, jittered icosahedron. size = (along ax, across, up)."""
    V, F = ico()
    ax = horiz(ax)
    side = np.cross(UP, ax)
    ids = []
    for v in V:
        v = v * rng.uniform(0.85, 1.12)
        z = v[2] if v[2] > 0 else v[2] * flat_bottom
        p = c + ax * v[0] * size[0] + side * v[1] * size[1] + UP * z * size[2]
        ids.append(m.v(p))
    for f in F:
        m.f([ids[k] for k in f], [(0.5, 0.5)] * 3, mat)


# ----------------------------------------------------------------------------------------------
# cards
def card(m, base, up, side, length, width, uvr, mat, nrm_fn=None, segs=1, bend=None):
    """Quad strip from base along `up` (bent by `bend(t)` offset), geometric normal = side x up.
    uvr = (u0, v0, u1, v1) with v0 at the base."""
    u0, v0, u1, v1 = uvr
    rows = []
    for k in range(segs + 1):
        t = k / segs
        c = base + up * length * t + (bend(t) if bend else 0)
        lv = c - side * width / 2
        rv = c + side * width / 2
        rows.append((m.v(lv, nrm_fn(lv) if nrm_fn else None), m.v(rv, nrm_fn(rv) if nrm_fn else None)))
    for k in range(segs):
        ta, tb = v0 + (v1 - v0) * k / segs, v0 + (v1 - v0) * (k + 1) / segs
        m.f((rows[k][0], rows[k][1], rows[k + 1][1], rows[k + 1][0]), ((u0, ta), (u1, ta), (u1, tb), (u0, tb)), mat)


def cluster(m, p, size, uvrs, mat, rng, nrm_fn, n=3, upish=0.0):
    """n crossed cards centred on p (leaf clusters / twig sprays), each facing outward-ish."""
    yaw0 = rng.uniform(0, math.pi)
    for k in range(n):
        yaw = yaw0 + k * math.pi / n + rng.uniform(-0.25, 0.25)
        tilt = rng.uniform(-0.5, 0.5) + upish
        side = np.array([math.cos(yaw), math.sin(yaw), 0.0])
        up = rot(UP, side, tilt)
        nv = np.cross(side, up)
        want = nrm_fn(p)
        if np.dot(nv, want) < 0:
            side = -side
        uvr = uvrs[rng.integers(len(uvrs))]
        s = size * rng.uniform(0.8, 1.15)
        card(m, p - up * s / 2, up, side, s, s, uvr, mat, nrm_fn)


def frond(m, base, path, width, uvr, mat, nrm_fn, roll=0.0, arch=0.12):
    """Conifer branch card that follows `path` (list of points), 3 verts across (arched)."""
    u0, v0, u1, v1 = uvr
    P = np.asarray(path, float)
    L = arclen(P)
    rows = []
    for i, p in enumerate(P):
        t = nrm(P[min(i + 1, len(P) - 1)] - P[max(i - 1, 0)])
        s = nrm(np.cross(t, UP))
        s = rot(s, t, roll)
        upv = nrm(np.cross(s, t))
        w = width
        lv = p - s * w / 2 - upv * arch * w
        cv = p + upv * arch * w * 0.5
        rv = p + s * w / 2 - upv * arch * w
        rows.append([m.v(x, nrm_fn(x)) for x in (lv, cv, rv)])
    um = (u0 + u1) / 2
    for i in range(len(P) - 1):
        ta = v0 + (v1 - v0) * L[i] / L[-1]
        tb = v0 + (v1 - v0) * L[i + 1] / L[-1]
        a, b = rows[i], rows[i + 1]
        m.f((a[0], a[1], b[1], b[0]), ((u0, ta), (um, ta), (um, tb), (u0, tb)), mat)
        m.f((a[1], a[2], b[2], b[1]), ((um, ta), (u1, ta), (u1, tb), (um, tb)), mat)


# ----------------------------------------------------------------------------------------------
# textures
def acg_maps(work: Path, asset: str) -> dict:
    d = work / "acg" / asset
    if not (d / f"{asset}_1K-JPG_Color.jpg").exists():
        d.mkdir(parents=True, exist_ok=True)
        req = urllib.request.Request(ACG_URL.format(id=asset), headers={"User-Agent": "tree-generator/1.0"})
        data = urllib.request.urlopen(req, timeout=120).read()
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            for n in z.namelist():
                if n.endswith(("_Color.jpg", "_NormalGL.jpg", "_Displacement.jpg")) and "/" not in n:
                    (d / n).write_bytes(z.read(n))
    return {k: d / f"{asset}_1K-JPG_{k}.jpg" for k in ("Color", "NormalGL", "Displacement")}


def _f(img):
    return np.asarray(img, np.float32) / 255.0


def _img(a):
    return Image.fromarray(np.clip(a * 255 + 0.5, 0, 255).astype(np.uint8))


def smoothstep(a, b, x):
    t = np.clip((x - a) / (b - a), 0, 1)
    return t * t * (3 - 2 * t)


def value_noise(size, cells, rng):
    g = rng.random((cells + 1, cells + 1)).astype(np.float32)
    g[-1, :] = g[0, :]
    g[:, -1] = g[:, 0]
    im = Image.fromarray((g * 255).astype(np.uint8)).resize((size, size), Image.BICUBIC)
    return _f(im)


def bark_textures(work: Path, out: Path, asset: str, size=512, nsize=None, sat=1.0, bright=1.0,
                  tint=(1, 1, 1), contrast=1.0, nstr=1.0, frost=False, char=False, q=80, seed=1):
    """Returns dict of written texture paths: color, normal (+ frost colour, emissive)."""
    src = acg_maps(work, asset)
    nsize = nsize or size
    rng = np.random.default_rng(seed)
    c = _f(Image.open(src["Color"]).convert("RGB").resize((size, size), Image.LANCZOS))
    lum = (c * [0.299, 0.587, 0.114]).sum(-1, keepdims=True)
    c = lum + (c - lum) * sat
    mean = c.mean()
    c = (c - mean) * contrast + mean
    c = c * bright * np.array(tint, np.float32)
    res = {}
    disp = _f(Image.open(src["Displacement"]).convert("L").resize((size, size), Image.LANCZOS))
    if char:
        # charcoal: crush to near black, keep ridges a touch lighter (ash) and cracks pitch black
        lc = (c * [0.299, 0.587, 0.114]).sum(-1, keepdims=True)
        ridge = smoothstep(0.45, 0.8, disp)[..., None]
        c = 0.035 + 0.13 * lc + 0.09 * ridge * np.array([0.9, 0.88, 0.85])
        c = c * np.array([1.0, 0.97, 0.95])
        crack = 1 - smoothstep(0.13, 0.27, disp)  # deepest parts of the relief: thin crack lines
        emb = crack * smoothstep(0.22, 0.55, value_noise(size, 7, rng))
        hot = np.stack([emb ** 0.8, emb ** 1.8 * 0.42, emb ** 3.0 * 0.05], -1)
        e = np.clip(hot * 1.6, 0, 1)
        c = c * (1 - 0.6 * emb[..., None]) + e * 0.1
        p = out / f"{asset}_char_emissive.jpg"
        _img(e).save(p, quality=q, optimize=True)
        res["emissive"] = p
    p = out / f"{asset}_{size}_{'char' if char else 'c'}{int(sat*100)}_{int(bright*100)}.jpg"
    _img(np.clip(c, 0, 1)).save(p, quality=q, optimize=True)
    res["color"] = p
    if frost:
        fm = smoothstep(0.42, 0.7, disp * 0.75 + value_noise(size, 8, rng) * 0.4)[..., None]
        fc = c * (1 - 0.88 * fm) + np.array([0.86, 0.9, 0.95]) * 0.88 * fm
        fc = fc * 0.55 + np.array([0.80, 0.84, 0.9]) * 0.45
        p = out / f"{asset}_frost.jpg"
        _img(np.clip(fc, 0, 1)).save(p, quality=q, optimize=True)
        res["frost"] = p
    n = _f(Image.open(src["NormalGL"]).convert("RGB").resize((nsize, nsize), Image.LANCZOS)) * 2 - 1
    n[..., :2] *= nstr
    n /= np.linalg.norm(n, axis=-1, keepdims=True)
    p = out / f"{asset}_{nsize}_n{int(nstr*100)}.jpg"
    _img(n * 0.5 + 0.5).save(p, quality=q, optimize=True)
    res["normal"] = p
    return res


class Painter:
    """Paint RGBA foliage atlases at 2x and reduce. Boxes are in atlas fractions (x0, y0, x1, y1), y down."""

    def __init__(self, size=512, ss=2):
        self.size, self.ss = size, ss
        self.S = size * ss
        self.img = Image.new("RGBA", (self.S, self.S), (0, 0, 0, 0))
        self.d = ImageDraw.Draw(self.img)

    def box(self, b):
        return [b[0] * self.S, b[1] * self.S, b[2] * self.S, b[3] * self.S]

    @staticmethod
    def uv(b):  # atlas box (y down) -> Blender uv rect (u0, v_base, u1, v_tip): base at the box bottom
        return (b[0], 1 - b[3], b[2], 1 - b[1])

    def line(self, pts, col, w):
        self.d.line([tuple(map(float, p)) for p in pts], fill=tuple(int(x) for x in col) + (255,),
                    width=max(1, int(round(w))), joint="curve")

    def save(self, path, colors=96):
        im = self.img.resize((self.size, self.size), Image.LANCZOS)
        a = np.asarray(im, np.float32) / 255
        rgb, al = a[..., :3], a[..., 3:]
        al = np.clip((al - 0.5) * 1.6 + 0.5, 0, 1)  # crisper edge for MASK
        # bleed colour into transparent texels so mipmaps don't show dark halos
        prem = Image.fromarray((np.clip(rgb * al, 0, 1) * 255).astype(np.uint8))
        am = Image.fromarray((al[..., 0] * 255).astype(np.uint8))
        bl = _f(prem.filter(ImageFilter.GaussianBlur(6)))
        ab = _f(am.filter(ImageFilter.GaussianBlur(6)))[..., None]
        fill = np.where(ab > 0.01, bl / np.maximum(ab, 1e-3), rgb[al[..., 0] > 0.5].mean(0) if (al > 0.5).any() else 0.3)
        rgb = np.where(al > 0.5, rgb, fill)
        out = np.concatenate([np.clip(rgb, 0, 1), al], -1)
        im = Image.fromarray((out * 255 + 0.5).astype(np.uint8), "RGBA")
        im = im.quantize(colors=colors, method=Image.Quantize.FASTOCTREE, dither=Image.Dither.NONE)
        im.save(path, optimize=True)
        return path


def jit(col, rng, amt=0.12):
    f = rng.uniform(1 - amt, 1 + amt)
    return tuple(min(255, max(0, c * f * rng.uniform(0.95, 1.05))) for c in col)


def paint_frond(pt: Painter, b, rng, pal, tip_col, twig_col=(70, 55, 38), needle=0.035, n_twigs=13,
                snow=0.0, spread=0.48, nw=1.6):
    """Flat conifer spray pointing up: rachis, side twigs, needles; optional snow on top."""
    x0, y0, x1, y1 = pt.box(b)
    w, h = x1 - x0, y1 - y0
    cx = (x0 + x1) / 2
    S = pt.ss
    nl = needle * h

    def rach(t):
        return np.array([cx + math.sin(t * 2.2) * w * 0.02, y1 - 4 * S - t * (h - 10 * S)])

    twigs = []
    for side in (-1, 1):
        for k in range(n_twigs):
            t = (k + 0.5 + 0.35 * (side > 0)) / n_twigs * 0.9 + 0.04
            env = math.sin(math.pi * min(1.0, t * 1.25) ** 0.8) * (1 - 0.6 * t) + 0.12
            L = env * w * spread * rng.uniform(0.85, 1.1)
            ang = math.radians(rng.uniform(48, 62))
            dv = np.array([side * math.sin(ang), -math.cos(ang)])
            p0 = rach(t)
            pts = [p0]
            for s in range(1, 6):
                dv2 = nrm(np.array([dv[0], dv[1] - 0.06 * s, 0]))[:2]
                pts.append(pts[-1] + dv2 * L / 5)
            twigs.append((pts, L, t))
    # needles first (back), then twigs, then upper needles
    for layer in range(2):
        for pts, L, t in twigs:
            P = np.array(pts)
            n = max(3, int(L / (nl * 0.32)))
            for i in range(n):
                f = (i + rng.random()) / n
                k = min(int(f * (len(P) - 1)), len(P) - 2)
                q = P[k] + (P[k + 1] - P[k]) * (f * (len(P) - 1) - k)
                tv = nrm(np.append(P[k + 1] - P[k], 0))[:2]
                for sgn in (-1, 1):
                    if rng.random() < 0.25 * layer:
                        continue
                    a = math.radians(rng.uniform(40, 75)) * sgn
                    dv = np.array([tv[0] * math.cos(a) - tv[1] * math.sin(a), tv[0] * math.sin(a) + tv[1] * math.cos(a)])
                    ln = nl * rng.uniform(0.7, 1.15) * (0.75 + 0.35 * f)
                    col = pal[rng.integers(len(pal))]
                    shade = 0.62 + 0.25 * f + 0.25 * layer
                    if f > 0.78 and rng.random() < 0.5:
                        col = tip_col
                    col = tuple(c * shade for c in jit(col, rng))
                    pt.line([q, q + dv * ln], col, nw * S)
        if layer == 0:
            for pts, L, t in twigs:
                pt.line(pts, twig_col, 1.6 * S)
            pt.line([rach(t) for t in np.linspace(0, 1, 20)], twig_col, 2.6 * S)
    # needles along the rachis
    for t in np.linspace(0.02, 0.98, 60):
        q = rach(t)
        for sgn in (-1, 1):
            a = math.radians(rng.uniform(30, 60)) * sgn
            dv = np.array([math.sin(a), -math.cos(a)])
            pt.line([q, q + dv * nl * 0.8], jit(pal[rng.integers(len(pal))], rng), nw * S)
    if snow > 0:
        for pts, L, t in twigs:
            P = np.array(pts)
            m = int(snow * 7)
            for i in range(m):
                f = rng.uniform(0.0, 0.85)
                k = min(int(f * (len(P) - 1)), len(P) - 2)
                q = P[k]
                r = rng.uniform(0.025, 0.055) * w * (1.2 - 0.5 * f)
                pt.d.ellipse([q[0] - r, q[1] - r * 0.7 + r * 0.35, q[0] + r, q[1] + r * 0.7 + r * 0.35],
                             fill=(150, 165, 190, 255))
                pt.d.ellipse([q[0] - r, q[1] - r * 0.7, q[0] + r, q[1] + r * 0.7], fill=(236, 241, 248, 255))
        for t in np.linspace(0.05, 0.9, int(18 * snow)):
            q = rach(t)
            r = rng.uniform(0.04, 0.07) * w
            pt.d.ellipse([q[0] - r, q[1] - r * 0.8 + r * 0.3, q[0] + r, q[1] + r * 0.8 + r * 0.3], fill=(150, 165, 190, 255))
            pt.d.ellipse([q[0] - r, q[1] - r * 0.8, q[0] + r, q[1] + r * 0.8], fill=(240, 244, 250, 255))


def leaf_poly(c, ang, L, W, n=8):
    pts = []
    for i in range(n + 1):
        t = i / n
        pts.append((t * L, math.sin(math.pi * t) ** 0.75 * W / 2))
    pts += [(x, -y) for x, y in reversed(pts[1:-1])]
    ca, sa = math.cos(ang), math.sin(ang)
    return [(c[0] + x * ca - y * sa, c[1] + x * sa + y * ca) for x, y in pts]


def paint_leaves(pt: Painter, b, rng, pal, n=70, leaf=(0.16, 0.24), aspect=0.38, twig=(70, 58, 44)):
    """Broadleaf cluster: twigs from the bottom centre, ~n pointed leaves, back-to-front shading."""
    x0, y0, x1, y1 = pt.box(b)
    w, h = x1 - x0, y1 - y0
    base = np.array([(x0 + x1) / 2, y1 - 2])
    ctr = np.array([(x0 + x1) / 2, y0 + 0.45 * h])
    hubs = [ctr + np.array([rng.uniform(-0.3, 0.3) * w, rng.uniform(-0.25, 0.2) * h]) for _ in range(5)]
    for hb in hubs:
        pt.line([base, (base + hb) / 2 + [rng.uniform(-8, 8) * pt.ss, 0], hb], twig, 2.2 * pt.ss)
    items = []
    for i in range(n):
        hb = hubs[rng.integers(len(hubs))]
        a = rng.uniform(0, 2 * math.pi)
        r = rng.uniform(0, 0.2) * w
        c = hb + np.array([math.cos(a) * r, math.sin(a) * r * 0.8])
        L = rng.uniform(*leaf) * w
        out = c - ctr
        ang = math.atan2(out[1], out[0]) + rng.uniform(-0.8, 0.8)
        tip = c + np.array([math.cos(ang), math.sin(ang)]) * L
        if not (x0 + 4 < tip[0] < x1 - 4 and y0 + 4 < tip[1] < y1 - 4):
            ang += math.pi
            tip = c + np.array([math.cos(ang), math.sin(ang)]) * L
            if not (x0 + 4 < tip[0] < x1 - 4 and y0 + 4 < tip[1] < y1 - 4):
                continue
        items.append((c, ang, L))
    for i, (c, ang, L) in enumerate(items):
        depth = i / max(1, len(items) - 1)
        col = jit(pal[rng.integers(len(pal))], rng, 0.1)
        col = tuple(x * (0.55 + 0.6 * depth) for x in col)
        poly = leaf_poly(c, ang, L, L * aspect)
        pt.d.polygon(poly, fill=tuple(int(min(255, x)) for x in col) + (255,))
        # lit half + midrib
        lit = tuple(int(min(255, x * 1.18)) for x in col)
        half = poly[: len(poly) // 2 + 1]
        pt.d.polygon(half, fill=lit + (255,))
        tipp = (c[0] + math.cos(ang) * L * 0.92, c[1] + math.sin(ang) * L * 0.92)
        pt.line([c, tipp], tuple(x * 0.72 for x in col), 1.0 * pt.ss)


def paint_feather(pt: Painter, b, rng, pal, sprays=7, twig=(95, 75, 55)):
    """Bald-cypress style flat feathery sprays fanning out from the bottom."""
    x0, y0, x1, y1 = pt.box(b)
    w, h = x1 - x0, y1 - y0
    base = np.array([(x0 + x1) / 2, y1 - 3])
    S = pt.ss
    for k in range(sprays):
        a = math.radians(-90 + (k - (sprays - 1) / 2) * (110 / max(1, sprays - 1)) + rng.uniform(-6, 6))
        L = h * rng.uniform(0.55, 0.92)
        pts = [base + np.array([rng.uniform(-0.05, 0.05) * w, 0])]
        d = np.array([math.cos(a), math.sin(a)])
        for i in range(10):
            d = nrm(np.append(d + np.array([rng.uniform(-0.08, 0.08), 0.03]), 0))[:2]
            pts.append(pts[-1] + d * L / 10)
        pt.line(pts, twig, 1.6 * S)
        P = np.array(pts)
        for i in range(1, len(P) - 1):
            for sgn in (-1, 1):
                tv = nrm(np.append(P[i + 1] - P[i], 0))[:2]
                aa = math.radians(55) * sgn
                bd = np.array([tv[0] * math.cos(aa) - tv[1] * math.sin(aa), tv[0] * math.sin(aa) + tv[1] * math.cos(aa)])
                bl = L * 0.2 * (1 - i / len(P)) + L * 0.05
                q0 = P[i] + (P[i + 1] - P[i]) * rng.random()
                q1 = q0 + bd * bl
                pt.line([q0, q1], twig, 0.9 * S)
                nlf = int(bl / (3.0 * S))
                for j in range(nlf):
                    f = (j + 0.5) / nlf
                    q = q0 + (q1 - q0) * f
                    ll = (5.5 - 2.5 * f) * S * rng.uniform(0.8, 1.2)
                    col = jit(pal[rng.integers(len(pal))], rng)
                    for s2 in (-1, 1):
                        a2 = math.radians(80) * s2
                        dd = np.array([bd[0] * math.cos(a2) - bd[1] * math.sin(a2), bd[0] * math.sin(a2) + bd[1] * math.cos(a2)])
                        pt.line([q, q + dd * ll + bd * ll * 0.4], col, 1.4 * S)


def paint_moss(pt: Painter, b, rng, n=110):
    """Hanging Spanish-moss strands from the top edge, grey-green, longest in the middle."""
    x0, y0, x1, y1 = pt.box(b)
    w, h = x1 - x0, y1 - y0
    S = pt.ss
    for i in range(n):
        u = rng.normal(0, 0.18)
        if abs(u) > 0.42:
            continue
        x = (x0 + x1) / 2 + u * w
        L = h * rng.uniform(0.35, 0.98) * (1 - abs(u) * 1.4)
        g = rng.uniform(0.75, 1.05)
        col = (int(150 * g), int(158 * g), int(128 * g))
        pts = [np.array([x, y0 + rng.uniform(0, 0.04) * h])]
        ph = rng.uniform(0, 6.28)
        steps = 24
        for k in range(steps):
            pts.append(pts[-1] + np.array([math.sin(ph + k * 0.7) * 1.6 * S + rng.normal(0, 0.8) * S, L / steps]))
        pt.line(pts, col, rng.uniform(0.8, 1.6) * S)
        for k in range(2, steps, 2):  # wispy side hairs
            q = pts[k]
            dx = rng.choice([-1, 1]) * rng.uniform(2, 6) * S
            pt.line([q, q + np.array([dx, rng.uniform(3, 9) * S])], tuple(int(c * 0.9) for c in col), 0.8 * S)


def paint_vine(pt: Painter, b, rng, pal, stem=(78, 72, 45)):
    x0, y0, x1, y1 = pt.box(b)
    w, h = x1 - x0, y1 - y0
    S = pt.ss
    cx = (x0 + x1) / 2
    pts = [np.array([cx + math.sin(t * 9) * w * 0.08, y0 + t * h]) for t in np.linspace(0, 1, 40)]
    pt.line(pts, stem, 2.2 * S)
    for i, t in enumerate(np.linspace(0.02, 0.97, 22)):
        c = pts[int(t * 39)]
        side = 1 if i % 2 else -1
        ang = math.radians(90 - side * rng.uniform(25, 60))  # pointing down-outwards (y down)
        ang = math.pi / 2 - side * (math.pi / 2 - ang)
        L = w * rng.uniform(0.28, 0.42) * (1.1 - 0.4 * t)
        col = jit(pal[rng.integers(len(pal))], rng)
        poly = leaf_poly(c, ang, L, L * 0.6)
        pt.d.polygon(poly, fill=tuple(int(x) for x in col) + (255,))
        pt.line([c, (c[0] + math.cos(ang) * L * 0.85, c[1] + math.sin(ang) * L * 0.85)],
                tuple(x * 0.7 for x in col), 0.9 * S)


def paint_twigs(pt: Painter, b, rng, col, frost=None, depth=5, wmin=1.0):
    """Bare twig spray growing up from the bottom centre (recursive)."""
    x0, y0, x1, y1 = pt.box(b)
    w, h = x1 - x0, y1 - y0
    S = pt.ss
    segs = []

    def rec(p, a, L, wd, d):
        q = p + np.array([math.cos(a), math.sin(a)]) * L
        mid = (p + q) / 2 + np.array([rng.normal(0, 0.06), rng.normal(0, 0.06)]) * L
        segs.append(([p, mid, q], wd))
        if d == 0:
            return
        for k in range(2 if d < depth else 3):
            da = rng.uniform(0.3, 0.65) * (1 if k % 2 else -1)
            rec(q if k else mid, a + da, L * rng.uniform(0.55, 0.75), wd * 0.7, d - 1)
        if rng.random() < 0.6:
            rec(q, a + rng.uniform(-0.15, 0.15), L * 0.6, wd * 0.7, d - 1)

    rec(np.array([(x0 + x1) / 2, y1 - 2]), -math.pi / 2 + rng.uniform(-0.1, 0.1), h * 0.3, 4.0 * S, depth)
    for pts, wd in segs:
        pts = [np.clip(p, [x0 + 2, y0 + 2], [x1 - 2, y1 - 2]) for p in pts]
        pt.line(pts, jit(col, rng, 0.08), max(wmin * S, wd))
        if frost:
            pt.line([p + [0, -0.5 * max(S, wd) * 0.6] for p in pts], frost, max(wmin * 0.7 * S, wd * 0.6))


# ----------------------------------------------------------------------------------------------
# Blender scene / materials / export
def reset_scene():
    bpy.ops.wm.read_factory_settings(use_empty=True)


def load_img(path, noncolor=False):
    im = bpy.data.images.load(str(path))
    if noncolor:
        im.colorspace_settings.name = "Non-Color"
    return im


def material(name, color=None, normal=None, nstr=1.0, alpha=False, rough=0.9, base=None, emissive=None,
             double=False):
    m = bpy.data.materials.new(name)
    try:
        m.use_nodes = True
    except Exception:
        pass
    nt = m.node_tree
    bsdf = next(n for n in nt.nodes if n.type == "BSDF_PRINCIPLED")
    bsdf.inputs["Roughness"].default_value = rough
    bsdf.inputs["Metallic"].default_value = 0.0
    if base is not None:
        bsdf.inputs["Base Color"].default_value = (*base, 1)
    if color:
        t = nt.nodes.new("ShaderNodeTexImage")
        t.image = load_img(color)
        nt.links.new(t.outputs["Color"], bsdf.inputs["Base Color"])
        if alpha:
            r = nt.nodes.new("ShaderNodeMath")
            r.operation = "ROUND"
            nt.links.new(t.outputs["Alpha"], r.inputs[0])
            nt.links.new(r.outputs[0], bsdf.inputs["Alpha"])
            for attr, val in (("blend_method", "CLIP"), ("alpha_threshold", 0.5)):
                try:
                    setattr(m, attr, val)
                except Exception:
                    pass
    if normal:
        t = nt.nodes.new("ShaderNodeTexImage")
        t.image = load_img(normal, noncolor=True)
        nm = nt.nodes.new("ShaderNodeNormalMap")
        nm.inputs["Strength"].default_value = nstr
        nt.links.new(t.outputs["Color"], nm.inputs["Color"])
        nt.links.new(nm.outputs["Normal"], bsdf.inputs["Normal"])
    if emissive:
        t = nt.nodes.new("ShaderNodeTexImage")
        t.image = load_img(emissive)
        nt.links.new(t.outputs["Color"], bsdf.inputs["Emission Color"])
        bsdf.inputs["Emission Strength"].default_value = 1.0
    m.use_backface_culling = not double
    return m


def build_and_export(m: Model, name: str, mats: dict, path: Path):
    keys = [k for k in mats if k in set(m.M)]
    me = bpy.data.meshes.new(name)
    V = np.array(m.V)
    me.from_pydata(V.tolist(), [], [list(f) for f in m.F])
    uvl = me.uv_layers.new(name="UVMap")
    uv = np.array([c for f in m.UV for c in f], np.float32)
    uvl.data.foreach_set("uv", uv.ravel())
    for k in keys:
        me.materials.append(mats[k])
    me.polygons.foreach_set("material_index", [keys.index(k) for k in m.M])
    me.shade_smooth()
    me.normals_split_custom_set_from_vertices([tuple(n) for n in m.N])
    me.update()
    ob = bpy.data.objects.new(name, me)
    bpy.context.scene.collection.objects.link(ob)
    kw = dict(filepath=str(path), export_format="GLB", export_yup=True, export_apply=True,
              export_image_format="AUTO", export_materials="EXPORT", export_texcoords=True,
              export_normals=True, export_animations=False, export_cameras=False, export_lights=False,
              export_extras=False)
    bpy.ops.export_scene.gltf(**kw)


def glb_stats(path: Path) -> dict:
    b = path.read_bytes()
    jl = struct.unpack_from("<I", b, 12)[0]
    g = json.loads(b[20:20 + jl])
    tris = 0
    for mesh in g["meshes"]:
        for p in mesh["primitives"]:
            if "indices" in p:
                tris += g["accessors"][p["indices"]]["count"] // 3
            else:
                tris += g["accessors"][p["attributes"]["POSITION"]]["count"] // 3
    imgs = [(im.get("mimeType"), g["bufferViews"][im["bufferView"]]["byteLength"]) for im in g.get("images", [])]
    mats = [{k: v for k, v in mt.items() if k in ("name", "alphaMode", "doubleSided", "emissiveFactor")}
            for mt in g.get("materials", [])]
    return {"tris": tris, "bytes": len(b), "images": imgs, "materials": mats,
            "extensionsUsed": g.get("extensionsUsed", []), "extensionsRequired": g.get("extensionsRequired", [])}


# ----------------------------------------------------------------------------------------------
# species
def snow_ridge(m, path, width, height, mat):
    """Snow lying along a branch: a flattened, end-tapered tube with a flat underside."""
    P = np.asarray(path, float)
    angs = [0.0, 0.8, 1.57, 2.34, 3.14, 4.71]
    rows = []
    for i, p in enumerate(P):
        t = nrm(P[min(i + 1, len(P) - 1)] - P[max(i - 1, 0)])
        s = nrm(np.cross(t, UP))
        u = nrm(np.cross(s, t))
        f = i / (len(P) - 1)
        tp = max(0.08, math.sin(math.pi * (0.04 + 0.92 * f)) ** 0.7)
        ring = []
        for a in angs:
            sa = math.sin(a)
            ring.append(m.v(p + s * math.cos(a) * width / 2 * tp + u * (sa if sa > 0 else sa * 0.3) * height * tp))
        rows.append(ring)
    k = len(angs)
    for i in range(len(P) - 1):
        for j in range(k):
            j2 = (j + 1) % k
            m.f((rows[i][j2], rows[i][j], rows[i + 1][j], rows[i + 1][j2]), [(0.5, 0.5)] * 4, mat)
    for ring, rev in ((rows[0], False), (rows[-1], True)):  # close the ends
        c = m.v(np.mean([m.V[x] for x in ring], axis=0))
        for j in range(k):
            a, b = ring[j], ring[(j + 1) % k]
            m.f((b, a, c) if rev else (a, b, c), [(0.5, 0.5)] * 3, mat)


def conifer(m, rng, sp, fol_uvs, bark_mat="bark", fol_mat="fol", snow_mat=None):
    """Whorled conifer: tapered trunk, whorls of drooping branches, each a main frond card plus two
    drooping side sprays (so the crown reads full from the side and from below)."""
    H, r0 = sp["H"], sp["r0"]
    sway = rng.normal(size=2) * 0.05

    def axis(z):
        return np.array([sway[0] * z * z / H, sway[1] * z * z / H, z])

    Ht = H - sp.get("leader", 0.9) * 0.45
    zs = np.concatenate([[-0.3], np.linspace(0, Ht, int(Ht / 0.7) + 2)])
    P = [axis(z) for z in zs]
    R = [r0 * (1 + 0.35 * math.exp(-max(z, 0) / 0.6)) * max(0.03, (1 - max(z, 0) / Ht)) ** 0.9 + 0.012 for z in zs]
    tube(m, P, R, sp.get("sides", 10), bark_mat, tex=0.7)
    for k in range(5):  # root flare knuckles
        a = k * 2 * math.pi / 5 + rng.uniform(-0.3, 0.3)
        d = np.array([math.cos(a), math.sin(a), -0.4])
        pts = grow(np.array([0, 0, 0.3]), d, r0 * 2.6, 3, rng, gravity=-0.3)
        tube(m, pts, np.linspace(r0 * 0.45, r0 * 0.12, len(pts)), 5, bark_mat, tex=0.5)

    def nfn_for(az):
        o = np.array([math.cos(az), math.sin(az), 0])
        return lambda p: nrm(o * 0.8 + UP * 0.55 + nrm(np.array([p[0], p[1], 0])) * 0.2)

    def pick():
        return fol_uvs[rng.integers(len(fol_uvs))]

    z = sp["zmin"]
    phase = rng.uniform(0, 6.28)
    arch = sp.get("arch", 0.1)
    while z < H - 0.5:
        rel = (z - sp["zmin"]) / (H - sp["zmin"])
        nb = int(rng.integers(sp["nwhorl"][0], sp["nwhorl"][1] + 1))
        if rel > 0.8:
            nb = max(3, nb - 2)
        phase += 2.39996
        for k in range(nb):
            if rng.random() < sp.get("gap", 0.0):
                continue
            az = phase + k * 2 * math.pi / nb + rng.uniform(-0.25, 0.25)
            L = sp["Lmax"] * ((H - z) / (H - sp["zmin"])) ** sp["exp"] * rng.uniform(*sp.get("lvar", (0.85, 1.1)))
            L = max(L, 0.3)
            zz = z + rng.uniform(-0.06, 0.06)
            o = np.array([math.cos(az), math.sin(az), 0.0])
            s0 = sp["s0"] + sp["s0top"] * rel ** 2
            dr = sp["droop"] * (1 - 0.6 * rel) * rng.uniform(0.8, 1.2)
            tu = sp["tipup"]
            base = axis(zz)
            path = [base + o * L * t + UP * L * (s0 * t - dr * t * t + tu * t ** 3) for t in np.linspace(0, 1, 5)]
            if L > 0.7:
                rb = 0.012 + 0.018 * L
                tube(m, [p - UP * 0.03 for p in path[:4]], np.linspace(rb, rb * 0.35, 4), 4, bark_mat, tex=0.3)
            w = max(0.35, L * sp["wfrac"])
            fp = list(path)
            fp[0] = base + o * min(0.2, L * 0.1) + UP * 0.02
            nf = nfn_for(az)
            frond(m, base, fp, w, pick(), fol_mat, nf, roll=rng.uniform(-0.45, 0.45), arch=arch)
            if L > 0.55:  # two drooping side sprays
                for sgn in (-1, 1):
                    q, tq, _ = sample(path, [1] * len(path), rng.uniform(0.25, 0.5))
                    d2 = nrm(rot(tq, UP, sgn * rng.uniform(0.55, 0.9)) - UP * sp.get("curtain", 0.5) * rng.uniform(0.5, 1.0))
                    L2 = L * rng.uniform(0.45, 0.62)
                    p2 = [q + d2 * L2 * t - UP * L2 * 0.3 * t * t - UP * 0.04 for t in np.linspace(0, 1, 4)]
                    frond(m, q, p2, max(0.3, L2 * sp["wfrac"]), pick(), fol_mat, nf,
                          roll=sgn * rng.uniform(0.3, 0.8), arch=arch)
            if snow_mat and L > 0.45 and rng.random() < sp.get("snow_p", 0.8):
                t0, t1 = 0.12, rng.uniform(0.75, 0.95)
                sp_pts = []
                for t in np.linspace(t0, t1, 5):
                    q, tq, _ = sample(fp, [1] * len(fp), t)
                    sp_pts.append(q + UP * (arch * w * 0.5 + 0.015))
                snow_ridge(m, sp_pts, w * rng.uniform(0.4, 0.55), 0.05 + 0.05 * L * rng.uniform(0.7, 1.2), snow_mat)
        step = sp["step"] * (1 - 0.45 * rel) * rng.uniform(0.85, 1.15)
        z += step
    # leader: crossed vertical fronds at the top
    lh = sp.get("leader", 0.9)
    top = axis(H)
    for k in range(2):
        a = rng.uniform(0, 3.14) + k * math.pi / 2
        side = np.array([math.cos(a), math.sin(a), 0])
        card(m, top - UP * lh, UP, side, lh, lh * 0.6, fol_uvs[0], fol_mat,
             lambda p: nrm(np.array([p[0], p[1], 0]) + UP * 0.6), segs=1)


def deciduous(m, rng, sp, p, d, L, r, level, tips, branches, mat="bark"):
    def g(k, default):
        v = sp.get(k)
        return v[level] if v is not None and level < len(v) else default

    nseg = max(2, int(math.ceil(L / g("seg", 0.4))))
    pts = grow(p, d, L, nseg, rng, gravity=g("grav", 0.0), noise=g("noise", 0.2), curl=g("curl", 0.0))
    rt = r * g("taper", 0.3)
    R = [r + (rt - r) * (i / nseg) ** 0.8 for i in range(nseg + 1)]
    n = max(0, g("n", 0) + int(rng.integers(-1, 2))) if g("n", 0) else 0
    broken = g("broken", False) and (n == 0 or rng.random() < 0.4)
    shape = sp.get("trunk_shape") if level == 0 else None
    tube(m, pts, R, g("sides", 4), mat, tex=g("tex", 0.3), cap=broken or (level == 0 and sp.get("cap_trunk", False)),
         shape=shape, twist=sp.get("twist", 0.0), jag=0.8 if broken else 0, rng=rng)
    branches.append((pts, R, level))
    kids = []
    if g("apical", False) and not broken:
        kids.append((0.985, rng.uniform(0.05, 0.25), g("alr", 0.7)))
    for k in range(n):
        kids.append((rng.uniform(*g("t", (0.3, 0.95))), math.radians(g("angle", 40) + rng.uniform(-12, 12)), g("lr", 0.5)))
    phase = rng.uniform(0, 6.28)
    for k, (t, ang, lr) in enumerate(kids):
        q, tan, rq = sample(pts, R, t)
        az = phase + k * 2.39996 + rng.uniform(-0.4, 0.4)
        sd = rot(perp(tan), tan, az)
        dn = nrm(math.cos(ang) * tan + math.sin(ang) * sd)
        dn = nrm(dn + UP * g("upb", 0.0))
        Lc = L * lr * rng.uniform(0.8, 1.15) * (1 - sp.get("tdec", 0.3) * t)
        rc = min(rq * 0.92, rq * g("rr", 0.6) * rng.uniform(0.85, 1.1))
        deciduous(m, rng, sp, q, dn, Lc, rc, level + 1, tips, branches, mat)
    if level >= sp.get("tip_level", 99) and not broken and not kids:
        tips.append((pts[-1], nrm(pts[-1] - pts[-2]), R[-1], level))
    elif level >= sp.get("tip_level", 99) and not broken:
        tips.append((pts[-1], nrm(pts[-1] - pts[-2]), R[-1], level))


def arch_root(m, rng, mat, a, h0, dist, r, rt, tex=0.5, sides=6):
    """Mangrove-style prop root: leaves the trunk at h0, arcs outward and dives into the ground."""
    o = np.array([math.cos(a), math.sin(a), 0])
    P0 = np.array([0, 0, h0]) + o * rt * 0.6
    P1 = P0 + o * dist * 0.35 + UP * 0.25 * h0
    P2 = o * dist * 0.85 + UP * 0.6 * h0
    P3 = o * dist + UP * -0.3
    pts = []
    for t in np.linspace(0, 1, 9):
        q = (1 - t) ** 3 * P0 + 3 * (1 - t) ** 2 * t * P1 + 3 * (1 - t) * t * t * P2 + t ** 3 * P3
        q = q + rng.normal(size=3) * 0.04 * math.sin(math.pi * t)
        pts.append(q)
    tube(m, pts, np.linspace(r, r * 0.55, len(pts)), sides, mat, tex=tex)
    return pts


def fin(m, mat, a, rt, D, Hb, th, rng, tex=1.0, nd=7, nh=5):
    """Plank buttress root: a thin wavy fin from the trunk (height Hb) out to the ground at distance D."""
    o = np.array([math.cos(a), math.sin(a), 0])
    s = np.array([-o[1], o[0], 0])
    wav = rng.uniform(0, 6.28)
    grid = {}
    for side in (-1, 1):
        for i in range(nd + 1):
            u = i / nd
            d = rt * 0.5 + (D - rt * 0.5) * u
            top = Hb * (1 - u) ** 1.8 + 0.05
            off = s * math.sin(wav + u * 3.0) * 0.25 * u
            for j in range(nh + 1):
                v = j / nh
                z = -0.3 + (top + 0.3) * v
                thick = th * (1 - u * 0.6) * (1 - v ** 3) + 0.01
                key = (side, i, j)
                if j == nh:
                    key = (0, i, j)
                    if key in grid:
                        continue
                    thick = 0
                grid[key] = m.v(o * d + off + s * side * thick / 2 + UP * z)
    for side in (-1, 1):
        for i in range(nd):
            for j in range(nh):
                def g(ii, jj):
                    return grid[(0, ii, jj)] if jj == nh else grid[(side, ii, jj)]
                d0, d1 = (rt * 0.5 + (D - rt * 0.5) * i / nd) / tex, (rt * 0.5 + (D - rt * 0.5) * (i + 1) / nd) / tex
                z0, z1 = j / nh * Hb / tex, (j + 1) / nh * Hb / tex
                q = (g(i, j), g(i + 1, j), g(i + 1, j + 1), g(i, j + 1))
                uv = ((d0, z0), (d1, z0), (d1, z1), (d0, z1))
                if side > 0:
                    q, uv = q[::-1], uv[::-1]
                m.f(q, uv, mat)


def lobed(rng, lobes, amp_fn, width=0.35, ridges=0.0, ridge_k=(5, 11)):
    """Cross-section multiplier with buttress lobes (amplitude by height) + optional vertical ridges."""
    angs = np.sort(rng.uniform(0, 2 * math.pi, lobes))
    amps = rng.uniform(0.6, 1.0, lobes)
    ks = [int(rng.integers(*ridge_k)) for _ in range(4)]
    ph = rng.uniform(0, 6.28, 4)

    def shape(a, i, z, p):
        da = np.abs((a - angs + math.pi) % (2 * math.pi) - math.pi)
        f = float(np.max(amps * np.exp(-(da / width) ** 2)))
        r = 1 + amp_fn(z) * (f - 0.25)
        if ridges:
            r += ridges * sum(math.sin(k * a + q + 0.05 * z) for k, q in zip(ks, ph)) / 4
        return r
    return shape


def crown_nfn(center, upw=0.5):
    return lambda p: nrm(nrm(p - center) + UP * upw)


# ----------------------------------------------------------------------------------------------
# the 9 trees
FIR_PAL = [(54, 92, 46), (66, 108, 54), (46, 80, 42), (76, 118, 60)]
SPRUCE_PAL = [(44, 78, 66), (52, 90, 76), (38, 68, 58), (62, 100, 82)]
PINE_DARK = [(36, 64, 44), (44, 76, 52), (32, 56, 40), (52, 84, 58)]


def make_tree(name, work: Path, tex_dir: Path, seed: int):
    rng = np.random.default_rng(seed)
    m = Model()
    mats = {}
    src = {}
    H = None

    def bark(asset, **kw):
        t = bark_textures(work, tex_dir, asset, **kw)
        src["bark"] = f"ambientCG {asset} (CC0) {ACG_PAGE.format(id=asset)}"
        return t

    def foliage_mat(path, rough=0.85):
        return material(name + "_foliage", color=path, alpha=True, double=True, rough=rough)

    if name == "tree_forest_giant":
        t = bark("Bark014", size=1024, nsize=512, sat=0.9, tint=(1.04, 0.84, 0.76), bright=0.8, contrast=1.4,
                 nstr=2.6, q=78)
        mats["bark"] = material("redwood_bark", t["color"], t["normal"], nstr=2.2, rough=0.92)
        Ht = 25.0
        zs = np.concatenate([[-0.4, -0.1], [0.05, 0.3, 0.6, 0.95, 1.35, 1.8, 2.35, 3.0, 3.8, 4.8, 6.0],
                             np.linspace(7.3, Ht, 14)])
        lean = rng.normal(size=2) * 0.008
        P = [np.array([lean[0] * z + 0.15 * math.sin(z * 0.2), lean[1] * z, z]) for z in zs]
        R = [2.0 * (1 - 0.009 * max(z, 0)) * (1 + 0.7 * math.exp(-max(z, 0) / 1.8)) for z in zs]
        shape = lobed(rng, 9, lambda z: 1.2 * math.exp(-max(z, 0) / 1.8) + 0.05, width=0.24, ridges=0.075,
                      ridge_k=(11, 23))
        tube(m, P, R, 64, "bark", tex=1.2, texv=3.2, cap=True, shape=shape)
        # stub branches high up (dead, broken) + a couple with short live-looking kinks
        for k in range(6):
            z = rng.uniform(13, 23.5)
            a = rng.uniform(0, 2 * math.pi)
            o = np.array([math.cos(a), math.sin(a), 0])
            rr = 2.0 * (1 - 0.009 * z)
            d = nrm(o + UP * rng.uniform(-0.3, 0.12))
            L = rng.uniform(1.2, 3.2)
            pts = grow(np.array([0, 0, z]) + o * rr * 0.6, d, L + rr * 0.4, 5, rng, gravity=-0.08, noise=0.1)
            rb = rng.uniform(0.3, 0.48)
            tube(m, pts, np.linspace(rb, rb * 0.55, len(pts)), 10, "bark", tex=0.9, cap=True, jag=0.8, rng=rng)
            if rng.random() < 0.6:
                q, tq, rq = sample(pts, np.linspace(rb, rb * 0.55, len(pts)), 0.7)
                d2 = nrm(horiz(rot(tq, UP, rng.choice([-1, 1]) * 0.8)) + UP * 0.5)
                p2 = grow(q, d2, rng.uniform(0.8, 1.6), 3, rng, noise=0.1)
                tube(m, p2, np.linspace(rq * 0.5, rq * 0.25, len(p2)), 6, "bark", tex=0.6, cap=True, jag=0.6, rng=rng)
        H = Ht
        src["foliage"] = "none"
    elif name in ("tree_forest_conifer", "tree_peaks_pine", "tree_ice_pine"):
        pt = Painter(512)
        if name == "tree_forest_conifer":
            t = bark("Bark004", size=512, nsize=256, sat=0.8, bright=0.85, tint=(1.0, 0.92, 0.88), nstr=1.4)
            for b in ((0, 0, 0.5, 1), (0.5, 0, 1, 1)):
                paint_frond(pt, b, rng, FIR_PAL, (104, 146, 72), needle=0.045, n_twigs=15, spread=0.5, nw=2.4)
            sp = dict(H=14.5, r0=0.3, zmin=2.2, nwhorl=(5, 6), Lmax=3.3, exp=0.95, lvar=(0.8, 1.12), s0=-0.08,
                      s0top=0.55, droop=0.42, tipup=0.22, step=0.55, wfrac=0.75, curtain=0.55, gap=0.05, arch=0.1,
                      leader=1.1)
            src["foliage"] = "procedural fir sprays painted in trees_woody.py (original)"
        elif name == "tree_peaks_pine":
            t = bark("Bark001", size=512, nsize=256, sat=0.7, bright=0.85, tint=(1.0, 0.93, 0.88), nstr=1.4)
            for b in ((0, 0, 0.5, 1), (0.5, 0, 1, 1)):
                paint_frond(pt, b, rng, SPRUCE_PAL, (84, 124, 104), needle=0.04, n_twigs=16, spread=0.5, nw=2.4)
            sp = dict(H=12.5, r0=0.26, zmin=0.9, nwhorl=(5, 7), Lmax=2.6, exp=1.0, lvar=(0.88, 1.08), s0=-0.12,
                      s0top=0.5, droop=0.25, tipup=0.12, step=0.44, wfrac=0.8, curtain=0.4, gap=0.0, arch=0.12,
                      leader=0.9)
            src["foliage"] = "procedural spruce sprays painted in trees_woody.py (original)"
        else:
            t = bark("Bark001", size=512, nsize=256, sat=0.6, bright=0.7, nstr=1.4)
            paint_frond(pt, (0, 0, 0.5, 1), rng, PINE_DARK, (62, 92, 66), needle=0.055, n_twigs=13, spread=0.5,
                        snow=0.8, nw=2.4)
            paint_frond(pt, (0.5, 0, 1, 1), rng, PINE_DARK, (62, 92, 66), needle=0.055, n_twigs=13, spread=0.5,
                        snow=0.3, nw=2.4)
            sp = dict(H=9.2, r0=0.22, zmin=1.3, nwhorl=(5, 6), Lmax=2.4, exp=0.9, lvar=(0.7, 1.15), s0=-0.15,
                      s0top=0.45, droop=0.32, tipup=0.12, step=0.45, wfrac=0.72, curtain=0.6, gap=0.1, arch=0.08,
                      leader=0.9, snow_p=0.85)
            mats["snow"] = material("snow", base=(0.86, 0.9, 0.96), rough=0.7)
            src["snow"] = "plain white PBR material"
        fp = pt.save(tex_dir / f"{name}_needles.png", colors=80)
        mats["bark"] = material(name + "_bark", t["color"], t["normal"], nstr=1.0)
        mats["fol"] = foliage_mat(fp)
        conifer(m, rng, sp, [Painter.uv((0, 0, 0.5, 1)), Painter.uv((0.5, 0, 1, 1))],
                snow_mat="snow" if name == "tree_ice_pine" else None)
        H = sp["H"]
    else:
        tips, branches = [], []
        if name == "tree_ice_dead":
            t = bark("Bark004", size=512, nsize=256, sat=0.25, bright=1.15, tint=(0.95, 0.97, 1.0), nstr=1.3,
                     frost=True)
            mats["bark"] = material("pale_bark", t["color"], t["normal"])
            mats["frost"] = material("frost_bark", t["frost"], t["normal"], nstr=0.5, rough=0.6)
            pt = None  # fine twigs are geometry here: alpha twig cards break up into specks at distance
            sp = dict(n=[5, 4, 3, 2, 0], t=[(0.4, 0.95), (0.2, 0.95), (0.25, 0.95), (0.3, 0.95)],
                      angle=[38, 45, 48, 45], lr=[0.8, 0.6, 0.5, 0.45], rr=[0.62, 0.55, 0.6, 0.6],
                      upb=[0.3, 0.15, 0.1, 0.15], apical=[True, True, False, False], alr=[0.85, 0.7],
                      grav=[0.02, 0.05, 0.02, 0.0, 0.0], noise=[0.05, 0.12, 0.18, 0.2, 0.3],
                      taper=[0.5, 0.35, 0.3, 0.25, 0.2], sides=[10, 7, 5, 4, 3], seg=[0.5, 0.5, 0.45, 0.4, 0.4],
                      tex=[0.7, 0.5, 0.4, 0.3, 0.2], tip_level=4)
            deciduous(m, rng, sp, np.array([0, 0, -0.3]), np.array([0.03, 0, 1]), 2.3, 0.22, 0, tips, branches)
            H = 6.0
            twig_uv = [Painter.uv((0, 0, 0.5, 1)), Painter.uv((0.5, 0, 1, 1))]
            src["foliage"] = "none (bare; twigs are 3-sided tubes)"
            twig_size = (0.7, 0.45)
        elif name == "tree_desert_dead":
            t = bark("Bark002", size=512, sat=0.12, bright=1.75, tint=(1.0, 0.98, 0.95), contrast=1.3, nstr=2.0)
            mats["bark"] = material("bleached_wood", t["color"], t["normal"], nstr=1.2, rough=0.8)
            pt = Painter(512)
            for b in ((0, 0, 0.5, 1), (0.5, 0, 1, 1)):
                paint_twigs(pt, b, rng, (176, 170, 160), depth=4, wmin=2.2)
            sp = dict(n=[3, 3, 2, 0], t=[(0.75, 1.0), (0.3, 0.95), (0.3, 0.9)], angle=[42, 45, 40],
                      lr=[1.15, 0.55, 0.5], rr=[0.68, 0.55, 0.6], upb=[0.15, 0.1, 0.1], apical=[False, False, False],
                      grav=[0.05, -0.05, 0.0, 0.0], noise=[0.25, 0.45, 0.5, 0.5], curl=[0.6, 0.8, 0.9, 0.9],
                      taper=[0.72, 0.4, 0.35, 0.3], sides=[9, 7, 5, 4], seg=[0.3, 0.35, 0.3, 0.3],
                      tex=[0.5, 0.4, 0.3, 0.3], broken=[False, True, True, True], twist=0.35, tdec=0.1,
                      tip_level=2, cap_trunk=False)
            deciduous(m, rng, sp, np.array([0, 0, -0.3]), np.array([0.15, 0.05, 1]), 1.5, 0.24, 0, tips, branches)
            for k in range(5):
                a = k * 2 * math.pi / 5 + rng.uniform(-0.4, 0.4)
                pts = grow(np.array([0, 0, 0.3]), np.array([math.cos(a), math.sin(a), -0.35]), rng.uniform(0.8, 1.4),
                           5, rng, gravity=-0.35, noise=0.25)
                tube(m, pts, np.linspace(0.13, 0.04, len(pts)), 6, "bark", tex=0.4, twist=0.3)
            H = 4.0
            twig_uv = [Painter.uv((0, 0, 0.5, 1)), Painter.uv((0.5, 0, 1, 1))]
            src["foliage"] = "procedural bleached twig sprays (original)"
            twig_size = (0.7, 0.35)
        elif name == "tree_lava_charred":
            t = bark("Bark001", size=512, contrast=1.0, nstr=1.8, char=True, seed=seed)
            mats["bark"] = material("charred_bark", t["color"], t["normal"], nstr=1.3, rough=0.95,
                                    emissive=t["emissive"])
            pt = None
            sp = dict(n=[8, 2, 0], t=[(0.25, 0.9), (0.3, 0.9)], angle=[60, 40], lr=[0.36, 0.5],
                      rr=[0.38, 0.55], upb=[0.25, 0.15], apical=[False, False], grav=[0.0, 0.05, 0.0],
                      noise=[0.06, 0.3, 0.4], taper=[0.6, 0.4, 0.35], sides=[10, 6, 5], seg=[0.5, 0.35, 0.3],
                      tex=[0.7, 0.4, 0.3], broken=[True, True, True], tdec=0.4, tip_level=9, cap_trunk=True)
            deciduous(m, rng, sp, np.array([0, 0, -0.3]), np.array([0.06, -0.03, 1]), 5.1, 0.26, 0, tips, branches)
            for k in range(5):
                a = k * 2 * math.pi / 5 + rng.uniform(-0.4, 0.4)
                pts = grow(np.array([0, 0, 0.35]), np.array([math.cos(a), math.sin(a), -0.5]), rng.uniform(0.6, 1.0),
                           4, rng, gravity=-0.5, noise=0.15)
                tube(m, pts, np.linspace(0.14, 0.05, len(pts)), 6, "bark", tex=0.4)
            H = 5.0
            src["foliage"] = "none"
            src["emissive"] = "ember cracks derived from the Bark001 displacement map (procedural)"
        elif name == "tree_swamp_cypress":
            t = bark("Bark015", size=512, sat=0.85, bright=0.85, nstr=1.6)
            mats["bark"] = material("cypress_bark", t["color"], t["normal"], nstr=1.2)
            pt = Painter(512)
            paint_feather(pt, (0, 0, 0.5, 0.5), rng, [(112, 136, 66), (126, 148, 74), (98, 120, 60)], sprays=7)
            paint_feather(pt, (0.5, 0, 1, 0.5), rng, [(104, 130, 70), (120, 144, 76), (90, 114, 58)], sprays=6)
            paint_moss(pt, (0, 0.5, 0.5, 1.0), rng)
            paint_moss(pt, (0.5, 0.5, 1.0, 1.0), rng, n=80)
            flare = lobed(rng, 6, lambda z: 1.4 * math.exp(-max(z, 0) / 1.1) + 0.02, width=0.42, ridges=0.03)
            sp = dict(n=[5, 4, 3, 0], t=[(0.5, 1.0), (0.25, 0.95), (0.3, 0.95)], angle=[55, 48, 45],
                      lr=[0.62, 0.55, 0.45], rr=[0.5, 0.55, 0.6], upb=[0.12, 0.0, 0.05], apical=[True, False, False],
                      alr=[0.45], grav=[0.0, -0.06, 0.02, 0.0], noise=[0.08, 0.2, 0.22, 0.25],
                      curl=[0.05, 0.15, 0.2, 0.2], taper=[0.38, 0.35, 0.3, 0.25], sides=[14, 7, 5, 4],
                      seg=[0.55, 0.6, 0.5, 0.45], tex=[0.9, 0.5, 0.4, 0.3], tip_level=2,
                      trunk_shape=lambda a, i, z, p: flare(a, i, z, p) * (1 + 0.12 * math.sin(z * 1.3 + a)))
            deciduous(m, rng, sp, np.array([0, 0, -0.3]), np.array([0.05, 0.03, 1]), 6.6, 0.42, 0, tips, branches)
            for k in range(11):
                a = k * 2 * math.pi / 11 + rng.uniform(-0.25, 0.25)
                arch_root(m, rng, "bark", a, rng.uniform(0.7, 2.0), rng.uniform(1.7, 3.2), rng.uniform(0.1, 0.16),
                          0.42 * 1.6)
            H = 10.5
            src["foliage"] = "procedural feathery cypress sprays + Spanish-moss strands (original)"
        elif name == "tree_temple_jungle":
            t = bark("Bark013", size=512, sat=0.85, bright=0.8, nstr=1.5)
            mats["bark"] = material("jungle_bark", t["color"], t["normal"], nstr=1.1)
            pt = Painter(512)
            pal = [(60, 114, 46), (76, 132, 52), (48, 98, 42), (96, 144, 56), (70, 124, 64)]
            paint_leaves(pt, (0, 0, 0.5, 0.5), rng, pal, n=75)
            paint_leaves(pt, (0.5, 0, 1.0, 0.5), rng, pal, n=65, leaf=(0.12, 0.2), aspect=0.5)
            paint_leaves(pt, (0, 0.5, 0.5, 1.0), rng, [(50, 102, 44), (64, 118, 50), (86, 134, 60)], n=80,
                         leaf=(0.14, 0.2), aspect=0.32)
            paint_vine(pt, (0.5, 0.5, 0.75, 1.0), rng, pal)
            paint_vine(pt, (0.75, 0.5, 1.0, 1.0), rng, pal)
            flare = lobed(rng, 6, lambda z: 0.45 * math.exp(-max(z, 0) / 1.4) + 0.02, width=0.3, ridges=0.02)
            sp = dict(n=[6, 4, 3, 0], t=[(0.78, 1.0), (0.3, 1.0), (0.3, 1.0)], angle=[58, 50, 42],
                      lr=[0.8, 0.55, 0.55], rr=[0.52, 0.6, 0.6], upb=[0.12, 0.08, 0.3], apical=[False, False, False],
                      grav=[0.0, -0.05, 0.0, 0.0], noise=[0.015, 0.12, 0.2, 0.2], taper=[0.6, 0.4, 0.35, 0.3],
                      sides=[14, 7, 5, 3], seg=[0.9, 0.8, 0.6, 0.5], tex=[1.0, 0.6, 0.4, 0.3], tdec=0.25,
                      tip_level=2, trunk_shape=flare)
            deciduous(m, rng, sp, np.array([0, 0, -0.3]), np.array([0.0, 0.0, 1]), 7.6, 0.6, 0, tips, branches)
            for k in range(6):
                a = k * 2 * math.pi / 6 + rng.uniform(-0.25, 0.25)
                fin(m, "bark", a, 0.6 * 1.2, rng.uniform(2.2, 3.3), rng.uniform(2.0, 3.0), rng.uniform(0.1, 0.17), rng)
            H = 14.0
            src["foliage"] = "procedural broadleaf clusters + vines (original)"
        if pt is not None:
            fpth = pt.save(tex_dir / f"{name}_foliage.png", colors=96)
            mats["fol"] = foliage_mat(fpth)
        # foliage / twig placement
        if tips:
            cen = np.mean([tp[0] for tp in tips], axis=0) - UP * 1.0
        if name == "tree_desert_dead":
            nf = lambda p: nrm(nrm(p - np.array([0, 0, H * 0.5])) + UP * 0.3)  # noqa: E731
            for p, d, r, lv in tips:
                if rng.random() < (0.5 if name == "tree_desert_dead" else 0.6):
                    continue
                for k in range(2):
                    side = rot(perp(d), d, k * math.pi / 2 + rng.uniform(0, 0.5))
                    if np.dot(np.cross(side, d), nf(p)) < 0:
                        side = -side
                    s = twig_size[0] * rng.uniform(0.8, 1.2)
                    card(m, p - d * s * 0.15, d, side, s, s * 0.9, twig_uv[rng.integers(2)], "fol", nf)
        elif name == "tree_swamp_cypress":
            nf = crown_nfn(cen, 0.6)
            uvs = [Painter.uv((0, 0, 0.5, 0.5)), Painter.uv((0.5, 0, 1, 0.5))]
            for p, d, r, lv in tips:
                cluster(m, p + UP * 0.15, 1.7, uvs, "fol", rng, nf, n=2, upish=0.0)
            # extra sprays along level-2 branches (sparse)
            for pts, R, lv in branches:
                if lv == 2 and rng.random() < 0.4:
                    q, _, _ = sample(pts, R, rng.uniform(0.3, 0.7))
                    cluster(m, q + UP * 0.1, 1.3, uvs, "fol", rng, nf, n=2)
            # hanging moss from level 1-2 branches
            muv = [Painter.uv((0, 0.5, 0.5, 1.0)), Painter.uv((0.5, 0.5, 1.0, 1.0))]
            cand = [b for b in branches if b[2] in (1, 2)]
            for k in range(70):
                pts, R, lv = cand[rng.integers(len(cand))]
                q, tq, rq = sample(pts, R, rng.uniform(0.15, 0.9))
                L = min(q[2] - 1.0, rng.uniform(1.0, 2.8))
                yaw = rng.uniform(0, 6.28)
                side = np.array([math.cos(yaw), math.sin(yaw), 0])
                if np.dot(np.cross(side, -UP), horiz(q)) < 0:
                    side = -side
                sway = horiz(rng.normal(size=3)) * rng.uniform(0.05, 0.25)
                # cards are mapped base (v0) at the top edge of the moss box: hang downwards
                u0, vb, u1, vt = muv[rng.integers(2)]
                card(m, q - UP * 0.02, -UP, -side, L, rng.uniform(0.5, 0.9), (u1, vt, u0, vb), "fol",
                     lambda p: nrm(horiz(p) + UP * 0.2), segs=2, bend=lambda t, s=sway: s * t * t)
        elif name == "tree_temple_jungle":
            nf = crown_nfn(cen, 0.7)
            uvs = [Painter.uv((0, 0, 0.5, 0.5)), Painter.uv((0.5, 0, 1, 0.5)), Painter.uv((0, 0.5, 0.5, 1.0))]
            for p, d, r, lv in tips:
                cluster(m, p + UP * 0.25, 2.6, uvs, "fol", rng, nf, n=3, upish=0.0)
            for pts, R, lv in branches:
                if lv == 2:
                    for tt in (0.45,):
                        q, _, _ = sample(pts, R, tt + rng.uniform(-0.1, 0.1))
                        cluster(m, q + UP * 0.2, 2.2, uvs, "fol", rng, nf, n=3)
            vuv = [Painter.uv((0.5, 0.5, 0.75, 1.0)), Painter.uv((0.75, 0.5, 1.0, 1.0))]
            cand = [b for b in branches if b[2] == 1]
            for k in range(9):
                pts, R, lv = cand[rng.integers(len(cand))]
                q, tq, rq = sample(pts, R, rng.uniform(0.2, 0.8))
                L = min(q[2] - 1.5, rng.uniform(3.0, 7.0))
                for j in range(2):
                    yaw = rng.uniform(0, 6.28) + j * math.pi / 2
                    side = np.array([math.cos(yaw), math.sin(yaw), 0])
                    u0, vb, u1, vt = vuv[rng.integers(2)]
                    sway = horiz(rng.normal(size=3)) * 0.3
                    card(m, q, -UP, -side, L, 0.4, (u1, vt, u0, vb), "fol", lambda p: nrm(horiz(p) + UP * 0.2),
                         segs=3, bend=lambda t, s=sway: s * math.sin(t * math.pi))
        if name == "tree_ice_dead":
            m.retag_up_faces("bark", "frost", 0.25, rng, 0.15)
    # uniform scale to the target height (giant is exact by construction)
    if name != "tree_forest_giant":
        m.transform(H / m.zmax())
    return m, mats, src, H


TREES = {  # name: (world, seed)
    "tree_forest_giant": ("forest", 11),
    "tree_forest_conifer": ("forest", 21),
    "tree_ice_pine": ("ice", 31),
    "tree_ice_dead": ("ice", 41),
    "tree_desert_dead": ("desert", 51),
    "tree_peaks_pine": ("peaks", 61),
    "tree_swamp_cypress": ("swamp", 71),
    "tree_temple_jungle": ("temple", 81),
    "tree_lava_charred": ("lava", 91),
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("out")
    ap.add_argument("--only", default="")
    ap.add_argument("--work", default=None)
    a = ap.parse_args()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    work = Path(a.work) if a.work else out.parent / "work_woody"
    tex_dir = work / "tex"
    tex_dir.mkdir(parents=True, exist_ok=True)
    names = [n for n in TREES if not a.only or n in a.only.split(",")]
    man_path = out / "woody_manifest.json"
    manifest = {e["file"]: e for e in json.loads(man_path.read_text())} if man_path.exists() else {}
    for name in names:
        reset_scene()
        world, seed = TREES[name]
        m, mats, src, H = make_tree(name, work, tex_dir, seed)
        path = out / f"{name}.glb"
        build_and_export(m, name, mats, path)
        st = glb_stats(path)
        sources = "; ".join(f"{k}: {v}" for k, v in src.items())
        manifest[path.name] = {"file": path.name, "world": world, "kind": "tree", "height_m": round(H, 2),
                               "tris": st["tris"], "bytes": st["bytes"], "source": sources}
        print(json.dumps({"tree": name, **st, "height_m": round(H, 2)}), flush=True)
    order = [f"{n}.glb" for n in TREES]
    man_path.write_text(json.dumps([manifest[k] for k in order if k in manifest], indent=1))


if __name__ == "__main__":
    main()
