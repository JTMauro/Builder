"""Video finishing: film looks, upscaling and smoother motion.

- film_look: colour grade presets (film, warm, cool, noir, vintage, bleach) with grain and vignette,
  or your own .cube LUT (e.g. from DaVinci Resolve or a free LUT pack).
- upscale: 2x/4x. Uses Real-ESRGAN (realesrgan-ncnn-vulkan, BSD-3, needs a GPU) when it is installed,
  else a sharp Lanczos upscale with ffmpeg.
- smooth_motion: higher frame rate or slow motion by creating in-between frames. Uses RIFE
  (rife-ncnn-vulkan, MIT, needs a GPU) when installed, else ffmpeg's motion interpolation (slower).

Set REALESRGAN_BIN / RIFE_BIN in builder.env if the tools aren't on PATH.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from .config import NO_WINDOW
from .ops import AUDIO_ENC, VIDEO_ENC, OpError, OpResult, _ffmpeg, _guarded, _src, _stem, media_info, operation

LOOKS = {
    "film": "colorbalance=rs=-0.05:gs=-0.02:bs=0.06:rh=0.06:gh=0.02:bh=-0.05,curves=master='0/0.03 0.25/0.22 0.75/0.8 1/0.97',eq=saturation=0.92",
    "warm": "colortemperature=temperature=5200:mix=0.6,eq=saturation=1.05:contrast=1.04",
    "cool": "colortemperature=temperature=7800:mix=0.6,eq=contrast=1.05",
    "noir": "hue=s=0,curves=master='0/0 0.3/0.2 0.7/0.82 1/1',eq=contrast=1.12",
    "vintage": "curves=preset=vintage,eq=saturation=0.85",
    "bleach": "eq=saturation=0.55:contrast=1.18,curves=master='0/0.02 0.5/0.52 1/0.98'",
    "none": "null",
}


class FilmLookParams(BaseModel):
    file_id: str
    look: Literal["film", "warm", "cool", "noir", "vintage", "bleach", "none"] = Field("film", description="film: teal shadows, warm highlights, soft contrast")
    lut_id: str | None = Field(None, description="Optional .cube LUT file id, applied instead of the preset")
    grain: float = Field(0.35, ge=0, le=1, description="Film grain amount (0 = none)")
    vignette: bool = Field(True, description="Darken the corners slightly")
    letterbox: bool = Field(False, description="Add 2.39:1 cinema bars")


@operation("film_look", FilmLookParams, "Give a video a finished film look: colour grade preset or your own .cube LUT, film grain, vignette and optional cinema bars.")
def film_look(p: FilmLookParams) -> OpResult:
    src = _src(p.file_id)
    info = media_info(p.file_id)
    if not info["has_video"]:
        raise OpError("film_look needs a video")
    chain = []
    if p.lut_id:
        lut = _src(p.lut_id)
        if lut.suffix.lower() != ".cube":
            raise OpError("lut_id must be a .cube file")
        s = str(lut).replace("\\", "/").replace(":", "\\:").replace("'", "\\'")
        chain.append(f"lut3d=file='{s}'")
    else:
        chain.append(LOOKS[p.look])
    if p.grain > 0:
        chain.append(f"noise=alls={int(4 + p.grain * 14)}:allf=t")
    if p.vignette:
        chain.append("vignette=angle=PI/5")
    if p.letterbox:
        chain.append("drawbox=x=0:y=0:w=iw:h=(ih-iw/2.39)/2:color=black:t=fill,drawbox=x=0:y=ih-(ih-iw/2.39)/2:w=iw:h=(ih-iw/2.39)/2:color=black:t=fill")
    chain.append("format=yuv420p")
    audio = ["-c:a", "copy"] if info["has_audio"] else []
    return _guarded("film_look", [p.file_id] + ([p.lut_id] if p.lut_id else []), f"{_stem(p.file_id)}_{p.look if not p.lut_id else 'lut'}.mp4",
                    lambda out: _ffmpeg("-i", str(src), "-vf", ",".join(chain), *VIDEO_ENC, *audio, str(out)))


def _tool(env: str, names: list[str]) -> str | None:
    p = os.environ.get(env, "").strip().strip('"')
    if p and Path(p).exists():
        return p
    for n in names:
        found = shutil.which(n)
        if found:
            return found
    return None


def _run_tool(args: list[str]) -> None:
    proc = subprocess.run(args, capture_output=True, text=True, **NO_WINDOW)
    if proc.returncode != 0:
        raise OpError(f"{Path(args[0]).name} failed:\n" + "\n".join((proc.stderr or proc.stdout).strip().splitlines()[-8:]))


def _frame_tool(file_id: str, tool_args, fps: float, out_name: str, op: str, data: dict) -> OpResult:
    """Explode to PNG frames, run an image/frame tool folder-to-folder, put the video back together with the audio."""
    src = _src(file_id)
    info = media_info(file_id)
    work = Path(tempfile.mkdtemp(prefix=f"{op}-"))
    try:
        a, b = work / "in", work / "out"
        a.mkdir(), b.mkdir()
        _ffmpeg("-i", str(src), "-vsync", "0", str(a / "%08d.png"))
        _run_tool(tool_args(a, b))
        audio = ["-i", str(src), "-map", "0:v", "-map", "1:a?", *AUDIO_ENC, "-shortest"] if info["has_audio"] else []
        return _guarded(op, [file_id], out_name,
                        lambda out: _ffmpeg("-framerate", f"{fps}", "-i", str(b / "%08d.png"), *audio, *VIDEO_ENC, str(out)), data=data)
    finally:
        shutil.rmtree(work, ignore_errors=True)


class UpscaleParams(BaseModel):
    file_id: str = Field(description="Video or image")
    factor: Literal[2, 4] = 2
    content: Literal["realistic", "anime"] = Field("realistic", description="Real-ESRGAN model to use when it's available")


@operation("upscale", UpscaleParams, "Upscale a video or image 2x or 4x (e.g. 1080p to 4K). Uses AI upscaling (Real-ESRGAN) when installed on this computer, otherwise a sharp standard upscale.")
def upscale(p: UpscaleParams) -> OpResult:
    src = _src(p.file_id)
    info = media_info(p.file_id)
    tool = _tool("REALESRGAN_BIN", ["realesrgan-ncnn-vulkan", "realesrgan-ncnn-vulkan.exe"])
    model = "realesr-animevideov3" if p.content == "anime" else "realesrgan-x4plus"
    is_image = not info.get("duration_s") or (info["has_video"] and (info["video"] or {}).get("fps") in (None, 0)) or src.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp")
    if is_image:
        name = f"{_stem(p.file_id)}_x{p.factor}.png"
        if tool:
            return _guarded("upscale", [p.file_id], name, lambda out: _run_tool([tool, "-i", str(src), "-o", str(out), "-s", str(p.factor), "-n", model]),
                            data={"engine": "Real-ESRGAN"})
        return _guarded("upscale", [p.file_id], name,
                        lambda out: _ffmpeg("-i", str(src), "-vf", f"scale=iw*{p.factor}:ih*{p.factor}:flags=lanczos,unsharp=5:5:0.5", str(out)),
                        data={"engine": "lanczos (install Real-ESRGAN for AI upscaling)"})
    if not info["has_video"]:
        raise OpError("upscale needs a video or an image")
    fps = (info["video"] or {}).get("fps") or 30
    name = f"{_stem(p.file_id)}_x{p.factor}.mp4"
    if tool:
        return _frame_tool(p.file_id, lambda a, b: [tool, "-i", str(a), "-o", str(b), "-s", str(p.factor), "-n", model, "-f", "png"],
                           fps, name, "upscale", {"engine": "Real-ESRGAN"})
    audio = ["-c:a", "copy"] if info["has_audio"] else []
    return _guarded("upscale", [p.file_id], name,
                    lambda out: _ffmpeg("-i", str(src), "-vf", f"scale=iw*{p.factor}:ih*{p.factor}:flags=lanczos,unsharp=5:5:0.5,format=yuv420p",
                                        *VIDEO_ENC, *audio, str(out)),
                    data={"engine": "lanczos (install Real-ESRGAN for AI upscaling)"})


class SmoothParams(BaseModel):
    file_id: str
    fps: int = Field(60, ge=24, le=240, description="Frame rate of the result")
    slow_motion: float = Field(1.0, ge=1.0, le=8.0, description="Slow down by this factor (2 = half speed), filling in the frames between")


@operation("smooth_motion", SmoothParams, "Make motion smoother: raise the frame rate (e.g. 24 or 30 to 60 fps) or make slow motion by creating in-between frames. Uses RIFE when installed, otherwise ffmpeg motion interpolation (slower). Audio is kept at normal speed only when not slowing down.")
def smooth_motion(p: SmoothParams) -> OpResult:
    src = _src(p.file_id)
    info = media_info(p.file_id)
    if not info["has_video"]:
        raise OpError("smooth_motion needs a video")
    src_fps = (info["video"] or {}).get("fps") or 30
    label = f"{p.fps}fps" + (f"_slow{p.slow_motion:g}x" if p.slow_motion > 1 else "")
    name = f"{_stem(p.file_id)}_{label}.mp4"
    tool = _tool("RIFE_BIN", ["rife-ncnn-vulkan", "rife-ncnn-vulkan.exe"])
    if tool and p.slow_motion == 1.0:
        n_frames = int(round((info["duration_s"] or 0) * p.fps))
        return _frame_tool(p.file_id, lambda a, b: [tool, "-i", str(a), "-o", str(b), "-n", str(max(n_frames, 2))],
                           p.fps, name, "smooth_motion", {"engine": "RIFE"})
    slow = f"setpts={p.slow_motion}*PTS," if p.slow_motion > 1 else ""
    vf = f"{slow}minterpolate=fps={p.fps}:mi_mode=mci:mc_mode=aobmc:me_mode=bidir:vsbmc=1,format=yuv420p"
    audio = (["-c:a", "copy"] if info["has_audio"] else []) if p.slow_motion == 1 else ["-an"]
    return _guarded("smooth_motion", [p.file_id], name, lambda out: _ffmpeg("-i", str(src), "-vf", vf, *VIDEO_ENC, *audio, str(out)),
                    data={"engine": "ffmpeg minterpolate" + ("" if tool else " (install RIFE for faster, cleaner results)"), "from_fps": src_fps})
