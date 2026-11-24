"""Small helpers for turning HDRI panoramas into game backdrops (used by worlds.py)."""
import subprocess

import numpy as np


def load_hdr(path, width=None):
    """Read a Radiance .hdr (equirectangular) as float32 RGB (H, W, 3) via ffmpeg."""
    vf = ["-vf", f"scale={width}:-1:flags=area"] if width else []
    probe = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height", "-of", "csv=p=0", path],
                           capture_output=True, text=True, check=True).stdout.strip().split(",")
    w, h = int(probe[0]), int(probe[1])
    if width:
        h, w = int(round(h * width / w)), width
        h -= h % 2
        vf = ["-vf", f"scale={w}:{h}:flags=area"]
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", path, *vf, "-f", "rawvideo", "-pix_fmt", "gbrpf32le", "-"],
                         capture_output=True, check=True).stdout
    a = np.frombuffer(raw, np.float32).reshape(3, h, w)
    return np.stack([a[2], a[0], a[1]], -1)   # gbr planes -> rgb


def tonemap(x, exposure=1.0, gamma=2.2):
    """ACES-style filmic curve, then display gamma. Returns 0..1 float."""
    x = x * exposure
    a, b, c, d, e = 2.51, 0.03, 2.43, 0.59, 0.14
    y = np.clip((x * (a * x + b)) / (x * (c * x + d) + e), 0, 1)
    return y ** (1 / gamma)


def auto_exposure(x, key=0.18):
    lum = 0.2126 * x[..., 0] + 0.7152 * x[..., 1] + 0.0722 * x[..., 2]
    return key / max(1e-4, float(np.exp(np.mean(np.log(lum + 1e-4)))))
