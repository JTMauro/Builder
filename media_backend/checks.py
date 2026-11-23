"""Checking work by eye, automatically: compare a new picture (or video frame) with an approved one.

visual_diff scores how alike two images are (SSIM 0..1, PSNR dB), says whether that passes a threshold,
and writes a heat map of what changed. Use it after re-rendering a character, a page or a shot to catch
changes nobody asked for (the hair, the posture, the lighting) before a person has to spot them.
"""

from __future__ import annotations

import re
import subprocess

from pydantic import BaseModel, Field

from .config import NO_WINDOW, get_settings
from .ops import OpError, OpResult, _ffmpeg, _guarded, _src, _stem, media_info, operation


class VisualDiffParams(BaseModel):
    file_id: str = Field(description="The new image (or video: its frame at `at` seconds)")
    reference_id: str = Field(description="The approved image (or video) to compare against")
    at: float = Field(0, ge=0, description="Seconds into a video to compare")
    min_ssim: float = Field(0.97, ge=0, le=1, description="Pass when similarity is at least this (1 = identical)")
    perceptual: bool = Field(False, description="Also measure LPIPS (perceptual distance, 0 = looks the same) with a local model; needs builder[senses]")


def _measure(a: str, b: str, at: float) -> tuple[float, float]:
    seek = ["-ss", f"{at}"] if at else []
    graph = "[0:v][1:v]scale2ref=flags=bicubic[a][r];[a]format=rgb24,split[a1][a2];[r]format=rgb24,split[r1][r2];[a1][r1]ssim;[a2][r2]psnr"
    proc = subprocess.run([get_settings().ffmpeg, "-hide_banner", *seek, "-i", a, *seek, "-i", b, "-frames:v", "1",
                           "-filter_complex", graph, "-f", "null", "-"], capture_output=True, text=True, **NO_WINDOW)
    s = re.search(r"SSIM .*All:([0-9.]+)", proc.stderr)
    p = re.search(r"PSNR .*average:([0-9.]+|inf)", proc.stderr)
    if proc.returncode != 0 or not s:
        raise OpError("could not compare these files:\n" + "\n".join(proc.stderr.strip().splitlines()[-6:]))
    return float(s.group(1)), float("inf") if not p or p.group(1) == "inf" else float(p.group(1))


@operation("visual_diff", VisualDiffParams, "Compare a new image or video frame with an approved one: similarity score (SSIM), pass/fail against a threshold, and a heat map of what changed. Use after re-rendering to catch unwanted changes.")
def visual_diff(p: VisualDiffParams) -> OpResult:
    a, b = _src(p.file_id), _src(p.reference_id)
    for fid in (p.file_id, p.reference_id):
        if not media_info(fid)["has_video"]:
            raise OpError("visual_diff needs images or videos")
    ssim, psnr = _measure(str(a), str(b), p.at)
    seek = ["-ss", f"{p.at}"] if p.at else []
    # heat map: the difference, boosted so small changes show, over a faded copy of the reference
    graph = ("[0:v][1:v]scale2ref[a][r];[r]split[r1][r2];[a]format=rgb24[a1];[r1]format=rgb24[rr];"
             "[a1][rr]blend=all_mode=difference,format=gray,curves=all='0/0 0.06/0.9 1/1',lutrgb=r=val:g=0:b=0,format=rgb24[d];"
             "[r2]format=gray,format=rgb24,colorlevels=romax=0.35:gomax=0.35:bomax=0.35[bg];[bg][d]blend=all_mode=addition")
    data = {"ssim": round(ssim, 5), "psnr_db": None if psnr == float("inf") else round(psnr, 2),
            "pass": ssim >= p.min_ssim, "min_ssim": p.min_ssim}
    if p.perceptual:
        from . import senses

        data.update(senses.lpips_files(p.file_id, p.reference_id, p.at))
    return _guarded("visual_diff", [p.file_id, p.reference_id], f"{_stem(p.file_id)}_vs_{_stem(p.reference_id)}_diff.png",
                    lambda out: _ffmpeg(*seek, "-i", str(a), *seek, "-i", str(b), "-frames:v", "1", "-filter_complex", graph, str(out)),
                    data=data)
