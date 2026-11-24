"""Leafy trees and plants for "Unhappy Woks" (Blender script, procedural; all sources CC0 or original).

    /root/.builder/makehuman/mhenv/bin/python -I tools/gamesfx/trees_leafy.py SRC_DIR OUT_DIR [model ...]

SRC_DIR holds the CC0 inputs:
  acg/BarkNNN/BarkNNN_1K-JPG_{Color,NormalGL}.jpg   ambientCG bark (1K-JPG zips, unzipped)
  ph/fern_02_{diff,alpha}_1k.jpg                     Poly Haven fern_02 frond textures
Everything else (birch bark, cactus skin, palm rings, stems, leaf sprigs, palm fronds, reed blades) is
painted here with numpy/PIL. Each model is built from tubes (branches, stems), leaf cards and bent strips
(fronds, blades, big leaves), exported as GLB (Y-up, metres, origin at the base, textures embedded:
JPEG for opaque, palette PNG for alpha cards), then the GLB JSON is patched so leaf materials are
alphaMode MASK + doubleSided. Writes OUT_DIR/<model>.glb and OUT_DIR/leafy_manifest.json.
"""
import bpy  # noqa: F401  (must be imported before bmesh / mathutils)
import bmesh
from mathutils import Vector

import json
import math
import os
import struct
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

Z = Vector((0, 0, 1))
LANCZOS = Image.Resampling.LANCZOS

# ====================================================================================== textures


def fbm(h, w=None, beta=2.0, seed=0, aniso=(1.0, 1.0)):
    """Seamless fractal noise (zero mean, unit std). aniso=(ax, ay) > 1 stretches features along that axis."""
    w = w or h
    rng = np.random.default_rng(seed)
    fy = np.fft.fftfreq(h)[:, None] * aniso[1]
    fx = np.fft.fftfreq(w)[None, :] * aniso[0]
    f = np.sqrt(fx ** 2 + fy ** 2)
    f[0, 0] = 1
    spec = (rng.normal(size=(h, w)) + 1j * rng.normal(size=(h, w))) / f ** (beta / 2)
    spec[0, 0] = 0
    x = np.real(np.fft.ifft2(spec))
    return (x - x.mean()) / (x.std() + 1e-9)


def normal_from_height(hgt, strength):
    """OpenGL-style (+Y up) tangent-space normal map from a periodic height field."""
    dx = (np.roll(hgt, -1, 1) - np.roll(hgt, 1, 1)) * 0.5
    dy_up = (np.roll(hgt, 1, 0) - np.roll(hgt, -1, 0)) * 0.5
    n = np.dstack([-dx * strength, -dy_up * strength, np.ones_like(hgt)])
    return n / np.linalg.norm(n, axis=2, keepdims=True)


def blend_normals(a, b):
    """Whiteout blend of two unit normal maps (float arrays)."""
    n = np.dstack([a[..., 0] + b[..., 0], a[..., 1] + b[..., 1], a[..., 2] * b[..., 2]])
    return n / np.linalg.norm(n, axis=2, keepdims=True)


def save_rgb(arr, path, q=84):
    Image.fromarray((np.clip(arr, 0, 1) * 255).astype(np.uint8)).save(path, quality=q, optimize=True)
    return path


def save_normal(n, path, q=80, size=None):
    if size and size != n.shape[0]:
        img = Image.fromarray((np.clip(n * 0.5 + 0.5, 0, 1) * 255).astype(np.uint8)).resize((size, size), LANCZOS)
        n = np.asarray(img, np.float32) / 255 * 2 - 1
    return save_rgb(n * 0.5 + 0.5, path, q)


def load_rgb(path, size):
    return np.asarray(Image.open(path).convert("RGB").resize((size, size), LANCZOS), np.float32) / 255


def save_cutout(img, path, cut=110, noise=0.0, seed=0):
    """RGBA -> binary alpha palette PNG; transparent texels get the mean leaf colour (no dark fringes).
    noise adds fine luminance grain (painted leaves otherwise look flat)."""
    a = np.asarray(img)[..., 3] >= cut
    rgb = np.asarray(img.convert("RGB")).astype(np.float32)
    if noise:
        g = fbm(rgb.shape[0], rgb.shape[1], beta=1.0, seed=seed) * 0.6 + fbm(rgb.shape[0], rgb.shape[1], beta=2.2,
                                                                               seed=seed + 1) * 0.4
        rgb = np.clip(rgb * (1 + noise * g[..., None]), 0, 255)
    mean = rgb[a].mean(0) if a.any() else np.array([80, 100, 50.0])
    rgb[~a] = mean * 0.9
    out = Image.fromarray(np.dstack([rgb.astype(np.uint8), (a * 255).astype(np.uint8)]), "RGBA")
    out.quantize(colors=255, method=Image.Quantize.FASTOCTREE).save(path, optimize=True)
    return path


def keep_connected(mask, seeds, iters=2000):
    """Flood-fill (4-neighbour) from seed pixels inside mask; drops stray specks in scanned atlases."""
    cur = seeds & mask
    for _ in range(iters):
        nxt = cur | np.roll(cur, 1, 0) | np.roll(cur, -1, 0) | np.roll(cur, 1, 1) | np.roll(cur, -1, 1)
        nxt &= mask
        if (nxt == cur).all():
            break
        cur = nxt
    return cur


# ---------------------------------------------------------------- bark / skins


def tex_bark_acg(src, asset, size, out, tint=(1, 1, 1), sat=1.0, nsize=None):
    base = Path(src) / "acg" / asset
    c = load_rgb(base / f"{asset}_1K-JPG_Color.jpg", size)
    grey = c.mean(2, keepdims=True)
    c = (grey + (c - grey) * sat) * np.array(tint, np.float32)
    n = load_rgb(base / f"{asset}_1K-JPG_NormalGL.jpg", nsize or size)
    save_rgb(c, out / f"{asset}_c.jpg", 80)
    save_rgb(n, out / f"{asset}_n.jpg", 78)
    return out / f"{asset}_c.jpg", out / f"{asset}_n.jpg"


def tex_birch(out, size=512, seed=7):
    rng = np.random.default_rng(seed)
    s = size
    base = np.ones((s, s, 3), np.float32) * np.array([0.87, 0.86, 0.81], np.float32)
    base *= (1 + 0.05 * fbm(s, seed=seed, aniso=(3, 0.5)))[..., None]
    base += (0.03 * fbm(s, seed=seed + 1, beta=2.6))[..., None] * np.array([1.0, 0.8, 0.3])
    hgt = 0.25 * fbm(s, seed=seed + 2, aniso=(4, 0.4), beta=1.8)
    dark = Image.new("L", (s * 3, s), 0)
    d = ImageDraw.Draw(dark)
    for _ in range(260):          # lenticels: thin horizontal dashes
        x, y = rng.uniform(s, 2 * s), rng.uniform(0, s)
        ln, th = rng.uniform(6, 45) * (2 if rng.random() < 0.15 else 1), rng.uniform(1.2, 3.2)
        d.ellipse([x - ln / 2, y - th / 2, x + ln / 2, y + th / 2], fill=int(rng.uniform(140, 255)))
    for _ in range(9):            # black branch scars: ragged, drooping chevrons
        x, y = rng.uniform(s, 2 * s), rng.uniform(0, s)
        w, h = rng.uniform(25, 70), rng.uniform(5, 14)
        top = [(x + w * u, y - h * (1 - abs(u)) ** 1.5 + h * 0.8 * u * u + rng.uniform(-2, 2))
               for u in np.linspace(-1, 1, 11)]
        bot = [(x + w * u * 0.9, y + h * 0.35 * (1 - abs(u)) + h * 0.8 * u * u + rng.uniform(1, 4))
               for u in np.linspace(1, -1, 11)]
        d.polygon(top + bot, fill=int(rng.uniform(180, 255)))
    dk = np.asarray(dark, np.float32) / 255
    dk = dk[:, s:2 * s] + dk[:, :s] + dk[:, 2 * s:]
    dk = np.asarray(Image.fromarray((np.clip(dk, 0, 1) * 255).astype(np.uint8)).filter(ImageFilter.GaussianBlur(0.8)),
                    np.float32) / 255
    blot = np.clip((fbm(s, seed=seed + 3, aniso=(2.5, 0.35)) - 1.3) * 0.9, 0, 1)
    dk = np.clip(dk + blot, 0, 1)
    col = base * (1 - dk[..., None]) + np.array([0.11, 0.10, 0.09]) * dk[..., None]
    hgt -= dk * 1.2
    save_rgb(col, out / "birch_c.jpg")
    save_normal(normal_from_height(hgt, 2.5), out / "birch_n.jpg")
    return out / "birch_c.jpg", out / "birch_n.jpg"


def tex_palm_trunk(src, out, size=512, rings=6, seed=11):
    rng = np.random.default_rng(seed)
    c = load_rgb(Path(src) / "acg/Bark012/Bark012_1K-JPG_Color.jpg", size)
    nb = load_rgb(Path(src) / "acg/Bark012/Bark012_1K-JPG_NormalGL.jpg", size) * 2 - 1
    grey = c.mean(2, keepdims=True)
    c = grey + (c - grey) * 0.55
    c = c * 0.75 + np.array([0.52, 0.48, 0.42], np.float32) * 0.25
    yy, xx = np.mgrid[0:size, 0:size].astype(np.float32)
    per = size / rings
    wav = 3.0 * np.sin(xx / size * 2 * np.pi * 2 + rng.uniform(0, 6)) + 2.0 * fbm(size, seed=seed)[0][None, :]
    ph = ((yy + wav) / per) % 1.0                    # 0 just below a ring line .. 1 just above the next
    hgt = ph * 1.0 - 4.0 * np.exp(-((ph - 0.0) / 0.05) ** 2) - 4.0 * np.exp(-((ph - 1.0) / 0.05) ** 2)
    line = np.exp(-((ph - 0.0) / 0.045) ** 2) + np.exp(-((ph - 1.0) / 0.045) ** 2)
    c *= (1 - 0.55 * line)[..., None]
    c *= (0.9 + 0.2 * ph)[..., None]
    nr = normal_from_height(hgt, 2.0)
    n = blend_normals(nb / np.linalg.norm(nb, axis=2, keepdims=True), nr)
    save_rgb(c, out / "palm_c.jpg")
    save_normal(n, out / "palm_n.jpg", size=256)
    return out / "palm_c.jpg", out / "palm_n.jpg"


def tex_cactus(out, size=512, seed=21):
    """Two ribs per tile: crests at u=0 and u=0.5, grooves at u=0.25/0.75; areoles + spines on crests."""
    rng = np.random.default_rng(seed)
    s = size
    xx = np.arange(s, dtype=np.float32)[None, :].repeat(s, 0)
    x = (xx / s * 2) % 1.0
    crest = np.abs(np.cos(np.pi * x)) ** 0.7       # 1 on crest, 0 in groove
    streak = fbm(s, seed=seed, aniso=(0.25, 3.5), beta=1.8)
    col = (np.array([0.30, 0.38, 0.29]) * (1 - crest[..., None]) + np.array([0.47, 0.56, 0.45]) * crest[..., None])
    col = col * (1 + 0.07 * streak[..., None])
    bloom = np.clip(fbm(s, seed=seed + 1, beta=2.4) * 0.5 + 0.2, 0, 1)
    col = col * (1 - 0.18 * bloom[..., None]) + np.array([0.62, 0.66, 0.60]) * 0.18 * bloom[..., None]
    hgt = crest * 3.0 + 0.25 * streak
    over = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    bump = Image.new("L", (s, s), 0)
    d, db = ImageDraw.Draw(over), ImageDraw.Draw(bump)
    for ri, cx in enumerate((0, s / 2, s)):
        stag = (s / 16) if ri == 1 else 0.0             # alternate ribs: areoles offset half a step
        for k in range(9):
            cy = (k + 0.5) * s / 8 + rng.uniform(-9, 9) - s / 16 + stag
            for oy in (-s, 0, s):
                y = cy + oy
                if y < -40 or y > s + 40:
                    continue
                for _ in range(int(rng.integers(5, 9))):      # spines
                    a = rng.uniform(0, 2 * np.pi)
                    ln = rng.uniform(10, 26)
                    d.line([(cx, y), (cx + ln * np.cos(a), y + ln * np.sin(a))], fill=(214, 206, 180, 255), width=1)
                r = rng.uniform(3.5, 5.0)
                d.ellipse([cx - r, y - r, cx + r, y + r], fill=(170, 150, 112, 255))
                db.ellipse([cx - r - 1, y - r - 1, cx + r + 1, y + r + 1], fill=255)
    ov = np.asarray(over, np.float32) / 255
    col = col * (1 - ov[..., 3:]) + ov[..., :3] * ov[..., 3:]
    hgt += np.asarray(bump.filter(ImageFilter.GaussianBlur(1.5)), np.float32) / 255 * 1.5
    save_rgb(col, out / "cactus_c.jpg")
    save_normal(normal_from_height(hgt, 1.6), out / "cactus_n.jpg")
    return out / "cactus_c.jpg", out / "cactus_n.jpg"


def tex_stem(out, name, size=256, seed=31, green=(0.30, 0.44, 0.17), head=None):
    """Plant stem skin with vertical fibres; with head=rgb the top half is a velvety cattail head."""
    s = size
    fib = fbm(s, seed=seed, aniso=(0.2, 4.0), beta=1.6)
    col = np.ones((s, s, 3), np.float32) * np.array(green, np.float32) * (1 + 0.10 * fib[..., None])
    hgt = 0.6 * fib
    if head is not None:
        fz = fbm(s, seed=seed + 1, beta=1.2)
        top = np.zeros((s, s), bool)
        top[: s // 2] = True
        hc = np.array(head, np.float32) * (1 + 0.12 * fz[..., None])
        col[top] = hc[top]
        hgt[top] = 0.8 * fz[top]
    save_rgb(col, out / f"{name}_c.jpg")
    save_normal(normal_from_height(hgt, 1.5), out / f"{name}_n.jpg")
    return out / f"{name}_c.jpg", out / f"{name}_n.jpg"


# ---------------------------------------------------------------- leaves / sprigs (procedural)

SHAPES = {
    "ovate": lambda t: np.sin(np.pi * t ** 0.8) ** 0.85,
    "birch": lambda t: np.sin(np.pi * t ** 0.55) ** 1.0,
    "lance": lambda t: np.sin(np.pi * t) ** 0.8,
}


def draw_leaf(d, base, ang, ln, wd, col, shape, serr=0.0, rng=None):
    """Leaf polygon from base point along angle (radians, image coords y down)."""
    ax, ay = np.cos(ang), -np.sin(ang)
    px, py = -ay, ax
    ts = np.linspace(0, 1, 22)
    hw = wd / 2 * SHAPES[shape](ts)
    if serr:
        hw = hw * (1 - serr * ((ts * 14) % 1.0))
    bx, by = base
    left = [(bx + ax * t * ln + px * h, by + ay * t * ln + py * h) for t, h in zip(ts, hw)]
    right = [(bx + ax * t * ln - px * h, by + ay * t * ln - py * h) for t, h in zip(ts, hw)]
    c1 = tuple(int(255 * min(1, v)) for v in col)
    c2 = tuple(int(255 * min(1, v * 0.82)) for v in col)
    d.polygon(left + right[::-1], fill=c1 + (255,))
    d.polygon(right + [(bx + ax * ln, by + ay * ln)], fill=c2 + (255,))
    c3 = tuple(int(255 * min(1, v * 1.18 + 0.03)) for v in col)
    d.line([(bx, by), (bx + ax * ln * 0.85, by + ay * ln * 0.85)], fill=c3 + (255,), width=max(1, int(wd * 0.05)))


SPRIG = {
    # leaves per cell, leaf length range (fraction of cell), width ratio, shape, palette, twig colour
    "broad": dict(n_side=6, leaves=7, L=(0.15, 0.22), wr=0.56, shape="ovate", serr=0.0, twig=(0.30, 0.24, 0.17),
                  pal=[(0.27, 0.42, 0.13), (0.33, 0.49, 0.16), (0.39, 0.54, 0.19), (0.30, 0.45, 0.15),
                       (0.44, 0.56, 0.21)], fill=0.42),
    "birch": dict(n_side=5, leaves=6, L=(0.09, 0.13), wr=0.72, shape="birch", serr=0.12, twig=(0.22, 0.18, 0.15),
                  pal=[(0.52, 0.66, 0.22), (0.60, 0.71, 0.27), (0.46, 0.61, 0.20), (0.66, 0.73, 0.31)], fill=0.0),
    "shrub": dict(n_side=7, leaves=4, L=(0.05, 0.08), wr=0.42, shape="lance", serr=0.0, twig=(0.42, 0.37, 0.31),
                  pal=[(0.50, 0.53, 0.40), (0.56, 0.57, 0.44), (0.45, 0.49, 0.35), (0.60, 0.55, 0.40),
                       (0.52, 0.44, 0.30)], fill=0.0),
}


def paint_sprig(d, S, kind, rng):
    """One sprig in an S x S cell: twig from bottom-centre with side twigs and leaves."""
    p = SPRIG[kind]
    leaves = []
    twigw = S * (0.012 if kind != "shrub" else 0.008)
    tw = tuple(int(255 * v) for v in p["twig"]) + (255,)
    ang = np.pi / 2 + rng.uniform(-0.15, 0.15)
    main_len = S * (0.80 if kind == "broad" else 0.86)
    pts = [(S / 2, S * 0.99)]
    for i in range(10):
        ang += rng.uniform(-0.12, 0.12)
        x, y = pts[-1]
        pts.append((x + np.cos(ang) * main_len / 10, y - np.sin(ang) * main_len / 10))
    d.line(pts, fill=tw, width=max(1, int(twigw * 1.6)), joint="curve")

    def twig(x, y, a, ln, w, depth):
        seg = [(x, y)]
        for _ in range(5):
            a += rng.uniform(-0.18, 0.18)
            x, y = x + np.cos(a) * ln / 5, y - np.sin(a) * ln / 5
            seg.append((x, y))
        d.line(seg, fill=tw, width=max(1, int(w)), joint="curve")
        n = p["leaves"]
        for k in range(n):
            j = int(1 + (len(seg) - 2) * (k + 0.5) / n)
            bx, by = seg[j]
            side = 1 if k % 2 else -1
            leaves.append(((bx, by), a + side * rng.uniform(0.5, 1.1)))
        leaves.append((seg[-1], a + rng.uniform(-0.2, 0.2)))
        if depth > 0 and kind == "shrub":
            for _ in range(2):
                j = int(rng.integers(1, len(seg) - 1))
                twig(*seg[j], a + rng.choice([-1, 1]) * rng.uniform(0.4, 0.9), ln * 0.55, w * 0.7, depth - 1)

    ns = p["n_side"]
    for i in range(ns):
        t = 0.18 + 0.78 * i / ns
        j = int(t * 10)
        x, y = pts[j]
        side = 1 if i % 2 else -1
        a0 = np.arctan2(-(pts[j + 1][1] - y), pts[j + 1][0] - x) if j < 10 else ang
        twig(x, y, a0 + side * rng.uniform(0.45, 0.95), S * rng.uniform(0.24, 0.40) * (1.1 - 0.5 * t),
             twigw, 1)
    for k in range(4):
        x, y = pts[10 - k]
        leaves.append(((x, y), ang + rng.uniform(-0.5, 0.5)))
    # fill (broad crowns): extra leaves on a disc so the cluster reads dense from far away
    for _ in range(int(p["fill"] * 60)):
        r = S * 0.30 * np.sqrt(rng.random())
        a = rng.uniform(0, 2 * np.pi)
        leaves.append(((S / 2 + r * np.cos(a), S * 0.48 + r * np.sin(a)), rng.uniform(0, 2 * np.pi)))
    rng.shuffle(leaves)
    for i, (b, a) in enumerate(leaves):
        depth = 0.72 + 0.38 * i / max(1, len(leaves) - 1)          # later = in front = brighter
        c = np.array(p["pal"][int(rng.integers(len(p["pal"])))]) * depth * rng.uniform(0.9, 1.1)
        c = c * (1 + 0.12 * np.sin(a))                              # up-facing leaves catch more light
        ln = S * rng.uniform(*p["L"])
        draw_leaf(d, b, a, ln, ln * p["wr"] * rng.uniform(0.85, 1.15), c, p["shape"], p["serr"], rng)


def tex_sprigs(out, kind, cell=256, ss=3, seed=0):
    rng = np.random.default_rng(seed)
    S = cell * ss
    big = Image.new("RGBA", (2 * S, 2 * S), (0, 0, 0, 0))
    for ci in range(4):
        lay = Image.new("RGBA", (S, S), (0, 0, 0, 0))
        paint_sprig(ImageDraw.Draw(lay), S, kind, rng)
        big.paste(lay, ((ci % 2) * S, (ci // 2) * S))
    img = big.resize((2 * cell, 2 * cell), LANCZOS)
    path = out / f"sprig_{kind}.png"
    save_cutout(img, path, noise=0.10, seed=seed)
    # UV cells (u0, v0, u1, v1), v from the bottom
    cells = [((ci % 2) * 0.5, 0.5 - (ci // 2) * 0.5, (ci % 2) * 0.5 + 0.5, 1.0 - (ci // 2) * 0.5) for ci in range(4)]
    return path, cells


def tex_palm_fronds(out, seed=41):
    """1024x1024: 4 frond columns (3 green, 1 dry). Rachis base at the bottom, tip at the top."""
    rng = np.random.default_rng(seed)
    ss, W, H = 2, 256, 1024
    big = Image.new("RGBA", (4 * W * ss, H * ss), (0, 0, 0, 0))
    d = ImageDraw.Draw(big)
    for col in range(4):
        dry = col == 3
        cx0 = (col + 0.5) * W * ss
        half = W * ss * 0.48
        for side in (-1, 1):
            n = 74
            for i in range(n):
                t = 0.11 + 0.87 * (i + rng.uniform(-0.3, 0.3)) / n
                if dry and rng.random() < 0.25:
                    continue
                y = H * ss * (1 - t)
                cx = cx0 + 6 * ss * np.sin(t * 3)
                lp = half * (0.25 + 0.75 * np.sin(np.pi * min(1, (t - 0.08) / 0.95)) ** 0.55) * rng.uniform(0.85, 1.05)
                if dry:
                    lp *= rng.uniform(0.6, 1.0)
                a0 = np.radians(rng.uniform(52, 64) - 20 * t)               # angle from rachis toward tip
                wb = rng.uniform(6.5, 9.0) * ss
                pts_l, pts_r = [], []
                for k in range(9):
                    u = k / 8
                    a = a0 + 0.35 * u * u                                    # leaflets curve back slightly
                    px = cx + side * np.sin(a) * lp * u
                    py = y - np.cos(a) * lp * u
                    ww = wb * (1 - u) ** 0.8 + 0.4
                    pts_l.append((px, py - ww / 2))
                    pts_r.append((px, py + ww / 2))
                if dry:
                    base = np.array([0.50, 0.38, 0.22]) * rng.uniform(0.8, 1.1)
                else:
                    base = np.array(rng.choice([[0.22, 0.36, 0.10], [0.27, 0.41, 0.12], [0.30, 0.44, 0.13],
                                                [0.24, 0.38, 0.12]])) * rng.uniform(0.85, 1.1)
                cc = tuple(int(255 * min(1, v)) for v in base) + (255,)
                d.polygon(pts_l + pts_r[::-1], fill=cc)
                if not dry and rng.random() < 0.3:                               # dry tips
                    k0 = int(rng.integers(5, 8))
                    tip = tuple(int(255 * v) for v in (0.55, 0.47, 0.24)) + (255,)
                    d.polygon(pts_l[k0:] + pts_r[k0:][::-1], fill=tip)
        rc = (0.58, 0.55, 0.30) if not dry else (0.55, 0.45, 0.30)
        ys = np.linspace(H * ss, 0, 40)
        for k in range(39):
            w = (9 - 7 * k / 39) * ss
            d.line([(cx0 + 6 * ss * np.sin((1 - ys[k] / H / ss) * 3), ys[k]),
                    (cx0 + 6 * ss * np.sin((1 - ys[k + 1] / H / ss) * 3), ys[k + 1])],
                   fill=tuple(int(255 * v) for v in rc) + (255,), width=int(w))
    img = big.resize((4 * W, H), LANCZOS)
    path = out / "palm_frond.png"
    save_cutout(img, path, noise=0.08, seed=seed)
    return path, [(c * 0.25, 0.0, c * 0.25 + 0.25, 1.0) for c in range(4)]


def tex_reed_blades(out, seed=51):
    """512x512: 8 blade columns (5 green, 3 drier). Base at the bottom, pointed tip at the top."""
    rng = np.random.default_rng(seed)
    ss, W, H = 2, 64, 512
    big = Image.new("RGBA", (8 * W * ss, H * ss), (0, 0, 0, 0))
    d = ImageDraw.Draw(big)
    for c in range(8):
        dry = c >= 5
        x0 = c * W * ss
        hw = W * ss * 0.44
        base = np.array([0.30, 0.42, 0.16]) if not dry else np.array([0.52, 0.50, 0.28])
        tipc = np.array([0.62, 0.55, 0.30])
        tipstart = rng.uniform(0.75, 0.92) if not dry else rng.uniform(0.3, 0.6)
        rows = H * ss
        for r in range(0, rows, 2):
            t = 1 - r / rows
            w = hw * (1.0 if t < 0.55 else max(0.0, 1 - (t - 0.55) / 0.45) ** 0.8)
            if w < 0.6:
                continue
            k = np.clip((t - tipstart) / 0.15, 0, 1)
            colr = base * (0.85 + 0.3 * t) * (1 - k) + tipc * k
            cx = x0 + W * ss / 2
            d.rectangle([cx - w, r, cx + w, r + 1], fill=tuple(int(255 * min(1, v)) for v in colr) + (255,))
        for v in np.linspace(-0.7, 0.7, 6):
            cx = x0 + W * ss / 2 + v * hw
            d.line([(cx, rows), (x0 + W * ss / 2 + v * 2, rows * 0.03)], fill=(255, 255, 220, 28), width=1)
        d.line([(x0 + W * ss / 2, rows), (x0 + W * ss / 2, rows * 0.05)], fill=(235, 240, 190, 70), width=2)
    img = big.resize((8 * W, H), LANCZOS)
    path = out / "reed_blades.png"
    save_cutout(img, path)
    return path, [(c / 8, 0.0, (c + 1) / 8, 1.0) for c in range(8)]


def tex_fern(src, out):
    """Poly Haven fern_02 atlas (5 fronds), cleaned of stray specks, 512 px."""
    c = Image.open(Path(src) / "ph/fern_02_diff_1k.jpg").convert("RGB").resize((512, 512), LANCZOS)
    a = np.asarray(Image.open(Path(src) / "ph/fern_02_alpha_1k.jpg").convert("L").resize((512, 512), LANCZOS)) > 110
    rects = [(88, 243, 111, 974), (318, 469, 22, 785), (517, 677, 25, 515), (522, 647, 520, 991), (728, 860, 109, 920)]
    keep = np.zeros_like(a)
    for x0, x1, y0, y1 in rects:
        seeds = np.zeros_like(a)
        xs, ys = slice(x0 // 2, x1 // 2), slice(y0 // 2, y1 // 2)
        region = np.zeros_like(a)
        region[ys, xs] = True
        cx = (x0 + x1) // 4
        seeds[ys, cx - 12:cx + 12] = True
        keep |= keep_connected(a & region, seeds & region)
    img = Image.fromarray(np.dstack([np.asarray(c), (keep * 255).astype(np.uint8)]), "RGBA")
    path = out / "fern_02_atlas.png"
    save_cutout(img, path)
    return path, [(x0 / 1024, 1 - y1 / 1024, x1 / 1024, 1 - y0 / 1024) for x0, x1, y0, y1 in rects]


def catmull(pts, n=8):
    """Closed-free Catmull-Rom through pts (list of (x, y)); returns a dense polyline."""
    P = [pts[0]] + list(pts) + [pts[-1]]
    out = []
    for i in range(1, len(P) - 2):
        p0, p1, p2, p3 = (np.array(P[i + k - 1], float) for k in range(4))
        for t in np.linspace(0, 1, n, endpoint=False):
            out.append(0.5 * ((2 * p1) + (-p0 + p2) * t + (2 * p0 - 5 * p1 + 4 * p2 - p3) * t * t
                              + (-p0 + 3 * p1 - 3 * p2 + p3) * t ** 3))
    out.append(np.array(P[-2], float))
    return out


EAR_J = 0.78        # petiole junction (notch) height in the cell, from the top


def tex_elephant_ear(out, seed=61):
    """1024x512: 8 heart-shaped (sagittate) elephant-ear leaves, tip up, lobes + notch at the bottom."""
    rng = np.random.default_rng(seed)
    ss, cell = 3, 256
    S = cell * ss
    big = Image.new("RGBA", (4 * S, 2 * S), (0, 0, 0, 0))
    half = [(0, -0.74), (0.17, -0.56), (0.31, -0.32), (0.39, -0.10), (0.37, 0.06), (0.26, 0.16), (0.11, 0.17),
            (0.0, 0.04)]
    for ci in range(8):
        ox, oy = (ci % 4) * S, (ci // 4) * S
        jx, jy = ox + S / 2, oy + EAR_J * S
        wf = rng.uniform(0.88, 1.0)
        right = catmull([(x * wf * (1 + rng.uniform(-0.03, 0.03)), y) for x, y in half])
        left = [(-x, y) for x, y in right[::-1]]
        poly = [(jx + x * S, jy + y * S) for x, y in right + left]
        lay = Image.new("RGBA", big.size, (0, 0, 0, 0))
        d = ImageDraw.Draw(lay)
        g = np.array(rng.choice([[0.20, 0.36, 0.12], [0.23, 0.40, 0.13], [0.18, 0.33, 0.11], [0.26, 0.42, 0.16]]))
        g = g * rng.uniform(0.95, 1.12)
        d.polygon(poly, fill=tuple(int(255 * v) for v in g) + (255,))
        # lighter inner zone
        inner = [(jx + x * S * 0.72, jy + (y * 0.8 - 0.06) * S) for x, y in right + left]
        d.polygon(inner, fill=tuple(int(255 * min(1, v * 1.12)) for v in g) + (255,))
        vc = tuple(int(255 * min(1, v)) for v in g * 1.55 + 0.06) + (255,)
        tip = (jx, jy - 0.72 * S)
        d.line([(jx, jy), tip], fill=vc, width=int(3.2 * ss))
        for k in range(1, 8):                            # lateral veins, curving toward the tip
            t = k / 8.5
            bx, by = jx, jy - 0.72 * S * t
            for sd in (-1, 1):
                ex = bx + sd * S * 0.36 * np.sin(np.pi * (0.25 + 0.6 * t)) * 0.95
                ey = by - S * (0.10 + 0.05 * t)
                mx, my = bx + sd * S * 0.16, by - S * 0.02
                d.line([(bx, by), (mx, my), (ex, ey)], fill=vc, width=int(1.6 * ss), joint="curve")
        for sd in (-1, 1):                               # basal veins into the lobes
            d.line([(jx, jy), (jx + sd * S * 0.14, jy + S * 0.06), (jx + sd * S * 0.24, jy + S * 0.13)],
                   fill=vc, width=int(2.2 * ss), joint="curve")
        mask = Image.new("L", big.size, 0)
        ImageDraw.Draw(mask).polygon(poly, fill=255)
        lay.putalpha(mask)                               # veins clipped to the blade
        big.alpha_composite(lay)
    img = big.resize((4 * cell, 2 * cell), LANCZOS)
    path = out / "elephant_ear.png"
    save_cutout(img, path, noise=0.09, seed=seed)
    return path, [((ci % 4) / 4, 0.5 - (ci // 4) * 0.5, (ci % 4 + 1) / 4, 1.0 - (ci // 4) * 0.5) for ci in range(8)]


# ====================================================================================== geometry


def nrm(v):
    v = Vector(v)
    return v.normalized() if v.length > 1e-9 else Vector((0, 0, 1))


def rvec(rng):
    return Vector(rng.normal(size=3))


class Part:
    """Accumulates per-vertex position / uv / normal / colour and faces for one material."""

    def __init__(self, name, mat):
        self.name, self.mat = name, mat
        self.P, self.UV, self.N, self.C, self.F = [], [], [], [], []

    def add(self, P, UV, N, C, F):
        o = len(self.P)
        self.P += [tuple(p) for p in P]
        self.UV += [tuple(u) for u in UV]
        self.N += [tuple(nrm(n)) for n in N]
        self.C += [tuple(c) for c in C]
        self.F += [tuple(i + o for i in f) for f in F]

    def tris(self):
        return sum(len(f) - 2 for f in self.F)


def grid_normals(G, wrap):
    """Normals of a vertex grid G[i][j] (i along, j across), by central differences."""
    ni, nj = len(G), len(G[0])
    N = []
    for i in range(ni):
        row = []
        for j in range(nj):
            if wrap:
                ja, jb = (j - 1) % (nj - 1), (j + 1) % (nj - 1)
            else:
                ja, jb = max(0, j - 1), min(nj - 1, j + 1)
            ia, ib = max(0, i - 1), min(ni - 1, i + 1)
            du = G[i][jb] - G[i][ja]
            dv = G[ib][j] - G[ia][j]
            row.append(du.cross(dv))
        N.append(row)
    return N


def tube(part, pts, radii, sides, urep=1.0, vscale=1.0, v0=0.0, prof=None, color=None, vmap=None):
    """Swept tube along pts with parallel-transport frames. prof(theta)->radius factor (ribs).
    color(i, z)->rgba per ring. vmap(s)->v overrides arc-length v."""
    n = len(pts)
    T = [nrm(pts[min(i + 1, n - 1)] - pts[max(i - 1, 0)]) for i in range(n)]
    a = T[0].cross(Vector((0.37, 0.61, 0.7))) if abs(T[0].z) > 0.9 else T[0].cross(Z)
    Nf = [nrm(a)]
    for i in range(1, n):
        b = Nf[-1] - T[i] * Nf[-1].dot(T[i])
        Nf.append(nrm(b))
    G, UV, C = [], [], []
    s = 0.0
    for i in range(n):
        if i:
            s += (pts[i] - pts[i - 1]).length
        B = T[i].cross(Nf[i])
        ring = []
        for j in range(sides + 1):
            th = 2 * np.pi * j / sides
            r = radii[i] * (prof(th) if prof else 1.0)
            ring.append(pts[i] + (Nf[i] * math.cos(th) + B * math.sin(th)) * r)
            v = vmap(s) if vmap else v0 + s / vscale
            UV.append((j / sides * urep, v))
            C.append(color(i, pts[i].z) if color else (1, 1, 1, 1))
        G.append(ring)
    Ng = grid_normals(G, True)
    Nout = []
    for i in range(n):
        for j in range(sides + 1):
            v = Ng[i][j]
            radial = G[i][j] - pts[i]
            if v.length < 1e-9 or radii[i] < 1e-4:
                v = radial if radial.length > 1e-9 else T[i]
            if v.dot(radial) < 0:
                v = -v
            Nout.append(v)
    F = []
    w = sides + 1
    for i in range(n - 1):
        for j in range(sides):
            F.append((i * w + j, i * w + j + 1, (i + 1) * w + j + 1, (i + 1) * w + j))
    part.add([p for r in G for p in r], UV, Nout, C, F)


def strip(part, pts, sides_v, ups, widths, uvrect, cross, color=None):
    """Bent ribbon (frond, blade, big leaf). cross = [(u, lateral, drop)] with u in 0..1 across,
    lateral/drop as fractions of the local width; ups = per-point 'up' (drop is along -up)."""
    u0, v0, u1, v1 = uvrect
    n = len(pts)
    L = [0.0]
    for i in range(1, n):
        L.append(L[-1] + (pts[i] - pts[i - 1]).length)
    G, UV, C = [], [], []
    for i in range(n):
        row = []
        for (u, lat, drop) in cross:
            row.append(pts[i] + sides_v[i] * lat * widths[i] - ups[i] * drop * widths[i])
            UV.append((u0 + (u1 - u0) * u, v0 + (v1 - v0) * L[i] / L[-1]))
            C.append(color(i / (n - 1)) if color else (1, 1, 1, 1))
        G.append(row)
    Ng = grid_normals(G, False)
    Nout = []
    for i in range(n):
        for j in range(len(cross)):
            v = Ng[i][j]
            if v.dot(ups[i]) < 0:
                v = -v
            Nout.append(v if v.length > 1e-9 else ups[i])
    m = len(cross)
    F = [(i * m + j, i * m + j + 1, (i + 1) * m + j + 1, (i + 1) * m + j) for i in range(n - 1) for j in range(m - 1)]
    part.add([p for r in G for p in r], UV, Nout, C, F)


def grow(start, d, length, nseg, rng, wander=0.12, up=0.0, grav=0.0, env=None):
    """Branch centre line. up/grav bend toward/away from +Z (grav grows toward the tip)."""
    pts = [Vector(start)]
    d = nrm(d)
    step = length / nseg
    for i in range(nseg):
        d = nrm(d + rvec(rng) * wander + Z * up - Z * grav * (i + 1) / nseg)
        p = pts[-1] + d * step
        if env is not None and env(p) > 1.0 and i >= 1:
            break
        pts.append(p)
    return pts


def at(pts, t):
    """Point and tangent at fraction t of a polyline."""
    f = t * (len(pts) - 1)
    i = min(int(f), len(pts) - 2)
    k = f - i
    return pts[i].lerp(pts[i + 1], k), nrm(pts[i + 1] - pts[i])


def perp(d, rng, ang):
    """Direction at angle ang (rad) from d, random roll."""
    a = nrm(d.cross(rvec(rng)))
    return nrm(d * math.cos(ang) + a * math.sin(ang))


def cards(part, anchors, centre, radii, size, cells, rng, up_fn, bias=0.72, ao=(0.38, 1.0), zr=None, tint=0.07):
    """Leaf cards anchored at branch points; front faces and normals point out of the crown ellipsoid."""
    C, R = Vector(centre), Vector(radii)

    def grad(p):
        q = p - C
        return nrm(Vector((q.x / R.x ** 2, q.y / R.y ** 2, q.z / R.z ** 2)))

    def edist(p):
        q = p - C
        return math.sqrt((q.x / R.x) ** 2 + (q.y / R.y) ** 2 + (q.z / R.z) ** 2)

    z0, z1 = zr if zr else (C.z - R.z, C.z + R.z)
    for (p, bd) in anchors:
        out = grad(p)
        s = rng.uniform(*size)
        up = nrm(up_fn(bd, out, rng))
        want = nrm(out + rvec(rng) * 0.45)
        n = want - up * want.dot(up)
        if n.length < 1e-3:
            n = up.cross(rvec(rng))
        n = nrm(n)
        side = up.cross(n)
        w, h = s * rng.uniform(0.9, 1.1), s
        base = p - up * 0.06 * s
        P = [base - side * w / 2, base + side * w / 2, base + side * w / 2 + up * h, base - side * w / 2 + up * h]
        u0, v0, u1, v1 = cells[int(rng.integers(len(cells)))]
        if rng.random() < 0.5:
            u0, u1 = u1, u0
        UV = [(u0, v0), (u1, v0), (u1, v1), (u0, v1)]
        N, Col = [], []
        tv = np.array([1 + rng.uniform(-tint, tint), 1 + rng.uniform(-tint, tint) * 0.6, 1 + rng.uniform(-tint, tint)])
        for q in P:
            N.append(nrm(grad(q) * bias + n * (1 - bias)))
            e = min(1.0, edist(q))
            k = ao[0] + (ao[1] - ao[0]) * (e ** 1.6)
            k *= 0.78 + 0.22 * min(1.0, max(0.0, (q.z - z0) / max(1e-3, z1 - z0)))
            Col.append((k * tv[0], k * tv[1], k * tv[2], 1.0))
        part.add(P, UV, N, Col, [(0, 1, 2, 3)])


def icosphere(part, centre, r, color, sub=1):
    bm = bmesh.new()
    bmesh.ops.create_icosphere(bm, subdivisions=sub, radius=r)
    P, UV, N, F = [], [], [], []
    for v in bm.verts:
        P.append(Vector(centre) + v.co)
        N.append(v.co)
        UV.append((0.5 + math.atan2(v.co.y, v.co.x) / (2 * np.pi), 0.5 + v.co.z / r * 0.5))
    for f in bm.faces:
        F.append(tuple(v.index for v in f.verts))
    bm.free()
    part.add(P, UV, N, [color] * len(P), F)


# ====================================================================================== blender


def make_material(name, color_tex, normal_tex=None, alpha=False, rough=0.85):
    mat = bpy.data.materials.new(name)
    mat.use_nodes = True
    nt = mat.node_tree
    b = nt.nodes["Principled BSDF"]
    im = nt.nodes.new("ShaderNodeTexImage")
    im.image = bpy.data.images.load(str(color_tex))
    nt.links.new(im.outputs["Color"], b.inputs["Base Color"])
    if alpha:
        nt.links.new(im.outputs["Alpha"], b.inputs["Alpha"])
        mat.use_backface_culling = False
    else:
        mat.use_backface_culling = True
    if normal_tex:
        ni = nt.nodes.new("ShaderNodeTexImage")
        ni.image = bpy.data.images.load(str(normal_tex))
        ni.image.colorspace_settings.name = "Non-Color"
        nm = nt.nodes.new("ShaderNodeNormalMap")
        nt.links.new(ni.outputs["Color"], nm.inputs["Color"])
        nt.links.new(nm.outputs["Normal"], b.inputs["Normal"])
    b.inputs["Roughness"].default_value = rough
    b.inputs["Metallic"].default_value = 0.0
    return mat


def build_object(part):
    me = bpy.data.meshes.new(part.name)
    me.from_pydata(part.P, [], part.F)
    me.update()
    uv = me.uv_layers.new(name="UVMap")
    vi = np.zeros(len(me.loops), np.int64)
    me.loops.foreach_get("vertex_index", vi)
    UV = np.array(part.UV, np.float32)
    uv.data.foreach_set("uv", UV[vi].ravel())
    Cv = np.array(part.C, np.float32)
    if not np.allclose(Cv, 1.0):
        ca = me.color_attributes.new("Col", "BYTE_COLOR", "POINT")
        ca.data.foreach_set("color", Cv.ravel())
        me.color_attributes.active_color = ca
    me.shade_smooth()
    me.normals_split_custom_set_from_vertices([tuple(n) for n in part.N])
    me.materials.append(part.mat)
    ob = bpy.data.objects.new(part.name, me)
    bpy.context.scene.collection.objects.link(ob)
    return ob


def patch_glb(path, cutoffs, sources):
    """Leaf materials -> alphaMode MASK (+cutoff, doubleSided); opaque ones -> OPAQUE; credit sources."""
    b = Path(path).read_bytes()
    jl = struct.unpack("<I", b[12:16])[0]
    j = json.loads(b[20:20 + jl])
    rest = b[20 + jl:]
    for m in j.get("materials", []):
        if m["name"] in cutoffs:
            m["alphaMode"] = "MASK"
            m["alphaCutoff"] = cutoffs[m["name"]]
            m["doubleSided"] = True
        else:
            m.pop("alphaMode", None)
            m.pop("alphaCutoff", None)
    j["asset"]["copyright"] = "CC0 / original. Sources: " + sources
    js = json.dumps(j, separators=(",", ":")).encode()
    js += b" " * ((4 - len(js) % 4) % 4)
    out = b[:12] + struct.pack("<I", len(js)) + b"JSON" + js + rest
    out = out[:8] + struct.pack("<I", len(out)) + out[12:]
    Path(path).write_bytes(out)


def glb_stats(path):
    b = Path(path).read_bytes()
    jl = struct.unpack("<I", b[12:16])[0]
    j = json.loads(b[20:20 + jl])
    tris, ymin, ymax = 0, 1e9, -1e9
    for m in j["meshes"]:
        for p in m["primitives"]:
            tris += j["accessors"][p["indices"]]["count"] // 3
            acc = j["accessors"][p["attributes"]["POSITION"]]
            ymin, ymax = min(ymin, acc["min"][1]), max(ymax, acc["max"][1])
    return tris, len(b), ymax, ymin


# ====================================================================================== models


def m_broadleaf(T, src, rng):
    bc, bn = tex_bark_acg(src, "Bark001", 512, T, tint=(1.0, 0.97, 0.92), sat=0.8, nsize=256)
    lp, cells = tex_sprigs(T, "broad", seed=3)
    bark = Part("trunk", make_material("bark", bc, bn))
    leaf = Part("leaves", make_material("leaf", lp, alpha=True, rough=0.8))
    C, R = Vector((0, 0, 6.9)), Vector((4.4, 4.4, 3.5))

    def env(p):
        q = p - C
        return math.sqrt((q.x / R.x) ** 2 + (q.y / R.y) ** 2 + (q.z / R.z) ** 2) / 0.93

    trunk = grow((0, 0, -0.15), (0.05, 0.02, 1), 8.6, 14, rng, wander=0.035)
    rad = [0.29 * (1 - 0.8 * i / 14) + 0.20 * math.exp(-max(0, p.z) / 0.35) for i, p in enumerate(trunk)]
    tube(bark, trunk, rad, 10, urep=2, vscale=1.6)
    anchors = []
    limbs = []
    for k in range(8):
        t = 0.40 + 0.40 * k / 7 + rng.uniform(-0.03, 0.03)
        p, dt = at(trunk, t)
        az = k * 2.4 + rng.uniform(-0.3, 0.3)
        el = math.radians(rng.uniform(38, 58) - 18 * (t - 0.36))
        d = Vector((math.cos(az) * math.sin(el), math.sin(az) * math.sin(el), math.cos(el)))
        pts = grow(p, d, rng.uniform(4.2, 5.4) * (1.1 - 0.4 * (t - 0.36)), 8, rng, wander=0.10, up=0.04, env=env)
        r0 = 0.15 * (1.05 - 0.6 * (t - 0.36))
        tube(bark, pts, [r0 * (1 - 0.85 * i / 8) for i in range(len(pts))], 7, urep=1, vscale=1.0)
        limbs.append((pts, r0))
    for pts, r0 in limbs + [(trunk[7:], 0.12)]:
        for k in range(6):
            t = 0.25 + 0.72 * k / 5 + rng.uniform(-0.04, 0.04)
            p, dt = at(pts, t)
            d = perp(dt, rng, math.radians(rng.uniform(35, 65)))
            d = nrm(d + Z * 0.15)
            sp = grow(p, d, rng.uniform(1.6, 2.6) * (1.1 - 0.45 * t), 5, rng, wander=0.18, grav=0.05, env=env)
            if len(sp) < 2:
                continue
            r1 = max(0.025, r0 * 0.45 * (1 - 0.5 * t))
            tube(bark, sp, [r1 * (1 - 0.8 * i / 5) for i in range(len(sp))], 4, vscale=0.6)
            for kk in range(3):
                tt = 0.35 + 0.6 * kk / 2
                q, dq = at(sp, tt)
                tw = grow(q, perp(dq, rng, math.radians(rng.uniform(30, 60))), rng.uniform(0.6, 1.0), 3, rng,
                          wander=0.2, env=env)
                if len(tw) >= 2:          # twigs are hidden in the foliage: leaves only, no geometry
                    for t3 in (0.5, 1.0):
                        anchors.append(at(tw, t3))
            for t2 in np.linspace(0.3, 1.0, 5):
                anchors.append(at(sp, t2))
        for t in np.linspace(0.55, 1.0, 4):
            anchors.append(at(pts, t))
    # fill the shell where branches don't reach: extra anchors projected from inner branch points
    extra = []
    for p, d in anchors:
        q = C + (p - C) * rng.uniform(1.0, 1.12)
        if env(q) < 1.12:
            extra.append((q, d))
    anchors += extra
    anchors = [a for a in anchors if a[0].z > 3.6]

    cards(leaf, anchors, C, R, (1.5, 2.1), cells, rng,
          lambda bd, out, r: bd * 0.5 + out * 0.5 + Z * 0.7 + rvec(r) * 0.35, ao=(0.55, 1.1))
    return [bark, leaf], {"leaf": 0.42}, "ambientCG Bark001 (CC0) bark; leaf sprigs painted procedurally (original)"


def m_birch(T, src, rng):
    bc, bn = tex_birch(T)
    lp, cells = tex_sprigs(T, "birch", seed=5)
    bark = Part("trunk", make_material("bark", bc, bn, rough=0.75))
    leaf = Part("leaves", make_material("leaf", lp, alpha=True, rough=0.75))
    C, R = Vector((0, 0, 5.4)), Vector((2.5, 2.5, 3.2))
    trunk = grow((0, 0, -0.12), (0.02, -0.03, 1), 8.5, 18, rng, wander=0.03)

    def tcol(i, z):
        k = 0.4 + 0.6 * min(1.0, max(0.0, (z - 0.05) / 1.0)) ** 0.7
        return (k, k * 0.98, k * 0.96, 1)

    tube(bark, trunk, [0.15 * (1 - 0.9 * i / 18) + 0.05 * math.exp(-max(0, p.z) / 0.3) + 0.008
                       for i, p in enumerate(trunk)], 9, urep=2, vscale=1.1, color=tcol)
    anchors = []
    grey = lambda i, z: (0.42, 0.40, 0.38, 1)  # noqa: E731  (thin birch twigs are dark grey-brown)
    n = 30
    for k in range(n):
        t = 0.28 + 0.68 * k / (n - 1) + rng.uniform(-0.01, 0.01)
        p, dt = at(trunk, t)
        az = k * 2.4 + rng.uniform(-0.25, 0.25)
        el = math.radians(rng.uniform(45, 70) - 20 * (t - 0.28))
        d = Vector((math.cos(az) * math.sin(el), math.sin(az) * math.sin(el), math.cos(el)))
        ln = 0.5 + 2.2 * math.sin(math.pi * min(1, (t - 0.22) / 0.84)) ** 0.9
        pts = grow(p, d, ln, 6, rng, wander=0.12, up=0.05, grav=0.22)
        r0 = 0.035 * (1.2 - 0.7 * t) + 0.008
        tube(bark, pts, [r0 * (1 - 0.8 * i / 6) for i in range(len(pts))], 4, vscale=0.5,
             color=(lambda i, z: (0.75, 0.73, 0.70, 1)) if r0 > 0.03 else grey)
        for kk in range(3):
            tt = 0.35 + 0.55 * kk / 2
            q, dq = at(pts, tt)
            tw = grow(q, perp(dq, rng, math.radians(rng.uniform(30, 55))), ln * rng.uniform(0.3, 0.45), 3, rng,
                      wander=0.15, grav=0.35)
            tube(bark, tw, [r0 * 0.4 * (1 - 0.9 * i / 3) for i in range(len(tw))], 3, vscale=0.3, color=grey)
            anchors.append(at(tw, 0.6))
            anchors.append(at(tw, 1.0))
        for t2 in (0.55, 0.8, 1.0):
            anchors.append(at(pts, t2))
    anchors.append(at(trunk, 0.97))
    anchors.append(at(trunk, 0.9))
    cards(leaf, anchors, C, R, (0.95, 1.35), cells, rng,
          lambda bd, out, r: bd * 0.4 + out * 0.5 - Z * 0.75 + rvec(r) * 0.3, bias=0.6, ao=(0.5, 1.0), tint=0.09)
    return [bark, leaf], {"leaf": 0.42}, "birch bark painted procedurally (original); leaf sprigs painted procedurally"


def m_palm(T, src, rng):
    bc, bn = tex_palm_trunk(src, T)
    fp, cols = tex_palm_fronds(T)
    bark = Part("trunk", make_material("bark", bc, bn, rough=0.9))
    leaf = Part("fronds", make_material("leaf", fp, alpha=True, rough=0.7))
    pts = [Vector((0, 0, -0.15))]
    d = nrm(Vector((0.30, 0.05, 1)))
    for i in range(20):
        d = nrm(d + Vector((0.035 if i < 6 else -0.012, 0.0, 0.0)) + Z * 0.012)
        pts.append(pts[-1] + d * 0.43)
    rad = [0.18 - 0.035 * i / 20 + 0.15 * math.exp(-max(0, p.z + 0.1) / 0.35) for i, p in enumerate(pts)]
    tube(bark, pts, rad, 10, urep=2, vscale=0.9)
    top, dtop = pts[-1], nrm(pts[-1] - pts[-2])
    # crownshaft / boot cluster
    cs = [top + dtop * (k * 0.12) for k in range(7)]
    tube(bark, cs, [0.17, 0.22, 0.25, 0.24, 0.2, 0.13, 0.04], 10, urep=2, vscale=0.9,
         color=lambda i, z: (0.62, 0.56, 0.40, 1))
    crown = top + dtop * 0.45
    for k in range(6):
        a = k * 1.1
        icosphere(bark, crown + Vector((math.cos(a) * 0.23, math.sin(a) * 0.23, -0.12 - 0.08 * (k % 2))), 0.11,
                  (0.55, 0.50, 0.30, 1), sub=1)
    nf = 28
    for k in range(nf):
        dead = k >= nf - 3
        az = k * 2.39996 + rng.uniform(-0.15, 0.15)
        el = math.radians(rng.uniform(20, 105) if not dead else rng.uniform(140, 160))
        d = Vector((math.cos(az) * math.sin(el), math.sin(az) * math.sin(el), math.cos(el)))
        ln = rng.uniform(4.3, 5.3) if not dead else rng.uniform(2.8, 3.4)
        steps = 10
        fpts = [crown + d * 0.15]
        dd = d
        for i in range(steps):
            g = 0.06 + 0.16 * (i / steps) ** 1.3
            dd = nrm(dd - Z * g * (0.4 if dead else 1.0))
            fpts.append(fpts[-1] + dd * ln / steps)
        sides_v, ups, widths = [], [], []
        for i, p in enumerate(fpts):
            tg = nrm(fpts[min(i + 1, steps)] - fpts[max(i - 1, 0)])
            sv = nrm(tg.cross(Z)) if abs(tg.z) < 0.98 else nrm(tg.cross(Vector((1, 0, 0))))
            twist = 0.25 * (i / steps) * (1 if k % 2 else -1)
            up = nrm(sv.cross(tg))
            sv = nrm(sv * math.cos(twist) + up * math.sin(twist))
            up = nrm(sv.cross(tg))
            sides_v.append(sv)
            ups.append(up)
            widths.append(2.1 if not dead else 1.4)
        fold = math.radians(rng.uniform(28, 42))
        cross = [(u, (u - 0.5) * math.cos(fold), abs(u - 0.5) * math.sin(fold) + 0.25 * (u - 0.5) ** 2)
                 for u in (0.0, 0.25, 0.5, 0.75, 1.0)]
        col = 3 if dead else int(rng.integers(3))
        strip(leaf, fpts, sides_v, ups, widths, cols[col], cross,
              color=lambda t: (0.75 + 0.25 * t, 0.75 + 0.25 * t, 0.75 + 0.25 * t, 1))
    return [bark, leaf], {"leaf": 0.35}, "ambientCG Bark012 (CC0) trunk with painted rings; fronds painted procedurally"


def m_fern(T, src, rng, temple=False):
    fp, rects = tex_fern(src, T)
    leaf = Part("fronds", make_material("leaf", fp, alpha=True, rough=0.75))
    nf = 34
    for k in range(nf):
        az = k * 2.39996 + rng.uniform(-0.2, 0.2)
        el = math.radians(10 + 62 * (k / nf) ** 0.8 + rng.uniform(-6, 6))
        d = Vector((math.cos(az) * math.sin(el), math.sin(az) * math.sin(el), math.cos(el)))
        r = rects[int(rng.integers(len(rects)))]
        ln = rng.uniform(0.6, 0.9)
        aspect = (r[2] - r[0]) / (r[3] - r[1])
        steps = 7
        base = Vector((rng.uniform(-0.04, 0.04), rng.uniform(-0.04, 0.04), 0.0))
        fpts = [base]
        dd = d
        for i in range(steps):
            dd = nrm(dd - Z * (0.05 + 0.2 * (i / steps) ** 1.4) * (0.5 + el))
            fpts.append(fpts[-1] + dd * ln / steps)
        sv, ups = [], []
        for i in range(steps + 1):
            tg = nrm(fpts[min(i + 1, steps)] - fpts[max(i - 1, 0)])
            s = nrm(tg.cross(Z)) if abs(tg.z) < 0.98 else nrm(tg.cross(Vector((1, 0, 0))))
            sv.append(s)
            ups.append(nrm(s.cross(tg)))
        fold = math.radians(rng.uniform(10, 25))
        cross = [(u, (u - 0.5) * math.cos(fold), abs(u - 0.5) * math.sin(fold)) for u in (0.0, 0.5, 1.0)]
        strip(leaf, fpts, sv, ups, [ln * aspect] * (steps + 1), r, cross,
              color=lambda t: (0.85 + 0.15 * t, 0.85 + 0.15 * t, 0.85 + 0.15 * t, 1))
    return [leaf], {"leaf": 0.4}, "Poly Haven fern_02 (CC0) frond textures on procedural arching fronds"


def m_temple_fern(T, src, rng):
    lp, rects = tex_elephant_ear(T)
    sc, sn = tex_stem(T, "temple_stem", green=(0.30, 0.42, 0.16))
    stem = Part("stems", make_material("stem", sc, sn, rough=0.6))
    leaf = Part("leaves", make_material("leaf", lp, alpha=True, rough=0.5))
    nl = 12
    for k in range(nl):
        az = k * 2.39996 + rng.uniform(-0.2, 0.2)
        el = math.radians(6 + 40 * (k / nl) + rng.uniform(-4, 4))
        d = Vector((math.cos(az) * math.sin(el), math.sin(az) * math.sin(el), math.cos(el)))
        plen = rng.uniform(1.15, 1.4) * (1.12 - 0.4 * k / nl)
        base = Vector((rng.uniform(-0.07, 0.07), rng.uniform(-0.07, 0.07), -0.03))
        pp = grow(base, d, plen, 6, rng, wander=0.03, grav=0.08 + 0.2 * k / nl)
        tube(stem, pp, [0.024 - 0.011 * i / 6 for i in range(len(pp))], 5, vscale=0.3)
        tip, tdir = pp[-1], nrm(pp[-1] - pp[-2])
        horiz = nrm(Vector((tdir.x, tdir.y, 0)))
        r = rects[int(rng.integers(len(rects)))]
        L = rng.uniform(0.62, 0.85) * (1.05 - 0.2 * k / nl)
        steps = 7
        dd = nrm(horiz * 0.8 + Z * rng.uniform(0.35, 0.75))
        lpts = [tip - dd * L * (1 - EAR_J)]
        for i in range(steps):
            if i >= 2:
                dd = nrm(dd - Z * (0.20 + 0.12 * i / steps))
            lpts.append(lpts[-1] + dd * L / steps)
        sv, ups = [], []
        roll = rng.uniform(-0.3, 0.3)
        for i in range(steps + 1):
            tg = nrm(lpts[min(i + 1, steps)] - lpts[max(i - 1, 0)])
            s_ = nrm(tg.cross(Z))
            up = nrm(s_.cross(tg))
            s_ = nrm(s_ * math.cos(roll) + up * math.sin(roll))
            sv.append(s_)
            ups.append(nrm(s_.cross(tg)))
        cross = [(u, (u - 0.5) * 0.96, 0.16 * abs(2 * (u - 0.5)) ** 1.8 - 0.04 * abs(2 * (u - 0.5)))
                 for u in (0.0, 0.2, 0.5, 0.8, 1.0)]
        strip(leaf, lpts, sv, ups, [L] * (steps + 1), r, cross,
              color=lambda t: (0.85 + 0.15 * t, 0.85 + 0.15 * t, 0.85 + 0.15 * t, 1))
    return [stem, leaf], {"leaf": 0.45}, "elephant-ear leaves and stems painted procedurally (original)"


def m_reeds(T, src, rng):
    bp, cols = tex_reed_blades(T)
    sc, sn = tex_stem(T, "reed_stem", green=(0.36, 0.42, 0.20), head=(0.27, 0.17, 0.10))
    stem = Part("stems", make_material("stem", sc, sn, rough=0.8))
    leaf = Part("blades", make_material("leaf", bp, alpha=True, rough=0.65))
    for k in range(120):
        rr = 0.22 * math.sqrt(rng.random())
        a = rng.uniform(0, 2 * np.pi)
        base = Vector((rr * math.cos(a), rr * math.sin(a), -0.02))
        outw = nrm(Vector((math.cos(a), math.sin(a), 0)) + rvec(rng) * 0.5)
        outw.z = 0
        el = math.radians(rng.uniform(2, 24) + 25 * rr)
        d = nrm(Z * math.cos(el) + nrm(outw) * math.sin(el))
        ln = rng.uniform(0.75, 1.5)
        steps = 6
        pts = [base]
        dd = d
        bend = rng.uniform(0.0, 0.12) * ln
        for i in range(steps):
            dd = nrm(dd + nrm(outw) * bend * (i / steps) - Z * 0.02 * i / steps)
            pts.append(pts[-1] + dd * ln / steps)
        sv, ups = [], []
        tw0 = rng.uniform(0, np.pi)
        for i in range(steps + 1):
            tg = nrm(pts[min(i + 1, steps)] - pts[max(i - 1, 0)])
            s0 = nrm(tg.cross(Vector((math.cos(tw0), math.sin(tw0), 0))))
            ang = 0.9 * i / steps
            u0 = nrm(s0.cross(tg))
            s = nrm(s0 * math.cos(ang) + u0 * math.sin(ang))
            sv.append(s)
            ups.append(nrm(s.cross(tg)))
        w = rng.uniform(0.028, 0.042)
        cross = [(0.0, -0.5, -0.12), (0.5, 0.0, 0.0), (1.0, 0.5, -0.12)]
        col = cols[int(rng.integers(5) if rng.random() < 0.8 else rng.integers(5, 8))]
        strip(leaf, pts, sv, ups, [w] * (steps + 1), col, cross,
              color=lambda t: (0.65 + 0.35 * t, 0.65 + 0.35 * t, 0.65 + 0.35 * t, 1))
    for k in range(7):
        a = k * 2.39996
        rr = rng.uniform(0.02, 0.16)
        base = Vector((rr * math.cos(a), rr * math.sin(a), -0.02))
        H = rng.uniform(1.25, 1.55)
        d = nrm(Z + Vector((math.cos(a), math.sin(a), 0)) * rng.uniform(0.03, 0.12))
        pts = grow(base, d, H, 8, rng, wander=0.02)
        head0 = H - 0.33
        tube(stem, pts, [0.0065 - 0.003 * i / 8 for i in range(len(pts))], 4, vmap=lambda s: 0.45 * s / H)
        hp, hd = at(pts, head0 / H)
        hl = rng.uniform(0.16, 0.22)
        hpts = [hp + hd * (hl * i / 6) for i in range(7)]
        hr = rng.uniform(0.019, 0.024)
        tube(stem, hpts, [hr * f for f in (0.55, 0.92, 1, 1, 1, 0.92, 0.5)], 8, vmap=lambda s: 0.52 + 0.46 * s / hl)
    return [stem, leaf], {"leaf": 0.45}, "reed blades and cattail stems/heads painted procedurally (original)"


def m_shrub(T, src, rng):
    bc, bn = tex_bark_acg(src, "Bark002", 256, T, tint=(0.95, 0.92, 0.88), sat=0.6, nsize=128)
    lp, cells = tex_sprigs(T, "shrub", seed=9)
    bark = Part("stems", make_material("bark", bc, bn, rough=0.95))
    leaf = Part("twigs", make_material("leaf", lp, alpha=True, rough=0.9))
    C, R = Vector((0, 0, 0.42)), Vector((0.62, 0.62, 0.52))
    anchors = []
    for k in range(15):
        a = k * 2.39996 + rng.uniform(-0.2, 0.2)
        base = Vector((0.04 * math.cos(a), 0.04 * math.sin(a), -0.04))
        el = math.radians(rng.uniform(18, 62))
        d = Vector((math.cos(a) * math.sin(el), math.sin(a) * math.sin(el), math.cos(el)))
        ln = rng.uniform(0.5, 0.85)
        pts = grow(base, d, ln, 5, rng, wander=0.22, grav=0.04)
        r0 = rng.uniform(0.012, 0.018)
        tube(bark, pts, [r0 * (1 - 0.75 * i / 5) for i in range(len(pts))], 4, vscale=0.25)
        for kk in range(3):
            t = 0.35 + 0.25 * kk + rng.uniform(-0.05, 0.05)
            p, dt = at(pts, t)
            sp = grow(p, perp(dt, rng, math.radians(rng.uniform(30, 60))), ln * rng.uniform(0.3, 0.5), 3, rng,
                      wander=0.3)
            tube(bark, sp, [r0 * 0.5 * (1 - 0.8 * i / 3) for i in range(len(sp))], 3, vscale=0.2)
            anchors.append(at(sp, 0.5))
            anchors.append(at(sp, 1.0))
        anchors.append(at(pts, 0.7))
        anchors.append(at(pts, 1.0))
    cards(leaf, anchors, C, R, (0.32, 0.46), cells, rng,
          lambda bd, out, r: bd * 0.7 + out * 0.5 + rvec(r) * 0.3, bias=0.55, ao=(0.55, 1.0), tint=0.05)
    return [bark, leaf], {"leaf": 0.45}, "ambientCG Bark002 (CC0) stems; twig/leaf sprigs painted procedurally"


def m_cactus(T, src, rng):
    cc, cn = tex_cactus(T)
    skin = Part("cactus", make_material("skin", cc, cn, rough=0.7))

    def ribbed(n):
        return lambda th: 1 - 0.13 * (1 - abs(math.cos(n * th / 2))) ** 0.75

    def column(pts, R, ribs, neck=False):
        n = len(pts)
        rad = []
        for i, p in enumerate(pts):
            r = R * (0.92 + 0.08 * math.sin(math.pi * min(1, i / (n - 1) * 1.3)))
            if neck and i < 3:
                r *= 0.78 + 0.07 * i
            rad.append(r)
        top, d = pts[-1], nrm(pts[-1] - pts[-2])
        P, Rr = list(pts), list(rad)
        for k in range(1, 6):
            a = k / 5 * math.pi / 2
            P.append(top + d * R * 0.85 * math.sin(a))
            Rr.append(max(0.004, rad[-1] * math.cos(a)))

        def col(i, z):
            k = min(1.0, max(0.0, z / 0.45))
            return (0.82 + 0.18 * k, 0.76 + 0.24 * k, 0.55 + 0.45 * k, 1)

        tube(skin, P, Rr, ribs * 4, urep=ribs / 2, vscale=0.24, prof=ribbed(ribs), color=col)

    main = [Vector((0, 0, -0.1 + 4.35 * i / 22)) + Vector((0.03 * math.sin(i / 5), 0.02 * math.sin(i / 7), 0))
            for i in range(23)]
    column(main, 0.27, 14)
    for k, (h, az, rise) in enumerate([(1.45, 0.4, 1.8), (2.45, 2.5, 1.2), (1.95, 4.3, 0.95), (3.1, 5.6, 0.35)]):
        p, _ = at(main, (h + 0.1) / 4.35)
        out = Vector((math.cos(az), math.sin(az), 0))
        pts = [p.copy()]
        d = nrm(out + Z * 0.25)
        step = 0.13
        for i in range(30):
            if i > 2:
                d = nrm(d + Z * 0.33)
            pts.append(pts[-1] + d * step)
            if pts[-1].z > p.z + rise:
                break
        column(pts, rng.uniform(0.17, 0.2) if rise > 0.5 else 0.13, 10, neck=True)
    return [skin], {}, "cactus skin (ribs, areoles, spines) painted procedurally; original design"


MODELS = {
    "tree_forest_broadleaf": ("forest", "tree", m_broadleaf),
    "plant_fern": ("forest", "plant", m_fern),
    "plant_desert_cactus": ("desert", "plant", m_cactus),
    "plant_desert_shrub": ("desert", "plant", m_shrub),
    "tree_peaks_birch": ("peaks", "tree", m_birch),
    "plant_swamp_reeds": ("swamp", "plant", m_reeds),
    "tree_temple_palm": ("temple", "tree", m_palm),
    "plant_temple_fern": ("temple", "plant", m_temple_fern),
}


def build(name, src, out, tex_root):
    world, kind, fn = MODELS[name]
    bpy.ops.wm.read_factory_settings(use_empty=True)
    T = Path(tex_root) / name
    T.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(sum(map(ord, name)))
    parts, cutoffs, source = fn(T, src, rng)
    for p in parts:
        if p.F:
            if p.mat.name.startswith("leaf"):      # drooping cards/fronds stop at the ground
                p.P = [(x, y, max(z, 0.01)) for x, y, z in p.P]
            build_object(p)
    path = Path(out) / f"{name}.glb"
    bpy.ops.export_scene.gltf(filepath=str(path), export_format="GLB", export_yup=True, export_apply=True,
                              export_image_format="AUTO", export_texcoords=True, export_normals=True,
                              export_tangents=False, export_materials="EXPORT", export_vertex_color="ACTIVE",
                              export_animations=False, export_extras=False)
    patch_glb(path, cutoffs, source)
    tris, size, ymax, ymin = glb_stats(path)
    return {"file": path.name, "world": world, "kind": kind, "height_m": round(ymax, 2), "tris": tris,
            "bytes": size, "source": source}


def main():
    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else sys.argv[1:]
    src, out = argv[0], argv[1]
    names = argv[2:] or list(MODELS)
    Path(out).mkdir(parents=True, exist_ok=True)
    tex_root = Path(out).parent / "leafy_tex"
    man_path = Path(out) / "leafy_manifest.json"
    manifest = {m["file"]: m for m in (json.loads(man_path.read_text()) if man_path.exists() else [])}
    for n in names:
        rec = build(n, src, out, tex_root)
        manifest[rec["file"]] = rec
        print(f"{rec['file']:32s} {rec['tris']:6d} tris {rec['bytes'] / 1024:7.1f} KB  h={rec['height_m']} m",
              flush=True)
    order = [f"{n}.glb" for n in MODELS]
    man_path.write_text(json.dumps([manifest[f] for f in order if f in manifest], indent=1) + "\n")


if __name__ == "__main__":
    main()
