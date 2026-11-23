"""Senses for checking your own work: look at a whole video at once, and "hear" a sound as numbers and pictures.

review_video turns a clip into a contact sheet (evenly spaced frames with timestamps), a motion map (a heat map
of where things moved plus per-frame motion / jitter / brightness strips with cuts, black and frozen stretches
marked) and a .json report: motion energy, scene cuts, black and frozen frames, flicker, brightness, contrast,
saturation, sharpness, letterbox bars and a jitter score (motion that is undone on the next frame).

describe_audio writes a spectrogram, an annotated waveform and a .json report with a plain-language summary:
loudness, brightness, harshness, bass weight, dynamic range, noise floor, tempo and beat regularity, voice pitch
(and how flat it is, a "robotic" warning), rough speech / music / silence shares, stereo width and clipping.

Everything here is signal heuristics, not perception: good at spotting problems (a frozen stretch, a jittery
limb, a clipped peak, a monotone voice), not a judge of taste. The reports say so.
"""

from __future__ import annotations

import json
import math
import re
import subprocess
import tempfile
import threading
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from . import storage
from .config import NO_WINDOW, get_settings
from .ops import (
    FONT,
    OpError,
    OpResult,
    _ffmpeg,
    _guarded,
    _src,
    _stem,
    media_info,
    operation,
)

HEURISTIC_NOTE = ("These numbers come from simple signal measurements (pixel differences, spectra, autocorrelation), "
                  "not from watching or listening; treat them as pointers to look or listen closer, not verdicts.")


def _np():
    try:
        import numpy as np
    except ImportError as e:
        raise OpError('Reviewing media needs numpy: pip install numpy (or pip install "builder[voice]")') from e
    return np


# ---------------------------------------------------------------- drawing helpers (numpy canvas + ffmpeg text)

def _esc(path: str) -> str:
    """Escape a file path for use inside an ffmpeg filter option."""
    return path.replace("\\", "/").replace(":", "\\:").replace("'", "\\'")


def _write_png(img, labels: list[tuple[int, int, str, int, str]], out: Path) -> None:
    """Encode an RGB uint8 array as PNG, burning in labels (x, y, text, size, color) with drawtext textfiles."""
    h, w = img.shape[:2]
    with tempfile.TemporaryDirectory() as td:
        raw = Path(td) / "img.rgb"
        raw.write_bytes(img.astype("uint8").tobytes())
        chain = []
        if labels and Path(FONT).exists():
            for i, (x, y, text, size, color) in enumerate(labels):
                tf = Path(td) / f"t{i}.txt"
                tf.write_text(text, encoding="utf-8")
                chain.append(f"drawtext=fontfile={_esc(FONT)}:textfile={_esc(str(tf))}:x={int(x)}:y={int(y)}"
                             f":fontsize={int(size)}:fontcolor={color}:expansion=none:box=1:boxcolor=black@0.55:boxborderw=2")
        _ffmpeg("-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{w}x{h}", "-i", str(raw),
                *(["-vf", ",".join(chain)] if chain else []), "-frames:v", "1", str(out))


def _resize(np, img, h: int, w: int):
    """Bilinear resize of a 2-D or 3-D float array."""
    H, W = img.shape[:2]
    ys = np.linspace(0, H - 1, h)
    xs = np.linspace(0, W - 1, w)
    y0 = np.floor(ys).astype(int)
    x0 = np.floor(xs).astype(int)
    y1 = np.minimum(y0 + 1, H - 1)
    x1 = np.minimum(x0 + 1, W - 1)
    wy = (ys - y0)[:, None]
    wx = (xs - x0)[None, :]
    if img.ndim == 3:
        wy, wx = wy[..., None], wx[..., None]
    top = img[y0][:, x0] * (1 - wx) + img[y0][:, x1] * wx
    bot = img[y1][:, x0] * (1 - wx) + img[y1][:, x1] * wx
    return top * (1 - wy) + bot * wy


def _heat_colors(np, v):
    """0..1 -> black, purple, red, orange, yellow, white (an inferno-like ramp)."""
    stops = np.array([0, 0.2, 0.45, 0.7, 0.9, 1.0])
    cols = np.array([[0, 0, 0], [70, 10, 110], [200, 30, 40], [250, 130, 20], [250, 230, 60], [255, 255, 255]], float)
    v = np.clip(v, 0, 1)
    return np.stack([np.interp(v, stops, cols[:, c]) for c in range(3)], axis=-1)


def _fmt_t(t: float) -> str:
    m, s = divmod(max(0.0, t), 60)
    return f"{int(m)}:{s:04.1f}"


def _tick_step(duration: float, max_ticks: int = 12) -> float:
    for step in (0.25, 0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 1800):
        if duration / step <= max_ticks:
            return step
    return 3600


def _plot_line(canvas, x0: int, y0: int, w: int, h: int, values, color, vmax: float = 1.0) -> None:
    """Draw a 2-px polyline of values (0..vmax) into canvas region (x0, y0, w, h), one value per column."""
    np = _np()
    n = len(values)
    if n == 0:
        return
    cols = np.interp(np.arange(w), np.linspace(0, w - 1, n), np.asarray(values, float)) if n > 1 else np.full(w, float(values[0]))
    ys = (y0 + h - 1 - np.clip(cols / (vmax or 1), 0, 1) * (h - 1)).astype(int)
    for i in range(w):
        a = ys[i]
        b = ys[i - 1] if i else a
        lo, hi = min(a, b), max(a, b)
        canvas[lo:hi + 2, x0 + i] = color


def _columns(np, values, w: int, how: str = "mean"):
    """Resample a per-frame series to w columns (mean or max of the frames under each column)."""
    values = np.asarray(values, float)
    n = len(values)
    if n == 0:
        return np.zeros(w)
    edges = np.linspace(0, n, w + 1)
    out = np.zeros(w)
    for i in range(w):
        a = int(edges[i])
        b = max(a + 1, int(math.ceil(edges[i + 1])))
        seg = values[a:min(b, n)]
        out[i] = (seg.max() if how == "max" else seg.mean()) if len(seg) else 0.0
    return out


# ---------------------------------------------------------------- review_video

class ReviewVideoParams(BaseModel):
    file_id: str = Field(description="The video to review")
    frames: int = Field(12, ge=4, le=36, description="Frames on the contact sheet (evenly spaced)")
    columns: int = Field(4, ge=1, le=8, description="Contact sheet columns")
    thumb_width: int = Field(320, ge=160, le=640, description="Width of each contact sheet frame (px)")
    max_analysis_fps: float = Field(30, ge=2, le=60, description="Analyse at most this many frames per second (lower = faster; jitter needs the real frame rate)")
    analysis_width: int = Field(160, ge=64, le=480, description="Frames are analysed at this small width (faster); sharpness uses the contact sheet frames")
    scene_threshold: float = Field(10, ge=1, le=100, description="Scene cut sensitivity (ffmpeg scdet score; lower finds more cuts)")


def _tile_size(W: int, H: int, thumb_width: int) -> tuple[int, int]:
    tw = thumb_width // 2 * 2
    th = max(16, int(round(tw * H / W / 2)) * 2)
    if th > tw * 2:  # very tall video: keep tiles a sane height
        th = tw * 2 // 2 * 2
        tw = max(16, int(round(th * W / H / 2)) * 2)
    return tw, th


def _spans(log: str, start_key: str, end_key: str, duration: float) -> list[dict[str, float]]:
    out: list[dict[str, float]] = []
    start = None
    for line in log.splitlines():
        m = re.search(start_key + r"\s*([0-9.]+)", line)
        if m:
            start = float(m.group(1))
        m = re.search(end_key + r"\s*([0-9.]+)", line)
        if m and start is not None:
            out.append({"start": round(start, 2), "end": round(float(m.group(1)), 2), "duration": round(float(m.group(1)) - start, 2)})
            start = None
    if start is not None:  # still frozen/black at the end of the file
        out.append({"start": round(start, 2), "end": round(duration, 2), "duration": round(duration - start, 2)})
    return out


def _analyse_video(src: Path, info: dict[str, Any], p: ReviewVideoParams) -> dict[str, Any]:
    np = _np()
    v = info.get("video") or {}
    W, H = int(v.get("width") or 0), int(v.get("height") or 0)
    if not W or not H:
        raise OpError("review_video couldn't read the picture size")
    duration = float(info.get("duration_s") or 0)
    src_fps = float(v.get("fps") or 25) or 25
    fps = min(src_fps, p.max_analysis_fps)
    if duration * fps > 9000:  # very long: keep it fast
        fps = max(1.0, 9000 / duration)
    aw = max(16, p.analysis_width // 2 * 2)
    ah = max(16, int(round(aw * H / W / 2)) * 2)
    vf = (([f"fps={fps:.4f}"] if fps < src_fps - 0.01 else []) +
          [f"scale={aw}:{ah}:flags=area", "blackdetect=d=0.1:pix_th=0.10", "freezedetect=n=-60dB:d=0.5",
           f"scdet=threshold={p.scene_threshold}", "format=rgb24"])
    # contact sheet frames come out of the same decode pass (seeking is slow on files with few keyframes):
    # pick the first frame in each of `frames` equal time slots, starting half a slot in
    tw, th = _tile_size(W, H, p.thumb_width)
    if duration > 0:
        slot = duration / p.frames
        t0 = slot / 2
        sel = (f"gte(t,{t0:.4f})*(isnan(prev_selected_t)+gt(floor((t-{t0:.4f})/{slot:.6f}),"
               f"floor((prev_selected_t-{t0:.4f})/{slot:.6f})))")
        times = [t0 + i * slot for i in range(p.frames)]
    else:
        sel = f"lt(n,{p.frames})"
        times = [0.0] * p.frames
    tmp = tempfile.TemporaryDirectory()
    thumbs_path = Path(tmp.name) / "thumbs.rgb"
    graph = (f"[0:v:0]split=2[a][b];[a]{','.join(vf)}[ao];"
             f"[b]select='{sel}',scale={tw}:{th}:flags=area,setsar=1,format=rgb24[bo]")
    cmd = [get_settings().ffmpeg, "-hide_banner", "-nostats", "-v", "info", "-i", str(src), "-filter_complex", graph,
           "-map", "[ao]", "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1",
           "-map", "[bo]", "-fps_mode", "passthrough", "-f", "rawvideo", "-pix_fmt", "rgb24", "-y", str(thumbs_path)]
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, **NO_WINDOW)
    except FileNotFoundError as e:
        tmp.cleanup()
        raise OpError("ffmpeg is not installed or not on PATH") from e
    err_chunks: list[bytes] = []
    reader = threading.Thread(target=lambda: err_chunks.append(proc.stderr.read()), daemon=True)  # type: ignore[union-attr]
    reader.start()
    size = aw * ah * 3
    luma_w = np.array([0.299, 0.587, 0.114], np.float32)
    bright, contrast, sat, motion, jitter = [], [], [], [], []
    heat = np.zeros((ah, aw), np.float64)
    mean_img = np.zeros((ah, aw, 3), np.float64)
    row_max = np.zeros(ah, np.float32)
    col_max = np.zeros(aw, np.float32)
    prev_y = prev_d = prev_z = None
    struct = []  # 1 - correlation of consecutive frames' normalised luma: a cut scores high even between similar colours
    thr = 4 / 255  # changes smaller than this are compression noise
    n = 0
    assert proc.stdout is not None
    try:
        while True:
            buf = proc.stdout.read(size)
            if len(buf) < size:
                break
            rgb = np.frombuffer(buf, np.uint8).reshape(ah, aw, 3).astype(np.float32) / 255
            y = rgb @ luma_w
            mx, mn = rgb.max(axis=2), rgb.min(axis=2)
            bright.append(float(y.mean()))
            contrast.append(float(y.std()))
            sat.append(float(np.where(mx > 0.08, (mx - mn) / np.maximum(mx, 1e-6), 0).mean()))
            mean_img += rgb
            z = (y - y.mean()) / (y.std() + 1e-3)
            struct.append(0.0 if prev_z is None else float(1 - (z * prev_z).mean()))
            prev_z = z
            row_max = np.maximum(row_max, y.max(axis=1))
            col_max = np.maximum(col_max, y.max(axis=0))
            if prev_y is None:
                motion.append(0.0)
                jitter.append(0.0)
            else:
                d = y - prev_y
                ad = np.abs(d)
                moved = ad * (ad > thr)  # ignore grain / compression noise
                heat += moved
                motion.append(float(moved.mean()))
                if prev_d is not None:
                    # back-and-forth: this frame undoes the previous frame's change at the same pixel
                    both = (ad > thr) & (np.abs(prev_d) > thr) & (np.sign(d) != np.sign(prev_d))
                    jitter.append(float(np.minimum(ad, np.abs(prev_d))[both].sum() / ad.size))
                else:
                    jitter.append(0.0)
                prev_d = d
            prev_y = y
            n += 1
    finally:
        proc.stdout.close()
        proc.wait()
        reader.join(timeout=30)
        raw = thumbs_path.read_bytes() if thumbs_path.exists() else b""
        tmp.cleanup()
    log = b"".join(err_chunks).decode(errors="replace")
    if n == 0:
        raise OpError("review_video couldn't decode any frames:\n" + "\n".join(log.strip().splitlines()[-6:]))
    tsize = tw * th * 3
    thumbs = [raw[i * tsize:(i + 1) * tsize] for i in range(min(p.frames, len(raw) // tsize))]
    a_dur = n / fps
    duration = duration or a_dur
    cuts = [{"time": round(float(t), 2), "score": round(float(s), 1), "by": "scdet"}
            for s, t in re.findall(r"lavfi\.scd\.score:\s*([0-9.]+),\s*lavfi\.scd\.time:\s*([0-9.]+)", log)]
    struct_a = np.array(struct)
    for i in range(1, n):  # structural cuts scdet missed (e.g. close-up to wide shot on the same backdrop)
        local = np.median(struct_a[max(1, i - int(fps)):i + int(fps) + 1])
        if struct_a[i] > max(0.15, 8 * local) and struct_a[i] == struct_a[max(1, i - 2):i + 3].max():
            t = i / fps
            if not any(abs(c["time"] - t) < 2.5 / fps for c in cuts):
                cuts.append({"time": round(t, 2), "score": round(float(struct_a[i]) * 100, 1), "by": "structure"})
    cuts.sort(key=lambda c: c["time"])
    black = [{"start": round(float(a), 2), "end": round(float(b), 2), "duration": round(float(c), 2)}
             for a, b, c in re.findall(r"black_start:\s*([0-9.]+)\s+black_end:\s*([0-9.]+)\s+black_duration:\s*([0-9.]+)", log)]
    freezes = _spans(log, r"freeze_start:", r"freeze_end:", a_dur)
    bright_a, motion_a, jitter_a = np.array(bright), np.array(motion), np.array(jitter)
    # flicker: frame-to-frame brightness wobble around a ~0.5 s moving average, ignoring frames next to cuts
    k = max(3, int(round(fps / 2)) | 1)
    pad = np.pad(bright_a, (k // 2, k // 2), mode="edge")
    smooth = np.convolve(pad, np.ones(k) / k, mode="valid")[:n]
    resid = np.abs(bright_a - smooth) * 255
    near_cut = np.zeros(n, bool)
    for c in cuts:
        i = int(round(c["time"] * fps))
        near_cut[max(0, i - k):i + k + 1] = True
    resid[near_cut] = 0
    secs = max(1, int(math.ceil(n / fps - 1e-6)))

    def per_sec(arr, scale=1.0, nd=3):
        return [round(float(arr[int(s * fps):max(int(s * fps) + 1, int((s + 1) * fps))].mean() * scale), nd) for s in range(secs)]

    motion_ps = per_sec(motion_a, 100)
    jitter_ps = per_sec(jitter_a, 100)
    jit_ratio_ps = [round(min(1.0, j / m), 2) if m > 0.05 else 0.0 for j, m in zip(jitter_ps, motion_ps)]
    flicker_ps = per_sec(resid, 1, 2)
    # letterbox / pillarbox: rows or columns that stay dark in every frame
    dark = 24 / 255
    top = int(np.argmax(row_max > dark)) if (row_max > dark).any() else 0
    bottom = int(np.argmax(row_max[::-1] > dark)) if (row_max > dark).any() else 0
    left = int(np.argmax(col_max > dark)) if (col_max > dark).any() else 0
    right = int(np.argmax(col_max[::-1] > dark)) if (col_max > dark).any() else 0
    sy, sx = H / ah, W / aw
    letterbox = {"top_px": int(top * sy), "bottom_px": int(bottom * sy), "left_px": int(left * sx), "right_px": int(right * sx)}
    letterbox["detected"] = bool(max(top, bottom) >= 0.04 * ah or max(left, right) >= 0.04 * aw)
    total_motion = float(motion_a.sum())
    return {
        "aw": aw, "ah": ah, "fps": fps, "frames": n, "heat": heat, "mean_img": mean_img / n,
        "thumbs": thumbs, "times": times, "tw": tw, "th": th,
        "motion": motion_a, "jitter": jitter_a, "bright": bright_a, "contrast": np.array(contrast), "sat": np.array(sat),
        "report": {
            "analysis": {"fps": round(fps, 3), "width": aw, "height": ah, "frames": n},
            "motion": {"mean_pct": round(float(motion_a[1:].mean() * 100) if n > 1 else 0.0, 3),
                       "peak_pct": round(float(motion_a.max() * 100), 3),
                       "per_second_pct": motion_ps,
                       "unit": "mean absolute luma change between analysed frames, % of full scale"},
            "jitter": {"ratio": round(float(jitter_a.sum() / total_motion), 3) if total_motion > 0 else 0.0,
                       "per_second_pct": jitter_ps, "per_second_ratio": jit_ratio_ps,
                       "meaning": "share of motion that is undone on the next frame (back-and-forth shaking); smooth motion ~0-0.2, jittery animation 0.4+"},
            "scene_cuts": cuts,
            "black": black,
            "frozen": freezes,
            "flicker": {"mean": round(float(resid.mean()), 3), "max_second": round(max(flicker_ps), 3), "per_second": flicker_ps,
                        "unit": "mean frame-to-frame brightness wobble in 8-bit luma levels (above ~1.5 is visible flicker)"},
            "brightness": {"mean": round(float(bright_a.mean()), 3), "per_second": per_sec(bright_a)},
            "contrast": {"mean": round(float(np.mean(contrast)), 3), "per_second": per_sec(np.array(contrast))},
            "saturation": {"mean": round(float(np.mean(sat)), 3), "per_second": per_sec(np.array(sat))},
            "letterbox": letterbox,
        },
    }


def _contact_sheet(np, a: dict[str, Any], info: dict[str, Any], p: ReviewVideoParams, name: str, duration: float) -> tuple[Any, list, list[dict[str, Any]]]:
    v = info["video"]
    W, H = int(v["width"]), int(v["height"])
    tw, th, times = a["tw"], a["th"], a["times"]
    raws = a["thumbs"] + [b""] * (p.frames - len(a["thumbs"]))
    cols = min(p.columns, p.frames)
    rows = int(math.ceil(p.frames / cols))
    gap, head = 6, 30
    sheet = np.full((head + rows * (th + gap) + gap, cols * (tw + gap) + gap, 3), 24, np.uint8)
    labels = [(gap, 8, f"{name}  ·  {W}x{H}  ·  {duration:.2f} s  ·  {p.frames} frames, time at top left", 15, "white")]
    samples = []
    for i, (t, raw) in enumerate(zip(times, raws)):
        r, c = divmod(i, cols)
        x, y = gap + c * (tw + gap), head + r * (th + gap)
        if len(raw) == tw * th * 3:
            img = np.frombuffer(raw, np.uint8).reshape(th, tw, 3)
            sheet[y:y + th, x:x + tw] = img
            g = img.astype(np.float32) @ np.array([0.299, 0.587, 0.114], np.float32)
            lap = (-4 * g[1:-1, 1:-1] + g[:-2, 1:-1] + g[2:, 1:-1] + g[1:-1, :-2] + g[1:-1, 2:])
            sharp = float(lap.var())
            samples.append({"time": round(t, 2), "sharpness": round(sharp, 1)})
            labels.append((x + 4, y + 4, _fmt_t(t), 14, "white"))
        else:
            sheet[y:y + th, x:x + tw] = 60
            labels.append((x + 4, y + 4, f"{_fmt_t(t)} (no frame)", 14, "red"))
    return sheet, labels, samples


def _motion_map(np, a: dict[str, Any], report: dict[str, Any], name: str, duration: float):
    W = 800
    m = 14
    aw, ah = a["aw"], a["ah"]
    ph = int(round((W - 2 * m) * ah / aw))
    pw = W - 2 * m
    if ph > 420:
        ph = 420
        pw = int(round(ph * aw / ah))
    heat = a["heat"]
    hv = heat / (np.percentile(heat, 99.5) or heat.max() or 1)
    hv = np.clip(hv, 0, 1) ** 0.6
    base = a["mean_img"] @ np.array([0.299, 0.587, 0.114])
    bg = np.repeat((base * 0.45 * 255)[..., None], 3, axis=2)
    hc = _heat_colors(np, hv)
    alpha = np.clip(hv * 1.6, 0, 1)[..., None]
    top_img = _resize(np, bg * (1 - alpha) + hc * alpha, ph, pw)
    title_h, strip_h, bstrip_h, axis_h, gap = 30, 150, 90, 24, 26
    Hc = title_h + ph + gap + strip_h + gap + bstrip_h + axis_h + 8
    can = np.full((Hc, W, 3), 24, np.uint8)
    x_img = (W - pw) // 2
    can[title_h:title_h + ph, x_img:x_img + pw] = np.clip(top_img, 0, 255).astype(np.uint8)
    labels = [(m, 8, f"Motion map · {name} · brighter = more movement (backdrop: average frame)", 13, "white")]
    pxw = W - 2 * m
    # motion strip (per analysed frame): cyan = motion, orange = back-and-forth (jitter) part of it
    y1 = title_h + ph + gap
    can[y1:y1 + strip_h, m:m + pxw] = 36
    mot = _columns(np, a["motion"] * 100, pxw, "max")
    jit = _columns(np, a["jitter"] * 100, pxw, "max")
    vmax = max(0.5, float(np.percentile(a["motion"] * 100, 99)) * 1.1 if len(a["motion"]) else 1)
    for f in report["frozen"]:
        can[y1:y1 + strip_h, m + int(f["start"] / duration * pxw):m + max(1, int(f["end"] / duration * pxw))] = (40, 60, 110)
    for b in report["black"]:
        can[y1:y1 + strip_h, m + int(b["start"] / duration * pxw):m + max(1, int(b["end"] / duration * pxw))] = (90, 30, 30)
    for i in range(pxw):
        hm = int(min(1, mot[i] / vmax) * (strip_h - 2))
        hj = int(min(1, jit[i] / vmax) * (strip_h - 2))
        if hm:
            can[y1 + strip_h - hm:y1 + strip_h, m + i] = (40, 200, 230)
        if hj:
            can[y1 + strip_h - hj:y1 + strip_h, m + i] = (255, 140, 30)
    labels.append((m, y1 - 20, f"Motion per frame (cyan, top {vmax:.2f}%) · jitter (orange) · frozen (blue) · black (dark red) · cuts (red)", 12, "white"))
    # brightness strip
    y2 = y1 + strip_h + gap
    can[y2:y2 + bstrip_h, m:m + pxw] = 36
    _plot_line(can, m, y2, pxw, bstrip_h, _columns(np, a["sat"], pxw), (230, 90, 200))
    _plot_line(can, m, y2, pxw, bstrip_h, _columns(np, a["contrast"] * 2, pxw), (140, 140, 140))
    _plot_line(can, m, y2, pxw, bstrip_h, _columns(np, a["bright"], pxw), (250, 250, 250))
    labels.append((m, y2 - 20, "Brightness (white)  ·  contrast x2 (grey)  ·  saturation (pink), scale 0-1", 12, "white"))
    for c in report["scene_cuts"]:
        x = m + min(pxw - 1, int(c["time"] / duration * pxw))
        can[y1:y1 + strip_h, x:x + 2] = (240, 40, 40)
        can[y2:y2 + bstrip_h, x:x + 2] = (240, 40, 40)
    # time axis
    step = _tick_step(duration)
    t = 0.0
    ya = y2 + bstrip_h
    while t <= duration + 1e-6:
        x = m + min(pxw - 1, int(t / duration * pxw))
        can[ya:ya + 5, x] = 200
        labels.append((min(W - 40, max(2, x - 10)), ya + 7, f"{t:g}s", 11, "white"))
        t += step
    return can, labels


def _soft_frames(np, samples: list[dict[str, Any]], cuts: list[dict[str, Any]]) -> list[float]:
    """Frames far softer than the other samples of the same shot (sharpness depends on content, so compare like with like)."""
    edges = [c["time"] for c in cuts]
    shot = [sum(s["time"] >= e for e in edges) for s in samples]
    out = []
    for s, k in zip(samples, shot):
        same = [o["sharpness"] for o, kk in zip(samples, shot) if kk == k]
        if len(same) >= 3 and s["sharpness"] < 0.25 * float(np.median(same)):
            out.append(s["time"])
    return out


@operation("review_video", ReviewVideoParams, "Look at a whole video at once to check your work: a contact sheet (evenly spaced frames with timestamps), a motion map (heat map of where things moved + per-frame motion/jitter and brightness strips with cuts, black and frozen stretches marked), and a .json report with per-second motion energy, scene cuts, black/frozen frames, flicker, brightness/contrast/saturation, sharpness, letterbox bars and a jitter score (back-and-forth shaking, e.g. jittery animation). Heuristics; read-only on the input.")
def review_video(p: ReviewVideoParams) -> OpResult:
    np = _np()
    src = _src(p.file_id)
    info = media_info(p.file_id)
    if not info["has_video"]:
        raise OpError("review_video needs a video")
    name = storage.get(p.file_id).name
    a = _analyse_video(src, info, p)
    report: dict[str, Any] = {"file": name, "width": info["video"]["width"], "height": info["video"]["height"],
                              "duration_s": round(float(info.get("duration_s") or a["frames"] / a["fps"]), 3),
                              "fps": info["video"].get("fps"), **a["report"]}
    duration = report["duration_s"] or 1.0
    sheet, sheet_labels, samples = _contact_sheet(np, a, info, p, name, duration)
    sharp = [s["sharpness"] for s in samples]
    med = float(np.median(sharp)) if sharp else 0.0
    report["sharpness"] = {"median": round(med, 1), "samples": samples,
                           "unit": f"variance of the Laplacian on {p.thumb_width}px-wide frames (higher = crisper; under ~30 looks soft, compare within a project)",
                           "soft_frames": _soft_frames(np, samples, report["scene_cuts"])}
    issues: list[str] = []
    mo, ji, fl = report["motion"], report["jitter"], report["flicker"]
    if mo["mean_pct"] < 0.05:
        issues.append("Almost no motion: the picture is (nearly) still the whole time")
    for b in report["black"]:
        issues.append(f"Black from {b['start']} s to {b['end']} s")
    for f in report["frozen"]:
        if f["duration"] >= 0.5 and mo["mean_pct"] >= 0.05:
            issues.append(f"Frozen picture from {f['start']} s to {f['end']} s ({f['duration']} s)")
    jit_secs = [i for i, (r, mp) in enumerate(zip(ji["per_second_ratio"], mo["per_second_pct"])) if r >= 0.35 and mp >= 0.1]
    if jit_secs:
        issues.append(f"Back-and-forth jitter in second(s) {', '.join(str(s) for s in jit_secs[:20])} (look for shaking limbs, popping or a wobbling camera)")
    flick = [i for i, v in enumerate(fl["per_second"]) if v > 1.5]
    if flick:
        issues.append(f"Brightness flicker in second(s) {', '.join(str(s) for s in flick[:20])}")
    if report["brightness"]["mean"] < 0.12:
        issues.append("Very dark overall")
    elif report["brightness"]["mean"] > 0.88:
        issues.append("Very bright overall (possibly blown out)")
    if report["contrast"]["mean"] < 0.06 and report["brightness"]["mean"] >= 0.12:
        issues.append("Very low contrast (flat, washed-out picture)")
    if med and med < 30:
        issues.append(f"Picture looks soft (median sharpness {med:.0f})")
    if report["sharpness"]["soft_frames"]:
        issues.append(f"Noticeably softer frames at {', '.join(str(t) for t in report['sharpness']['soft_frames'])} s")
    lb = report["letterbox"]
    if lb["detected"]:
        issues.append(f"Dark bars: top {lb['top_px']} / bottom {lb['bottom_px']} / left {lb['left_px']} / right {lb['right_px']} px")
    report["issues"] = issues
    report["ok"] = not issues
    report["note"] = HEURISTIC_NOTE
    stem = _stem(p.file_id)
    can, labels = _motion_map(np, a, report, name, duration)
    outputs = [
        _guarded("review_video", [p.file_id], f"{stem}_contact_sheet.png", lambda out: _write_png(sheet, sheet_labels, out)).outputs[0],
        _guarded("review_video", [p.file_id], f"{stem}_motion_map.png", lambda out: _write_png(can, labels, out)).outputs[0],
    ]
    report["contact_sheet_id"], report["motion_map_id"] = outputs[0].id, outputs[1].id
    res = _guarded("review_video", [p.file_id], f"{stem}_review.json",
                   lambda out: out.write_text(json.dumps(report, indent=1), encoding="utf-8"), data=report)
    return OpResult(outputs=outputs + res.outputs, data=report)


# ---------------------------------------------------------------- describe_audio

SR_A = 22050
N_FFT, HOP = 2048, 512


class DescribeAudioParams(BaseModel):
    file_id: str = Field(description="A sound file, or a video (its first audio track is described)")
    max_seconds: float = Field(600, ge=5, le=3600, description="Analyse at most this many seconds from the start")


def _decode(src: Path, seconds: float, channels: int):
    np = _np()
    cmd = [get_settings().ffmpeg, "-v", "error", "-i", str(src), "-map", "0:a:0", "-t", f"{seconds}", "-f", "f32le",
           "-acodec", "pcm_f32le", "-ac", str(channels), "-ar", str(SR_A), "-"]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=1800, **NO_WINDOW)
    except FileNotFoundError as e:
        raise OpError("ffmpeg is not installed or not on PATH") from e
    if proc.returncode != 0:
        raise OpError("Couldn't read the audio:\n" + "\n".join(proc.stderr.decode(errors="replace").strip().splitlines()[-6:]))
    x = np.frombuffer(proc.stdout[: len(proc.stdout) // (4 * channels) * 4 * channels], np.float32)
    return x.reshape(-1, channels)


def _spectral(np, mono):
    """Per-STFT-frame features: power, centroid, band shares, flatness, onset flux (on log magnitude)."""
    n = max(1, 1 + (len(mono) - N_FFT) // HOP) if len(mono) >= N_FFT else 1
    x = np.pad(mono, (0, max(0, N_FFT - len(mono))))
    win = np.hanning(N_FFT).astype(np.float32)
    freqs = np.fft.rfftfreq(N_FFT, 1 / SR_A)
    bands = {"sub_bass": freqs < 150, "low_mid": (freqs >= 150) & (freqs < 2000),
             "presence": (freqs >= 2000) & (freqs < 5000), "air": freqs >= 5000}
    out = {k: np.zeros(n) for k in ("power", "centroid", "flatness", "flux", "sub_bass", "low_mid", "presence", "air")}
    mean_spec = np.zeros(len(freqs))
    prev = None
    for b0 in range(0, n, 1024):
        idx = np.arange(b0, min(n, b0 + 1024))
        frames = x[idx[:, None] * HOP + np.arange(N_FFT)[None, :]] * win
        mag = np.abs(np.fft.rfft(frames, axis=1)).astype(np.float64)
        pw = mag ** 2
        tot = pw.sum(axis=1) + 1e-20
        out["power"][idx] = tot
        out["centroid"][idx] = (pw * freqs).sum(axis=1) / tot
        for k, sel in bands.items():
            out[k][idx] = pw[:, sel].sum(axis=1) / tot
        lp = np.log(pw[:, 1:] + 1e-12)
        out["flatness"][idx] = np.exp(lp.mean(axis=1)) / (pw[:, 1:].mean(axis=1) + 1e-20)
        lm = np.log1p(100 * mag)
        full = lm if prev is None else np.vstack([prev[None, :], lm])
        fl = np.maximum(0, np.diff(full, axis=0)).sum(axis=1)
        out["flux"][idx] = np.concatenate([[0.0], fl]) if prev is None else fl
        prev = lm[-1]
        mean_spec += pw.sum(axis=0)
    out["mean_spec"] = mean_spec / n
    out["freqs"] = freqs
    return out


def _tempo(np, flux, fps: float) -> dict[str, Any]:
    """Tempo from the autocorrelation of the onset envelope, with a gentle preference for ~120 BPM."""
    if len(flux) < fps * 4:
        return {"bpm": None, "regularity": 0.0}
    k = max(3, int(fps * 0.5))
    env = flux - np.convolve(flux, np.ones(k) / k, mode="same")
    env = np.maximum(env, 0)
    env = env - env.mean()
    if not env.any():
        return {"bpm": None, "regularity": 0.0}
    n = len(env)
    spec = np.fft.rfft(env, 2 * n)
    ac = np.fft.irfft(np.abs(spec) ** 2)[:n]
    ac = ac / (ac[0] or 1)
    lo, hi = int(fps * 60 / 220), min(n - 2, int(fps * 60 / 45))
    if hi <= lo + 2:
        return {"bpm": None, "regularity": 0.0}
    lags = np.arange(lo, hi + 1)
    bpms = 60 * fps / lags
    prior = np.exp(-0.5 * (np.log2(bpms / 120) / 1.0) ** 2)
    # a beat period also shows up at 2x and 3x the lag: add those so the true period beats its harmonics
    score = ac[lags] + 0.5 * ac[np.minimum(2 * lags, n - 1)] + 0.33 * ac[np.minimum(3 * lags, n - 1)]
    best = int(lags[np.argmax(score * prior)])
    a, b, c = ac[best - 1], ac[best], ac[best + 1]
    shift = 0.5 * (a - c) / (a - 2 * b + c) if (a - 2 * b + c) != 0 else 0.0
    lag = best + max(-0.5, min(0.5, shift))
    return {"bpm": round(float(60 * fps / lag), 1), "regularity": round(float(max(0.0, b)), 3)}


def _pitch(np, mono, gate_db: float) -> dict[str, Any]:
    """YIN-style F0 on 23 ms windows every ~12 ms (60-1000 Hz). Returns per-frame f0 (0 = unvoiced) and times."""
    Wn, hop = 512, 256
    tmin, tmax = int(SR_A / 1000), int(SR_A / 60)
    L = Wn + tmax + 1
    n = (len(mono) - L) // hop + 1
    if n <= 0:
        return {"f0": np.zeros(0), "hop_s": hop / SR_A}
    stride = max(1, n // 30000)
    starts = np.arange(0, n, stride) * hop
    f0 = np.zeros(len(starts))
    N = 2048
    for b0 in range(0, len(starts), 1500):
        st = starts[b0:b0 + 1500]
        fr = mono[st[:, None] + np.arange(L)[None, :]].astype(np.float64)
        e0 = (fr[:, :Wn] ** 2).sum(axis=1)
        rms_db = 10 * np.log10(e0 / Wn + 1e-20)
        live = rms_db > gate_db
        if not live.any():
            continue
        fr = fr[live]
        A = np.fft.rfft(fr[:, :Wn], N)
        B = np.fft.rfft(fr, N)
        r = np.fft.irfft(np.conj(A) * B, N)[:, :tmax + 1]
        cs = np.concatenate([np.zeros((len(fr), 1)), np.cumsum(fr ** 2, axis=1)], axis=1)
        taus = np.arange(tmax + 1)
        e_tau = cs[:, taus + Wn] - cs[:, taus]
        d = e_tau[:, :1] + e_tau - 2 * r
        d[:, 0] = 0
        cum = np.cumsum(d[:, 1:], axis=1)
        cm = np.ones_like(d)
        cm[:, 1:] = d[:, 1:] * taus[1:] / np.maximum(cum, 1e-12)
        seg = cm[:, tmin:tmax]
        below = seg < 0.15
        first = np.where(below.any(axis=1), below.argmax(axis=1), seg.argmin(axis=1))
        rows = np.arange(len(seg))
        for _ in range(60):  # walk down to the local minimum
            nxt = np.minimum(first + 1, seg.shape[1] - 1)
            move = seg[rows, nxt] < seg[rows, first]
            if not move.any():
                break
            first = np.where(move, nxt, first)
        val = seg[rows, first]
        t = first + tmin
        a = cm[rows, np.maximum(t - 1, 1)]
        b = cm[rows, t]
        c = cm[rows, np.minimum(t + 1, tmax)]
        den = a - 2 * b + c
        frac = np.where(np.abs(den) > 1e-9, 0.5 * (a - c) / np.where(den == 0, 1, den), 0)
        tau = t + np.clip(frac, -0.5, 0.5)
        f = np.where(val < 0.2, SR_A / tau, 0.0)
        out = np.zeros(len(st))
        out[live] = f
        f0[b0:b0 + 1500] = out
    return {"f0": f0, "hop_s": hop * stride / SR_A}


def _classify(np, frame_db, flat, cent, f0, f0_hop: float, seconds: float, silence_db: float):
    """Rough 1-second labels: silence, speech, music, or other (noise, ambience, effects)."""
    fps = SR_A / HOP
    labels = []
    for s in range(int(math.ceil(seconds))):
        a, b = int(s * fps), int((s + 1) * fps)
        dbs = frame_db[a:b]
        if not len(dbs):
            break
        lvl = 10 * np.log10(np.mean(10 ** (dbs / 10)) + 1e-20)
        if lvl < silence_db:
            labels.append("silence")
            continue
        loud = dbs > lvl - 25
        fa, fb = int(s / f0_hop), int((s + 1) / f0_hop)
        fw = f0[fa:fb]
        voiced = float((fw > 0).mean()) if len(fw) else 0.0
        # speech pitch glides all the time; notes in music hold still (or jump): share of small, non-zero pitch steps
        pair = (fw[1:] > 0) & (fw[:-1] > 0) if len(fw) > 1 else np.zeros(0, bool)
        steps = np.abs(12 * np.log2(np.where(pair, fw[1:], 1) / np.where(pair, fw[:-1], 1)))[pair] if pair.any() else np.zeros(0)
        glide = float(((steps > 0.04) & (steps < 1.5)).mean()) if len(steps) >= 5 else 0.0
        tonal = float(np.median(flat[a:b][loud])) if loud.any() else 1.0
        c = float(np.median(cent[a:b][loud])) if loud.any() else 0.0
        if 0.15 <= voiced <= 0.95 and glide >= 0.55 and c < 4000:
            labels.append("speech")
        elif tonal < 0.08 or voiced > 0.5:
            labels.append("music")
        else:
            labels.append("other")
    return labels


def _runs(labels: list[str]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for i, k in enumerate(labels):
        if out and out[-1]["kind"] == k:
            out[-1]["end"] = i + 1
        else:
            out.append({"kind": k, "start": i, "end": i + 1})
    return out


def _waveform_png(np, mono, frame_db, labels: list[str], f0, f0_hop: float, report: dict[str, Any], name: str):
    W, m = 1200, 14
    pw = W - 2 * m
    title_h, wave_h, band_h, pitch_h, axis_h, gap = 30, 200, 16, 100, 24, 24
    voiced = report["voice"]["voiced_share"] >= 0.05
    Hc = title_h + wave_h + 6 + band_h + (gap + pitch_h if voiced else 0) + axis_h + 6
    can = np.full((Hc, W, 3), 24, np.uint8)
    dur = len(mono) / SR_A or 1.0
    can[title_h:title_h + wave_h, m:m + pw] = 34
    mid = title_h + wave_h // 2
    edges = np.linspace(0, len(mono), pw + 1).astype(int)
    for i in range(pw):
        seg = mono[edges[i]:max(edges[i] + 1, edges[i + 1])]
        if not len(seg):
            continue
        pk = float(np.abs(seg).max())
        rm = float(np.sqrt(np.mean(seg.astype(np.float64) ** 2)))
        hp, hr = int(min(1, pk) * (wave_h // 2 - 2)), int(min(1, rm) * (wave_h // 2 - 2))
        can[mid - hp:mid + hp + 1, m + i] = (70, 120, 170)
        can[mid - hr:mid + hr + 1, m + i] = (130, 200, 250)
        if pk >= 0.999:
            can[title_h:title_h + 6, m + i] = (255, 40, 40)
    for level_db in (-6, -20):  # guide lines
        h = int(10 ** (level_db / 20) * (wave_h // 2 - 2))
        can[mid - h, m:m + pw:4] = 90
        can[mid + h, m:m + pw:4] = 90
    lab = [(m, 8, f"Waveform · {name} · peak (dark) and RMS (light), dotted lines -6/-20 dBFS, red ticks = clipping", 14, "white"),
           (m + 4, title_h + 4, f"loudness {report['loudness']['lufs']} LUFS", 12, "white")]
    yb = title_h + wave_h + 6
    colors = {"silence": (60, 60, 60), "speech": (70, 140, 255), "music": (60, 200, 110), "other": (220, 170, 60)}
    for i, k in enumerate(labels):
        can[yb:yb + band_h, m + int(i / dur * pw):m + min(pw, int((i + 1) / dur * pw))] = colors[k]
    lab.append((W - 480, title_h + 4, "band below: speech blue · music green · other yellow · silence grey", 11, "white"))
    y = yb + band_h
    if voiced:
        y += gap
        can[y:y + pitch_h, m:m + pw] = 34
        lo, hi = math.log2(60), math.log2(1000)
        for hz in (100, 200, 400, 800):
            yy = y + pitch_h - 1 - int((math.log2(hz) - lo) / (hi - lo) * (pitch_h - 1))
            can[yy, m:m + pw:4] = 80
            lab.append((m + 2, yy - 12, f"{hz} Hz", 10, "gray"))
        for j, f in enumerate(f0):
            if f > 0:
                x = m + min(pw - 1, int(j * f0_hop / dur * pw))
                yy = y + pitch_h - 1 - int((math.log2(f) - lo) / (hi - lo) * (pitch_h - 1))
                can[max(y, yy - 1):min(y + pitch_h, yy + 2), x:x + 2] = (255, 200, 80)
        lab.append((m, y - 20, "Pitch track of voiced (periodic) sound, log scale 60-1000 Hz", 12, "white"))
        y += pitch_h
    step = _tick_step(dur, 16)
    t = 0.0
    while t <= dur + 1e-6:
        x = m + min(pw - 1, int(t / dur * pw))
        can[y:y + 5, x] = 200
        lab.append((min(W - 40, max(2, x - 10)), y + 7, f"{t:g}s", 11, "white"))
        t += step
    return can, lab


def _describe(r: dict[str, Any]) -> str:
    parts: list[str] = []
    mix = r["content"]
    main = max(mix, key=mix.get)
    words = []
    if r["spectrum"]["bass_share"] > 0.45:
        words.append("bass-heavy")
    elif r["spectrum"]["bass_share"] < 0.05 and main != "speech":
        words.append("light on bass")
    c = r["spectrum"]["centroid_hz"]
    words.insert(0, "dark/warm" if c < 900 else "balanced" if c < 2500 else "bright")
    noun = {"speech": "speech", "music": "music", "other": "sound (noise, ambience or effects)", "silence": "near-silence"}[main]
    s = f"Mostly {noun} ({mix[main] * 100:.0f}%), {', '.join(words)}"
    others = [f"{k} {v * 100:.0f}%" for k, v in mix.items() if k != main and v >= 0.1]
    if others:
        s += f", with {', '.join(others)}"
    t = r["rhythm"]
    if t["bpm"] and t["regularity"] >= 0.25 and main in ("music", "other"):
        s += (f"; about {t['bpm']:.0f} BPM with a {'steady' if t['regularity'] >= 0.5 else 'loose'} beat" if main == "music" else
              f"; a {'steady' if t['regularity'] >= 0.5 else 'loose'} repeating pulse about {t['bpm']:.0f} times a minute")
    elif main == "music":
        s += "; no clear steady beat"
    parts.append(s + ".")
    lv = r["loudness"]
    clip = r["clipping"]
    p2 = f"{lv['lufs']} LUFS integrated, true peak {lv['true_peak_db']} dBTP, " if lv["lufs"] is not None else "No measurable loudness, "
    p2 += "no clipping" if not clip["clip_events"] and not clip["clipped_samples"] else f"CLIPPING in {clip['clip_events']} place(s)"
    if lv["true_peak_db"] is not None and lv["true_peak_db"] > -1.0:
        p2 += " but the true peak is above -1 dBTP (may distort once encoded)"
    dr = r["dynamics"]["range_db"]
    p2 += f"; {'very compressed' if dr < 6 else 'moderate' if dr < 15 else 'wide'} dynamics ({dr} dB)."
    parts.append(p2)
    tone = []
    if r["spectrum"]["harshness"] > 0.3:
        tone.append("a lot of 2-5 kHz energy (may sound harsh)")
    if r["spectrum"]["air_share"] < 0.005 and main in ("music", "other"):
        tone.append("dull top end (little above 5 kHz)")
    elif r["spectrum"]["air_share"] > 0.25:
        tone.append("lots of hiss/air above 5 kHz")
    if r["noise_floor_dbfs"] > -50 and mix.get("silence", 0) < 0.05 and main == "speech":
        tone.append(f"the quietest moments sit around {r['noise_floor_dbfs']} dBFS (background noise or a music bed under the voice)")
    if tone:
        parts.append("Tone: " + "; ".join(tone) + ".")
    st = r["stereo"]
    if st["channels"] >= 2:
        parts.append(f"Stereo: {st['label']} (L/R correlation {st['correlation']}).")
    v = r["voice"]
    if v["median_f0_hz"] and mix.get("speech", 0) >= 0.2:
        parts.append(f"Voiced pitch around {v['median_f0_hz']:.0f} Hz, spread {v['pitch_spread_semitones']} semitones "
                     f"({v['robotic_risk']} monotone/robotic risk).")
    parts.append("(Heuristic measurements, not listening.)")
    return " ".join(parts)


@operation("describe_audio", DescribeAudioParams, "Describe a sound (or a video's sound) in words and numbers so you can judge it without hearing: loudness, brightness, harshness (2-5 kHz), bass weight, dynamic range, noise floor, tempo and beat regularity, voice pitch and monotone ('robotic') risk, rough speech/music/silence shares, stereo width, clipping; plus a spectrogram PNG, an annotated waveform PNG and a .json report. Heuristics.")
def describe_audio(p: DescribeAudioParams) -> OpResult:
    from . import sound_design

    np = _np()
    src = _src(p.file_id)
    info = media_info(p.file_id)
    if not info["has_audio"]:
        raise OpError("describe_audio needs a file with sound")
    a = info.get("audio") or {}
    ch = int(a.get("channels") or 2)
    sr_native = int(a.get("sample_rate") or 48000)
    name = storage.get(p.file_id).name
    st = _decode(src, p.max_seconds, 2 if ch >= 2 else 1)
    if not len(st):
        raise OpError("describe_audio found no audio samples")
    mono = st.mean(axis=1).astype(np.float32)
    dur = len(mono) / SR_A
    trim = ["-t", f"{p.max_seconds}"]
    loud = sound_design._loudness(["-i", str(src), "-map", "0:a:0", *trim])
    scan = sound_design._scan(src, ch, sr_native, -55, 0.5)
    sp = _spectral(np, mono)
    frame_db = 10 * np.log10(sp["power"] / (np.hanning(N_FFT) ** 2).sum() / N_FFT * 2 + 1e-20)
    # 50 ms RMS windows for dynamics and noise floor
    w = int(SR_A * 0.05)
    nw = len(mono) // w
    wdb = 10 * np.log10(np.mean(mono[: nw * w].astype(np.float64).reshape(nw, w) ** 2, axis=1) + 1e-20) if nw else np.array([-120.0])
    noise_floor = max(-120.0, float(np.percentile(wdb, 10)))  # -120 = digital silence
    act = wdb[wdb > max(-60, float(wdb.max()) - 45)]
    dyn = float(np.percentile(act, 95) - np.percentile(act, 10)) if len(act) > 4 else 0.0
    loud_frames = frame_db > max(-60, float(frame_db.max()) - 40)
    wts = sp["power"] * loud_frames
    tot = wts.sum() or 1.0

    def share(k):
        return round(float((sp[k] * wts).sum() / tot), 4)

    centroid = float((sp["centroid"] * wts).sum() / tot)
    fps = SR_A / HOP
    rhythm = _tempo(np, sp["flux"], fps)
    silence_db = max(-55.0, float(np.percentile(wdb, 99)) - 45)
    pt = _pitch(np, mono, max(-50.0, silence_db + 5))
    f0, f0_hop = pt["f0"], pt["hop_s"]
    labels = _classify(np, frame_db, sp["flatness"], sp["centroid"], f0, f0_hop, dur, silence_db)
    weights = [1.0] * len(labels)
    if labels and dur % 1 > 1e-6:
        weights[-1] = dur % 1  # the last window is only partly filled
    counts = {k: sum(w for lab, w in zip(labels, weights) if lab == k) / (sum(weights) or 1) for k in ("speech", "music", "other", "silence")}
    # the voice summary uses the seconds that sound like speech when there are any (so a music bed's bass notes don't count)
    in_speech = np.array([labels[min(len(labels) - 1, int(i * f0_hop))] == "speech" for i in range(len(f0))], bool) if labels else np.zeros(len(f0), bool)
    voiced_f0 = f0[(f0 > 0) & in_speech] if (f0[(f0 > 0) & in_speech]).size >= 20 else f0[f0 > 0]
    voice: dict[str, Any] = {"voiced_share": round(float((f0 > 0).mean()) if len(f0) else 0.0, 3),
                             "median_f0_hz": None, "f0_range_hz": None, "pitch_spread_semitones": None, "robotic_risk": "n/a"}
    if len(voiced_f0) >= 20:
        semis = 12 * np.log2(voiced_f0 / np.median(voiced_f0))
        spread = float(np.percentile(semis, 90) - np.percentile(semis, 10))
        voice.update({"median_f0_hz": round(float(np.median(voiced_f0)), 1),
                      "f0_range_hz": [round(float(np.percentile(voiced_f0, 10)), 1), round(float(np.percentile(voiced_f0, 90)), 1)],
                      "pitch_spread_semitones": round(spread, 2),
                      "pitch_std_semitones": round(float(np.std(semis)), 2),
                      "robotic_risk": "high" if spread < 2.0 else "moderate" if spread < 4.0 else "low",
                      "meaning": "spread = 10th-90th percentile pitch range of voiced frames; natural speech usually spans 4-12 semitones, flat TTS under ~2-3"})
    stereo: dict[str, Any] = {"channels": ch}
    if st.shape[1] >= 2:
        L, R = st[:, 0].astype(np.float64), st[:, 1].astype(np.float64)
        den = math.sqrt(float((L ** 2).sum() * (R ** 2).sum())) or 1.0
        corr = float((L * R).sum()) / den
        mid_, side = (L + R) / 2, (L - R) / 2
        width = math.sqrt(float((side ** 2).mean()) / (float((mid_ ** 2).mean()) or 1e-20))
        stereo.update({"correlation": round(corr, 3), "side_to_mid": round(width, 3),
                       "label": "mono (L = R)" if corr > 0.98 else "out of phase - may vanish in mono" if corr < -0.2
                       else "narrow" if width < 0.25 else "wide" if width > 0.7 else "normal width"})
    report: dict[str, Any] = {
        "file": name, "duration_s": round(dur, 3), "analysed_s": round(dur, 3),
        "loudness": {"lufs": loud["lufs"], "true_peak_db": loud["true_peak_db"], "lra": loud["lra"],
                     "rms_dbfs": scan["rms_dbfs"], "peak_dbfs": scan["peak_dbfs"]},
        "clipping": {"clipped_samples": scan["clipped_samples"], "clip_events": scan["clip_events"]},
        "spectrum": {"centroid_hz": round(centroid, 1), "brightness": "dark/warm" if centroid < 900 else "balanced" if centroid < 2500 else "bright",
                     "bass_share": share("sub_bass"), "low_mid_share": share("low_mid"), "harshness": share("presence"), "air_share": share("air"),
                     "flatness": round(float(np.median(sp["flatness"][loud_frames])) if loud_frames.any() else 1.0, 4),
                     "meaning": "shares of signal power: bass <150 Hz, low-mid 150-2k, harshness = 2-5 kHz, air >5 kHz; flatness ~0 = tonal, ~1 = noise"},
        "dynamics": {"range_db": round(dyn, 1), "crest_db": round((scan["peak_dbfs"] or 0) - (scan["rms_dbfs"] or 0), 1)},
        "noise_floor_dbfs": round(noise_floor, 1),
        "noise_floor_meaning": "level of the quietest 10% of 50 ms windows: the background noise when the file has pauses, otherwise just its quietest passages",
        "rhythm": {**rhythm, "meaning": "regularity = autocorrelation of the onset envelope at the beat period (0 none .. 1 metronome); bpm may be half/double the felt tempo"},
        "voice": voice,
        "content": {k: round(v, 3) for k, v in counts.items()},
        "timeline": _runs(labels),
        "stereo": stereo,
    }
    report["description"] = _describe(report)
    report["note"] = HEURISTIC_NOTE
    stem = _stem(p.file_id)
    wave, wlab = _waveform_png(np, mono, frame_db, labels, f0, f0_hop, report, name)
    spec_graph = (f"[0:a]atrim=0:{p.max_seconds},showspectrumpic=s=1024x400:legend=1:fscale=log:scale=log:"
                  f"color=intensity:start=20:stop={min(sr_native // 2, 11000)}:drange=100")
    outputs = [
        _guarded("describe_audio", [p.file_id], f"{stem}_spectrogram.png",
                 lambda out: _ffmpeg("-i", str(src), "-filter_complex", spec_graph, "-frames:v", "1", str(out))).outputs[0],
        _guarded("describe_audio", [p.file_id], f"{stem}_waveform.png", lambda out: _write_png(wave, wlab, out)).outputs[0],
    ]
    report["spectrogram_id"], report["waveform_id"] = outputs[0].id, outputs[1].id
    res = _guarded("describe_audio", [p.file_id], f"{stem}_audio_review.json",
                   lambda out: out.write_text(json.dumps(report, indent=1), encoding="utf-8"), data=report)
    return OpResult(outputs=outputs + res.outputs, data=report)
