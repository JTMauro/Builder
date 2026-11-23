"""Local AI senses for checking your own work: watch a clip, put a sound into words, score how a picture looks.

watch_video     A small vision-language model (Qwen3-VL-2B-Instruct, Apache-2.0) watches frames sampled over time
                (as a video, with timestamps, so it sees motion) and answers a question about the clip, or describes
                it, with per-timestamp notes. CPU only; the first call downloads ~4.3 GB to <models>/vlm.
caption_audio   CLAP zero-shot tagging (laion/larger_clap_general, Apache-2.0) against a vocabulary of sounds,
                voices, instruments, rooms, moods and recording qualities, joined with describe_audio's measurements
                into a sentence. (Qwen3-Omni-30B-A3B-Captioner would write real captions, but at ~30B parameters
                it needs a large GPU; CLAP runs on a CPU in seconds.)
quality_score   Perceptual distance between a render and a reference (LPIPS, BSD-2, AlexNet features) and a
                no-reference aesthetic score (LAION aesthetic predictor v1, MIT, a linear head on OpenAI CLIP ViT-B/32,
                MIT).

These are small models: good at "is there a person waving?", "is this rain or applause?", "did the look change?";
unreliable on fine detail (a hand clipping through a leg for two frames). Treat answers as a second opinion and
check the frames they point to (review_video's contact sheet) before trusting them.

pip install "builder[senses]" (torch CPU, transformers, torchvision, lpips, huggingface_hub).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from . import storage
from .config import NO_WINDOW, get_settings
from .ops import OpError, OpResult, _guarded, _src, _stem, media_info, operation

VLM_REPO = "Qwen/Qwen3-VL-2B-Instruct"
CLAP_REPO = "laion/larger_clap_general"
CLIP_REPO = "openai/clip-vit-base-patch32"
AESTHETIC_URL = "https://raw.githubusercontent.com/LAION-AI/aesthetic-predictor/main/sa_0_4_vit_b_32_linear.pth"
LICENSES = {
    VLM_REPO: ("Apache-2.0", "https://huggingface.co/Qwen/Qwen3-VL-2B-Instruct"),
    CLAP_REPO: ("Apache-2.0", "https://huggingface.co/laion/larger_clap_general"),
    CLIP_REPO: ("MIT", "https://github.com/openai/CLIP/blob/main/LICENSE"),
    "aesthetic": ("MIT", "https://github.com/LAION-AI/aesthetic-predictor/blob/main/LICENSE"),
    "lpips": ("BSD-2-Clause", "https://github.com/richzhang/PerceptualSimilarity/blob/master/LICENSE"),
}
SENSES_NOTE = ("Small local models: a second opinion, not a verdict. Check the moments they point to "
               "(review_video's contact sheet, describe_audio's spectrogram) before trusting them.")

_lock = threading.RLock()
_models: dict[str, Any] = {}
_last_used = [0.0]
_reaper: list[threading.Timer | None] = [None]


# ---------------------------------------------------------------- shared plumbing

def senses_dir() -> Path:
    from .voice import models_dir

    d = models_dir() / "vlm"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _torch():
    try:
        import torch
    except ImportError as e:
        raise OpError('This needs PyTorch and transformers: pip install "builder[senses]" '
                      "--extra-index-url https://download.pytorch.org/whl/cpu") from e
    want = int(os.environ.get("BUILDER_SENSES_THREADS") or max(1, min(4, (os.cpu_count() or 2) - 1)))
    if torch.get_num_threads() != want:
        torch.set_num_threads(want)
    return torch


def _download(repo: str, patterns: list[str] | None = None) -> Path:
    """Fetch a Hugging Face model once into <models>/vlm/<name>, with a license.json next to it."""
    dest = senses_dir() / repo.split("/")[-1]
    marker = dest / ".complete"
    if marker.exists():
        return dest
    try:
        from huggingface_hub import snapshot_download
    except ImportError as e:
        raise OpError('Downloading the model needs huggingface_hub: pip install "builder[senses]"') from e
    try:
        snapshot_download(repo, local_dir=dest, allow_patterns=patterns)
    except Exception as e:  # noqa: BLE001 - network errors come in many types
        raise OpError(f"Couldn't download {repo}: {e}") from e
    lic, url = LICENSES.get(repo, ("see model card", f"https://huggingface.co/{repo}"))
    (dest / "license.json").write_text(json.dumps({"repo": repo, "license": lic, "source": url}, indent=1), encoding="utf-8")
    marker.write_text("ok", encoding="utf-8")
    return dest


def _cached(key: str, load):
    """Keep loaded models in memory between calls; drop them after BUILDER_SENSES_IDLE seconds (default 300) unused."""
    with _lock:
        _last_used[0] = time.time()
        if key not in _models:
            _models[key] = load()
        _schedule_unload()
        return _models[key]


def _schedule_unload() -> None:
    idle = float(os.environ.get("BUILDER_SENSES_IDLE") or 300)
    if idle <= 0:
        return
    if _reaper[0] is not None:
        _reaper[0].cancel()
    t = threading.Timer(idle + 1, _maybe_unload, args=(idle,))
    t.daemon = True
    _reaper[0] = t
    t.start()


def _maybe_unload(idle: float) -> None:
    with _lock:
        if time.time() - _last_used[0] >= idle:
            unload()


def unload() -> None:
    """Free the models' memory (they load again on the next call)."""
    with _lock:
        _models.clear()
    import gc

    gc.collect()


def _np():
    try:
        import numpy as np
    except ImportError as e:
        raise OpError('This needs numpy: pip install "builder[senses]"') from e
    return np


def _fit(w: int, h: int, max_side: int, mult: int = 32) -> tuple[int, int]:
    s = min(1.0, max_side / max(w, h))
    return max(mult, int(round(w * s / mult)) * mult), max(mult, int(round(h * s / mult)) * mult)


def _frames(src: Path, times: list[float], w: int, h: int):
    """RGB uint8 frames (N, h, w, 3) at the given times, one fast seek each (works for stills too)."""
    np = _np()
    out = []
    for t in times:
        cmd = [get_settings().ffmpeg, "-v", "error", *(["-ss", f"{t:.3f}"] if t > 0 else []), "-i", str(src),
               "-frames:v", "1", "-vf", f"scale={w}:{h}:flags=bicubic,format=rgb24", "-f", "rawvideo", "-"]
        proc = subprocess.run(cmd, capture_output=True, timeout=300, **NO_WINDOW)
        if proc.returncode != 0 or len(proc.stdout) < w * h * 3:
            if out:  # past the last frame: repeat the previous one
                out.append(out[-1])
                continue
            raise OpError("Couldn't read a frame:\n" + proc.stderr.decode(errors="replace").strip()[-400:])
        out.append(np.frombuffer(proc.stdout[: w * h * 3], np.uint8).reshape(h, w, 3))
    return np.stack(out)


def _video_dims(file_id: str) -> tuple[int, int, float, float]:
    info = media_info(file_id)
    if not info["has_video"]:
        raise OpError("this needs an image or a video")
    v = info["video"]
    return int(v["width"]), int(v["height"]), float(info.get("duration_s") or 0.0), float(v.get("fps") or 0.0)


# ---------------------------------------------------------------- watch_video

DESCRIBE = ("Describe what happens in this video: who is there, what they do and how they move, the setting, the camera "
            "(shot changes, close-ups) and any on-screen text. "
            "Point out anything that looks wrong (limbs passing through bodies or objects, sliding feet, frozen or jittery "
            "motion, popping, glitches).")


class WatchVideoParams(BaseModel):
    file_id: str = Field(description="The video (or image) to watch")
    question: str | None = Field(None, max_length=2000, description="What to ask about the clip; leave empty to get a description")
    frames: int = Field(16, ge=2, le=48, description="Frames sampled evenly over the clip (pairs are merged in time, so 16 frames = 8 moments)")
    start: float = Field(0, ge=0, description="Seconds into the clip to start watching")
    end: float | None = Field(None, ge=0, description="Seconds to stop watching (default: the end)")
    max_side: int = Field(448, ge=128, le=1024, description="Longest frame side fed to the model (lower = faster)")
    max_words: int = Field(150, ge=20, le=1000, description="Roughly how long the answer may be")
    timeline: bool = Field(True, description="Also ask for one short note per moment (per-timestamp notes)")


def _vlm():
    torch = _torch()
    try:
        from transformers import AutoModelForImageTextToText, AutoProcessor
    except ImportError as e:
        raise OpError('watch_video needs transformers: pip install "builder[senses]"') from e
    path = _download(VLM_REPO)

    def load():
        proc = AutoProcessor.from_pretrained(path)
        model = AutoModelForImageTextToText.from_pretrained(path, dtype=torch.bfloat16)
        model.eval()
        return proc, model

    return _cached("vlm", load)


def _secs(tok: str) -> float:
    if ":" in tok:  # 0:03 or 1:02.5
        m, sec = tok.split(":", 1)
        return int(m) * 60 + float(sec)
    return float(tok)


def _parse_timeline(text: str, moments: list[float]) -> list[dict[str, Any]]:
    """Lines like '3.5 s: ...' (or '0:03 s - ...'); keep one note per asked moment, drop repeats and invented times."""
    notes: list[dict[str, Any]] = []
    seen: set[str] = set()
    hi = (moments[-1] + 1.0) if moments else float("inf")
    for line in text.splitlines():
        m = re.match(r"^\s*[-*•]?\s*[<\[(]?\s*(\d+(?::\d+)?(?:\.\d+)?)\s*(?:s|sec|secs|seconds)?\s*[>\])]?\s*[:\-–—]\s*(.+)$", line)
        if not m:
            continue
        t, note = _secs(m.group(1)), m.group(2).strip()
        if t > hi or note.lower() in seen or len(notes) >= len(moments):
            continue
        seen.add(note.lower())
        notes.append({"t": round(t, 2), "note": note})
    if not notes:  # the model often drops the times: pair its lines with the moments in order
        lines = [re.sub(r"^\s*(?:[-*•]|\d+[.)])\s*", "", ln).strip() for ln in text.splitlines()]
        lines = [ln for ln in lines if ln]
        notes = [{"t": m, "note": ln} for m, ln in zip(moments, lines)]
    return notes


def _generate_vlm(frames, times: list[float], prompt: str, max_new_tokens: int) -> str:
    """Run the VLM on frames (N, H, W, 3) shown as one video with the given timestamps."""
    torch = _torch()
    from transformers.video_utils import VideoMetadata

    proc, model = _vlm()
    # timestamps reach the model through frame indices / fps: use a fine virtual frame rate
    fps = 100.0
    meta = VideoMetadata(total_num_frames=int(round(times[-1] * fps)) + 1, fps=fps, width=frames.shape[2],
                         height=frames.shape[1], frames_indices=[int(round(t * fps)) for t in times])
    msgs = [{"role": "user", "content": [{"type": "video"}, {"type": "text", "text": prompt}]}]
    text = proc.apply_chat_template(msgs, tokenize=False, add_generation_prompt=True)
    inputs = proc(text=[text], videos=[frames], video_metadata=[meta], do_sample_frames=False, do_resize=False,
                  return_tensors="pt")
    with torch.inference_mode():
        out = model.generate(**inputs, max_new_tokens=max_new_tokens, do_sample=False, repetition_penalty=1.05)
    return proc.batch_decode(out[:, inputs["input_ids"].shape[1]:], skip_special_tokens=True)[0].strip()


@operation("watch_video", WatchVideoParams, "Watch a clip with a small local video-understanding model (Qwen3-VL-2B, runs on CPU) and answer a question about it ('does his hand pass through his leg?', 'is she waving?') or describe what happens, with per-timestamp notes. Sees motion across frames, not just stills; a second opinion, unreliable on tiny details. First use downloads ~4.3 GB. Writes a .json report.", read_only=True)
def watch_video(p: WatchVideoParams) -> OpResult:
    src = _src(p.file_id)
    W, H, dur, _fps = _video_dims(p.file_id)
    end = min(p.end, dur) if (p.end and dur) else (p.end or dur)
    if dur and p.start >= dur:
        raise OpError(f"start is past the end of the clip ({dur:.2f} s)")
    span = max(0.0, (end or 0) - p.start)
    n = p.frames + (p.frames % 2)  # the model merges frames in pairs
    times = [round(p.start + (i + 0.5) * span / n, 3) for i in range(n)] if span > 0 else [p.start] * n
    w, h = _fit(W, H, p.max_side)
    frames = _frames(src, times, w, h)
    q = (p.question or "").strip() or DESCRIBE
    t0 = time.time()
    raw = _generate_vlm(frames, times, f"{q}\nAnswer in at most {p.max_words} words.", int(p.max_words * 1.6) + 16)
    answer, notes, raw_tl = raw, [], ""
    if p.timeline:
        # the model sees frames in pairs: one moment per pair, at the pair's middle (what its own timestamps say)
        moments = [round((times[i] + times[i + 1]) / 2, 1) for i in range(0, len(times) - 1, 2)]
        ask = ("For each moment below, write one short line (at most 15 words) about what is happening then, "
               "keeping the given time. Do not add other moments.\n" + "\n".join(f"{m:.1f} s:" for m in moments))
        raw_tl = _generate_vlm(frames, times, ask, 24 * len(moments) + 16)
        notes = _parse_timeline(raw_tl, moments)
    report = {"file": storage.get(p.file_id).name, "question": q, "answer": answer, "timeline": notes,
              "frames_seen": times, "frame_size": [w, h], "model": VLM_REPO, "seconds": round(time.time() - t0, 1),
              "raw_timeline": raw_tl, "note": SENSES_NOTE}
    return _guarded("watch_video", [p.file_id], f"{_stem(p.file_id)}_watch.json",
                    lambda out: out.write_text(json.dumps(report, indent=1), encoding="utf-8"), data=report)


# ---------------------------------------------------------------- caption_audio

VOCAB: dict[str, list[str]] = {
    "content": ["people speaking", "music", "people speaking over background music", "a singer with music",
                "environmental ambience", "sound effects", "silence"],
    "sound": ["people talking", "a man speaking", "a woman speaking", "a child speaking", "whispering", "shouting",
              "singing", "laughter", "a crowd murmuring", "applause", "footsteps", "a door closing",
              "keyboard typing", "a car engine", "traffic", "a siren", "a dog barking", "birds singing", "rain",
              "wind", "thunder", "flowing water", "ocean waves", "insects buzzing", "fire crackling",
              "an explosion", "a gunshot", "glass breaking", "a phone ringing", "an alarm beeping",
              "electrical hum", "white noise", "static hiss", "clicks and pops"],
    "instrument": ["piano", "acoustic guitar", "electric guitar", "bass guitar", "drums", "electronic beats",
                   "synthesizer", "strings", "orchestra", "brass", "flute", "choir", "bells", "no musical instrument"],
    "voice": ["a natural human voice", "a robotic synthetic voice", "a deep voice", "a high-pitched voice",
              "a breathy voice", "a monotone voice", "an expressive voice", "no voice"],
    "space": ["a small room", "a large hall with reverb", "outdoors in an open space", "a dry studio recording",
              "an echoey empty room", "a muffled sound behind a wall", "a telephone line"],
    "mood": ["calm", "happy", "tense", "sad", "epic", "playful", "dreamy", "dark and ominous", "energetic", "neutral"],
    "quality": ["a clean professional recording", "a noisy recording", "a distorted recording", "a muffled recording",
                "a lo-fi recording", "a harsh tinny recording"],
}
PROMPT = {"content": "This is a recording of {}.", "sound": "This is a sound of {}.", "instrument": "This is a sound of {}.", "voice": "This is {}.",
          "space": "This sound was recorded in {}.", "mood": "This sound feels {}.", "quality": "This is {}."}
CLAP_SR = 48000


class CaptionAudioParams(BaseModel):
    file_id: str = Field(description="A sound file, or a video (its first audio track)")
    max_seconds: float = Field(60, ge=1, le=600, description="Listen to at most this many seconds from the start")
    window_s: float = Field(10, ge=2, le=10, description="Length of each listening window (CLAP hears 10 s at a time)")
    extra_labels: list[str] = Field(default_factory=list, max_length=40, description="Extra sound labels to test, e.g. 'a basketball bouncing'")
    numbers: bool = Field(True, description="Also run describe_audio and fold its measurements into the sentence")


def _clap():
    _torch()
    try:
        from transformers import ClapModel, ClapProcessor
    except ImportError as e:
        raise OpError('caption_audio needs transformers: pip install "builder[senses]"') from e
    path = _download(CLAP_REPO)

    def load():
        m = ClapModel.from_pretrained(path)
        m.eval()
        return ClapProcessor.from_pretrained(path), m

    return _cached("clap", load)


def _vec(x):
    """transformers 5 returns model outputs from get_*_features; older versions return tensors."""
    if hasattr(x, "pooler_output") and not hasattr(x, "shape"):
        x = x.pooler_output
    return x / x.norm(dim=-1, keepdim=True)


def _clap_embed_audio(windows: list) -> Any:
    torch = _torch()
    proc, model = _clap()
    with torch.inference_mode():
        inp = proc(audio=windows, sampling_rate=CLAP_SR, return_tensors="pt")
        return _vec(model.get_audio_features(**inp))


def _clap_embed_text(texts: list[str]) -> Any:
    torch = _torch()
    proc, model = _clap()
    key = ("clap_text", tuple(texts))
    with _lock:
        if key in _models:
            return _models[key]
    with torch.inference_mode():
        inp = proc(text=texts, padding=True, return_tensors="pt")
        emb = _vec(model.get_text_features(**inp))
    with _lock:
        _models[key] = emb
    return emb


def _clap_scale() -> float:
    _, model = _clap()
    ls = getattr(model, "logit_scale_a", None)
    return float(ls.detach().exp()) if ls is not None else 33.0


def _decode_48k(src: Path, seconds: float):
    np = _np()
    cmd = [get_settings().ffmpeg, "-v", "error", "-i", str(src), "-map", "0:a:0", "-t", f"{seconds}",
           "-f", "f32le", "-ac", "1", "-ar", str(CLAP_SR), "-"]
    proc = subprocess.run(cmd, capture_output=True, timeout=600, **NO_WINDOW)
    if proc.returncode != 0:
        raise OpError("Couldn't read the audio:\n" + proc.stderr.decode(errors="replace").strip()[-400:])
    return np.frombuffer(proc.stdout[: len(proc.stdout) // 4 * 4], np.float32)


def _tag(audio_emb, vocab: dict[str, list[str]], scale: float) -> dict[str, list[dict[str, Any]]]:
    """Per category: labels ranked by softmax over that category's prompts."""
    out = {}
    for cat, labels in vocab.items():
        temb = _clap_embed_text([PROMPT.get(cat, "This is a sound of {}.").format(lab) for lab in labels])
        logits = (audio_emb @ temb.T) * scale
        prob = logits.softmax(dim=-1)
        cos = audio_emb @ temb.T
        ranked = sorted(range(len(labels)), key=lambda i: -float(prob[i]))
        out[cat] = [{"label": labels[i], "p": round(float(prob[i]), 3), "similarity": round(float(cos[i]), 3)} for i in ranked[:5]]
    return out


def _pick(tags: list[dict[str, Any]], min_p: float = 0.15, k: int = 3) -> list[str]:
    top = [t["label"] for t in tags[:k] if t["p"] >= min_p]
    return top or [tags[0]["label"]]


def _sentence(tags: dict[str, list[dict[str, Any]]], numbers: dict[str, Any] | None) -> str:
    content = tags["content"][0]["label"]
    if content == "silence" and tags["content"][0]["p"] >= 0.5:
        s = "Near silence"
    else:
        s = f"Sounds like {content}"
        events = _pick(tags["sound"], 0.2, 3)
        if content != "music":
            s += f" ({', '.join(events)})"
        inst = [i for i in _pick(tags["instrument"], 0.2) if i != "no musical instrument"]
        if inst and ("music" in content or tags["instrument"][0]["p"] >= 0.5):
            s += f" with {', '.join(inst)}"
        voice = [v for v in _pick(tags["voice"], 0.25, 2) if v != "no voice"]
        if voice and any(w in content for w in ("speak", "singer")):
            s += f"; voice: {', '.join(voice)}"
        s += f"; {tags['space'][0]['label']}"
    s += f"; mood {tags['mood'][0]['label']}; {tags['quality'][0]['label']}."
    if numbers:
        bits = []
        lufs = (numbers.get("loudness") or {}).get("lufs")
        if lufs is not None:
            bits.append(f"{lufs:.0f} LUFS")
        spec = numbers.get("spectrum") or {}
        if spec.get("brightness"):
            bits.append(f"{spec['brightness']} tone")
        rh = numbers.get("rhythm") or {}
        if rh.get("bpm") and (rh.get("regularity") or 0) >= 0.4:
            bits.append(f"steady beat near {rh['bpm']:.0f} BPM")
        v = numbers.get("voice") or {}
        if v.get("median_f0_hz") and any(w in content for w in ("speak", "singer")):
            bits.append(f"voice pitch ~{v['median_f0_hz']:.0f} Hz ({v.get('robotic_risk')} robotic risk)")
        if numbers.get("noise_floor_dbfs") is not None:
            bits.append(f"noise floor {numbers['noise_floor_dbfs']:.0f} dBFS")
        if bits:
            s += " Measured: " + ", ".join(bits) + "."
    return s


@operation("caption_audio", CaptionAudioParams, "Put a sound into words: what it is (speech, music, rain, footsteps...), instruments, voice qualities (natural / robotic / deep...), the room (small room, hall, outdoors), mood and recording quality, from CLAP zero-shot tagging (local, CPU), joined with describe_audio's measurements into a sentence, plus per-10-second tags. Tags are likelihoods, not certainty. First use downloads ~780 MB. Writes a .json report.", read_only=True)
def caption_audio(p: CaptionAudioParams) -> OpResult:
    np = _np()
    src = _src(p.file_id)
    info = media_info(p.file_id)
    if not info["has_audio"]:
        raise OpError("caption_audio needs a file with sound")
    x = _decode_48k(src, p.max_seconds)
    if not len(x):
        raise OpError("caption_audio found no audio samples")
    win = int(p.window_s * CLAP_SR)
    windows = [x[i:i + win] for i in range(0, len(x), win)]
    if len(windows) > 1 and len(windows[-1]) < 0.3 * win:  # fold a short tail into the previous window
        windows[-2] = np.concatenate([windows[-2], windows.pop()])
    t0 = time.time()
    emb = _clap_embed_audio([w.astype(np.float32) for w in windows])
    scale = _clap_scale()
    vocab = dict(VOCAB)
    if p.extra_labels:
        vocab["sound"] = VOCAB["sound"] + [s.strip() for s in p.extra_labels if s.strip()]
    rms = [float(np.sqrt(np.mean(w.astype(np.float64) ** 2)) + 1e-12) for w in windows]
    weights = np.array(rms) / sum(rms)  # louder windows count more in the overall tags
    whole = (emb * _torch().tensor(weights, dtype=emb.dtype)[:, None]).sum(0)
    whole = whole / whole.norm()
    tags = _tag(whole, vocab, scale)
    timeline = []
    for i, e in enumerate(emb):
        tw = _tag(e, {"content": vocab["content"], "sound": vocab["sound"]}, scale)
        timeline.append({"start": round(i * p.window_s, 2), "end": round(min(len(x) / CLAP_SR, (i + 1) * p.window_s), 2),
                         "content": tw["content"][0]["label"], "sounds": _pick(tw["sound"]), "top": tw["sound"][:3]})
    numbers, outputs = None, []
    if p.numbers:
        from .ops import run_operation

        try:
            d = run_operation("describe_audio", {"file_id": p.file_id, "max_seconds": max(5.0, p.max_seconds)})
            numbers, outputs = d.data, list(d.outputs)
        except OpError:
            numbers = None
    caption = _sentence(tags, numbers)
    report = {"file": storage.get(p.file_id).name, "caption": caption, "tags": tags, "timeline": timeline,
              "seconds_listened": round(len(x) / CLAP_SR, 2), "model": CLAP_REPO, "seconds": round(time.time() - t0, 1),
              "measurements": None if numbers is None else {k: numbers.get(k) for k in ("loudness", "spectrum", "rhythm", "voice", "content", "noise_floor_dbfs", "description")},
              "why_not_a_bigger_captioner": "Qwen3-Omni-30B-A3B-Captioner writes free-form captions but is ~30B parameters (GPU 24 GB+); CLAP tagging runs here on CPU.",
              "note": "p = share among the labels of that category (softmax), similarity = raw cosine; " + SENSES_NOTE}
    res = _guarded("caption_audio", [p.file_id], f"{_stem(p.file_id)}_caption.json",
                   lambda out: out.write_text(json.dumps(report, indent=1), encoding="utf-8"), data=report)
    return OpResult(outputs=outputs + res.outputs, data=report)


# ---------------------------------------------------------------- quality_score

class QualityParams(BaseModel):
    file_id: str = Field(description="The render (image or video)")
    reference_id: str | None = Field(None, description="An approved image or video to compare against (LPIPS); leave empty for the aesthetic score only")
    at: float | None = Field(None, ge=0, description="Seconds into a video for a single frame (default: several frames over the clip)")
    frames: int = Field(5, ge=1, le=30, description="Frames sampled over a video when `at` is not set")
    aesthetic: bool = Field(True, description="Add the no-reference LAION aesthetic score (1-10, higher = more pleasing)")
    max_side: int = Field(512, ge=64, le=1024, description="Frames are scaled to fit this before scoring")


def _lpips_model():
    torch = _torch()
    try:
        import lpips
    except ImportError as e:
        raise OpError('LPIPS needs the lpips package: pip install "builder[senses]"') from e

    def load():
        torch.hub.set_dir(str(senses_dir() / "torch"))  # AlexNet weights (~240 MB) land next to the other models
        import contextlib
        import io

        import warnings

        with contextlib.redirect_stdout(io.StringIO()), warnings.catch_warnings():
            warnings.simplefilter("ignore")
            m = lpips.LPIPS(net="alex", verbose=False)
        m.eval()
        return m

    return _cached("lpips", load)


def lpips_distance(a, b) -> list[float]:
    """LPIPS (AlexNet) between two uint8 frame stacks (N, H, W, 3) of the same size: 0 = same, ~0.7 = unrelated."""
    torch = _torch()
    m = _lpips_model()
    ta = torch.from_numpy(a.copy()).permute(0, 3, 1, 2).float() / 127.5 - 1
    tb = torch.from_numpy(b.copy()).permute(0, 3, 1, 2).float() / 127.5 - 1
    with torch.inference_mode():
        return [round(float(v), 4) for v in m(ta, tb).flatten()]


def _aesthetic_models():
    torch = _torch()
    try:
        from transformers import CLIPModel, CLIPProcessor
    except ImportError as e:
        raise OpError('The aesthetic score needs transformers: pip install "builder[senses]"') from e
    path = _download(CLIP_REPO, ["*.json", "*.txt", "pytorch_model.bin"])
    head = senses_dir() / "aesthetic" / "sa_0_4_vit_b_32_linear.pth"
    if not head.exists():
        from .voice import _fetch

        _fetch(AESTHETIC_URL, head)
        (head.parent / "license.json").write_text(json.dumps({"repo": "LAION-AI/aesthetic-predictor", "license": "MIT",
                                                              "source": LICENSES["aesthetic"][1]}, indent=1), encoding="utf-8")

    def load():
        clip = CLIPModel.from_pretrained(path)
        clip.eval()
        lin = torch.nn.Linear(512, 1)
        lin.load_state_dict(torch.load(head, map_location="cpu", weights_only=True))
        lin.eval()
        return CLIPProcessor.from_pretrained(path), clip, lin

    return _cached("aesthetic", load)


def aesthetic_scores(frames) -> list[float]:
    torch = _torch()
    proc, clip, lin = _aesthetic_models()
    with torch.inference_mode():
        inp = proc(images=list(frames), return_tensors="pt")
        emb = _vec(clip.get_image_features(**inp))
        return [round(float(v), 3) for v in lin(emb).flatten()]


def _lpips_meaning(d: float) -> str:
    return ("practically the same" if d < 0.05 else "close, small changes" if d < 0.15
            else "noticeably different" if d < 0.3 else "very different")


@operation("quality_score", QualityParams, "Score how a render looks: perceptual distance to a reference (LPIPS: 0 = looks the same, >0.3 = very different; catches changes in look that SSIM misses or over-reports) and a no-reference aesthetic score (LAION predictor, 1-10). Images or videos (several frames averaged). Local CPU models (~850 MB on first use). Writes a .json report.", read_only=True)
def quality_score(p: QualityParams) -> OpResult:
    np = _np()
    src = _src(p.file_id)
    W, H, dur, _ = _video_dims(p.file_id)
    if p.at is not None:
        times = [p.at]
    elif dur > 0.2 and p.frames > 1:
        times = [round((i + 0.5) * dur / p.frames, 3) for i in range(p.frames)]
    else:
        times = [0.0]
    w, h = _fit(W, H, p.max_side, 2)
    a = _frames(src, times, w, h)
    report: dict[str, Any] = {"file": storage.get(p.file_id).name, "times": times, "size": [w, h]}
    parents = [p.file_id]
    if p.reference_id:
        rsrc = _src(p.reference_id)
        _rw, _rh, rdur, _ = _video_dims(p.reference_id)
        rtimes = [min(t, max(0.0, rdur - 0.05)) for t in times] if rdur > 0.2 else [0.0] * len(times)
        b = _frames(rsrc, rtimes, w, h)
        d = lpips_distance(a, b)
        mean = round(float(np.mean(d)), 4)
        report["reference"] = storage.get(p.reference_id).name
        report["lpips"] = {"mean": mean, "max": max(d), "per_frame": d, "meaning": _lpips_meaning(mean),
                           "scale": "0 = looks the same; <0.05 practically the same; <0.15 close; <0.3 noticeably different; above: very different"}
        parents.append(p.reference_id)
    if p.aesthetic:
        s = aesthetic_scores(a)
        report["aesthetic"] = {"mean": round(float(np.mean(s)), 3), "per_frame": s,
                               "scale": "LAION aesthetic v1 (CLIP ViT-B/32): about 4 dull .. 6+ pleasing; trained on photo/art ratings, compare within a project, not across styles"}
        if p.reference_id:
            report["aesthetic"]["reference_mean"] = round(float(np.mean(aesthetic_scores(b))), 3)
    report["note"] = SENSES_NOTE
    return _guarded("quality_score", parents, f"{_stem(p.file_id)}_quality.json",
                    lambda out: out.write_text(json.dumps(report, indent=1), encoding="utf-8"), data=report)


def status() -> dict[str, Any]:
    """What `builder doctor` shows: which packages and models are ready."""
    import importlib.util

    has = lambda m: importlib.util.find_spec(m) is not None  # noqa: E731
    d = senses_dir()
    return {"torch": has("torch"), "transformers": has("transformers"), "lpips": has("lpips"), "torchvision": has("torchvision"),
            "vlm": (d / VLM_REPO.split("/")[-1] / ".complete").exists(),
            "clap": (d / CLAP_REPO.split("/")[-1] / ".complete").exists(),
            "clip": (d / CLIP_REPO.split("/")[-1] / ".complete").exists()}


def lpips_files(file_id: str, reference_id: str, at: float = 0.0, max_side: int = 512) -> dict[str, Any]:
    """LPIPS between one frame of two files, scaled to the reference's shape (used by visual_diff perceptual=true)."""
    W, H, _, _ = _video_dims(reference_id)
    w, h = _fit(W, H, max_side, 2)
    d = lpips_distance(_frames(_src(file_id), [at], w, h), _frames(_src(reference_id), [at], w, h))[0]
    return {"lpips": d, "lpips_meaning": _lpips_meaning(d)}
