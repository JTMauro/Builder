"""Media operations. One registry feeds the HTTP API, the built-in assistant and the MCP connector.

Every operation takes a pydantic params model and returns an OpResult. Inputs are file ids from
the store; outputs are new files in the store (inputs are never modified).
"""

from __future__ import annotations

import json
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Literal

from pydantic import BaseModel, Field, model_validator

from . import storage
from .config import NO_WINDOW, get_settings
from .storage import FileInfo

FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"


class OpError(RuntimeError):
    pass


class OpResult(BaseModel):
    outputs: list[FileInfo] = []
    data: dict[str, Any] = {}


@dataclass(frozen=True)
class Operation:
    name: str
    description: str
    params: type[BaseModel]
    fn: Callable[[Any], OpResult]
    read_only: bool = False


REGISTRY: dict[str, Operation] = {}


def operation(name: str, params: type[BaseModel], description: str, read_only: bool = False):
    def wrap(fn):
        REGISTRY[name] = Operation(name, description.strip(), params, fn, read_only)
        return fn

    return wrap


def run_operation(name: str, params: dict[str, Any] | BaseModel) -> OpResult:
    op = REGISTRY.get(name)
    if op is None:
        raise OpError(f"Unknown operation '{name}'. Available: {', '.join(sorted(REGISTRY))}")
    parsed = params if isinstance(params, op.params) else op.params.model_validate(params)
    return op.fn(parsed)


# ---------------------------------------------------------------- helpers


def _run(args: list[str], timeout: int = 3600) -> str:
    try:
        proc = subprocess.run(args, capture_output=True, text=True, timeout=timeout, **NO_WINDOW)
    except FileNotFoundError as e:
        raise OpError(f"{args[0]} is not installed or not on PATH") from e
    except subprocess.TimeoutExpired as e:
        raise OpError(f"{Path(args[0]).name} timed out after {timeout}s") from e
    if proc.returncode != 0:
        tail = "\n".join(proc.stderr.strip().splitlines()[-12:])
        raise OpError(f"{Path(args[0]).name} failed:\n{tail}")
    return proc.stdout


def _ffmpeg(*args: str) -> None:
    _run([get_settings().ffmpeg, "-hide_banner", "-loglevel", "error", "-y", *args])


def _probe_raw(path: Path) -> dict[str, Any]:
    out = _run(
        [get_settings().ffprobe, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)],
        timeout=120,
    )
    return json.loads(out)


def _summary(raw: dict[str, Any]) -> dict[str, Any]:
    fmt = raw.get("format", {})
    streams = raw.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video" and not s.get("disposition", {}).get("attached_pic")), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    out: dict[str, Any] = {
        "container": fmt.get("format_name"),
        "duration_s": round(float(fmt["duration"]), 3) if fmt.get("duration") not in (None, "N/A") else None,
        "size_bytes": int(fmt.get("size", 0) or 0),
        "bit_rate": int(fmt["bit_rate"]) if fmt.get("bit_rate") not in (None, "N/A") else None,
        "has_video": video is not None,
        "has_audio": audio is not None,
    }
    if video:
        num, _, den = (video.get("avg_frame_rate") or "0/1").partition("/")
        fps = float(num) / float(den) if den and float(den) else None
        out["video"] = {
            "codec": video.get("codec_name"),
            "width": video.get("width"),
            "height": video.get("height"),
            "fps": round(fps, 3) if fps else None,
            "pix_fmt": video.get("pix_fmt"),
        }
    if audio:
        out["audio"] = {
            "codec": audio.get("codec_name"),
            "sample_rate": int(audio.get("sample_rate", 0) or 0),
            "channels": audio.get("channels"),
        }
    return out


def media_info(file_id: str) -> dict[str, Any]:
    return _summary(_probe_raw(storage.path_of(file_id)))


def _src(file_id: str) -> Path:
    try:
        return storage.path_of(file_id)
    except storage.FileNotFound as e:
        raise OpError(f"No file with id '{file_id}'") from e


def _stem(file_id: str) -> str:
    return Path(storage.get(file_id).name).stem


def _out(name: str) -> tuple[str, Path]:
    return storage.new_output_path(name)


def _finish(file_id: str, path: Path, op: str, parents: list[str], data: dict | None = None) -> OpResult:
    if not path.exists() or path.stat().st_size == 0:
        raise OpError(f"{op} produced no output")
    info = storage.register(file_id, path, op, parents)
    return OpResult(outputs=[info], data=data or {})


def _guarded(op: str, parents: list[str], name: str, build: Callable[[Path], None], data: dict | None = None) -> OpResult:
    """Reserve an output, run build(path), register it; clean up the reserved slot on failure."""
    file_id, path = _out(name)
    try:
        build(path)
        return _finish(file_id, path, op, parents, data)
    except BaseException:
        import shutil

        shutil.rmtree(path.parent, ignore_errors=True)
        raise


def _even_scale(width: int | None, height: int | None) -> str:
    w = str(width) if width else "-2"
    h = str(height) if height else "-2"
    return f"scale={w}:{h}:flags=lanczos"


VIDEO_ENC = ["-c:v", "libx264", "-preset", "medium", "-crf", "20", "-pix_fmt", "yuv420p", "-movflags", "+faststart"]
AUDIO_ENC = ["-c:a", "aac", "-b:a", "192k"]

AUDIO_CODECS = {
    "mp3": ["-c:a", "libmp3lame", "-q:a", "2"],
    "wav": ["-c:a", "pcm_s16le"],
    "flac": ["-c:a", "flac"],
    "aac": ["-c:a", "aac", "-b:a", "192k"],
    "m4a": ["-c:a", "aac", "-b:a", "192k"],
    "ogg": ["-c:a", "libopus", "-b:a", "128k"],
    "opus": ["-c:a", "libopus", "-b:a", "128k"],
}
VIDEO_FORMATS = {
    "mp4": VIDEO_ENC + AUDIO_ENC,
    "mov": VIDEO_ENC + AUDIO_ENC,
    "mkv": ["-c:v", "libx264", "-crf", "20", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k"],
    "webm": ["-c:v", "libvpx-vp9", "-crf", "32", "-b:v", "0", "-row-mt", "1", "-c:a", "libopus", "-b:a", "128k"],
}


class FileRef(BaseModel):
    file_id: str = Field(description="Id of a file in the media store")


# ---------------------------------------------------------------- operations


@operation("probe", FileRef, "Read duration, resolution, frame rate, codecs and streams of a media file.", read_only=True)
def probe(p: FileRef) -> OpResult:
    _src(p.file_id)
    return OpResult(data=media_info(p.file_id))


class ConvertParams(FileRef):
    format: Literal["mp4", "mov", "mkv", "webm", "mp3", "wav", "flac", "aac", "m4a", "ogg", "opus"] = Field(
        description="Target container/format"
    )
    crf: int | None = Field(None, ge=0, le=51, description="Video quality for H.264/VP9 (lower = better). Optional.")
    audio_bitrate: str | None = Field(None, pattern=r"^\d{2,4}k$", description="Audio bitrate such as '192k'. Optional.")


@operation("convert", ConvertParams, "Convert/transcode to another format (video: mp4 mov mkv webm; audio: mp3 wav flac aac m4a ogg opus).")
def convert(p: ConvertParams) -> OpResult:
    src = _src(p.file_id)
    if p.format in AUDIO_CODECS:
        args = ["-vn", *AUDIO_CODECS[p.format]]
    else:
        args = list(VIDEO_FORMATS[p.format])
        if p.crf is not None:
            args[args.index("-crf") + 1] = str(p.crf)
    if p.audio_bitrate and "-b:a" in args:
        args[args.index("-b:a") + 1] = p.audio_bitrate
    return _guarded("convert", [p.file_id], f"{_stem(p.file_id)}.{p.format}", lambda out: _ffmpeg("-i", str(src), *args, str(out)))


class TrimParams(FileRef):
    start: float = Field(0, ge=0, description="Start time in seconds")
    end: float | None = Field(None, gt=0, description="End time in seconds (use end or duration)")
    duration: float | None = Field(None, gt=0, description="Length in seconds (use end or duration)")
    precise: bool = Field(True, description="Re-encode for frame-accurate cuts (slower). False = fast stream copy.")

    @model_validator(mode="after")
    def _check(self):
        if self.end is not None and self.end <= self.start:
            raise ValueError("end must be greater than start")
        return self


@operation("trim", TrimParams, "Cut a section out of an audio or video file.")
def trim(p: TrimParams) -> OpResult:
    src = _src(p.file_id)
    length = p.duration if p.duration is not None else (p.end - p.start if p.end is not None else None)
    ext = src.suffix or ".mp4"
    info = media_info(p.file_id)

    def build(out: Path):
        args = ["-ss", f"{p.start}", "-i", str(src)]
        if length is not None:
            args += ["-t", f"{length}"]
        if not p.precise:
            args += ["-c", "copy", "-avoid_negative_ts", "make_zero"]
        elif info["has_video"]:
            args += VIDEO_ENC + (AUDIO_ENC if info["has_audio"] else [])
        _ffmpeg(*args, str(out))

    return _guarded("trim", [p.file_id], f"{_stem(p.file_id)}_trim{ext}", build)


class ExtractAudioParams(FileRef):
    format: Literal["mp3", "wav", "flac", "aac", "m4a", "ogg", "opus"] = "mp3"


@operation("extract_audio", ExtractAudioParams, "Pull the audio track out of a video into an audio file.")
def extract_audio(p: ExtractAudioParams) -> OpResult:
    src = _src(p.file_id)
    if not media_info(p.file_id)["has_audio"]:
        raise OpError("This file has no audio track")
    return _guarded(
        "extract_audio", [p.file_id], f"{_stem(p.file_id)}.{p.format}",
        lambda out: _ffmpeg("-i", str(src), "-vn", *AUDIO_CODECS[p.format], str(out)),
    )


class ResizeParams(FileRef):
    width: int | None = Field(None, ge=16, le=7680, description="Target width in px (height keeps aspect if omitted)")
    height: int | None = Field(None, ge=16, le=4320, description="Target height in px (width keeps aspect if omitted)")
    fps: float | None = Field(None, gt=0, le=240, description="Optional new frame rate")

    @model_validator(mode="after")
    def _check(self):
        if not (self.width or self.height or self.fps):
            raise ValueError("give width, height or fps")
        return self


@operation("resize", ResizeParams, "Scale a video (keeping aspect ratio when only one side is given) and/or change its frame rate.")
def resize(p: ResizeParams) -> OpResult:
    src = _src(p.file_id)
    info = media_info(p.file_id)
    filters = []
    if p.width or p.height:
        filters.append(_even_scale(p.width, p.height))
    if p.fps:
        filters.append(f"fps={p.fps}")
    return _guarded(
        "resize", [p.file_id], f"{_stem(p.file_id)}_resized.mp4",
        lambda out: _ffmpeg("-i", str(src), "-vf", ",".join(filters), *VIDEO_ENC,
                            *(AUDIO_ENC if info["has_audio"] else ["-an"]), str(out)),
    )


class ThumbnailParams(FileRef):
    time: float = Field(1.0, ge=0, description="Timestamp in seconds")
    width: int | None = Field(None, ge=16, le=3840)


@operation("thumbnail", ThumbnailParams, "Grab a single PNG frame from a video at a timestamp.")
def thumbnail(p: ThumbnailParams) -> OpResult:
    src = _src(p.file_id)
    vf = ["-vf", _even_scale(p.width, None)] if p.width else []
    return _guarded(
        "thumbnail", [p.file_id], f"{_stem(p.file_id)}_{p.time:g}s.png",
        lambda out: _ffmpeg("-ss", f"{p.time}", "-i", str(src), "-frames:v", "1", *vf, str(out)),
    )


class FramesParams(FileRef):
    count: int = Field(6, ge=1, le=24, description="How many evenly spaced frames to grab")
    width: int = Field(480, ge=64, le=1920)


@operation("frames", FramesParams, "Grab N evenly spaced PNG frames across a video (useful for previewing or reviewing it).")
def frames(p: FramesParams) -> OpResult:
    src = _src(p.file_id)
    info = media_info(p.file_id)
    if not info["has_video"]:
        raise OpError("This file has no video")
    dur = info["duration_s"] or 0
    outputs = []
    for i in range(p.count):
        t = dur * (i + 0.5) / p.count if dur else 0
        res = thumbnail(ThumbnailParams(file_id=p.file_id, time=round(t, 3), width=p.width))
        outputs += res.outputs
    return OpResult(outputs=outputs, data={"times_s": [round(dur * (i + 0.5) / p.count, 3) for i in range(p.count)]})


class NormalizeParams(FileRef):
    target_lufs: float = Field(-16, ge=-40, le=-5, description="Integrated loudness target (-16 web/podcast, -23 broadcast)")


@operation("normalize_audio", NormalizeParams, "Even out loudness to a LUFS target (EBU R128). Works on audio or video files.")
def normalize_audio(p: NormalizeParams) -> OpResult:
    src = _src(p.file_id)
    info = media_info(p.file_id)
    if not info["has_audio"]:
        raise OpError("This file has no audio track")
    af = f"loudnorm=I={p.target_lufs}:TP=-1.5:LRA=11"
    if info["has_video"]:
        name, args = f"{_stem(p.file_id)}_normalized.mp4", ["-c:v", "copy", *AUDIO_ENC]
    else:
        ext = src.suffix.lstrip(".").lower()
        ext = ext if ext in AUDIO_CODECS else "wav"
        name, args = f"{_stem(p.file_id)}_normalized.{ext}", AUDIO_CODECS[ext]
    return _guarded("normalize_audio", [p.file_id], name,
                    lambda out: _ffmpeg("-i", str(src), "-af", af, "-ar", "48000", *args, str(out)))


class SpeedParams(FileRef):
    factor: float = Field(description="Playback speed multiplier, 0.25-4 (2 = twice as fast)", ge=0.25, le=4)


def _atempo_chain(factor: float) -> str:
    parts, f = [], factor
    while f > 2.0:
        parts.append("atempo=2.0"); f /= 2.0
    while f < 0.5:
        parts.append("atempo=0.5"); f /= 0.5
    parts.append(f"atempo={f:.6f}")
    return ",".join(parts)


@operation("change_speed", SpeedParams, "Speed up or slow down audio and/or video (pitch is preserved).")
def change_speed(p: SpeedParams) -> OpResult:
    src = _src(p.file_id)
    info = media_info(p.file_id)
    args = ["-i", str(src)]
    if info["has_video"]:
        args += ["-filter:v", f"setpts=PTS/{p.factor}", *VIDEO_ENC]
        name = f"{_stem(p.file_id)}_x{p.factor:g}.mp4"
        args += (["-filter:a", _atempo_chain(p.factor), *AUDIO_ENC] if info["has_audio"] else ["-an"])
    else:
        ext = src.suffix.lstrip(".").lower()
        ext = ext if ext in AUDIO_CODECS else "wav"
        name = f"{_stem(p.file_id)}_x{p.factor:g}.{ext}"
        args += ["-filter:a", _atempo_chain(p.factor), *AUDIO_CODECS[ext]]
    return _guarded("change_speed", [p.file_id], name, lambda out: _ffmpeg(*args, str(out)))


class ConcatParams(BaseModel):
    file_ids: list[str] = Field(min_length=2, max_length=50, description="Files to join, in order")
    width: int | None = Field(None, ge=16, le=7680, description="Output width for video (defaults to the first clip's)")
    height: int | None = Field(None, ge=16, le=4320, description="Output height for video (defaults to the first clip's)")
    fps: float = Field(30, gt=0, le=120)


@operation("concat", ConcatParams, "Join several clips end to end. Videos are scaled/padded to one size; audio-only files are joined as audio.")
def concat(p: ConcatParams) -> OpResult:
    srcs = [_src(f) for f in p.file_ids]
    infos = [media_info(f) for f in p.file_ids]
    any_video = any(i["has_video"] for i in infos)
    args: list[str] = []
    for s in srcs:
        args += ["-i", str(s)]
    n = len(srcs)
    if not any_video:
        chains = "".join(f"[{i}:a]aformat=sample_rates=48000:channel_layouts=stereo[a{i}];" for i in range(n))
        fc = chains + "".join(f"[a{i}]" for i in range(n)) + f"concat=n={n}:v=0:a=1[a]"
        name, enc, maps = f"{_stem(p.file_ids[0])}_joined.m4a", AUDIO_ENC, ["-map", "[a]"]
    else:
        first = next(i for i in infos if i["has_video"])["video"]
        w, h = p.width or first["width"], p.height or first["height"]
        w, h = w - w % 2, h - h % 2
        parts = []
        for i, info in enumerate(infos):
            if not info["has_video"]:
                raise OpError("Can't mix audio-only files into a video join; use replace_audio instead")
            parts.append(
                f"[{i}:v]scale={w}:{h}:force_original_aspect_ratio=decrease,pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,"
                f"setsar=1,fps={p.fps},format=yuv420p[v{i}];"
            )
            if info["has_audio"]:
                parts.append(f"[{i}:a]aformat=sample_rates=48000:channel_layouts=stereo[a{i}];")
            else:
                d = info["duration_s"] or 0
                parts.append(f"anullsrc=r=48000:cl=stereo,atrim=0:{d}[a{i}];")
        fc = "".join(parts) + "".join(f"[v{i}][a{i}]" for i in range(n)) + f"concat=n={n}:v=1:a=1[v][a]"
        name, enc, maps = f"{_stem(p.file_ids[0])}_joined.mp4", VIDEO_ENC + AUDIO_ENC, ["-map", "[v]", "-map", "[a]"]
    return _guarded("concat", list(p.file_ids), name,
                    lambda out: _ffmpeg(*args, "-filter_complex", fc, *maps, *enc, str(out)))


class ReplaceAudioParams(BaseModel):
    video_id: str = Field(description="Video file id")
    audio_id: str = Field(description="Audio (or video with audio) file id to use as the soundtrack")
    mode: Literal["replace", "mix"] = Field("replace", description="replace the original audio, or mix both together")
    audio_volume: float = Field(1.0, ge=0, le=4, description="Volume multiplier for the new audio")
    loop_audio: bool = Field(False, description="Loop the new audio if it is shorter than the video")


@operation("replace_audio", ReplaceAudioParams, "Put a soundtrack/voice-over on a video, replacing or mixing with its existing audio. Output is the video's length.")
def replace_audio(p: ReplaceAudioParams) -> OpResult:
    vsrc, asrc = _src(p.video_id), _src(p.audio_id)
    vinfo = media_info(p.video_id)
    if not vinfo["has_video"]:
        raise OpError("video_id has no video stream")
    if not media_info(p.audio_id)["has_audio"]:
        raise OpError("audio_id has no audio")
    loop = ["-stream_loop", "-1"] if p.loop_audio else []
    dur = vinfo["duration_s"]
    if p.mode == "mix" and vinfo["has_audio"]:
        fc = f"[1:a]volume={p.audio_volume}[n];[0:a][n]amix=inputs=2:duration=first:dropout_transition=0[a]"
    else:
        fc = f"[1:a]volume={p.audio_volume}[a]"
    t = ["-t", f"{dur}"] if dur else ["-shortest"]
    return _guarded(
        "replace_audio", [p.video_id, p.audio_id], f"{_stem(p.video_id)}_with_audio.mp4",
        lambda out: _ffmpeg("-i", str(vsrc), *loop, "-i", str(asrc), "-filter_complex", fc,
                            "-map", "0:v", "-map", "[a]", "-c:v", "copy", *AUDIO_ENC, *t, str(out)),
    )


@operation("mute", FileRef, "Remove the audio track from a video.")
def mute(p: FileRef) -> OpResult:
    src = _src(p.file_id)
    return _guarded("mute", [p.file_id], f"{_stem(p.file_id)}_muted{src.suffix or '.mp4'}",
                    lambda out: _ffmpeg("-i", str(src), "-an", "-c:v", "copy", str(out)))


class GifParams(FileRef):
    start: float = Field(0, ge=0)
    duration: float = Field(4, gt=0, le=60)
    width: int = Field(480, ge=64, le=1280)
    fps: float = Field(12, gt=0, le=50)


@operation("make_gif", GifParams, "Turn a section of a video into a looping, palette-optimised GIF.")
def make_gif(p: GifParams) -> OpResult:
    src = _src(p.file_id)
    fc = (f"fps={p.fps},{_even_scale(p.width, None)},split[a][b];[a]palettegen=stats_mode=diff[pal];"
          f"[b][pal]paletteuse=dither=bayer:bayer_scale=4")
    return _guarded("make_gif", [p.file_id], f"{_stem(p.file_id)}.gif",
                    lambda out: _ffmpeg("-ss", f"{p.start}", "-t", f"{p.duration}", "-i", str(src),
                                        "-filter_complex", fc, "-loop", "0", str(out)))


class WaveformParams(FileRef):
    width: int = Field(1600, ge=200, le=4000)
    height: int = Field(300, ge=50, le=1200)
    color: str = Field("#2d81ff", pattern=r"^#[0-9a-fA-F]{6}$")


@operation("waveform", WaveformParams, "Render a PNG picture of an audio waveform.")
def waveform(p: WaveformParams) -> OpResult:
    src = _src(p.file_id)
    color = "0x" + p.color.lstrip("#")
    fc = f"[0:a]aformat=channel_layouts=mono,showwavespic=s={p.width}x{p.height}:colors={color}"
    return _guarded("waveform", [p.file_id], f"{_stem(p.file_id)}_waveform.png",
                    lambda out: _ffmpeg("-i", str(src), "-filter_complex", fc, "-frames:v", "1", str(out)))


class FadeParams(FileRef):
    fade_in: float = Field(0.5, ge=0, le=30, description="Seconds of fade-in at the start")
    fade_out: float = Field(0.5, ge=0, le=30, description="Seconds of fade-out at the end")


@operation("fade", FadeParams, "Fade audio and video in at the start and out at the end.")
def fade(p: FadeParams) -> OpResult:
    src = _src(p.file_id)
    info = media_info(p.file_id)
    dur = info["duration_s"] or 0
    if dur <= 0:
        raise OpError("Can't read this file's duration")
    out_start = max(0.0, dur - p.fade_out)
    vf = f"fade=t=in:st=0:d={p.fade_in},fade=t=out:st={out_start}:d={p.fade_out}"
    af = f"afade=t=in:st=0:d={p.fade_in},afade=t=out:st={out_start}:d={p.fade_out}"
    args = ["-i", str(src)]
    if info["has_video"]:
        args += ["-vf", vf, *VIDEO_ENC]
        args += ["-af", af, *AUDIO_ENC] if info["has_audio"] else ["-an"]
        name = f"{_stem(p.file_id)}_faded.mp4"
    else:
        ext = src.suffix.lstrip(".").lower()
        ext = ext if ext in AUDIO_CODECS else "wav"
        args += ["-af", af, *AUDIO_CODECS[ext]]
        name = f"{_stem(p.file_id)}_faded.{ext}"
    return _guarded("fade", [p.file_id], name, lambda out: _ffmpeg(*args, str(out)))


class TextParams(FileRef):
    text: str = Field(min_length=1, max_length=300, description="Caption text; use \\n for line breaks. Shrinks to fit the frame.")
    position: Literal["top", "center", "bottom"] = "bottom"
    font_size: int = Field(48, ge=8, le=300)
    color: str = Field("white", pattern=r"^(#[0-9a-fA-F]{6}|[a-z]+)$")
    box: bool = Field(True, description="Draw a translucent box behind the text")
    start: float | None = Field(None, ge=0, description="Show from this time (s); default whole clip")
    end: float | None = Field(None, gt=0, description="Hide after this time (s)")


@operation("add_text", TextParams, "Burn a caption/title onto a video.")
def add_text(p: TextParams) -> OpResult:
    src = _src(p.file_id)
    info = media_info(p.file_id)
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as tf:
        tf.write(p.text)
        textfile = tf.name
    y = {"top": "h*0.06", "center": "(h-text_h)/2", "bottom": "h-text_h-h*0.06"}[p.position]
    color = p.color.replace("#", "0x")
    # Shrink the font so the longest line fits in ~90% of the frame width (bold sans is ~0.62em per char).
    width = (info.get("video") or {}).get("width") or 1280
    longest = max((len(line) for line in p.text.splitlines()), default=1) or 1
    size = max(8, min(p.font_size, int(width * 0.9 / (0.62 * longest))))
    dt = (f"drawtext=fontfile={FONT}:textfile={textfile}:fontsize={size}:fontcolor={color}"
          f":x=(w-text_w)/2:y={y}")
    if p.box:
        dt += ":box=1:boxcolor=black@0.45:boxborderw=18"
    if p.start is not None or p.end is not None:
        dt += f":enable='between(t,{p.start or 0},{p.end if p.end is not None else 1e9})'"
    try:
        return _guarded("add_text", [p.file_id], f"{_stem(p.file_id)}_text.mp4",
                        lambda out: _ffmpeg("-i", str(src), "-vf", dt, *VIDEO_ENC,
                                            *(["-c:a", "copy"] if info["has_audio"] else ["-an"]), str(out)))
    finally:
        Path(textfile).unlink(missing_ok=True)


class ImagesToVideoParams(BaseModel):
    file_ids: list[str] = Field(min_length=1, max_length=2000, description="Image file ids, in frame order")
    fps: float = Field(12, gt=0, le=60, description="Frames per second for frame-by-frame animation")
    seconds_per_image: float | None = Field(None, gt=0, le=60, description="Hold each image this long instead (slideshow)")
    width: int | None = Field(None, ge=16, le=3840, description="Output width (defaults to the first image's)")
    height: int | None = Field(None, ge=16, le=2160, description="Output height (defaults to the first image's)")
    audio_id: str | None = Field(None, description="Optional soundtrack file id (trimmed to the animation's length)")
    loops: int = Field(1, ge=1, le=100, description="Repeat the frame sequence this many times")


@operation("images_to_video", ImagesToVideoParams, "Turn a sequence of images into an MP4: frame-by-frame animation (fps) or a slideshow (seconds_per_image), with optional soundtrack.")
def images_to_video(p: ImagesToVideoParams) -> OpResult:
    srcs = [_src(f) for f in p.file_ids] * p.loops
    first = media_info(p.file_ids[0]).get("video") or {}
    w = p.width or first.get("width") or 1280
    h = p.height or first.get("height") or 720
    w, h = w - w % 2, h - h % 2
    per = p.seconds_per_image or 1.0 / p.fps
    out_fps = 30 if p.seconds_per_image else p.fps
    with tempfile.TemporaryDirectory() as td:
        listing = Path(td) / "frames.txt"
        lines = []
        for s in srcs:
            lines.append(f"file '{str(s).replace(chr(39), chr(39) + chr(92) + chr(39) + chr(39))}'")
            lines.append(f"duration {per:.6f}")
        lines.append(lines[-2])  # concat demuxer needs the last file repeated
        listing.write_text("\n".join(lines) + "\n")
        vf = (f"scale={w}:{h}:force_original_aspect_ratio=decrease,pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color=white,"
              f"setsar=1,fps={out_fps},format=yuv420p")
        args = ["-f", "concat", "-safe", "0", "-i", str(listing)]
        total = per * len(srcs)
        if p.audio_id:
            args += ["-i", str(_src(p.audio_id))]
        enc = ["-vf", vf, *VIDEO_ENC]
        if p.audio_id:
            enc += ["-map", "0:v", "-map", "1:a", *AUDIO_ENC]
        enc += ["-t", f"{total:.3f}"]
        parents = list(dict.fromkeys(p.file_ids + ([p.audio_id] if p.audio_id else [])))
        return _guarded("images_to_video", parents, f"{_stem(p.file_ids[0])}_animation.mp4",
                        lambda out: _ffmpeg(*args, *enc, str(out)),
                        data={"frames": len(srcs), "duration_s": round(total, 3)})


class AnimateImageParams(FileRef):
    effect: Literal["zoom_in", "zoom_out", "pan_left", "pan_right", "bounce", "wobble"] = "zoom_in"
    duration: float = Field(4, gt=0, le=60)
    width: int = Field(1280, ge=64, le=3840)
    height: int = Field(720, ge=64, le=2160)
    fps: int = Field(30, ge=1, le=60)
    background: str = Field("white", pattern=r"^(#[0-9a-fA-F]{6}|[a-z]+)$")


@operation("animate_image", AnimateImageParams, "Bring a still image (e.g. a character drawing) to life as a short MP4 with a camera move or motion effect.")
def animate_image(p: AnimateImageParams) -> OpResult:
    src = _src(p.file_id)
    w, h = p.width - p.width % 2, p.height - p.height % 2
    n = max(1, int(p.duration * p.fps))
    bg = p.background.replace("#", "0x")
    fit = f"scale={w}:{h}:force_original_aspect_ratio=decrease,pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color={bg},setsar=1"
    if p.effect in ("zoom_in", "zoom_out", "pan_left", "pan_right"):
        # upscale first so zoompan has pixels to work with
        big = f"scale={w * 2}:{h * 2}"
        z = {"zoom_in": f"1+0.3*on/{n}", "zoom_out": f"1.3-0.3*on/{n}"}.get(p.effect, "1.2")
        x = {"pan_left": f"(iw-iw/zoom)*(1-on/{n})", "pan_right": f"(iw-iw/zoom)*on/{n}"}.get(p.effect, "iw/2-(iw/zoom/2)")
        vf = f"{fit},{big},zoompan=z='{z}':x='{x}':y='ih/2-(ih/zoom/2)':d={n}:s={w}x{h}:fps={p.fps},format=yuv420p"
        inputs = ["-loop", "1", "-i", str(src)]
        frames_args = ["-frames:v", str(n)]
    else:
        # character moves on a static canvas
        sw, sh = int(w * 0.8), int(h * 0.8)
        if p.effect == "bounce":
            ox, oy = "(W-w)/2", f"(H-h)/2-abs(sin(t*{3.1416 * 2}))*H*0.08+H*0.04"
        else:  # wobble
            ox, oy = f"(W-w)/2+sin(t*6)*W*0.02", "(H-h)/2"
        vf = None
        inputs = ["-f", "lavfi", "-i", f"color=c={bg}:s={w}x{h}:r={p.fps}:d={p.duration}", "-loop", "1", "-i", str(src)]
        fc = (f"[1:v]scale={sw}:{sh}:force_original_aspect_ratio=decrease[c];"
              f"[0:v][c]overlay=x='{ox}':y='{oy}':shortest=1,format=yuv420p")
        frames_args = ["-filter_complex", fc, "-t", f"{p.duration}"]
    args = inputs + (["-vf", vf] if vf else []) + frames_args + [a for a in VIDEO_ENC]
    return _guarded("animate_image", [p.file_id], f"{_stem(p.file_id)}_{p.effect}.mp4",
                    lambda out: _ffmpeg(*args, str(out)))


ASPECTS = {"9:16": (9, 16), "1:1": (1, 1), "4:5": (4, 5), "16:9": (16, 9), "21:9": (21, 9)}


class ReframeParams(FileRef):
    aspect: Literal["9:16", "1:1", "4:5", "16:9", "21:9"] = Field("9:16", description="Shape of the new video: 9:16 vertical (Reels/TikTok/Shorts), 1:1 square, 4:5 feed, 16:9 wide, 21:9 cinema")
    mode: Literal["crop", "blur"] = Field("crop", description="crop: fill the frame (cuts the sides); blur: show the whole picture over a blurred, zoomed copy of itself")
    height: int = Field(1920, ge=144, le=4320, description="Output height in px (width follows the aspect)")
    focus: float = Field(0.5, ge=0, le=1, description="When cropping: where to keep, 0 = left/top, 0.5 = centre, 1 = right/bottom")


@operation("reframe", ReframeParams, "Make a different-shaped version of a video for social media or cinema: vertical 9:16, square, 4:5, 16:9 or 21:9, by cropping or by placing it over a blurred background.")
def reframe(p: ReframeParams) -> OpResult:
    src = _src(p.file_id)
    info = media_info(p.file_id)
    if not info["has_video"]:
        raise OpError("reframe needs a video")
    aw, ah = ASPECTS[p.aspect]
    h = p.height - p.height % 2
    w = int(round(h * aw / ah / 2)) * 2
    if p.mode == "crop":
        f = p.focus
        vf = (f"scale={w}:{h}:force_original_aspect_ratio=increase,"
              f"crop={w}:{h}:(iw-{w})*{f}:(ih-{h})*{f},setsar=1,format=yuv420p")
        args = ["-i", str(src), "-vf", vf]
    else:
        fc = (f"[0:v]split[a][b];[a]scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},boxblur=24:2,eq=brightness=-0.06[bg];"
              f"[b]scale={w}:{h}:force_original_aspect_ratio=decrease[fg];[bg][fg]overlay=(W-w)/2:(H-h)/2,setsar=1,format=yuv420p[v]")
        args = ["-i", str(src), "-filter_complex", fc, "-map", "[v]", "-map", "0:a?"]
    tag = p.aspect.replace(":", "x")
    return _guarded("reframe", [p.file_id], f"{_stem(p.file_id)}_{tag}.mp4",
                    lambda out: _ffmpeg(*args, *VIDEO_ENC, *AUDIO_ENC, str(out)),
                    data={"width": w, "height": h})


class TimelineClip(BaseModel):
    file_id: str = Field(description="Video (or audio) file id")
    start: float = Field(0, ge=0, description="Seconds into the file where this clip starts")
    end: float | None = Field(None, gt=0, description="Seconds into the file where it ends (default: end of file)")
    name: str | None = Field(None, max_length=120, description="Clip name shown in the editor")


class TimelineParams(BaseModel):
    clips: list[TimelineClip] = Field(min_length=1, max_length=300, description="Video clips in order, end to end")
    music_id: str | None = Field(None, description="Optional soundtrack laid under the whole timeline")
    name: str = Field("Builder timeline", max_length=120, description="Timeline (project) name")
    fps: Literal[23.976, 24, 25, 29.97, 30, 48, 50, 59.94, 60] = Field(30, description="Timeline frame rate")
    width: int | None = Field(None, ge=16, le=7680, description="Timeline width (default: first clip's)")
    height: int | None = Field(None, ge=16, le=4320, description="Timeline height (default: first clip's)")


def _rate(fps: float) -> tuple[int, int]:
    """Frame duration as (numerator, denominator) seconds, e.g. 30 -> (1, 30), 29.97 -> (1001, 30000)."""
    return {23.976: (1001, 24000), 29.97: (1001, 30000), 59.94: (1001, 60000)}.get(fps, (1, int(fps)))


@operation("timeline_export", TimelineParams, "Write an edit timeline (FCPXML) that DaVinci Resolve, Final Cut and Premiere open: the clips in order with their in/out points, plus an optional soundtrack. In Resolve: File > Import > Timeline.")
def timeline_export(p: TimelineParams) -> OpResult:
    from xml.sax.saxutils import quoteattr

    num, den = _rate(p.fps)
    def t(frames: int) -> str:  # a frame count as rational seconds, the way FCPXML writes time
        return f"{frames * num}/{den}s"

    def fr(seconds: float) -> int:  # seconds to whole frames, halves rounding up (Python's round() rounds them to even)
        return int(seconds * den / num + 0.5)
    infos = {c.file_id: media_info(c.file_id) for c in p.clips}
    first_video = next((infos[c.file_id]["video"] for c in p.clips if infos[c.file_id]["has_video"]), None)
    if first_video is None:
        raise OpError("timeline_export needs at least one video clip")
    w = p.width or first_video["width"]
    h = p.height or first_video["height"]
    resources = [f'<format id="r0" name="FFVideoFormat{h}p{p.fps:g}" frameDuration="{num}/{den}s" width="{w}" height="{h}"/>']
    assets, spine, offset = {}, [], 0
    music_ids = [p.music_id] if p.music_id else []
    for fid in [c.file_id for c in p.clips] + music_ids:
        if fid in assets:
            continue
        info = infos.get(fid) or media_info(fid)
        infos[fid] = info
        frames = fr(info["duration_s"] or 0)
        rid = f"r{len(assets) + 1}"
        assets[fid] = rid
        src = quoteattr(_src(fid).resolve().as_uri())
        name = quoteattr(storage.get(fid).name)
        resources.append(
            f'<asset id="{rid}" name={name} src={src} start="0s" duration="{t(frames)}" '
            f'hasVideo="{int(info["has_video"])}" hasAudio="{int(info["has_audio"])}"'
            + (' format="r0"' if info["has_video"] else "")
            + (' audioSources="1" audioChannels="2"' if info["has_audio"] else "") + "/>")
    for c in p.clips:
        info = infos[c.file_id]
        if not info["has_video"]:
            raise OpError(f"clip {c.file_id} has no video; put music in music_id instead")
        total = info["duration_s"] or 0
        end = min(c.end if c.end is not None else total, total) if total else (c.end or 0)
        if end <= c.start:
            raise OpError(f"clip {c.file_id}: end must be after start (file is {total:.2f}s long)")
        s_fr, d_fr = fr(c.start), fr(end - c.start)
        name = quoteattr(c.name or Path(storage.get(c.file_id).name).stem)
        spine.append(f'<asset-clip ref="{assets[c.file_id]}" name={name} offset="{t(offset)}" start="{t(s_fr)}" '
                     f'duration="{t(d_fr)}" format="r0" tcFormat="NDF"/>')
        offset += d_fr
    if p.music_id:
        mi = infos[p.music_id]
        m_fr = min(offset, fr(mi["duration_s"] or 0))
        music = (f'<asset-clip ref="{assets[p.music_id]}" lane="-1" name="Soundtrack" offset="0s" start="0s" '
                 f'duration="{t(m_fr)}" audioRole="music"/>')
        # a connected clip lives inside the first spine clip
        spine[0] = spine[0][:-2] + ">" + music + "</asset-clip>"
    xml = "\n".join([
        '<?xml version="1.0" encoding="UTF-8"?>', "<!DOCTYPE fcpxml>", '<fcpxml version="1.9">',
        "  <resources>", *("    " + r for r in resources), "  </resources>",
        "  <library>", '    <event name="Builder">', f"      <project name={quoteattr(p.name)}>",
        f'        <sequence format="r0" duration="{t(offset)}" tcStart="0s" tcFormat="NDF">', "          <spine>",
        *("            " + s for s in spine), "          </spine>", "        </sequence>", "      </project>",
        "    </event>", "  </library>", "</fcpxml>", ""])
    parents = list(dict.fromkeys([c.file_id for c in p.clips] + music_ids))
    safe = storage.safe_name(p.name).replace(" ", "_") or "timeline"
    return _guarded("timeline_export", parents, f"{safe}.fcpxml", lambda out: out.write_text(xml, encoding="utf-8"),
                    data={"clips": len(p.clips), "duration_s": round(offset * num / den, 3), "fps": p.fps, "width": w, "height": h,
                          "how_to_open": "DaVinci Resolve: File > Import > Timeline, pick this .fcpxml (keep the media files where they are)."})


def describe_operations() -> list[dict[str, Any]]:
    return [
        {"name": op.name, "description": op.description, "read_only": op.read_only,
         "params_schema": op.params.model_json_schema()}
        for op in REGISTRY.values()
    ]


from . import unreal as _unreal_ops  # noqa: E402,F401  (registers the unreal_* operations)
from . import assets as _asset_ops  # noqa: E402,F401  (registers asset_search, asset_import, page_references)
from . import voice as _voice_ops  # noqa: E402,F401  (speak, transcribe, burn_captions, lipsync, audio polish)
from . import finish as _finishing_ops  # noqa: E402,F401  (film_look, upscale, smooth_motion)
from . import sound_design as _sound_ops  # noqa: E402,F401  (make_music, make_ambience, footsteps, audio_check)
from . import scene as _scene_ops  # noqa: E402,F401  (scene_video)
from . import checks as _check_ops  # noqa: E402,F401  (visual_diff)
from . import motion as _motion_ops  # noqa: E402,F401  (video_to_motion)
from . import stills as _still_ops  # noqa: E402,F401  (render_still)
from . import director as _director_ops  # noqa: E402,F401  (direct_scene, make_anything, style_check)
from . import image3d as _image3d_ops  # noqa: E402,F401  (image_to_3d)
