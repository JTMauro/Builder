"""Terrain textures and backdrops for the 10 worlds of "Unhappy Woks" (all original / CC0 sources).

    python3 tools/gamesfx/worlds.py SRC_DIR OUT_DIR

SRC_DIR holds the CC0 inputs: acg/<AssetId>/{Color,NormalGL,Displacement}.jpg (ambientCG 1K) and
hdri/<world>.hdr (Poly Haven 4K). Ground textures are photo-scanned materials blended and painted
with periodic (seamless) noise; backdrops are tone-mapped HDRI crops plus painted layers (mountains,
volcanoes, sea, cloud sea, spires, temples, space). Writes JPEGs + manifest.json.
"""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

sys.path.insert(0, str(Path(__file__).parent))
from hdri_tools import auto_exposure, load_hdr, tonemap  # noqa: E402

RNG = np.random.default_rng(424242)
T = 1024          # ground texture size
BW, BH = 2048, 768
HOR = int(BH * 0.60)   # horizon row (40% up from the bottom)


def hexrgb(h):
    h = h.lstrip("#")
    return np.array([int(h[i:i + 2], 16) / 255 for i in (0, 2, 4)], np.float32)


FOG = {"forest": "#a77c62", "ice": "#d2dfea", "desert": "#e9c49a", "station": "#0c1220", "peaks": "#c6dbe6",
       "lava": "#5a1a0c", "swamp": "#56634a", "clouds": "#f2cfd6", "ocean": "#3a4858", "temple": "#a8b894"}

# ---------------------------------------------------------------- seamless helpers


def fbm(n=T, beta=2.0, seed=None, aniso=(1.0, 1.0)):
    """Periodic fractal noise in 0..1 (seamless by construction: built in the Fourier domain)."""
    r = np.random.default_rng(seed) if seed is not None else RNG
    fy = np.fft.fftfreq(n)[:, None] * aniso[1]
    fx = np.fft.rfftfreq(n)[None, :] * aniso[0]
    f = np.sqrt(fx ** 2 + fy ** 2)
    f[0, 0] = 1
    spec = (r.standard_normal(f.shape) + 1j * r.standard_normal(f.shape)) / f ** (beta / 2)
    spec[0, 0] = 0
    x = np.fft.irfft2(spec, s=(n, n))
    x = (x - x.min()) / (np.ptp(x) + 1e-9)
    return x.astype(np.float32)


def smooth(a, lo, hi):
    t = np.clip((a - lo) / (hi - lo + 1e-9), 0, 1)
    return t * t * (3 - 2 * t)


def load(src, aid, kind="Color", size=T):
    im = Image.open(src / "acg" / aid / f"{kind}.jpg").convert("RGB").resize((size, size), Image.LANCZOS)
    return np.asarray(im, np.float32) / 255


def blur_wrap(a, r):
    """Gaussian blur that wraps around the edges (keeps tiling)."""
    from scipy.ndimage import gaussian_filter

    sig = (r, r) + (0,) * (a.ndim - 2)
    return gaussian_filter(a.astype(np.float32), sig, mode="wrap").astype(np.float32)


def stamp_layer(n, count, draw_fn, seed, mode="L"):
    """Draw small features at random spots on a 3x3 tiled canvas, keep the centre: seamless sprinkles."""
    r = np.random.default_rng(seed)
    big = Image.new(mode, (n * 3, n * 3), 0)
    d = ImageDraw.Draw(big)
    for _ in range(count):
        x, y = r.uniform(0, n), r.uniform(0, n)
        for ox in (0, n, 2 * n):
            for oy in (0, n, 2 * n):
                draw_fn(d, x + ox, y + oy, r)
    a = np.asarray(big, np.float32)[n:2 * n, n:2 * n] / 255
    return a


def voronoi_edges(n, cells, seed, width=1.5, warp=0.0):
    """Periodic Voronoi cell edges (cracks), 0..1 with 1 on the edges; warp (px) makes them wander."""
    r = np.random.default_rng(seed)
    pts = r.uniform(0, n, (cells, 2))
    yy, xx = np.mgrid[0:n, 0:n].astype(np.float32)
    if warp:
        xx = (xx + (fbm(n, 2.2, seed + 1000) - 0.5) * 2 * warp) % n
        yy = (yy + (fbm(n, 2.2, seed + 1001) - 0.5) * 2 * warp) % n
    d1 = np.full((n, n), 1e9, np.float32)
    d2 = np.full((n, n), 1e9, np.float32)
    for px, py in pts:
        dx = np.abs(xx - px)
        dx = np.minimum(dx, n - dx)
        dy = np.abs(yy - py)
        dy = np.minimum(dy, n - dy)
        d = np.sqrt(dx * dx + dy * dy)
        m = d < d1
        d2 = np.where(m, d1, np.minimum(d2, d))
        d1 = np.where(m, d, d1)
    return np.clip(1 - (d2 - d1) / width, 0, 1)


def height_normal(h, strength=4.0):
    """Tangent-space normal (OpenGL, green up) from a seamless height map, as 0..1 RGB."""
    dx = (np.roll(h, -1, 1) - np.roll(h, 1, 1)) * 0.5
    dy = (np.roll(h, -1, 0) - np.roll(h, 1, 0)) * 0.5
    nx, ny, nz = -dx * strength, dy * strength, np.ones_like(h)
    l = np.sqrt(nx ** 2 + ny ** 2 + nz ** 2)
    return np.stack([nx / l, ny / l, nz / l], -1) * 0.5 + 0.5


def combine_normals(base, detail, w=1.0):
    """Whiteout blend of two OpenGL normal maps (0..1 RGB)."""
    a, b = base * 2 - 1, detail * 2 - 1
    n = np.stack([a[..., 0] + w * b[..., 0], a[..., 1] + w * b[..., 1], a[..., 2] * (1 + (b[..., 2] - 1) * w)], -1)
    n /= np.linalg.norm(n, axis=-1, keepdims=True) + 1e-9
    return n * 0.5 + 0.5


def lerp(a, b, m):
    m = np.asarray(m, np.float32)
    if m.ndim == 2 and (np.ndim(a) in (1, 3) or np.ndim(b) in (1, 3)):
        m = m[..., None]
    return a * (1 - m) + b * m


def grade(c, sat=1.0, gain=(1, 1, 1), gamma=1.0, lift=0.0):
    l = (c * [0.2126, 0.7152, 0.0722]).sum(-1, keepdims=True)
    c = l + (c - l) * sat
    c = c * np.array(gain, np.float32)
    return np.clip(lift + (1 - lift) * np.clip(c, 0, 1) ** gamma, 0, 1)


# ---------------------------------------------------------------- grounds

def ground(world, src):
    N = load
    if world == "forest":
        grass, gn = N(src, "Grass004"), N(src, "Grass004", "NormalGL")
        litter, ln = N(src, "Ground023"), N(src, "Ground023", "NormalGL")
        m = smooth(fbm(beta=2.4, seed=1), 0.42, 0.62)
        col = lerp(grade(grass, 0.8, (0.85, 0.95, 0.7), 1.1), litter, m)
        nrm = lerp(gn, ln, m)
        needles = stamp_layer(T, 1400, lambda d, x, y, r: d.line([(x, y), (x + r.uniform(-9, 9), y + r.uniform(-9, 9))], fill=int(r.uniform(120, 255)), width=1), 11)
        col = lerp(col, np.array([0.33, 0.2, 0.1]), needles * 0.75)
        clover = stamp_layer(T, 90, lambda d, x, y, r: [d.ellipse([x + ox - 3, y + oy - 3, x + ox + 3, y + oy + 3], fill=255) for ox, oy in ((0, -3), (-3, 2), (3, 2))], 12)
        col = lerp(col, np.array([0.22, 0.42, 0.14]), clover * 0.8)
        flowers = stamp_layer(T, 55, lambda d, x, y, r: d.ellipse([x - 2.5, y - 2.5, x + 2.5, y + 2.5], fill=255), 13)
        col = lerp(col, np.array([0.95, 0.9, 0.55]), flowers)
        twigs = stamp_layer(T, 45, lambda d, x, y, r: d.line([(x, y), (x + r.uniform(-30, 30), y + r.uniform(-30, 30))], fill=255, width=2), 14)
        col = lerp(col, np.array([0.25, 0.16, 0.09]), twigs * 0.9)
        nrm = combine_normals(nrm, height_normal(blur_wrap(twigs + needles * 0.3, 1.2), 3), 0.8)
        col = col * (0.9 + 0.2 * fbm(beta=3, seed=15))[..., None]
    elif world == "ice":
        col, nrm = N(src, "Snow010A"), N(src, "Snow010A", "NormalGL")
        sastrugi = fbm(beta=2.2, seed=21, aniso=(4.0, 0.6))
        dents = fbm(beta=1.6, seed=22)
        tracks = stamp_layer(T, 3, lambda d, x, y, r: [d.ellipse([x + k * 34 + (k % 2) * 6 - 5, y + k * 9 + (k % 2) * 14 - 7, x + k * 34 + (k % 2) * 6 + 5, y + k * 9 + (k % 2) * 14 + 7], fill=255) for k in range(9)], 23)
        h = sastrugi * 0.7 + dents * 0.3 - blur_wrap(tracks, 1.5) * 0.6
        col = grade(col, 0.7, (0.97, 0.99, 1.03)) * (0.92 + 0.1 * h)[..., None]
        col = lerp(col, np.array([0.72, 0.8, 0.9]), blur_wrap(tracks, 2) * 0.5)
        glint = (RNG.random((T, T)) > 0.9993).astype(np.float32)
        col = np.clip(col + blur_wrap(glint, 0.7)[..., None] * 1.5, 0, 1)
        nrm = combine_normals(nrm, height_normal(blur_wrap(h, 1.0), 6), 1.0)
    elif world == "desert":
        col, nrm = N(src, "Ground080"), N(src, "Ground080", "NormalGL")
        col = grade(col, 1.05, (1.04, 0.97, 0.86))
        crack_mask = smooth(fbm(beta=2.6, seed=31), 0.6, 0.72)
        cracks = voronoi_edges(T, 160, 32, 2.2, warp=10) * crack_mask
        col = lerp(col, grade(col, 0.8, (1.06, 1.0, 0.92)) * 1.06, crack_mask * 0.6)
        col = lerp(col, np.array([0.45, 0.33, 0.22]), cracks * 0.8)
        pebbles = stamp_layer(T, 220, lambda d, x, y, r: d.ellipse([x - (s := r.uniform(2, 6)), y - s * r.uniform(0.6, 1), x + s, y + s * r.uniform(0.6, 1)], fill=int(r.uniform(150, 255))), 33)
        pc = np.array([0.55, 0.45, 0.36]) * (0.8 + 0.4 * fbm(beta=1, seed=34))[..., None]
        col = lerp(col, pc, (pebbles > 0).astype(np.float32) * 0.9)
        trail = stamp_layer(T, 2, lambda d, x, y, r: d.line([(x + k * 8, y + 12 * math.sin(k * 0.5)) for k in range(40)], fill=255, width=2), 35)
        col = lerp(col, col * 0.82, blur_wrap(trail, 1))
        nrm = combine_normals(nrm, height_normal(blur_wrap(pebbles - cracks * 0.6 - trail * 0.3, 1.2), 5), 1.0)
    elif world == "station":
        col, nrm = N(src, "MetalPlates006"), N(src, "MetalPlates006", "NormalGL")
        col = grade(col, 0.6, (1.0, 1.0, 1.02)) * 1.15
        yy, xx = np.mgrid[0:T, 0:T]
        band = ((yy > 0.78 * T) & (yy < 0.86 * T)).astype(np.float32)
        stripes = (((xx + yy) // 48) % 2).astype(np.float32)
        wear = smooth(fbm(beta=1.2, seed=41), 0.35, 0.65)
        haz = lerp(np.array([0.9, 0.7, 0.12]), np.array([0.08, 0.08, 0.08]), stripes)
        col = lerp(col, haz, band * wear * 0.85)
        scuffs = stamp_layer(T, 260, lambda d, x, y, r: d.line([(x, y), (x + r.uniform(-25, 25), y + r.uniform(-4, 4))], fill=int(r.uniform(80, 200)), width=1), 42)
        col = np.clip(col + scuffs[..., None] * 0.12, 0, 1)
        grime = fbm(beta=2.2, seed=43)
        col = col * (0.8 + 0.3 * grime)[..., None]
    elif world == "peaks":
        col, nrm = N(src, "Grass004"), N(src, "Grass004", "NormalGL")
        col = grade(col, 1.1, (0.95, 1.05, 0.85), 0.95) * 1.08
        for seed, rgb, cnt in ((51, (0.97, 0.97, 0.92), 160), (52, (0.98, 0.85, 0.2), 120), (53, (0.62, 0.42, 0.85), 90), (54, (0.4, 0.55, 0.95), 70)):
            fl = stamp_layer(T, cnt, lambda d, x, y, r: d.ellipse([x - (s := r.uniform(1.5, 3.2)), y - s, x + s, y + s], fill=255), seed)
            col = lerp(col, np.array(rgb), fl)
        stones = stamp_layer(T, 40, lambda d, x, y, r: d.ellipse([x - (s := r.uniform(4, 11)), y - s * 0.7, x + s, y + s * 0.7], fill=255), 55)
        col = lerp(col, np.array([0.55, 0.55, 0.52]) * (0.8 + 0.3 * fbm(beta=1, seed=56))[..., None], stones)
        nrm = combine_normals(nrm, height_normal(blur_wrap(stones, 2), 6), 1.0)
    elif world == "lava":
        col, nrm = N(src, "Rock035"), N(src, "Rock035", "NormalGL")
        col = grade(col, 0.25, (1.0, 0.97, 0.95), 1.15) * 0.75
        big = voronoi_edges(T, 22, 61, 4.0, warp=45)
        small = voronoi_edges(T, 90, 62, 2.0, warp=18) * smooth(fbm(beta=2.4, seed=63), 0.45, 0.6)
        cracks = np.clip(big + small * 0.8, 0, 1)
        core = cracks ** 2
        glow = blur_wrap(cracks, 4) * 0.8 + core
        heat = np.clip(glow, 0, 1)
        emiss = np.stack([heat, heat ** 1.6 * 0.55, heat ** 3 * 0.12], -1)
        col = np.clip(col * (1 - core[..., None] * 0.9) + emiss * 0.9, 0, 1)
        ash = smooth(fbm(beta=1.4, seed=64), 0.62, 0.8)
        col = lerp(col, np.array([0.42, 0.4, 0.4]), ash * 0.35 * (1 - heat))
        shards = stamp_layer(T, 60, lambda d, x, y, r: d.polygon([(x, y), (x + r.uniform(4, 12), y + r.uniform(-3, 3)), (x + r.uniform(0, 6), y + r.uniform(6, 12))], fill=255), 65)
        col = lerp(col, np.array([0.05, 0.05, 0.07]), shards * 0.9)
        col = np.clip(col + shards[..., None] * stamp_layer(T, 60, lambda d, x, y, r: d.point((x + 3, y + 2), fill=255), 65)[..., None] * 0.5, 0, 1)
        nrm = combine_normals(nrm, height_normal(blur_wrap(-cracks + shards * 0.5, 1.5), 5), 1.0)
        emissive = np.clip(emiss * 1.3, 0, 1)
        return col, nrm, emissive
    elif world == "swamp":
        col, nrm = N(src, "Ground109"), N(src, "Ground109", "NormalGL")
        col = grade(col, 0.85, (0.82, 0.9, 0.7), 1.2) * 0.8
        wet = fbm(beta=2.6, seed=71)
        puddle = smooth(1 - wet, 0.62, 0.7)
        water = np.array([0.16, 0.19, 0.17]) + 0.08 * fbm(beta=3, seed=72)[..., None]
        col = lerp(col, water, puddle * 0.95)
        moss = smooth(fbm(beta=2, seed=73), 0.55, 0.75) * (1 - puddle)
        col = lerp(col, np.array([0.24, 0.32, 0.14]), moss * 0.7)
        roots = stamp_layer(T, 25, lambda d, x, y, r: d.line([(x + k * 9, y + 14 * math.sin(k * 0.4 + r.uniform(0, 6)) + k * r.uniform(-3, 3)) for k in range(14)], fill=255, width=int(r.uniform(3, 7))), 74)
        col = lerp(col, np.array([0.2, 0.14, 0.09]), roots * 0.9)
        reeds = stamp_layer(T, 400, lambda d, x, y, r: d.line([(x, y), (x + r.uniform(-6, 6), y + r.uniform(-16, -4))], fill=255, width=1), 75)
        col = lerp(col, np.array([0.36, 0.42, 0.2]), reeds * (1 - puddle) * 0.8)
        nrm = lerp(nrm, np.full_like(nrm, [0.5, 0.5, 1.0]), puddle)   # still water is flat
        nrm = combine_normals(nrm, height_normal(blur_wrap(roots * 1.0, 1.5), 6), 1.0)
    elif world == "clouds":
        col, nrm = N(src, "PavingStones138"), N(src, "PavingStones138", "NormalGL")
        g = col.copy()
        greenness = np.clip((g[..., 1] - (g[..., 0] + g[..., 2]) / 2) * 6, 0, 1)
        col = grade(col, 0.15, (1.06, 1.02, 0.98), 0.75) * 1.05
        col = lerp(col, np.array([0.95, 0.93, 0.9]), greenness * 0.8)   # the moss joints become pale mortar
        mist = smooth(fbm(beta=2.1, seed=81), 0.45, 0.85)
        col = lerp(col, np.array([1.0, 0.97, 0.97]), mist * 0.65)
        nrm = lerp(nrm, np.full_like(nrm, [0.5, 0.5, 1.0]), mist * 0.6)
    elif world == "ocean":
        col, nrm = N(src, "MetalPlates013"), N(src, "MetalPlates013", "NormalGL")
        col = grade(col, 0.6, (0.92, 0.96, 1.02), 1.25) * 0.72
        wet = smooth(fbm(beta=2.5, seed=91), 0.55, 0.62)
        col = lerp(col, col * 0.55 + np.array([0.08, 0.1, 0.13]), wet * 0.8)
        drops = stamp_layer(T, 700, lambda d, x, y, r: d.ellipse([x - 1.2, y - 1.2, x + 1.2, y + 1.2], fill=255), 92)
        col = np.clip(col + drops[..., None] * 0.18 * (1 - wet[..., None]), 0, 1)
        paint = smooth(fbm(beta=1.6, seed=93), 0.7, 0.75)
        col = lerp(col, np.array([0.55, 0.5, 0.18]), paint * 0.35)
        nrm = lerp(nrm, np.full_like(nrm, [0.5, 0.5, 1.0]), wet * 0.85)
    else:   # temple
        soil, sn = N(src, "Ground106"), N(src, "Ground106", "NormalGL")
        leaves, ln = N(src, "Ground023"), N(src, "Ground023", "NormalGL")
        tiles, tn = N(src, "PavingStones138"), N(src, "PavingStones138", "NormalGL")
        m = smooth(fbm(beta=2.3, seed=101), 0.4, 0.6)
        col = lerp(grade(soil, 1.0, (0.9, 1.0, 0.8)), grade(leaves, 1.1, (0.95, 1.0, 0.8)), m)
        nrm = lerp(sn, ln, m)
        tile_m = smooth(fbm(beta=2.8, seed=102), 0.74, 0.8)
        col = lerp(col, grade(tiles, 0.6, (0.95, 1.0, 0.9), 1.1) * 0.85, tile_m)
        nrm = lerp(nrm, tn, tile_m)
        moss = smooth(fbm(beta=2, seed=103), 0.5, 0.72)
        col = lerp(col, np.array([0.2, 0.33, 0.12]), moss * 0.55)
        roots = stamp_layer(T, 18, lambda d, x, y, r: d.line([(x + k * 10, y + 16 * math.sin(k * 0.35 + r.uniform(0, 6))) for k in range(16)], fill=255, width=int(r.uniform(4, 9))), 104)
        col = lerp(col, np.array([0.3, 0.22, 0.13]), roots * 0.9)
        nrm = combine_normals(nrm, height_normal(blur_wrap(roots, 2), 7), 1.0)
    return np.clip(col, 0, 1), np.clip(nrm, 0, 1), None


# ---------------------------------------------------------------- backdrops

def crop_pano(x, lon_deg, fov=120.0):
    """A cylindrical crop of an equirect panorama: lon centre, horizontal FOV; horizon at HOR."""
    H, W = x.shape[:2]
    ppd = W / 360.0
    ox = BW / (fov * ppd)                     # output px per input px
    cols = (lon_deg * ppd + (np.arange(BW) - BW / 2) / ox) % W
    rows = H / 2 + (np.arange(BH) - HOR) / ox
    rows = np.clip(rows, 0, H - 1)
    c0, r0 = np.floor(cols).astype(int) % W, np.floor(rows).astype(int)
    fc, fr = (cols - c0)[None, :, None], (rows - r0)[:, None, None]
    c1, r1 = (c0 + 1) % W, np.minimum(r0 + 1, H - 1)
    a = x[r0][:, c0] * (1 - fc) + x[r0][:, c1] * fc
    b = x[r1][:, c0] * (1 - fc) + x[r1][:, c1] * fc
    return a * (1 - fr) + b * fr


def ridge(seed, base, amp, rough=0.48, octaves=6, w=BW):
    r = np.random.default_rng(seed)
    xs = np.arange(w) / w
    y = np.zeros(w)
    a, f = 1.0, 2.0
    for _ in range(octaves):
        y += a * np.interp(xs * f % 1, np.linspace(0, 1, int(f) + 1), r.uniform(-1, 1, int(f) + 1))
        a *= rough
        f *= 2
    y = (y - y.min()) / (np.ptp(y) + 1e-9)
    from scipy.ndimage import gaussian_filter1d
    y = gaussian_filter1d(y, 2.5, mode="wrap")
    return base - amp * y


def paint_range(img, top, color, haze, snow=None, shade=0.0, seed=0):
    """Fill below a ridge line (row per column) with a colour; optional snow near the crest."""
    yy = np.arange(BH)[:, None]
    m = ((yy >= top[None, :]) & (yy <= HOR + 3)).astype(np.float32)
    m = np.asarray(Image.fromarray((m * 255).astype(np.uint8)).filter(ImageFilter.GaussianBlur(0.8)), np.float32) / 255
    tex = fbm(BW, 1.8, seed + 500)[:BH]
    shade2d = fbm(BW, 2.6, seed + 502)[:BH]                    # rock faces and gullies (2D, so no column streaks)
    light = 0.7 + 0.6 * shade2d
    col = np.array(color)[None, None, :] * (0.75 + 0.5 * tex)[..., None] * light[..., None]
    col = col * (1 - shade * np.clip((yy - top[None, :]) / 160, 0, 1))[..., None]
    if snow is not None:
        snowline = top.min() + snow + 40 * (fbm(BW, 2.0, seed + 501)[0] - 0.5)
        sm = smooth((snowline[None, :] - yy) / 25 + (tex - 0.5) * 1.5, 0.0, 0.6)
        col = lerp(col, np.array([0.93, 0.95, 1.0]) * light[..., None] ** 0.5, sm)
    return lerp(img, col, m)


def fade_bottom(img, fog, start=0.85):
    yy = np.arange(BH)[:, None] / BH
    k = smooth(yy, start, 1.0)[..., None]
    return lerp(img, np.broadcast_to(fog, img.shape), k)


def aerial(img, row_from, row_to, haze, amount):
    yy = np.arange(BH)[:, None]
    k = (1 - smooth(np.abs(yy - row_from) / max(1, abs(row_to - row_from)), 0, 1))[..., None] * amount
    return lerp(img, np.broadcast_to(np.array(haze), img.shape), k)


def sun(img, cx, cy, r, color, glow=8.0):
    yy, xx = np.mgrid[0:BH, 0:BW]
    d = np.sqrt((xx - cx) ** 2 + (yy - cy) ** 2)
    disc = np.clip((r - d) / 2 + 1, 0, 1)[..., None]
    halo = (np.exp(-d / (r * glow)) * 0.6)[..., None]
    return np.clip(img + np.array(color) * halo + disc * (1 - img) * 0.98, 0, 1)


def sky_from(src, world, lon, grade_args, fov=120):
    x = load_hdr(str(src / "hdri" / f"{world}.hdr"))
    c = crop_pano(x, lon, fov)
    return grade(tonemap(c, auto_exposure(c[:HOR]) * 1.0), **grade_args)


def fbm_wide(seed, beta=2.2, aniso=(1, 1)):
    a = fbm(BW, beta, seed, aniso)
    return a[:BH]


def backdrop(world, src):
    fog = hexrgb(FOG[world])
    if world == "forest":
        img = sky_from(src, "forest", 200, dict(sat=0.9, gain=(1.18, 0.88, 0.62), gamma=1.05))
        yy = np.arange(BH)[:, None] / BH
        img = lerp(img, np.array([1.0, 0.62, 0.32]), (smooth(1 - np.abs(yy - 0.45) / 0.3, 0, 1) * 0.35)[..., None])   # low sunset glow through the trunks
    elif world == "ice":
        img = sky_from(src, "ice", 90, dict(sat=0.7, gain=(0.96, 1.0, 1.08), gamma=0.95))
        for seed, base, amp, col, sn in ((1, HOR + 2, 150, (0.62, 0.68, 0.78), 80), (2, HOR + 4, 95, (0.5, 0.56, 0.66), 50)):
            img = paint_range(img, ridge(seed, base, amp), col, (0.85, 0.9, 0.96), snow=sn, shade=0.25, seed=seed)
            img = aerial(img, HOR, HOR - amp, (0.85, 0.89, 0.95), 0.35)
    elif world == "desert":
        img = sky_from(src, "desert", 290, dict(sat=1.0, gain=(1.05, 0.97, 0.85)))
        img = aerial(img, HOR, HOR - 70, (0.93, 0.83, 0.72), 0.3)
        img = sun(img, BW * 0.66, BH * 0.16, 18, (1.0, 0.85, 0.55), 6)
        img = sun(img, BW * 0.8, BH * 0.27, 11, (1.0, 0.65, 0.45), 5)
    elif world == "station":
        img = np.zeros((BH, BW, 3), np.float32) + np.array([0.01, 0.012, 0.025])
        neb = fbm_wide(11, 2.0) ** 3
        img += neb[..., None] * np.array([0.12, 0.06, 0.2]) + (fbm_wide(12, 2.2) ** 4)[..., None] * np.array([0.02, 0.08, 0.14])
        stars = (RNG.random((BH, BW)) > 0.9985).astype(np.float32) * RNG.uniform(0.3, 1.0, (BH, BW))
        img += np.asarray(Image.fromarray((stars * 255).astype(np.uint8)).filter(ImageFilter.GaussianBlur(0.6)), np.float32)[..., None] / 255 * 2.2
        cx, cy, R = BW * 0.68, BH * 0.36, 260
        yy, xx = np.mgrid[0:BH, 0:BW]
        d = np.sqrt((xx - cx) ** 2 + (yy - cy) ** 2) / R
        lat = (yy - cy) / R
        bands = np.sin(lat * 26 + 2.5 * np.sin(lat * 7) + 0.6 * (fbm_wide(13, 2.5, (6, 1)) - 0.5) * 6)
        planet = np.stack([0.75 + 0.15 * bands, 0.6 + 0.12 * bands, 0.45 + 0.08 * bands], -1)
        light = np.clip(0.25 + 0.85 * ((xx - cx) * -0.7 + (yy - cy) * -0.5) / R, 0, 1) ** 0.8
        disc = np.clip((1 - d) * R / 2, 0, 1)
        limb = np.clip(1 - d ** 6, 0, 1)
        img = lerp(img, planet * (light * limb)[..., None], disc)
        rim = np.exp(-np.abs(d - 1) * 30) * light
        img = np.clip(img + rim[..., None] * np.array([0.4, 0.5, 0.8]) * 0.6, 0, 1)
        base = np.full(BW, float(HOR))
        sil = Image.new("L", (BW, BH), 0)
        dd = ImageDraw.Draw(sil)
        r = np.random.default_rng(14)
        lights = []
        for _ in range(26):   # distant station towers, spars and dishes
            x0, w, h = r.uniform(0, BW), r.uniform(20, 120), r.uniform(20, 170)
            dd.rectangle([x0, HOR - h, x0 + w, HOR + 2], fill=255)
            if r.random() < 0.4:
                dd.rectangle([x0 - r.uniform(20, 90), HOR - h * 0.7, x0 + w + r.uniform(20, 90), HOR - h * 0.7 + 5], fill=255)
            if r.random() < 0.25:
                dd.ellipse([x0 + w / 2 - 22, HOR - h - 30, x0 + w / 2 + 22, HOR - h], fill=255)
            for _ in range(int(r.integers(2, 8))):
                lights.append((x0 + r.uniform(0, w), HOR - r.uniform(0, h)))
        dd.rectangle([0, HOR, BW, BH], fill=255)
        s = np.asarray(sil, np.float32)[..., None] / 255
        img = lerp(img, np.array([0.05, 0.07, 0.11]), s)
        lt = Image.new("L", (BW, BH), 0)
        dl = ImageDraw.Draw(lt)
        for x, y in lights:
            dl.ellipse([x - 1.5, y - 1.5, x + 1.5, y + 1.5], fill=255)
        L = np.asarray(lt.filter(ImageFilter.GaussianBlur(1.2)), np.float32)[..., None] / 255
        img = np.clip(img + L * np.array([1.0, 0.75, 0.4]) * 1.5, 0, 1)
    elif world == "peaks":
        img = sky_from(src, "peaks", 180, dict(sat=1.05, gain=(0.95, 1.0, 1.05)))
        yy = np.arange(BH)[:, None]
        lake = smooth((yy - HOR) / 60, 0.05, 0.3) * (1 - smooth((yy - HOR) / 60, 1.2, 2.2))
        refl = img[np.clip(2 * HOR - yy[:, 0] + 4, 0, BH - 1)] * np.array([0.75, 0.85, 0.95])
        rip = 0.92 + 0.08 * fbm_wide(15, 2.0, (8, 1))[..., None]
        img = lerp(img, refl * rip, lake[..., None] * 0.85)
    elif world == "lava":
        img = sky_from(src, "lava", 225, dict(sat=0.3, gain=(1.35, 0.55, 0.32), gamma=1.35))
        ash = fbm_wide(21, 2.0) ** 2
        img = lerp(img, np.array([0.22, 0.08, 0.06]), (ash * 0.6 * (np.arange(BH)[:, None] < HOR))[..., None])
        for seed, cx, h, wdt in ((22, 0.3, 260, 520), (23, 0.72, 190, 420), (24, 0.08, 130, 300)):
            xs = np.arange(BW)
            prof = HOR - h * np.clip(1 - np.abs(xs - cx * BW) / wdt, 0, 1) ** 1.4 - 8 * (fbm_wide(seed)[0] - 0.5)
            crater = (np.abs(xs - cx * BW) < wdt * 0.06)
            prof = np.where(crater, prof + 14, prof)
            img = paint_range(img, prof, (0.12, 0.06, 0.05), (0.5, 0.2, 0.1), shade=-0.3)
            yy, xx = np.mgrid[0:BH, 0:BW]
            tip = HOR - h + 14
            glow = np.exp(-(((xx - cx * BW) / 60) ** 2 + ((yy - tip) / 40) ** 2))
            img = np.clip(img + glow[..., None] * np.array([1.0, 0.45, 0.1]) * 0.9, 0, 1)
            for k in range(3):   # lava streams down the slope
                xs0 = cx * BW + (k - 1) * wdt * 0.12
                pts = [(xs0 + 10 * math.sin(j * 0.6 + k) + j * (k - 1) * 3, tip + j * 8) for j in range(int(h / 9))]
                st = Image.new("L", (BW, BH), 0)
                ImageDraw.Draw(st).line(pts, fill=255, width=3)
                S = np.asarray(st.filter(ImageFilter.GaussianBlur(2)), np.float32)[..., None] / 255
                img = np.clip(img + S * np.array([1.0, 0.4, 0.08]) * 0.8, 0, 1)
            smoke = fbm_wide(seed + 10, 2.3) * np.exp(-((xx - cx * BW) / 120) ** 2) * (yy < tip)
            img = lerp(img, np.array([0.3, 0.2, 0.18]), (smoke * 0.6)[..., None])
    elif world == "swamp":
        img = sky_from(src, "swamp", 135, dict(sat=0.5, gain=(0.85, 0.98, 0.78), gamma=1.1))
        r = np.random.default_rng(31)
        for layer, (cnt, scale, tone, alpha) in enumerate(((7, 0.6, 0.62, 0.45), (6, 0.85, 0.45, 0.65), (5, 1.15, 0.3, 0.85))):
            lay = Image.new("L", (BW, BH), 0)
            d = ImageDraw.Draw(lay)

            def branch(x, y, ang, ln, wdt, depth):
                if depth == 0 or ln < 6:
                    return
                x2, y2 = x + ln * math.cos(ang), y - ln * math.sin(ang)
                d.line([(x, y), (x2, y2)], fill=255, width=max(1, int(wdt)))
                for _ in range(2):
                    branch(x2, y2, ang + r.uniform(-0.9, 0.9), ln * r.uniform(0.55, 0.8), wdt * 0.65, depth - 1)
            for _ in range(cnt):
                x0 = r.uniform(0, BW)
                base = HOR + 10 + layer * 8
                trunk = 140 * scale
                pts = [(x0 + 18 * math.sin(k * 0.7 + r.uniform(0, 3)) * scale, base - k * trunk / 8) for k in range(9)]
                d.line(pts, fill=255, width=int(10 * scale))
                for k in range(4, 9):
                    branch(pts[k][0], pts[k][1], r.uniform(0.3, 2.8), 60 * scale, 5 * scale, 4)
            M = np.asarray(lay.filter(ImageFilter.GaussianBlur(2.5 - layer * 0.7)), np.float32)[..., None] / 255
            img = lerp(img, np.array([0.28, 0.32, 0.25]) * tone / 0.6, M * alpha)
            img = lerp(img, np.array([0.6, 0.66, 0.57]), np.full((BH, BW, 1), 0.12))   # mist between layers
        yy = np.arange(BH)[:, None]
        water = smooth((yy - HOR) / 40, 0.2, 0.6)[..., None]
        img = lerp(img, np.array([0.3, 0.35, 0.28]) * (0.9 + 0.15 * fbm_wide(32, 2, (6, 1)))[..., None], water * 0.7)
    elif world == "clouds":
        img = sky_from(src, "clouds", 90, dict(sat=0.8, gain=(1.2, 0.85, 0.78), gamma=0.95))
        yy = np.arange(BH)[:, None] / BH
        img = lerp(img, np.array([1.0, 0.68, 0.55]), (smooth(1 - np.abs(yy - HOR / BH) / 0.25, 0, 1) * 0.5)[..., None])   # sunset band
        sea = fbm_wide(41, 2.4, (3, 1))
        top = HOR - 125 + 22 * (fbm_wide(42, 2.6)[0] - 0.5)   # this photo's horizon sits high: clouds cover its land
        yyi = np.arange(BH)[:, None]
        cm = smooth((yyi - top[None, :]) / 16 + sea * 0.5, 0.1, 0.55)
        cloudcol = lerp(np.array([1.0, 0.86, 0.84]), np.array([0.85, 0.66, 0.72]), smooth((yyi - HOR) / 200, 0, 1)[..., None] * 0.8)
        cloudcol = cloudcol * (0.85 + 0.25 * sea)[..., None]
        r = np.random.default_rng(43)
        sp = Image.new("L", (BW, BH), 0)
        d = ImageDraw.Draw(sp)
        for _ in range(14):   # tall white spires of the floating city
            x0, w, h = r.uniform(80, BW - 80), r.uniform(26, 60), r.uniform(140, 330)
            base = HOR - 95
            tone = int(r.uniform(170, 255))
            tiers = int(r.integers(2, 4))
            for t in range(tiers):   # a tower that narrows in tiers, with a domed or pointed top
                tw = w * (1 - t * 0.25)
                y0, y1 = base - h * (t + 1) / tiers, base - h * t / tiers
                d.rectangle([x0 - tw / 2, y0, x0 + tw / 2, y1], fill=tone)
                d.rectangle([x0 - tw * 0.7, y1 - 6, x0 + tw * 0.7, y1], fill=max(150, tone - 30))
            tw = w * (1 - (tiers - 1) * 0.25)
            if r.random() < 0.5:
                d.ellipse([x0 - tw / 2, base - h - tw * 0.6, x0 + tw / 2, base - h + tw * 0.4], fill=tone)
            else:
                d.polygon([(x0 - tw / 2, base - h), (x0, base - h - tw * 1.6), (x0 + tw / 2, base - h)], fill=tone)
        S = np.asarray(sp.filter(ImageFilter.GaussianBlur(0.8)), np.float32)[..., None] / 255
        shade = np.linspace(1.0, 0.8, BW)[None, :, None]
        img = lerp(img, np.array([0.97, 0.9, 0.88]) * shade, (S > 0) * (0.55 + 0.45 * S))
        img = lerp(img, cloudcol, cm[..., None])
    elif world == "ocean":
        img = sky_from(src, "ocean", 160, dict(sat=0.15, gain=(0.5, 0.56, 0.64), gamma=1.7))
        img = np.clip((img - 0.25) * 1.35 + 0.25, 0, 1)   # moodier contrast
        yy = np.arange(BH)[:, None]
        sea_m = smooth((yy - HOR) / 6, 0, 1)[..., None]
        waves = fbm_wide(51, 2.0, (6, 1))
        crest = smooth(fbm_wide(52, 1.8, (4, 1)), 0.78, 0.9) * smooth((yy - HOR) / 120, 0, 1)
        seacol = lerp(np.array([0.16, 0.2, 0.25]), np.array([0.3, 0.36, 0.42]), smooth((HOR + 80 - yy) / 80, 0, 1)) * (0.75 + 0.45 * waves)[..., None]
        seacol = lerp(seacol, np.array([0.75, 0.8, 0.84]), crest[..., None] * 0.7)
        img = lerp(img, seacol, sea_m)
        rain = Image.new("L", (BW, BH), 0)
        d = ImageDraw.Draw(rain)
        r = np.random.default_rng(53)
        for _ in range(2500):
            x, y = r.uniform(0, BW), r.uniform(0, BH)
            d.line([(x, y), (x - 8, y + 34)], fill=int(r.uniform(40, 120)), width=1)
        R = np.asarray(rain, np.float32)[..., None] / 255
        img = np.clip(img + R * 0.25, 0, 1)
    else:   # temple
        img = sky_from(src, "swamp", 300, dict(sat=0.6, gain=(1.08, 1.04, 0.86), gamma=0.95))
        r = np.random.default_rng(61)
        layers = [(0.75, (0.55, 0.62, 0.5), 0.55, 140), (0.9, (0.4, 0.48, 0.36), 0.75, 90), (1.1, (0.24, 0.32, 0.2), 0.95, 50)]
        for li, (scale, col, alpha, haze) in enumerate(layers):
            lay = Image.new("L", (BW, BH), 0)
            d = ImageDraw.Draw(lay)
            for _ in range(2 if li < 2 else 1):   # stepped temples
                x0 = r.uniform(150, BW - 150)
                base = HOR + 6 + li * 6
                steps, w0, sh = int(r.integers(5, 8)), r.uniform(220, 380) * scale, r.uniform(24, 34) * scale
                for s in range(steps):
                    w = w0 * (1 - s / (steps + 1.5))
                    d.rectangle([x0 - w / 2, base - (s + 1) * sh, x0 + w / 2, base - s * sh], fill=255)
                d.rectangle([x0 - w0 * 0.08, base - (steps + 1.6) * sh, x0 + w0 * 0.08, base - steps * sh], fill=255)
            xs = np.arange(BW)
            from scipy.ndimage import gaussian_filter1d
            bumps = np.zeros(BW)
            for _ in range(int(90 / scale)):   # tree crowns of different sizes
                cx, rad = r.uniform(0, BW), r.uniform(14, 40) * scale
                bumps = np.maximum(bumps, np.sqrt(np.clip(rad ** 2 - (xs - cx) ** 2, 0, None)))
            canopy = HOR + 14 + li * 8 - gaussian_filter1d(bumps, 1.5) - 30 * scale * fbm_wide(62 + li)[0]
            Tm = np.asarray(lay.filter(ImageFilter.GaussianBlur(1.0)), np.float32)[..., None] / 255
            yy = np.arange(BH)[:, None]
            Cm = np.asarray(Image.fromarray(((yy >= canopy[None, :]) * 255).astype(np.uint8)).filter(ImageFilter.GaussianBlur(1.2)), np.float32)[..., None] / 255
            stone = np.array([0.55, 0.56, 0.48]) * (0.7 + 0.5 * fbm_wide(80 + li, 1.6))[..., None]
            side = np.asarray(lay.filter(ImageFilter.FIND_EDGES).filter(ImageFilter.GaussianBlur(2)), np.float32)[..., None] / 255
            stone = stone * (1 - 0.35 * side)
            mossy = lerp(stone, np.array([0.3, 0.4, 0.22]), smooth(fbm_wide(90 + li, 2.0), 0.55, 0.8))
            haze = np.array([0.66, 0.7, 0.6])
            img = lerp(img, lerp(mossy, haze, 0.55 - li * 0.2), Tm * alpha)
            leaf = 0.65 + 0.7 * fbm_wide(70 + li, 1.3)
            img = lerp(img, np.array(col) * leaf[..., None], Cm * alpha)
    img = fade_bottom(img, fog)
    return np.clip(img, 0, 1)


# ---------------------------------------------------------------- main

def save(arr, path, size=None, q=80):
    im = Image.fromarray((np.clip(arr, 0, 1) * 255).astype(np.uint8))
    if size:
        im = im.resize((size, size), Image.LANCZOS)
    im.save(path, quality=q, optimize=True, progressive=True)
    return im.size


def main():
    src, out = Path(sys.argv[1]), Path(sys.argv[2])
    only = set(sys.argv[3].split(",")) if len(sys.argv) > 3 else None
    out.mkdir(parents=True, exist_ok=True)
    man = []
    for w in FOG:
        if only and w not in only:
            continue
        col, nrm, emi = ground(w, src)
        for kind, arr, size in (("color", col, None), ("normal", nrm, 512), ("emissive", emi, None)):
            if arr is None:
                continue
            p = out / f"ground_{w}_{kind}.jpg"
            wh = save(arr, p, size, 80 if kind != "normal" else 85)
            man.append({"file": p.name, "world": w, "kind": kind, "w": wh[0], "h": wh[1], "bytes": p.stat().st_size})
        b = backdrop(w, src)
        p = out / f"backdrop_{w}.jpg"
        wh = save(b, p, None, 80)
        man.append({"file": p.name, "world": w, "kind": "backdrop", "w": wh[0], "h": wh[1], "bytes": p.stat().st_size})
        print(w, "done")
    if not only:
        (out / "manifest.json").write_text(json.dumps({"game": "Unhappy Woks", "fog": FOG, "files": man}, indent=1))
        print(sum(m["bytes"] for m in man) // 1024, "KB total")


if __name__ == "__main__":
    main()
