"""Prepare plain textures for a MakeHuman person (run with any Python that has numpy + Pillow).

    python3 -I tex.py ASSETS_DIR OUT_DIR

Colours come from environment variables (sRGB 0..1, comma separated):
  SKIN_RGB   mean skin colour        default .73,.53,.39  (light olive)
  HAIR_RGB   hair colour             default .075,.05,.035 (dark brown)
  TOP_RGB    shirt colour            default .78,.10,.10  (red); "keep" leaves the asset's colour
  EYE        eye material file name  default brown_eye  (see eyes/materials/*.png)
  HAIR       hair asset              default short02
  CLOTHES    clothes asset           default male_casualsuit06
  SHOES      shoes asset             default shoes05
  SHOES_RGB  shoe upper colour       default: the asset's own colours
  JEANS_LIGHT  brighten the jeans (e.g. 1.3 for a light wash)   default: off
"""
import os
import sys

import numpy as np
from PIL import Image, ImageFilter

A, O = sys.argv[1], sys.argv[2]
os.makedirs(O, exist_ok=True)
rgb = lambda k, d: np.array([float(x) for x in os.environ.get(k, d).split(",")])
HAIR = os.environ.get("HAIR", "short02")
CLOTHES = os.environ.get("CLOTHES", "male_casualsuit06")
SHOES = os.environ.get("SHOES", "shoes05")
HAIR_RGB = rgb("HAIR_RGB", ".075,.05,.035")


def arr(p, size=1024):
    return np.asarray(Image.open(p).resize((size, size), Image.LANCZOS)).astype(np.float32) / 255


def save(a, p, q=90):
    im = Image.fromarray((np.clip(a, 0, 1) * 255).astype(np.uint8))
    if p.endswith(".jpg"):
        im.convert("RGB").save(p, quality=q)
    else:
        im.save(p, optimize=True)


def first(*paths):
    for p in paths:
        if os.path.exists(p):
            return p
    raise FileNotFoundError(paths[0])


# skin: scale the texture so its mean matches the target colour
s = arr(f"{A}/skins/young_caucasian_male/young_lightskinned_male_diffuse.png", 2048)[..., :3]
ref = s[300:1700, 1400:1950].reshape(-1, 3).mean(0)
s = s * (rgb("SKIN_RGB", ".73,.53,.39") / ref)
save(s, f"{O}/skin.jpg", 88)
save(s, f"{O}/skin_base.jpg", 92)

# clothes: the top 43% of the casual-suit UV is the tee; remove the printed logo and recolour by luminance
c = arr(f"{A}/clothes/{CLOTHES}/{CLOTHES}_diffuse.png", 2048)[..., :3]
top_rgb = os.environ.get("TOP_RGB", ".78,.10,.10")
if top_rgb != "keep":
    top = int(c.shape[0] * 0.43)
    t = c[:top]
    L = t @ np.array([0.3, 0.59, 0.11])
    sat = t.max(-1) - t.min(-1)
    med = np.asarray(Image.fromarray((L * 255).astype(np.uint8)).filter(ImageFilter.MedianFilter(31))).astype(np.float32) / 255
    L = np.where((sat > 0.05) | (np.abs(L - med) > 0.05), med, L)
    L = 0.65 * med + 0.35 * L          # flatten leftover print marks
    # the asset bakes heavy fold shading into the tee (it reads like ribs and abs on the chest):
    # keep only a soft trace of it, around the tee's average brightness
    soft = np.asarray(Image.fromarray((np.clip(L, 0, 1) * 255).astype(np.uint8)).filter(ImageFilter.GaussianBlur(18))).astype(np.float32) / 255
    m = max(float(L.mean()), 1e-3)
    shade = 1 + 0.35 * (soft / m - 1) + 0.12 * (L / m - soft / m)
    c[:top] = rgb("TOP_RGB", top_rgb)[None, None] * np.clip(shade, 0.7, 1.15)[..., None]
if os.environ.get("JEANS_LIGHT"):
    # lighter, faded wash for the trousers (the lower part of the casual-suit UV)
    k = float(os.environ["JEANS_LIGHT"]); low = int(c.shape[0] * 0.43)
    c[low:] = np.clip(c[low:] * k + 0.03 * (k - 1) * 4, 0, 1)
save(c, f"{O}/clothes.jpg", 88)
n = f"{A}/clothes/{CLOTHES}/{CLOTHES}_normal.png"
if os.path.exists(n):
    save(arr(n, 1024)[..., :3], f"{O}/clothes_n.jpg", 90)

# hair: keep the strand detail (luminance), set the colour
hr = arr(f"{A}/hair/{HAIR}/{HAIR}_diffuse.png", 1024)
L = hr[..., :3] @ np.array([0.3, 0.59, 0.11])
# keep strand detail but lift the darkest texels, which otherwise show as black patches
rel = L / max(L[hr[..., 3] > 0.5].mean(), 1e-3)
hr[..., :3] = np.clip(0.55 + 0.45 * rel, 0, 1.6)[..., None] * HAIR_RGB
save(hr, f"{O}/hair.png")
n = f"{A}/hair/{HAIR}/{HAIR}_normal.png"
if os.path.exists(n):
    save(arr(n, 1024)[..., :3], f"{O}/hair_n.jpg")

sh = arr(first(f"{A}/clothes/{SHOES}/{SHOES}_diffuse.png"), 1024)[..., :3]
if os.environ.get("SHOES_RGB"):
    # recolour the uppers by luminance; near-white parts (soles, laces) stay as they are
    L = sh @ np.array([0.3, 0.59, 0.11]); up = L < 0.72
    tint = rgb("SHOES_RGB", ".45,.48,.30")[None, None] * (L / max(L[up].mean(), 1e-3))[..., None]
    sh = np.where(up[..., None], np.clip(tint, 0, 1), sh)
save(sh, f"{O}/shoes.jpg")

# brows and lashes: darker than the hair, alpha boosted so they show in three.js
brow = HAIR_RGB * 0.8
for name, src in [("brows", "eyebrows/eyebrow001/eyebrow001.png"), ("lashes", "eyelashes/eyelashes01/eyelashes01.png")]:
    im = arr(f"{A}/{src}", 512)
    im[..., 3] = np.clip(im[..., 3] * 1.6, 0, 1)
    im[..., :3] = brow if name == "brows" else np.array([0.06, 0.04, 0.03])
    save(im, f"{O}/{name}.png")
save(arr(f"{A}/eyes/materials/{os.environ.get('EYE', 'brown_eye')}.png", 512)[..., :3], f"{O}/eye.jpg")
print("textures ready in", O)
