"""make_video_short: one request -> a finished vertical social short with 3D characters.

The director plans the scene (Claude, via director._ask; without ANTHROPIC_API_KEY give the beats yourself),
the shots are made vertical-friendly (closer: medium / close / over instead of wide), scene_video renders it
at 1080x1920 (or 720x1280 for drafts) with big bold captions from the exact line timings, then make_short
trims it to length and sets social loudness.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from .ops import OpError, OpResult, operation, run_operation

VERTICAL_HINT = (" (This is a vertical 9:16 social short: favour close, medium and over-the-shoulder shots, keep characters"
                 " near each other (within 1.5 m), keep it punchy, under {s} seconds.)")


def vertical_shots(beats: list[dict], cast_ids: list[str]) -> list[dict]:
    """Closer shots for a tall frame: wide -> two (or medium for one person); a scene without shots gets a medium
    opening, a medium on whoever speaks (a close-up when the beat has view "face") and on whoever moves."""
    out = [dict(b) for b in beats]
    first = cast_ids[0] if cast_ids else None
    has_shots = any(b.get("shot") for b in out)
    for i, b in enumerate(out):
        who = b.get("actor") or first
        if has_shots:
            s = b.get("shot")
            if s and s.get("type") == "wide":
                b["shot"] = {**s, "type": "two" if len(cast_ids) > 1 else "medium", **({} if len(cast_ids) > 1 else {"actor": s.get("actor") or who})}
            continue
        face = b.pop("view", None) == "face"
        if b.get("say"):   # a close-up is very tight in a tall frame: medium (head and hands) unless the beat asks for the face
            b["shot"] = {"type": "close" if face else "medium", "actor": who}
        elif b.get("move") or b.get("walk_to") or b.get("on"):
            b["shot"] = {"type": "medium", "actor": who} if not b.get("walk_to") else {"type": "two" if len(cast_ids) > 1 else "medium", **({"actor": who} if len(cast_ids) < 2 else {})}
        elif i == 0:
            b["shot"] = {"type": "two" if len(cast_ids) > 1 else "medium", **({"actor": who} if len(cast_ids) < 2 else {})}
    if out and not out[0].get("shot"):
        out[0]["shot"] = {"type": "two" if len(cast_ids) > 1 else "medium", **({"actor": first} if len(cast_ids) < 2 and first else {})}
    return out


class VideoShortParams(BaseModel):
    request: str | None = Field(None, max_length=2000, description="What happens, in plain words (the director plans it; needs ANTHROPIC_API_KEY)")
    beats: list[dict[str, Any]] = Field(default_factory=list, max_length=60, description="Or the scene's beats yourself (scene_video beats)")
    cast: list[dict[str, Any]] = Field(default_factory=list, max_length=6)
    set: str | None = None
    stage: str = "teen"
    resolution: Literal["1080x1920", "720x1280", "360x640"] = Field("1080x1920", description="720x1280 renders about twice as fast (drafts)")
    fps: int = Field(30, ge=12, le=60)
    quality: Literal["film", "fast"] = "film"
    voice: str | None = None
    music: Literal["none", "calm", "upbeat", "tense", "sad", "epic", "playful", "lofi"] | None = Field(None, description="Default: the director's pick (or none)")
    look: Literal["film", "warm", "cool", "noir", "vintage", "bleach", "none"] | None = None
    max_seconds: float = Field(45, ge=3, le=180)
    title: str | None = Field(None, max_length=80)


@operation("make_video_short", VideoShortParams, "Make a vertical social short (TikTok / Reels / Shorts) with 3D characters from one request: the director plans it (or give beats), shots are made closer for the tall frame, it's rendered at 1080x1920 with big bold captions, then trimmed and levelled for social. Long: start with wait=false.")
def make_video_short(p: VideoShortParams) -> OpResult:
    from . import director

    plan: dict[str, Any] = {}
    if p.beats:
        beats = p.beats
    elif p.request:
        try:
            plan = director._plan(p.request + VERTICAL_HINT.format(s=int(p.max_seconds)), p.stage)
        except OpError as e:
            if "ANTHROPIC_API_KEY" in str(e):
                raise OpError(str(e) + "; or give the beats yourself (beats=[...])") from e
            raise
        beats = plan["beats"]
    else:
        raise OpError("say what should happen (request) or give the beats")
    cast = p.cast or plan.get("cast") or []
    ids = [c.get("id") for c in cast if c.get("id")] or ["a"]
    params = {"beats": vertical_shots(beats, ids), "stage": p.stage, "cast": cast, "set": p.set if p.set is not None else plan.get("set"),
              "resolution": p.resolution, "fps": p.fps, "quality": p.quality, "captions": True, "caption_style": "bold",
              "music": p.music or plan.get("music") or "none", "look": p.look or plan.get("look") or "film"}
    if p.voice or plan.get("voice"):
        params["voice"] = p.voice or plan["voice"]
    scene = run_operation("scene_video", params)
    vid = scene.outputs[0].id
    short = run_operation("make_short", {"file_id": vid, "aspect": "9:16", "mode": "crop", "captions": False, "max_seconds": p.max_seconds})
    short.data.update({"title": p.title or plan.get("title"), "scene_video": vid, "plan": {**plan, "beats": params["beats"]},
                       "duration_s": scene.data.get("duration_s"), "resolution": p.resolution})
    return short
