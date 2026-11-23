"""Make a scene: a character on a stage says lines and does moves, rendered to a finished video.

One call does the whole chain: Kokoro voice for each line (`speak`), mouth shapes and word times
(`lipsync`), voice polish and room sound, a frame-exact render of the stage page in a headless browser
(tools/makehuman/view/scene_render.cjs), footsteps from the feet's real contact times, optional music,
then captions and a film look in the final encode.

A stage is a folder holding the talking viewer (talk.html as index.html, anim.js) and its assets:
models/teen.json, models/light.json, clips/... (tools/makehuman/make_stage.py builds one).
Stages live in <data>/stages/<name> or BUILDER_STAGES_DIR. Rendering needs Node.js with Playwright;
set SCENE_ANGLE=d3d11 (Windows) / metal (macOS) to render on the GPU instead of the slow software path.
"""

from __future__ import annotations

import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from .config import NO_WINDOW, get_settings
from . import storage
from .ops import AUDIO_ENC, REGISTRY, VIDEO_ENC, OpError, OpResult, _ffmpeg, _guarded, operation, run_operation
from .voices import VOICES

REPO_VIEW = Path(__file__).resolve().parents[1] / "tools" / "makehuman" / "view"
SPEED = {"walk": 1.15, "run": 3.4}


def stages_dir() -> Path:
    return Path(os.environ.get("BUILDER_STAGES_DIR") or (get_settings().data_dir / "stages")).expanduser()


def list_stages() -> list[str]:
    d = stages_dir()
    return sorted(p.name for p in d.iterdir() if (p / "index.html").exists()) if d.exists() else []


class Shot(BaseModel):
    type: Literal["wide", "two", "medium", "close", "over"] = Field(description="wide: everyone; two: a tighter group shot; medium/close: one actor; over: over actor's shoulder looking at target")
    actor: str | None = None
    target: str | None = None
    move: float = Field(0, ge=0, le=20, description="Seconds to glide to this shot (0 = cut)")


class CastMember(BaseModel):
    id: str = Field(min_length=1, max_length=20)
    model: str = Field("teen", description="Character model on the stage (models/<name>.json)")
    voice: str | None = Field(None, description="Kokoro voice for this character's lines")
    at: list[float] = Field(default_factory=lambda: [0.0, 0.0], min_length=2, max_length=2)
    face: float = 0


class Beat(BaseModel):
    actor: str | None = Field(None, description="Which cast member (default: the first)")
    look: str | None = Field(None, description="Where the actor looks: 'camera' or another cast id")
    shot: Shot | None = Field(None, description="Change the camera shot (studio stages)")
    prop: dict | str | None = Field(None, description='Hold a prop: {"prop": "football", "hand": "right"|"left"}, or "none" to put it away (studio stages; props in the stage\'s props folder)')
    say: str | None = Field(None, max_length=600, description="A line to speak")
    voice: str | None = Field(None, description="Kokoro voice for this line (default: the scene's voice)")
    move: str | None = Field(None, max_length=40, description="A move or gesture: idle, walk, run, wave, point, thumbs up, agree (nod), headShake, arms crossed, jump, sit, dance, shrug, bow, stretch, kick, pickup, hello, laugh, or any extra CMU move on the stage (explain, sneak, happy walk, moonwalk, ...)")
    view: Literal["body", "face"] | None = Field(None, description="Camera: full body or close-up on the face")
    walk_to: list[float] | None = Field(None, min_length=2, max_length=2, description="[x, z] metres on the floor to walk (or run) to")
    run: bool = False
    face: float | None = Field(None, description="Turn to this heading in degrees (0 = towards the default camera)")
    at: float | None = Field(None, ge=0, description="Start time in seconds (default: after the previous beat)")
    hold: float = Field(2.5, ge=0, le=30, description="Seconds to give a move without a line before the next beat")


class SceneParams(BaseModel):
    beats: list[Beat] = Field(min_length=1, max_length=60)
    stage: str = Field("teen", description="Stage (character) name; see the stages folder")
    cast: list[CastMember] = Field(default_factory=list, max_length=6, description="Several characters (default: the stage's one)")
    set: str | None = Field(None, description="A place on the stage: park, living_room, street, beach, gym, school, cafe, stadium, snow (see the stage's sets folder); default the grey studio")
    quality: Literal["film", "fast"] = Field("film", description="fast skips ambient occlusion (quicker renders)")
    voice: Literal[tuple(VOICES)] = Field("am_puck", description="Default voice")  # type: ignore[valid-type]
    resolution: Literal["640x360", "1280x720", "1920x1080", "2560x1440", "3840x2160"] = "1280x720"
    fps: int = Field(30, ge=12, le=60)
    captions: bool = True
    look: Literal["film", "warm", "cool", "noir", "vintage", "bleach", "none"] = "film"
    music: Literal["none", "calm", "upbeat", "tense", "sad", "epic", "playful", "lofi"] = "none"
    footsteps: bool = True
    room: Literal["studio", "small room", "living room", "hall", "outdoors"] = "studio"
    tail: float = Field(1.0, ge=0, le=10, description="Seconds to keep rolling after the last beat")


def _srt_time(t: float) -> str:
    ms = int(round(t * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02}:{m:02}:{s:02},{ms:03}"


def _captions(lines: list[dict], max_words: int = 7) -> str:
    """SRT with short caption chunks from each line's word times (already offset to scene time)."""
    cues = []
    for ln in lines:
        words = ln["words"]
        for i in range(0, len(words), max_words):
            chunk = words[i:i + max_words]
            cues.append([ln["t"] + chunk[0]["s"], ln["t"] + chunk[-1]["e"] + 0.25, " ".join(w["w"] for w in chunk)])
    cues.sort()
    for a, b in zip(cues, cues[1:]):   # never two captions on screen at once
        a[1] = min(a[1], b[0] - 0.01)
    return "\n".join(f"{n}\n{_srt_time(s)} --> {_srt_time(e)}\n{t}\n" for n, (s, e, t) in enumerate(cues, 1))


def _node() -> str:
    node = shutil.which("node")
    if not node:
        raise OpError("scene_video needs Node.js with Playwright (see `builder doctor`)")
    return node


def render_stage(stage: Path, scene: dict, work: Path) -> tuple[Path, list[dict]]:
    """Render a scene dict on a stage folder to work/frames/*.png; returns (frames dir, foot events)."""
    driver = Path(os.environ.get("SCENE_RENDERER") or REPO_VIEW / "scene_render.cjs")
    if not driver.exists():
        raise OpError(f"scene renderer not found at {driver}")
    (work / "scene.json").write_text(json.dumps(scene), encoding="utf-8")
    frames = work / "frames"
    runner = [sys.executable] if driver.suffix == ".py" else [_node()]   # .py: a stand-in renderer (tests)
    proc = subprocess.run([*runner, str(driver), str(stage), str(work / "scene.json"), str(frames)],
                          capture_output=True, text=True, cwd=str(driver.parent), **NO_WINDOW)
    if proc.returncode != 0:
        raise OpError("render failed:\n" + "\n".join((proc.stderr or proc.stdout).strip().splitlines()[-8:]))
    events = json.loads((frames / "events.json").read_text(encoding="utf-8")) if (frames / "events.json").exists() else []
    return frames, events


def stage_moves(stage: str) -> list[str]:
    """Moves a stage can play: built-in clips and gestures, plus its extra motion-capture moves."""
    base = ["idle", "walk", "run", "wave", "point", "thumbs up", "agree", "headShake", "arms crossed",
            "jump", "sit", "dance", "shrug", "bow", "stretch", "kick", "pickup", "hello", "laugh"]
    ix = stages_dir() / stage / "clips" / "cmu" / "index.json"
    extra = list(json.loads(ix.read_text(encoding="utf-8"))) if ix.exists() else []
    return base + [m for m in extra if m not in base]


@operation("scene_video", SceneParams, "Make a finished video of a 3D character acting out a scene: give beats (lines to say, moves, camera, walking); Builder voices each line, lip-syncs it, renders the stage frame by frame, adds footsteps, optional original music, captions and a film look. Long: start with wait=false.")
def scene_video(p: SceneParams) -> OpResult:
    stage = stages_dir() / p.stage
    if not (stage / "index.html").exists():
        raise OpError(f"no stage called {p.stage!r} (have: {', '.join(list_stages()) or 'none'}); build one with tools/makehuman/make_stage.py")
    w, h = (int(x) for x in p.resolution.split("x"))

    # 1. voices and the timeline
    beats, lines, voice_tracks, made = [], [], [], []
    cursor, end = 0.4, 0.0
    for i, b in enumerate(p.beats):
        t = b.at if b.at is not None else cursor
        beat = {"t": round(t, 3)}
        for k in ("move", "view", "face", "actor", "look", "prop"):
            if getattr(b, k) is not None:
                beat[k] = getattr(b, k)
        if b.shot:
            beat["shot"] = b.shot.model_dump(exclude_none=True)
        if b.walk_to:
            beat["walkTo"], beat["run"] = b.walk_to, b.run
        dur = b.hold if (b.move or b.walk_to) else 0.3
        if b.walk_to:
            prev = next((x["walkTo"] for x in reversed(beats) if "walkTo" in x), [0, 0])
            dur = max(dur, math.dist(prev, b.walk_to) / SPEED["run" if b.run else "walk"] + 0.4)
        if b.say:
            key = f"l{i}"
            cast_voice = next((c.voice for c in p.cast if c.id == b.actor and c.voice), None)
            sp = run_operation("speak", {"text": b.say, "voice": b.voice or cast_voice or p.voice, "name": f"scene_{key}"}).outputs[0].id
            cues = json.loads(storage.path_of(run_operation("lipsync", {"file_id": sp}).outputs[0].id).read_text(encoding="utf-8"))
            polished = run_operation("voice_polish", {"file_id": sp, "style": "podcast"}).outputs[0].id
            roomed = run_operation("room_reverb", {"file_id": polished, "room": p.room}).outputs[0].id
            made += [sp, polished, roomed]
            words = [{"w": x["word"].strip(), "s": round(x["start"], 3), "e": round(x["end"], 3)} for x in cues.get("words", [])]
            lines.append({"key": key, "text": b.say, "duration": cues["metadata"]["duration"], "t": t,
                          "cues": [[c["start"], c["value"]] for c in cues["mouthCues"]], "words": words})
            beat["say"] = key
            voice_tracks.append((roomed, t))
            dur = max(dur if (b.move or b.walk_to) else 0, cues["metadata"]["duration"] + 0.35)
        beats.append(beat)
        cursor = t + dur
        end = max(end, cursor)
    duration = round(end + p.tail, 2)

    work = Path(tempfile.mkdtemp(prefix="scene-"))
    try:
        # 2. frames
        scene = {"width": w, "height": h, "fps": p.fps, "duration": duration, "beats": beats,
                 "lines": [{k: v for k, v in ln.items() if k != "t"} for ln in lines]}
        studio = (stage / "studio.html").exists()
        if p.cast or p.set or any(b.shot or b.prop for b in p.beats):
            if not studio:
                raise OpError("this stage has no studio page (several characters, sets and shots need studio.html; rebuild it with make_stage.py)")
            if p.set and not (stage / "sets" / p.set / "set.json").exists():
                have = sorted(d.name for d in (stage / "sets").iterdir()) if (stage / "sets").exists() else []
                raise OpError(f"no set called {p.set!r} on this stage (have: {', '.join(have) or 'none'}; add one with add_set.py)")
        if studio:
            cast = [c.model_dump() for c in p.cast] or [{"id": "a", "model": "teen", "at": [0, 0], "face": 0}]
            scene.update({"page": "studio.html", "cast": cast, "set": p.set, "quality": p.quality})
        frames, events = render_stage(stage, scene, work)

        # 3. sound: voices at their times, footsteps at the feet's contact times, music under it all
        tracks: list[tuple[Path, float, float]] = [(storage.path_of(fid), t, 1.0) for fid, t in voice_tracks]
        steps = [e["t"] for e in events if "step" in e]
        if p.footsteps and steps and "footsteps" in REGISTRY:
            fs = run_operation("footsteps", {"surface": "concrete", "times": steps, "length_s": duration}).outputs[0].id
            made.append(fs)
            tracks.append((storage.path_of(fs), 0.0, 0.6))
        if p.music != "none" and "make_music" in REGISTRY:
            mu = run_operation("make_music", {"mood": p.music, "length_s": max(5.0, duration)}).outputs[0].id
            made.append(mu)
            tracks.append((storage.path_of(mu), 0.0, 0.22))
        args = ["-framerate", str(p.fps), "-i", str(frames / "%06d.png")]
        for path, _, _ in tracks:
            args += ["-i", str(path)]
        fc = []
        for n, (_, t, vol) in enumerate(tracks, start=1):
            ms = int(round(t * 1000))
            fc.append(f"[{n}:a]adelay={ms}|{ms},volume={vol}[a{n}]")
        if tracks:
            fc.append("".join(f"[a{n}]" for n in range(1, len(tracks) + 1)) + f"amix=inputs={len(tracks)}:normalize=0:duration=longest,"
                      f"apad,atrim=0:{duration},loudnorm=I=-16:TP=-2:LRA=11,aresample=48000[aout]")

        # 4. picture: film look, then captions on top
        vf = []
        if p.look != "none":
            from .finish import LOOKS

            vf += [LOOKS[p.look], "noise=alls=7:allf=t", "vignette=angle=PI/5"]
        if p.captions and lines:
            from .voice import _filter_path

            srt = work / "captions.srt"
            srt.write_text(_captions(lines), encoding="utf-8")
            size = max(14, round(h / 720 * 20))
            vf.append(f"subtitles='{_filter_path(srt)}':force_style='FontName=DejaVu Sans,Fontsize={size},PrimaryColour=&H00FFFFFF,"
                      f"BackColour=&H80000000,BorderStyle=4,Outline=0,Shadow=0,Alignment=2,MarginV=22'")
        vf.append("format=yuv420p")
        args += ["-filter_complex", ";".join([f"[0:v]{','.join(vf)}[vout]"] + fc), "-map", "[vout]"]
        if tracks:
            args += ["-map", "[aout]", *AUDIO_ENC]
        args += [*VIDEO_ENC, "-t", str(duration)]
        n_frames = len(list(frames.glob("*.png")))
        return _guarded("scene_video", made, f"scene_{p.stage}_{w}x{h}.mp4", lambda out: _ffmpeg(*args, str(out)),
                        data={"duration_s": duration, "frames": n_frames, "lines": len(lines), "footsteps": len(steps),
                              "timeline": beats, "stage": p.stage})
    finally:
        shutil.rmtree(work, ignore_errors=True)
