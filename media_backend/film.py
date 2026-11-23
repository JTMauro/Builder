"""Longer pieces and other versions: films from several scenes, dubbing, and social shorts.

- make_film: renders several scenes (scene_video), joins them with cuts, fades or dissolves, adds a
  title card and end credits, lays one soundtrack under the whole film that dips under speech, and can
  also write a DaVinci Resolve timeline of the scenes for editing.
- dub_scene: the same scene in another language: Claude translates the lines, voices change to that
  language's voices, and the scene is re-rendered so the lips match the new words.
- dub_video: any video with speech: transcribe, translate, speak each sentence at its time in the new
  language over the (lowered) original sound.
- make_short: a vertical (or square) social version with big captions and social loudness.
"""

from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from . import storage
from .ops import AUDIO_ENC, FONT, VIDEO_ENC, OpError, OpResult, _ffmpeg, _guarded, media_info, operation, run_operation

LANG_NAMES = {"es": "Spanish", "fr": "French", "it": "Italian", "pt": "Brazilian Portuguese", "hi": "Hindi", "en": "English"}
LANG_VOICES = {"es": ("ef_dora", "em_alex"), "fr": ("ff_siwis", "ff_siwis"), "it": ("if_sara", "im_nicola"),
               "pt": ("pf_dora", "pm_alex"), "hi": ("hf_alpha", "hm_omega"), "en": ("af_heart", "am_liam")}


def _voice_for(lang: str, original: str | None) -> str:
    female, male = LANG_VOICES[lang]
    return female if (original or "a")[1:2] == "f" else male


def _translate(lines: list[str], lang: str, context: str = "") -> list[str]:
    """Translate short spoken lines with Claude, keeping their number and order."""
    from .director import _ask, _json

    if not lines:
        return []
    system = (f"Translate each spoken line into natural, casual {LANG_NAMES[lang]} that fits the same speaking time. "
              'Keep names. Answer only JSON: {"lines": [..same count, same order..]}')
    out = _json(_ask(system, (context + "\n" if context else "") + json.dumps(lines, ensure_ascii=False), 2000)).get("lines", [])
    if len(out) != len(lines):
        raise OpError("the translation came back with a different number of lines")
    return [str(x) for x in out]


def _card(text: str, seconds: float, w: int, h: int, fps: int, out: Path, sub: str = "") -> None:
    """A dark title card with centred text (text goes through a file, never into the filter string)."""
    from .voice import _filter_path

    tmp = Path(tempfile.mkdtemp(prefix="card-"))
    try:
        (tmp / "t.txt").write_text(text, encoding="utf-8")
        (tmp / "s.txt").write_text(sub or " ", encoding="utf-8")
        size = max(18, int(h * 0.075))
        vf = (f"drawtext=fontfile='{_filter_path(Path(FONT))}':textfile='{_filter_path(tmp / 't.txt')}':fontsize={size}:fontcolor=white:"
              f"x=(w-text_w)/2:y=(h-text_h)/2-{size // 2}:line_spacing={size // 3},"
              f"drawtext=fontfile='{_filter_path(Path(FONT))}':textfile='{_filter_path(tmp / 's.txt')}':fontsize={max(12, size // 3)}:"
              f"fontcolor=0xb8c0c6:x=(w-text_w)/2:y=(h/2)+{size}:line_spacing=6,fade=t=in:st=0:d=0.5,fade=t=out:st={max(0.1, seconds - 0.6)}:d=0.5")
        _ffmpeg("-f", "lavfi", "-i", f"color=c=0x111417:s={w}x{h}:r={fps}:d={seconds}", "-f", "lavfi", "-i", f"anullsrc=r=48000:cl=stereo",
                "-t", str(seconds), "-vf", vf, *VIDEO_ENC, *AUDIO_ENC, "-shortest", str(out))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _normalize(src: Path, w: int, h: int, fps: int, out: Path) -> None:
    """Same size, frame rate and audio layout, so clips can be joined with transitions."""
    _ffmpeg("-i", str(src), "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo",
            "-filter_complex", f"[0:v]scale={w}:{h}:force_original_aspect_ratio=decrease,pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,fps={fps},format=yuv420p,setsar=1[v];"
                               f"[0:a][1:a]amix=inputs=2:duration=first:normalize=0,aresample=48000,aformat=channel_layouts=stereo[a]",
            "-map", "[v]", "-map", "[a]", *VIDEO_ENC, *AUDIO_ENC, str(out))


def _duration(p: Path) -> float:
    return float(media_info(storage.import_path(p).id)["duration_s"] or 0)


class FilmParams(BaseModel):
    scenes: list[dict[str, Any]] = Field(min_length=1, max_length=20, description="scene_video parameters for each scene, in order (their resolution/fps are set from the film)")
    title: str | None = Field(None, max_length=120)
    subtitle: str | None = Field(None, max_length=200)
    credits: bool = Field(True, description="End card crediting the free tools and assets used")
    transition: Literal["cut", "fade", "dissolve", "wipe"] = "dissolve"
    transition_s: float = Field(0.6, ge=0, le=3)
    music: Literal["none", "calm", "upbeat", "tense", "sad", "epic", "playful", "lofi"] = Field("calm", description="One soundtrack under the whole film; it dips under speech")
    resolution: Literal["640x360", "1280x720", "1920x1080", "3840x2160"] = "1280x720"
    fps: int = Field(24, ge=12, le=60)
    timeline: bool = Field(True, description="Also write a DaVinci Resolve / Final Cut timeline (.fcpxml) of the scenes")


CREDITS = ("Made with Builder\nCharacters: MakeHuman (CC0) · Voices: Kokoro (Apache-2.0)\n"
           "Motion capture: CMU Graphics Lab, mocap.cs.cmu.edu (NSF EIA-0196217)\nSets and light: Poly Haven (CC0)")


@operation("make_film", FilmParams, "Make a short film from several scenes: each scene rendered with scene_video, joined with cuts, fades or dissolves, a title card and end credits, one soundtrack that dips under speech, plus an editable DaVinci Resolve timeline. Very long on CPU: start with wait=false.")
def make_film(p: FilmParams) -> OpResult:
    w, h = (int(x) for x in p.resolution.split("x"))
    work = Path(tempfile.mkdtemp(prefix="film-"))
    try:
        parts: list[Path] = []
        scene_ids = []
        if p.title:
            _card(p.title, 3.0, w, h, p.fps, work / "title.mp4", p.subtitle or "")
            parts.append(work / "title.mp4")
        for i, sc in enumerate(p.scenes):
            params = {**sc, "resolution": p.resolution, "fps": p.fps}
            if p.music != "none":
                params["music"] = "none"   # one soundtrack for the whole film instead
            res = run_operation("scene_video", params)
            scene_ids.append(res.outputs[0].id)
            _normalize(storage.path_of(res.outputs[0].id), w, h, p.fps, work / f"s{i}.mp4")
            parts.append(work / f"s{i}.mp4")
        if p.credits:
            _card("Thanks for watching", 4.5, w, h, p.fps, work / "credits.mp4", CREDITS)
            parts.append(work / "credits.mp4")
        durs = [_duration(x) for x in parts]
        n = len(parts)
        args: list[str] = []
        for x in parts:
            args += ["-i", str(x)]
        fc, vlast, alast, offset = [], "0:v", "0:a", durs[0]
        T = p.transition_s if p.transition != "cut" else 0
        kind = {"fade": "fadeblack", "dissolve": "dissolve", "wipe": "wipeleft", "cut": "fade"}[p.transition]
        for i in range(1, n):
            if T > 0:
                offset -= T
                fc.append(f"[{vlast}][{i}:v]xfade=transition={kind}:duration={T}:offset={offset:.3f}[v{i}]")
                fc.append(f"[{alast}][{i}:a]acrossfade=d={T}[a{i}]")
            else:
                fc.append(f"[{vlast}][{alast}][{i}:v][{i}:a]concat=n=2:v=1:a=1[v{i}][a{i}]")
            vlast, alast, offset = f"v{i}", f"a{i}", offset + durs[i]
        total = round(offset, 2)
        made = list(scene_ids)
        if p.music != "none":
            mu = run_operation("make_music", {"mood": p.music, "length_s": max(5.0, total)}).outputs[0].id
            made.append(mu)
            args += ["-i", str(storage.path_of(mu))]
            m = n
            # the dialogue pushes the music down (sidechain ducking), then everything is levelled
            fc.append(f"[{alast}]asplit=2[dlg][key]")
            fc.append(f"[{m}:a]volume=0.5,afade=t=in:d=1.5,afade=t=out:st={max(0, total - 3)}:d=3[mus]")
            fc.append("[mus][key]sidechaincompress=threshold=0.03:ratio=8:attack=20:release=400[duck]")
            fc.append(f"[dlg][duck]amix=inputs=2:normalize=0:duration=first,loudnorm=I=-16:TP=-2:LRA=11,aresample=48000[aout]")
        else:
            fc.append(f"[{alast}]loudnorm=I=-16:TP=-2:LRA=11,aresample=48000[aout]")
        vmap = f"[{vlast}]" if n > 1 else "0:v"
        res = _guarded("make_film", made, f"{(p.title or 'film').replace(' ', '_')[:40]}.mp4",
                       lambda out: _ffmpeg(*args, "-filter_complex", ";".join(fc), "-map", vmap, "-map", "[aout]",
                                           *VIDEO_ENC, *AUDIO_ENC, "-t", str(total), str(out)),
                       data={"duration_s": total, "scenes": len(p.scenes), "transition": p.transition})
        if p.timeline and scene_ids:
            fps = p.fps if p.fps in (24, 25, 30, 48, 50, 60) else 30
            tl = run_operation("timeline_export", {"clips": [{"file_id": f, "name": f"Scene {i + 1}"} for i, f in enumerate(scene_ids)],
                                                   "name": p.title or "Builder film", "fps": fps})
            res.outputs += tl.outputs
            res.data["timeline"] = tl.outputs[0].name
        return res
    finally:
        shutil.rmtree(work, ignore_errors=True)


class DubSceneParams(BaseModel):
    scene: dict[str, Any] = Field(description="The scene_video parameters to re-make in another language")
    language: Literal["es", "fr", "it", "pt", "hi", "en"] = Field(description="es Spanish, fr French, it Italian, pt Portuguese (Brazil), hi Hindi, en English")


@operation("dub_scene", DubSceneParams, "Re-make a character scene in another language (Spanish, French, Italian, Portuguese, Hindi, English): lines translated by Claude, voices switched to that language, and lips re-synced to the new words.")
def dub_scene(p: DubSceneParams) -> OpResult:
    sc = json.loads(json.dumps(p.scene))
    beats = sc.get("beats") or []
    idx = [i for i, b in enumerate(beats) if b.get("say")]
    new = _translate([beats[i]["say"] for i in idx], p.language)
    for i, text in zip(idx, new):
        beats[i]["say"] = text
        if beats[i].get("voice"):
            beats[i]["voice"] = _voice_for(p.language, beats[i]["voice"])
    for c in sc.get("cast") or []:
        c["voice"] = _voice_for(p.language, c.get("voice"))
    sc["voice"] = _voice_for(p.language, sc.get("voice") or "am_puck")
    res = run_operation("scene_video", sc)
    res.data.update({"language": p.language, "lines": new})
    return res


class DubVideoParams(BaseModel):
    file_id: str = Field(description="A video (or audio) with speech")
    language: Literal["es", "fr", "it", "pt", "hi", "en"]
    voice: str | None = Field(None, description="Voice to use (default: a voice for that language)")
    original_level: float = Field(0.18, ge=0, le=1, description="How loud the original sound stays underneath")


@operation("dub_video", DubVideoParams, "Dub any video into another language: transcribe the speech, translate it with Claude, speak each sentence at its original time with a local voice, over the lowered original sound. (Lips aren't changed; for 3D scenes use dub_scene.)")
def dub_video(p: DubVideoParams) -> OpResult:
    from .voice import words_of

    info = media_info(p.file_id)
    segments, _, _ = words_of(p.file_id)
    segments = [s for s in segments if s.get("text", "").strip()]
    if not segments:
        raise OpError("no speech found to dub")
    texts = _translate([s["text"].strip() for s in segments], p.language)
    voice = p.voice or _voice_for(p.language, "am_")
    made, tracks = [], []
    for seg, text in zip(segments, texts):
        sp = run_operation("speak", {"text": text, "voice": voice, "name": f"dub_{p.language}"}).outputs[0].id
        made.append(sp)
        slot = max(0.4, float(seg["end"]) - float(seg["start"]))
        dur = float(media_info(sp)["duration_s"] or slot)
        tempo = min(1.35, max(1.0, dur / slot))   # squeeze a long sentence a little to fit its slot
        tracks.append((storage.path_of(sp), float(seg["start"]), tempo))
    src = storage.path_of(p.file_id)
    args = ["-i", str(src)]
    for t, _, _ in tracks:
        args += ["-i", str(t)]
    fc = [f"[0:a]volume={p.original_level}[orig]"]
    for n, (_, start, tempo) in enumerate(tracks, start=1):
        ms = int(start * 1000)
        fc.append(f"[{n}:a]atempo={tempo:.3f},adelay={ms}|{ms}[d{n}]")
    fc.append("[orig]" + "".join(f"[d{n}]" for n in range(1, len(tracks) + 1)) +
              f"amix=inputs={len(tracks) + 1}:normalize=0:duration=first,loudnorm=I=-16:TP=-2:LRA=11[aout]")
    vmap = ["-map", "0:v", "-c:v", "copy"] if info["has_video"] else []
    ext = "mp4" if info["has_video"] else "m4a"
    return _guarded("dub_video", [p.file_id, *made], f"{Path(src).stem}_{p.language}.{ext}",
                    lambda out: _ffmpeg(*args, "-filter_complex", ";".join(fc), *vmap, "-map", "[aout]", *AUDIO_ENC, str(out)),
                    data={"language": p.language, "sentences": len(tracks), "lines": texts})


class ShortParams(BaseModel):
    file_id: str
    aspect: Literal["9:16", "1:1", "4:5"] = "9:16"
    mode: Literal["crop", "blur"] = Field("blur", description="crop fills the frame; blur keeps the whole picture over a blurred copy")
    captions: bool = Field(True, description="Big social-style captions from the speech")
    max_seconds: float = Field(60, ge=3, le=180)
    focus: float = Field(0.5, ge=0, le=1)


@operation("make_short", ShortParams, "Make a social short (TikTok, Reels, YouTube Shorts) from a video: vertical or square, big captions from the speech, trimmed to length, social loudness (-14 LUFS).")
def make_short(p: ShortParams) -> OpResult:
    dur = float(media_info(p.file_id)["duration_s"] or 0)
    fid = p.file_id
    made = []
    if dur > p.max_seconds:
        fid = run_operation("trim", {"file_id": fid, "start": 0, "end": p.max_seconds}).outputs[0].id
        made.append(fid)
    fid = run_operation("reframe", {"file_id": fid, "aspect": p.aspect, "mode": p.mode, "height": 1920 if p.aspect == "9:16" else 1080, "focus": p.focus}).outputs[0].id
    made.append(fid)
    if p.captions and media_info(fid)["has_audio"]:
        fid = run_operation("burn_captions", {"file_id": fid, "style": "bold-yellow", "position": "middle", "size": 20}).outputs[0].id
        made.append(fid)
    src = storage.path_of(fid)
    has_audio = media_info(fid)["has_audio"]
    return _guarded("make_short", made, f"{Path(src).stem}_short.mp4",
                    lambda out: _ffmpeg("-i", str(src), "-c:v", "copy", *(["-af", "loudnorm=I=-14:TP=-1.5:LRA=11", *AUDIO_ENC] if has_audio else ["-an"]), str(out)),
                    data={"aspect": p.aspect, "captions": p.captions})
