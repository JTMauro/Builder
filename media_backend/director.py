"""The director: plain words in, a finished, checked piece out.

- direct_scene: Claude turns a request ("he greets the viewer, then shows off a dance") into a shot
  plan for scene_video, Builder renders it, then Claude watches frames of the result next to the
  request and the automatic sound check; if it isn't right it revises the plan and renders again.
- make_anything: one entry point that picks the right tool for a request (scene, still, music,
  ambience, footsteps, voice, image to 3D...) and runs it. Games and web pages are Craft builds, which
  only a person or Rivet starts, so for those it returns how to start one instead.
- style_check: renders a stage from fixed camera views and compares them with the approved pictures
  (visual_diff), or saves the current look as approved.
Needs ANTHROPIC_API_KEY for the two Claude steps (the same key Rivet uses).
"""

from __future__ import annotations

import base64
import json
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError

from . import storage
from .config import get_settings
from .ops import REGISTRY, OpError, OpResult, _guarded, operation, run_operation
from .scene import Beat, SceneParams, list_stages, render_stage, stage_moves, stages_dir
from .voices import VOICES

MOODS = ["none", "calm", "upbeat", "tense", "sad", "epic", "playful", "lofi"]


def _ask(system: str, content: list[dict[str, Any]] | str, max_tokens: int = 3000) -> str:
    """One Claude call; returns the text. (Tests replace this.)"""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise OpError("the director needs ANTHROPIC_API_KEY (the same key Rivet uses) in builder.env")
    import anthropic

    resp = anthropic.Anthropic().messages.create(model=get_settings().model, max_tokens=max_tokens, system=system,
                                                 messages=[{"role": "user", "content": content}])
    return "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")


def _json(text: str) -> Any:
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise OpError("the director's answer had no plan in it")
    return json.loads(m.group(0))


def _plan_prompt(stage: str) -> str:
    moves = stage_moves(stage)
    return f"""You direct short videos with a 3D character (stage "{stage}"). Turn the request into a shot plan.
Answer only JSON: {{"title": str, "voice": one of {list(VOICES)}, "music": one of {MOODS}, "look": one of ["film","warm","cool","noir","vintage","bleach","none"],
"beats": [{{"say"?: str, "move"?: str, "view"?: "body"|"face", "walk_to"?: [x, z], "run"?: bool, "face"?: degrees, "hold"?: seconds}}]}}
Rules: beats run in order. Moves must be from: {", ".join(moves)}. Keep lines short and natural (spoken, under 25 words each).
Use "face" view for important lines and "body" for moves; walk_to stays within 3 metres of the centre ([0, 0]); facing 0 = towards camera.
Aim for the length the request implies (default 10-20 seconds). Keep it kind and original: no real people's likeness, no famous characters, songs or brands."""


def _plan(request: str, stage: str, feedback: str | None = None, previous: dict | None = None) -> dict[str, Any]:
    msg = f"Request: {request}"
    if previous:
        msg += f"\n\nYour previous plan:\n{json.dumps(previous)}\n\nWhat was wrong with the result:\n{feedback}\n\nWrite a corrected plan."
    plan = _json(_ask(_plan_prompt(stage), msg))
    moves = set(stage_moves(stage))
    beats = []
    for b in plan.get("beats", [])[:40]:
        if b.get("move") and b["move"] not in moves:
            b.pop("move")
        try:
            beats.append(Beat(**{k: v for k, v in b.items() if k in Beat.model_fields}).model_dump(exclude_none=True))
        except ValidationError:
            continue
    if not beats:
        raise OpError("the director couldn't make a usable plan from that request")
    plan["beats"] = beats
    plan["voice"] = plan.get("voice") if plan.get("voice") in VOICES else "am_puck"
    plan["music"] = plan.get("music") if plan.get("music") in MOODS else "none"
    plan["look"] = plan.get("look") if plan.get("look") in ("film", "warm", "cool", "noir", "vintage", "bleach", "none") else "film"
    return plan


def _review(request: str, plan: dict, video_id: str, sound: dict) -> dict[str, Any]:
    frames = run_operation("frames", {"file_id": video_id, "count": 6, "width": 640}).outputs
    content: list[dict[str, Any]] = [{"type": "text", "text": f"Request: {request}\nPlan: {json.dumps(plan['beats'])}\nSound check: {json.dumps(sound)}\n"
                                      "These are 6 evenly spaced frames of the rendered video."}]
    for f in frames:
        content.append({"type": "image", "source": {"type": "base64", "media_type": f.mime_type,
                                                    "data": base64.standard_b64encode(storage.path_of(f.id).read_bytes()).decode()}})
    content.append({"type": "text", "text": 'Does the video do what was asked and look right (character visible and framed, no broken poses, captions readable)? '
                                            'Answer only JSON: {"ok": bool, "problems": [str], "notes": str}'})
    return _json(_ask("You review short videos made by a 3D scene renderer. Be specific and brief.", content, 800))


class DirectParams(BaseModel):
    request: str = Field(min_length=3, max_length=2000, description="What should happen, in plain words")
    stage: str = "teen"
    resolution: Literal["640x360", "1280x720", "1920x1080", "3840x2160"] = "1280x720"
    fps: int = Field(24, ge=12, le=60)
    review: bool = Field(True, description="Let Claude watch the result and fix it")
    max_revisions: int = Field(1, ge=0, le=3)


@operation("direct_scene", DirectParams, "Make a short video from a plain-words request: Claude plans the shots (lines, moves, camera, music), Builder renders it with scene_video, then Claude watches the result and fixes it if needed. Long: start with wait=false.")
def direct_scene(p: DirectParams) -> OpResult:
    if p.stage not in list_stages():
        raise OpError(f"no stage called {p.stage!r} (have: {', '.join(list_stages()) or 'none'})")
    plan = _plan(p.request, p.stage)
    history = []
    for attempt in range(p.max_revisions + 1):
        params = SceneParams(beats=plan["beats"], stage=p.stage, voice=plan["voice"], music=plan["music"], look=plan["look"],
                             resolution=p.resolution, fps=p.fps)
        res = run_operation("scene_video", params)
        vid = res.outputs[0].id
        sound = run_operation("audio_check", {"file_id": vid}).data if "audio_check" in REGISTRY else {}
        sound = {k: sound.get(k) for k in ("lufs", "true_peak_db", "issues") if k in sound}
        verdict = _review(p.request, plan, vid, sound) if p.review else {"ok": True, "problems": [], "notes": "not reviewed"}
        history.append({"attempt": attempt + 1, "video": vid, "verdict": verdict})
        if verdict.get("ok") or attempt == p.max_revisions:
            break
        plan = _plan(p.request, p.stage, "; ".join(verdict.get("problems") or []) or verdict.get("notes", ""), plan)
    res.data.update({"title": plan.get("title"), "plan": plan, "reviews": history})
    return res


# ---------------------------------------------------------------- make anything

ROUTES = {
    "direct_scene": "a video of a 3D character acting, talking, moving (needs a stage)",
    "render_still": "a photoreal picture of a 3D character",
    "make_music": "original background music (mood, length)",
    "make_ambience": "background sound of a place (rain, forest, city, room...)",
    "footsteps": "footstep sounds",
    "speak": "a voice-over / spoken line",
    "image_to_3d": "a 3D model from a picture (needs an image file)",
    "craft": "a game, web page, app, tool, 3D object or sound built by the Craft engine",
}


class AnythingParams(BaseModel):
    request: str = Field(min_length=3, max_length=2000)
    file_ids: list[str] = Field(default_factory=list, description="Files the request is about (e.g. a picture to turn into 3D)")


@operation("make_anything", AnythingParams, "Make whatever is asked with the right Builder tool: character videos, photoreal stills, music, ambience, voice-overs, 3D from a picture. For games and pages it explains how to start a Craft build.")
def make_anything(p: AnythingParams) -> OpResult:
    routes = {k: v for k, v in ROUTES.items() if k == "craft" or k in REGISTRY}
    schemas = {k: REGISTRY[k].params.model_json_schema() for k in routes if k in REGISTRY}
    system = ("Pick the one tool that best makes what is asked, and its parameters (follow each tool's JSON schema; leave out what you don't need). "
              f"Tools: {json.dumps(routes)}\nSchemas: {json.dumps(schemas)}\nStages (characters for video): {list_stages()}\n"
              f"Files given: {p.file_ids}\n"
              'Answer only JSON: {"tool": str, "params": {...}, "why": str}')
    choice = _json(_ask(system, p.request, 1500))
    tool, params = choice.get("tool"), dict(choice.get("params") or {})
    if tool == "craft" or tool not in REGISTRY:
        return OpResult(outputs=[], data={"tool": "craft", "why": choice.get("why"),
                                          "next": "This is a Craft build: start it with craft_build_start (Rivet, the Craft screen or the connector) and answer its approvals there."})
    if p.file_ids and "file_id" in REGISTRY[tool].params.model_fields and "file_id" not in params:
        params["file_id"] = p.file_ids[0]
    res = run_operation(tool, params)
    res.data.update({"tool": tool, "params": params, "why": choice.get("why")})
    return res


# ---------------------------------------------------------------- style check

VIEWS = {"face": [{"t": 0, "view": "face"}], "body": [{"t": 0, "view": "body"}], "three_quarter": [{"t": 0, "view": "body", "face": 40}]}


def approved_dir(stage: str) -> Path:
    return Path(os.environ.get("BUILDER_APPROVED_DIR") or (get_settings().data_dir / "approved")).expanduser() / stage


class StyleParams(BaseModel):
    stage: str = "teen"
    approve: bool = Field(False, description="Save the current look as the approved one instead of checking")
    min_ssim: float = Field(0.97, ge=0.5, le=1)


@operation("style_check", StyleParams, "Check a character still looks the way it was approved: renders fixed views (face, full body, three-quarter) and compares each with the approved pictures, with heat maps of any change. approve=true saves the current look as approved.")
def style_check(p: StyleParams) -> OpResult:
    stage = stages_dir() / p.stage
    if not (stage / "index.html").exists():
        raise OpError(f"no stage called {p.stage!r}")
    adir = approved_dir(p.stage)
    if not p.approve and not adir.exists():
        raise OpError("nothing approved yet for this stage: run style_check with approve=true first")
    work = Path(tempfile.mkdtemp(prefix="style-"))
    try:
        results, outputs = {}, []
        for view, beats in VIEWS.items():
            vw = work / view
            vw.mkdir()
            frames, _ = render_stage(stage, {"width": 960, "height": 540, "fps": 4, "duration": 1.5, "beats": beats}, vw)
            last = sorted(frames.glob("*.png"))[-1]
            if p.approve:
                adir.mkdir(parents=True, exist_ok=True)
                shutil.copy(last, adir / f"{view}.png")
                outputs.append(_guarded("style_check", [], f"{p.stage}_{view}_approved.png", lambda out, src=last: shutil.copy(src, out)).outputs[0])
                results[view] = "approved"
                continue
            new = storage.import_path(last).id
            ref = storage.import_path(adir / f"{view}.png").id
            d = run_operation("visual_diff", {"file_id": new, "reference_id": ref, "min_ssim": p.min_ssim})
            outputs += [storage.get(new), *d.outputs]
            results[view] = d.data
        ok = p.approve or all(r["pass"] for r in results.values())
        return OpResult(outputs=outputs, data={"stage": p.stage, "pass": ok, "views": results, "approved_dir": str(adir)})
    finally:
        shutil.rmtree(work, ignore_errors=True)
