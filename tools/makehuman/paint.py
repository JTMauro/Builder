"""Paint hair colour into the skin texture where the hair covers the scalp (no bald patches between cards).

    python3 -I paint.py TEX_DIR SCALP_TRIS_JSON

gen.py writes SCALP_TRIS_JSON: UV triangles of the scalp, each corner flagged 1 where a ray along
the skin normal hits the hair mesh. Hair colour comes from HAIR_RGB (as in tex.py).
"""
import json
import os
import sys

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

T, tris = sys.argv[1], json.load(open(sys.argv[2]))
HAIR_RGB = np.array([float(x) for x in os.environ.get("HAIR_RGB", ".075,.05,.035").split(",")])
skin = Image.open(T + "/skin_base.jpg").convert("RGB")
W, H = skin.size
mask = Image.new("L", (W, H), 0)
d = ImageDraw.Draw(mask)
for tri in tris:
    pts = [(u * W, (1 - v) * H) for u, v, f in tri]
    val = int(255 * sum(f for *_, f in tri) / len(tri))
    if val:
        d.polygon(pts, fill=val)
mask = mask.filter(ImageFilter.MaxFilter(5)).filter(ImageFilter.GaussianBlur(10))
m = np.asarray(mask).astype(np.float32)[..., None] / 255
rs = np.random.default_rng(5)
noise = rs.random((H // 3, W // 24)).astype(np.float32)
noise = np.asarray(Image.fromarray((noise * 255).astype(np.uint8)).resize((W, H), Image.BILINEAR).filter(ImageFilter.GaussianBlur(1))).astype(np.float32)[..., None] / 255
hair = HAIR_RGB * (0.7 + 0.6 * noise)
s = np.asarray(skin).astype(np.float32) / 255
out = s * (1 - m * 0.96) + hair * m * 0.96
Image.fromarray((np.clip(out, 0, 1) * 255).astype(np.uint8)).save(T + "/skin.jpg", quality=88)
r = (0.62 + 0.3 * m[..., 0]) * 255
Image.fromarray(np.stack([np.zeros_like(r), r, np.zeros_like(r)], -1).astype(np.uint8)).resize((512, 512)).save(T + "/skin_rough.png")
print("scalp painted")
