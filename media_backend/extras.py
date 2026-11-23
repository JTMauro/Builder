"""Free extras that run on an ordinary CPU (picked from docs/free-apps.md).

- separate_stems: split a song or a video's sound into voice and music (or vocals/drums/bass/other) with
  Demucs (MIT). Used by dub_video to keep the original music under a new language.
- remove_background: cut a person or object out of a picture (transparent PNG) with rembg (MIT).
- clean_speech: AI noise removal for speech with DeepFilterNet (MIT/Apache).
- optimize_model: make a .glb smaller and faster to load (glTF-Transform, MIT; via npx).
Each needs its package (`pip install "builder[extras]"`; optimize_model needs Node.js) and says so if missing.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from . import storage
from .config import NO_WINDOW
from .ops import AUDIO_CODECS, OpError, OpResult, _ffmpeg, _guarded, _src, _stem, media_info, operation


def _need(module: str, pip: str) -> None:
    import importlib.util

    if importlib.util.find_spec(module) is None:
        raise OpError(f"This needs {pip}: pip install {pip}")


def _run(args: list[str], what: str) -> None:
    proc = subprocess.run(args, capture_output=True, text=True, **NO_WINDOW)
    if proc.returncode != 0:
        raise OpError(f"{what} failed:\n" + "\n".join((proc.stderr or proc.stdout).strip().splitlines()[-8:]))


class StemsParams(BaseModel):
    file_id: str = Field(description="Song, voice recording or video")
    stems: Literal["vocals", "all"] = Field("vocals", description="vocals: voice + everything else; all: vocals, drums, bass, other")
    format: Literal["wav", "mp3"] = "wav"


def demucs_split(src: Path, stems: str, work: Path) -> dict[str, Path]:
    """Run Demucs; returns {stem name: wav path}."""
    _need("demucs", "demucs")
    wav = work / "in.wav"
    _ffmpeg("-i", str(src), "-vn", "-ac", "2", "-ar", "44100", str(wav))
    args = [sys.executable, "-m", "demucs", "-n", "htdemucs", "-o", str(work / "out"), "-d", "cpu"]
    if stems == "vocals":
        args += ["--two-stems", "vocals"]
    _run(args + [str(wav)], "Demucs")
    folder = work / "out" / "htdemucs" / "in"
    return {p.stem: p for p in sorted(folder.glob("*.wav"))}


@operation("separate_stems", StemsParams, "Split audio (or a video's sound) into separate parts with Demucs: the voice and the music, or vocals, drums, bass and other. Use to remix, to dub over the original music, or to clean up a voice.")
def separate_stems(p: StemsParams) -> OpResult:
    src = _src(p.file_id)
    if not media_info(p.file_id)["has_audio"]:
        raise OpError("separate_stems needs a file with sound")
    work = Path(tempfile.mkdtemp(prefix="stems-"))
    try:
        parts = demucs_split(src, p.stems, work)
        outputs = []
        for name, wav in parts.items():
            label = "music" if name == "no_vocals" else name
            res = _guarded("separate_stems", [p.file_id], f"{_stem(p.file_id)}_{label}.{p.format}",
                           lambda out, w=wav: _ffmpeg("-i", str(w), *AUDIO_CODECS[p.format], str(out)))
            outputs += res.outputs
        return OpResult(outputs=outputs, data={"stems": [o.name for o in outputs], "engine": "Demucs htdemucs (MIT)"})
    finally:
        shutil.rmtree(work, ignore_errors=True)


class CutoutParams(BaseModel):
    file_id: str = Field(description="A picture")
    model: Literal["u2net", "isnet-general-use", "u2net_human_seg", "birefnet-general"] = Field("isnet-general-use", description="u2net_human_seg for people; birefnet-general is the sharpest (bigger download)")


@operation("remove_background", CutoutParams, "Cut the subject out of a picture (person, product, object) onto a transparent background, with rembg. Free and offline after a one-time model download.")
def remove_background(p: CutoutParams) -> OpResult:
    _need("rembg", '"rembg[cpu]"')
    from rembg import new_session, remove

    data = _src(p.file_id).read_bytes()
    session = new_session(p.model)
    return _guarded("remove_background", [p.file_id], f"{_stem(p.file_id)}_cutout.png",
                    lambda out: out.write_bytes(remove(data, session=session)), data={"model": p.model})


class CleanSpeechParams(BaseModel):
    file_id: str = Field(description="Speech recording or video")
    strength: float = Field(1.0, ge=0.1, le=1.0, description="1 = full noise removal; lower keeps more room sound")


@operation("clean_speech", CleanSpeechParams, "Remove background noise from speech with an AI model (DeepFilterNet): fans, traffic, hum, crowd. Stronger and cleaner than denoise_audio for voices; video keeps its picture.")
def clean_speech(p: CleanSpeechParams) -> OpResult:
    _need("df", "deepfilternet")
    src = _src(p.file_id)
    info = media_info(p.file_id)
    if not info["has_audio"]:
        raise OpError("clean_speech needs a file with sound")
    work = Path(tempfile.mkdtemp(prefix="clean-"))
    try:
        wav = work / "in.wav"
        _ffmpeg("-i", str(src), "-vn", "-ac", "1", "-ar", "48000", str(wav))
        from df.enhance import enhance, init_df, load_audio, save_audio

        model, state, _ = init_df()
        audio, _ = load_audio(str(wav), sr=state.sr())
        clean = enhance(model, state, audio, atten_lim_db=None if p.strength >= 1 else 6 + 30 * p.strength)
        save_audio(str(work / "clean.wav"), clean, state.sr())
        if info["has_video"]:
            return _guarded("clean_speech", [p.file_id], f"{_stem(p.file_id)}_clean.mp4",
                            lambda out: _ffmpeg("-i", str(src), "-i", str(work / "clean.wav"), "-map", "0:v", "-map", "1:a", "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", str(out)))
        return _guarded("clean_speech", [p.file_id], f"{_stem(p.file_id)}_clean.wav", lambda out: shutil.copyfile(work / "clean.wav", out))
    finally:
        shutil.rmtree(work, ignore_errors=True)


class OptimizeParams(BaseModel):
    file_id: str = Field(description="A .glb 3D model")
    texture_size: int = Field(1024, ge=128, le=8192, description="Largest texture side after resizing")


@operation("optimize_model", OptimizeParams, "Make a .glb 3D model smaller and faster to load for games and pages: merges and simplifies, compresses textures to WebP and meshes (glTF-Transform). Needs Node.js.")
def optimize_model(p: OptimizeParams) -> OpResult:
    src = _src(p.file_id)
    if src.suffix.lower() != ".glb":
        raise OpError("optimize_model needs a .glb file")
    npx = shutil.which("npx")
    if not npx:
        raise OpError("optimize_model needs Node.js (npx)")
    before = src.stat().st_size

    def build(out: Path) -> None:
        _run([npx, "-y", "@gltf-transform/cli@4", "optimize", str(src), str(out), "--compress", "meshopt",
              "--texture-compress", "webp", "--texture-size", str(p.texture_size)], "glTF-Transform")

    res = _guarded("optimize_model", [p.file_id], f"{_stem(p.file_id)}_optimized.glb", build)
    after = storage.path_of(res.outputs[0].id).stat().st_size
    res.data.update({"bytes_before": before, "bytes_after": after, "saved_pct": round(100 * (1 - after / max(1, before)), 1)})
    return res

