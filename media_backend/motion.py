"""Motion capture from ordinary video (free, offline): a person's body and face, frame by frame.

video_to_motion runs Google's MediaPipe on each frame:
- body: 33 3D points in metres (hips at the origin), smoothed; tools/makehuman/view/anim.js
  `fromLandmarks()` turns them into an animation clip for any Mixamo-style rig.
- face (optional): 52 expression scores (jaw open, smile, blinks, brows...) plus the same mapped onto
  Builder's talking-character shapes (viseme_A..H, blink_L/R, smile, brows_up, brows_frown).
Works best with one person, whole body in frame, steady camera. Needs `pip install mediapipe`;
the models (about 13 MB) download on first use into the models folder.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from .config import NO_WINDOW, get_settings
from .ops import OpError, OpResult, _guarded, _src, _stem, media_info, operation
from .voice import _fetch, models_dir

POSE_URL = "https://storage.googleapis.com/mediapipe-models/pose_landmarker/pose_landmarker_full/float16/latest/pose_landmarker_full.task"
FACE_URL = "https://storage.googleapis.com/mediapipe-models/face_landmarker/face_landmarker/float16/latest/face_landmarker.task"
POINTS = ["nose", "left_eye_inner", "left_eye", "left_eye_outer", "right_eye_inner", "right_eye", "right_eye_outer", "left_ear",
          "right_ear", "mouth_left", "mouth_right", "left_shoulder", "right_shoulder", "left_elbow", "right_elbow", "left_wrist",
          "right_wrist", "left_pinky", "right_pinky", "left_index", "right_index", "left_thumb", "right_thumb", "left_hip", "right_hip",
          "left_knee", "right_knee", "left_ankle", "right_ankle", "left_heel", "right_heel", "left_foot_index", "right_foot_index"]


def _model(name: str, url: str) -> Path:
    p = models_dir() / "mediapipe" / name
    if not p.exists():
        p.parent.mkdir(parents=True, exist_ok=True)
        _fetch(url, p)
    return p


def face_to_morphs(bs: dict[str, float]) -> dict[str, float]:
    """ARKit-style face scores to Builder's talking shapes (what gen.py MORPHS=talk makes)."""
    g = lambda k: float(bs.get(k, 0.0))  # noqa: E731
    jaw, funnel, pucker = g("jawOpen"), g("mouthFunnel"), g("mouthPucker")
    smile = (g("mouthSmileLeft") + g("mouthSmileRight")) / 2
    close = max(g("mouthClose"), (g("mouthPressLeft") + g("mouthPressRight")) / 2, g("mouthRollLower"))
    m = {
        "viseme_A": min(1.0, close * 1.5) * (1 - jaw),
        "viseme_D": min(1.0, jaw * 1.4) * (1 - pucker),
        "viseme_C": min(1.0, (g("mouthStretchLeft") + g("mouthStretchRight")) / 2 * 1.5) * (1 - jaw),
        "viseme_E": min(1.0, funnel * 1.4),
        "viseme_F": min(1.0, pucker * 1.4),
        "blink_L": g("eyeBlinkLeft"), "blink_R": g("eyeBlinkRight"),
        "smile": min(1.0, smile * 1.3),
        "brows_up": min(1.0, (g("browInnerUp") + (g("browOuterUpLeft") + g("browOuterUpRight")) / 2) * 0.9),
        "brows_frown": min(1.0, (g("browDownLeft") + g("browDownRight")) / 2 * 1.2),
    }
    return {k: round(max(0.0, v), 3) for k, v in m.items()}


def _smooth(frames: list[list[list[float]] | None], strength: float) -> list[list[list[float]] | None]:
    """Fill short gaps and soften jitter with a centred, visibility-weighted average."""
    import numpy as np

    n = len(frames)
    have = [i for i, f in enumerate(frames) if f is not None]
    if not have:
        return frames
    arr = np.zeros((n, 33, 4), "float32")
    for i in range(n):  # nearest detected frame fills gaps
        j = min(have, key=lambda k: abs(k - i))
        arr[i] = np.asarray(frames[j], "float32")
    r = int(round(strength * 4))
    if r <= 0:
        return arr.tolist()
    out = arr.copy()
    for i in range(n):
        lo, hi = max(0, i - r), min(n, i + r + 1)
        w = np.array([1.0 / (1 + abs(k - i)) for k in range(lo, hi)], "float32")[:, None] * np.clip(arr[lo:hi, :, 3], 0.05, 1)
        out[i, :, :3] = (arr[lo:hi, :, :3] * w[..., None]).sum(0) / w.sum(0)[:, None]
    return out.round(4).tolist()


class MotionParams(BaseModel):
    file_id: str = Field(description="Video of a person moving (whole body in frame works best)")
    fps: int = Field(30, ge=5, le=60, description="Frames per second to sample")
    face: bool = Field(True, description="Also capture facial expressions (needs the face visible)")
    body: bool = True
    smooth: float = Field(0.5, ge=0, le=1, description="Jitter smoothing (0 = raw)")
    max_seconds: float = Field(120, gt=0, le=900)


@operation("video_to_motion", MotionParams, "Capture a person's motion (and facial expressions) from an ordinary video, free and offline (MediaPipe). Returns a .motion.json that the 3D character tools turn into an animation for any rigged person. Use for 'make him move like the person in my video' or acting out a line on camera.")
def video_to_motion(p: MotionParams) -> OpResult:
    try:
        import mediapipe as mp
        import numpy as np
        from mediapipe.tasks.python import BaseOptions, vision
    except ImportError as e:
        raise OpError("video_to_motion needs MediaPipe: pip install mediapipe") from e
    if not (p.face or p.body):
        raise OpError("ask for body, face or both")
    src = _src(p.file_id)
    info = media_info(p.file_id)
    if not info["has_video"]:
        raise OpError("video_to_motion needs a video")
    w0, h0 = info["video"]["width"], info["video"]["height"]
    scale = min(1.0, 960 / max(w0, h0))
    w, h = int(w0 * scale) // 2 * 2, int(h0 * scale) // 2 * 2
    proc = subprocess.run([get_settings().ffmpeg, "-hide_banner", "-loglevel", "error", "-i", str(src), "-t", str(p.max_seconds),
                           "-vf", f"fps={p.fps},scale={w}:{h}", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], capture_output=True, **NO_WINDOW)
    if proc.returncode != 0:
        raise OpError("could not read the video: " + proc.stderr.decode(errors="replace")[-400:])
    frames = np.frombuffer(proc.stdout, np.uint8)
    n = len(frames) // (w * h * 3)
    frames = frames[: n * w * h * 3].reshape(n, h, w, 3)

    pose = vision.PoseLandmarker.create_from_options(vision.PoseLandmarkerOptions(
        base_options=BaseOptions(model_asset_path=str(_model("pose_landmarker_full.task", POSE_URL))),
        running_mode=vision.RunningMode.VIDEO, num_poses=1)) if p.body else None
    face = vision.FaceLandmarker.create_from_options(vision.FaceLandmarkerOptions(
        base_options=BaseOptions(model_asset_path=str(_model("face_landmarker.task", FACE_URL))),
        running_mode=vision.RunningMode.VIDEO, num_faces=1, output_face_blendshapes=True)) if p.face else None
    body: list[Any] = []
    screen: list[Any] = []
    faces: list[Any] = []
    try:
        for i in range(n):
            img = mp.Image(image_format=mp.ImageFormat.SRGB, data=np.ascontiguousarray(frames[i]))
            ts = int(i * 1000 / p.fps)
            if pose:
                r = pose.detect_for_video(img, ts)
                if r.pose_world_landmarks:
                    body.append([[l.x, l.y, l.z, l.visibility] for l in r.pose_world_landmarks[0]])
                    hl, hr = r.pose_landmarks[0][23], r.pose_landmarks[0][24]
                    screen.append([round((hl.x + hr.x) / 2, 4), round((hl.y + hr.y) / 2, 4)])
                else:
                    body.append(None)
                    screen.append(None)
            if face:
                r = face.detect_for_video(img, ts)
                faces.append({c.category_name: round(c.score, 3) for c in r.face_blendshapes[0]} if r.face_blendshapes else None)
    finally:
        for m in (pose, face):
            if m:
                m.close()
    found_body = sum(f is not None for f in body)
    found_face = sum(f is not None for f in faces)
    if p.body and not found_body and (not p.face or not found_face):
        raise OpError("no person found in the video (try a brighter video with the whole body in frame)")
    doc: dict[str, Any] = {"format": "builder-motion/1", "fps": p.fps, "frames": n, "source": _stem(p.file_id),
                           "coords": "metres; x = person's left, y = down, z = away from camera (MediaPipe world landmarks)",
                           "points": POINTS}
    if p.body:
        doc["body"] = _smooth(body, p.smooth)
        doc["hips_screen"] = screen
    if p.face:
        doc["face"] = faces
        doc["morphs"] = [face_to_morphs(f) if f else None for f in faces]
    data = {"frames": n, "fps": p.fps, "body_frames": found_body, "face_frames": found_face, "seconds": round(n / p.fps, 2)}
    return _guarded("video_to_motion", [p.file_id], f"{_stem(p.file_id)}.motion.json",
                    lambda out: out.write_text(json.dumps(doc, separators=(",", ":")), encoding="utf-8"), data=data)
