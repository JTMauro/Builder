"""Voices, captions and lip-sync, all free and running on this computer.

- speak: text to natural speech with Kokoro (Apache-2.0 model, 13+ English voices).
- transcribe: speech to captions (.srt + .vtt) and word timings with Whisper (faster-whisper, MIT).
- burn_captions: put captions on a video (styled, bottom or top).
- lipsync: mouth shapes over time for any voice track (Rhubarb-style cues A-H and X), from
  Whisper's word timings and espeak pronunciations. Drives a character's mouth in three.js.
- denoise_audio, voice_polish, room_reverb: clean up and place a voice (ffmpeg).

The speech engines are an optional extra (`pip install "builder[voice]"`); their models download on
first use into Builder's models folder (BUILDER_MODELS_DIR, default <data>/models).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import time
import urllib.request
import wave
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from .config import NO_WINDOW, get_settings
from .ops import AUDIO_CODECS, AUDIO_ENC, OpError, OpResult, _ffmpeg, _guarded, _src, _stem, media_info, operation

HF = "https://huggingface.co/onnx-community/Kokoro-82M-v1.0-ONNX/resolve/main"
VOICES = {
    "am_puck": "US male, young and bright", "am_liam": "US male, warm", "am_michael": "US male, deep",
    "am_adam": "US male, calm", "am_eric": "US male, clear", "af_heart": "US female, warm (best)", "af_bella": "US female, lively",
    "af_nicole": "US female, soft", "af_sarah": "US female, clear", "bf_emma": "UK female", "bf_alice": "UK female, bright",
    "bm_george": "UK male, deep", "bm_daniel": "UK male", "bm_lewis": "UK male, warm",
}
WHISPER_MODELS = ("tiny", "base", "small", "medium", "large-v3")


def models_dir() -> Path:
    d = Path(os.environ.get("BUILDER_MODELS_DIR") or (get_settings().data_dir / "models")).expanduser()
    d.mkdir(parents=True, exist_ok=True)
    return d


def _fetch(url: str, dest: Path, tries: int = 5) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Builder/0.3"})
            with urllib.request.urlopen(req, timeout=120) as r, open(tmp, "wb") as f:
                while chunk := r.read(1 << 20):
                    f.write(chunk)
            if tmp.stat().st_size < 1024:   # a busy server answers with a short text message
                raise OSError(tmp.read_text(errors="replace")[:100])
            tmp.replace(dest)
            return
        except OSError as e:
            if i == tries - 1:
                raise OpError(f"Couldn't download {url}: {e}") from e
            time.sleep(2 * (i + 1))


_kokoro = None


def _kokoro_engine():
    global _kokoro
    if _kokoro is not None:
        return _kokoro
    try:
        import numpy as np
        from kokoro_onnx import Kokoro
    except ImportError as e:
        raise OpError('Voices need the speech extra: pip install "builder[voice]" (Kokoro, Whisper)') from e
    d = models_dir() / "kokoro"
    model, pack = d / "model.onnx", d / "voices.npz"
    if not model.exists():
        _fetch(f"{HF}/onnx/model.onnx", model)
    have = set(np.load(pack).files) if pack.exists() else set()
    if not set(VOICES) <= have:
        arrays = {}
        for v in VOICES:
            f = d / "voices" / f"{v}.bin"
            if not f.exists():
                _fetch(f"{HF}/voices/{v}.bin", f)
            arrays[v] = np.fromfile(f, dtype=np.float32).reshape(-1, 1, 256)
        np.savez(pack, **arrays)
    _kokoro = Kokoro(str(model), str(pack))
    return _kokoro


_whisper: dict[str, Any] = {}


def _whisper_model(name: str):
    if name not in _whisper:
        try:
            from faster_whisper import WhisperModel
        except ImportError as e:
            raise OpError('Captions need the speech extra: pip install "builder[voice]" (Kokoro, Whisper)') from e
        _whisper[name] = WhisperModel(name, device="cpu", compute_type="int8", download_root=str(models_dir() / "whisper"))
    return _whisper[name]


def _pcm16k(path: Path):
    import numpy as np

    raw = subprocess.run([get_settings().ffmpeg, "-v", "error", "-i", str(path), "-f", "f32le", "-ac", "1", "-ar", "16000", "-"],
                         capture_output=True, check=False, **NO_WINDOW)
    if raw.returncode != 0 or not raw.stdout:
        raise OpError("Couldn't read audio from that file")
    return np.frombuffer(raw.stdout, np.float32)


def words_of(file_id: str, model: str = "base", language: str | None = "en") -> tuple[list[dict[str, Any]], list[dict[str, Any]], str]:
    """(segments, words, language) with times in seconds."""
    name = f"{model}.en" if language == "en" and model in ("tiny", "base", "small", "medium") else model
    segs, info = _whisper_model(name).transcribe(_pcm16k(_src(file_id)), word_timestamps=True, language=language, vad_filter=True)
    segments, words = [], []
    for s in segs:
        segments.append({"start": round(float(s.start), 3), "end": round(float(s.end), 3), "text": s.text.strip()})
        for w in s.words or []:
            words.append({"word": w.word.strip(), "start": round(float(w.start), 3), "end": round(float(w.end), 3)})
    return segments, words, getattr(info, "language", language or "")


# ---------------------------------------------------------------- speak


class SpeakParams(BaseModel):
    text: str = Field(min_length=1, max_length=8000, description="What to say. Punctuation shapes the delivery (commas pause, ! and ? change tone).")
    voice: Literal[tuple(VOICES)] = Field("af_heart", description="; ".join(f"{k}: {v}" for k, v in VOICES.items()))  # type: ignore[valid-type]
    speed: float = Field(1.0, ge=0.5, le=2.0)
    format: Literal["wav", "mp3"] = "wav"
    name: str | None = Field(None, max_length=80, description="File name (without extension)")


@operation("speak", SpeakParams, "Turn text into natural speech with a free, local voice (Kokoro). Use for narration, character lines and voice-overs; pair with lipsync to animate a mouth and transcribe for captions.")
def speak(p: SpeakParams) -> OpResult:
    import numpy as np

    samples, sr = _kokoro_engine().create(p.text, voice=p.voice, speed=p.speed, lang="en-gb" if p.voice.startswith("b") else "en-us")
    pcm = (np.clip(samples, -1, 1) * 32767).astype("<i2").tobytes()
    base = re.sub(r"_+", "_", re.sub(r"[^A-Za-z0-9_-]+", "_", p.name or "_".join(p.text.split()[:5]))).strip("_")[:60] or "speech"

    def build(out: Path) -> None:
        wav = out if p.format == "wav" else out.with_suffix(".tmp.wav")
        with wave.open(str(wav), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(sr)
            w.writeframes(pcm)
        if p.format == "mp3":
            _ffmpeg("-i", str(wav), *AUDIO_CODECS["mp3"], str(out))
            wav.unlink(missing_ok=True)

    return _guarded("speak", [], f"{base}.{p.format}", build,
                    data={"voice": p.voice, "duration_s": round(len(samples) / sr, 3), "engine": "Kokoro-82M (Apache-2.0)"})


# ---------------------------------------------------------------- captions


def _ts(t: float, sep: str) -> str:
    ms = int(round(t * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02}:{m:02}:{s:02}{sep}{ms:03}"


def _cues(words: list[dict[str, Any]], max_chars: int = 42, max_s: float = 4.5) -> list[tuple[float, float, str]]:
    """Group words into readable caption lines."""
    out, cur = [], []
    for w in words:
        if cur and (len(" ".join(x["word"] for x in cur + [w])) > max_chars or w["end"] - cur[0]["start"] > max_s
                    or w["start"] - cur[-1]["end"] > 0.8):
            out.append((cur[0]["start"], cur[-1]["end"], " ".join(x["word"] for x in cur)))
            cur = []
        cur.append(w)
        if re.search(r"[.!?]$", w["word"]) and len(" ".join(x["word"] for x in cur)) > 12:
            out.append((cur[0]["start"], cur[-1]["end"], " ".join(x["word"] for x in cur)))
            cur = []
    if cur:
        out.append((cur[0]["start"], cur[-1]["end"], " ".join(x["word"] for x in cur)))
    return out


class TranscribeParams(BaseModel):
    file_id: str = Field(description="Audio or video with speech")
    model: Literal["tiny", "base", "small", "medium", "large-v3"] = Field("base", description="Bigger = more accurate, slower (small is a good balance; large-v3 for difficult audio)")
    language: str | None = Field("en", max_length=8, description="Language code, or null to detect it")


@operation("transcribe", TranscribeParams, "Transcribe speech with Whisper (free, local): captions as .srt and .vtt files, plus the text and every word's timing.")
def transcribe(p: TranscribeParams) -> OpResult:
    segments, words, lang = words_of(p.file_id, p.model, p.language)
    cues = _cues(words) if words else [(s["start"], s["end"], s["text"]) for s in segments]
    srt = "\n".join(f"{i}\n{_ts(a, ',')} --> {_ts(b, ',')}\n{t}\n" for i, (a, b, t) in enumerate(cues, 1))
    vtt = "WEBVTT\n\n" + "\n".join(f"{_ts(a, '.')} --> {_ts(b, '.')}\n{t}\n" for a, b, t in cues)
    stem = _stem(p.file_id)
    a = _guarded("transcribe", [p.file_id], f"{stem}.srt", lambda out: out.write_text(srt, encoding="utf-8"))
    b = _guarded("transcribe", [p.file_id], f"{stem}.vtt", lambda out: out.write_text(vtt, encoding="utf-8"))
    return OpResult(outputs=a.outputs + b.outputs,
                    data={"language": lang, "text": " ".join(s["text"] for s in segments), "captions": len(cues), "words": words[:2000]})


def _filter_path(path: Path) -> str:
    """A file path written so ffmpeg's filter parser reads it (Windows drive colons, backslashes, quotes)."""
    s = str(path).replace("\\", "/")
    return s.replace(":", "\\:").replace("'", "\\'")


class BurnCaptionsParams(BaseModel):
    file_id: str = Field(description="Video to caption")
    captions_id: str | None = Field(None, description="An .srt or .vtt file id; when empty the video's speech is transcribed first")
    position: Literal["bottom", "top", "middle"] = "bottom"
    size: int = Field(22, ge=8, le=80, description="Text size (relative to the video height)")
    style: Literal["clean", "boxed", "bold-yellow"] = Field("clean", description="clean: white with outline; boxed: on a dark box; bold-yellow: big yellow social-media style")


@operation("burn_captions", BurnCaptionsParams, "Put captions onto a video permanently (styled), from an .srt/.vtt file or by transcribing the video's speech first.")
def burn_captions(p: BurnCaptionsParams) -> OpResult:
    src = _src(p.file_id)
    if not media_info(p.file_id)["has_video"]:
        raise OpError("burn_captions needs a video")
    cap_id = p.captions_id or next(o.id for o in transcribe(TranscribeParams(file_id=p.file_id)).outputs if o.name.endswith(".srt"))
    align = {"bottom": 2, "top": 8, "middle": 5}[p.position]
    styles = {
        "clean": f"FontName=DejaVu Sans,Fontsize={p.size},PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,BorderStyle=1,Outline=2,Shadow=0",
        "boxed": f"FontName=DejaVu Sans,Fontsize={p.size},PrimaryColour=&H00FFFFFF,BackColour=&H80000000,BorderStyle=4,Outline=0,Shadow=0",
        "bold-yellow": f"FontName=DejaVu Sans,Fontsize={int(p.size * 1.3)},Bold=1,PrimaryColour=&H0000FFFF,OutlineColour=&H00000000,BorderStyle=1,Outline=3,Shadow=1",
    }[p.style]
    vf = f"subtitles='{_filter_path(_src(cap_id))}':force_style='{styles},Alignment={align},MarginV=24'"
    from .ops import VIDEO_ENC

    return _guarded("burn_captions", [p.file_id, cap_id], f"{_stem(p.file_id)}_captioned.mp4",
                    lambda out: _ffmpeg("-i", str(src), "-vf", vf, *VIDEO_ENC, "-c:a", "copy", str(out)))


# ---------------------------------------------------------------- lip-sync

# IPA (espeak) sounds to the Rhubarb/Preston Blair mouth shapes:
# A closed (M B P) · B teeth together (most consonants, EE) · C open (EH AE) · D wide open (AA)
# E rounded (AO ER) · F puckered (OO W) · G F/V · H L · X rest
_VISEME = [
    ("tʃ", "B"), ("dʒ", "B"), ("aɪ", "D"), ("aʊ", "D"), ("eɪ", "C"), ("oʊ", "F"), ("ɔɪ", "E"),
    ("p", "A"), ("b", "A"), ("m", "A"), ("f", "G"), ("v", "G"), ("l", "H"), ("w", "F"),
    ("uː", "F"), ("u", "F"), ("ʊ", "F"), ("ɔː", "E"), ("ɔ", "E"), ("ɚ", "E"), ("ɜː", "E"), ("ɜ", "E"), ("ɝ", "E"), ("o", "E"),
    ("ɑː", "D"), ("ɑ", "D"), ("a", "D"), ("æ", "C"), ("ɛ", "C"), ("e", "C"), ("ʌ", "C"), ("ə", "C"), ("ɐ", "C"), ("ɪ", "C"),
    ("iː", "B"), ("i", "B"),
]
_VOWELS = set("aeiouæɑɐɔəɚɛɜɝɪʊʌ")
_espeak_ready = False


def _phonemes(words: list[str], lang: str = "en-us") -> list[str]:
    global _espeak_ready
    try:
        from phonemizer import phonemize
        if not _espeak_ready:
            import espeakng_loader
            from phonemizer.backend.espeak.wrapper import EspeakWrapper

            EspeakWrapper.set_library(espeakng_loader.get_library_path())
            EspeakWrapper.set_data_path(espeakng_loader.get_data_path())
            _espeak_ready = True
    except ImportError as e:
        raise OpError('Lip-sync needs the speech extra: pip install "builder[voice]"') from e
    clean = [re.sub(r"[^\w']", "", w) or "a" for w in words]
    return phonemize(clean, language=lang, backend="espeak", strip=True, preserve_punctuation=False)


def _split_ipa(ipa: str) -> list[tuple[str, bool]]:
    """IPA text to (mouth shape, is_vowel) per sound."""
    out, i = [], 0
    s = re.sub(r"[ˈˌ\s.]", "", ipa)
    while i < len(s):
        for sym, vis in _VISEME:
            if s.startswith(sym, i):
                out.append((vis, sym[0] in _VOWELS))
                i += len(sym)
                break
        else:
            if s[i] != "ː":
                out.append(("B", False))   # any other consonant: teeth together
            i += 1
    return out


def mouth_cues(words: list[dict[str, Any]], duration: float, lang: str = "en-us") -> list[dict[str, Any]]:
    ipa = _phonemes([w["word"] for w in words], lang) if words else []
    cues: list[dict[str, Any]] = []

    def add(start: float, end: float, v: str) -> None:
        if end - start < 0.01:
            return
        if cues and cues[-1]["value"] == v and abs(cues[-1]["end"] - start) < 0.02:
            cues[-1]["end"] = round(end, 3)
        else:
            cues.append({"start": round(start, 3), "end": round(end, 3), "value": v})

    t = 0.0
    for w, ph in zip(words, ipa):
        if w["start"] - t > 0.12:
            add(t, w["start"], "X")
        sounds = _split_ipa(ph) or [("C", True)]
        weights = [2.0 if vowel else 1.0 for _, vowel in sounds]
        span, pos = max(w["end"] - w["start"], 0.06), w["start"]
        for (vis, _), wt in zip(sounds, weights):
            d = span * wt / sum(weights)
            add(pos, pos + d, vis)
            pos += d
        t = w["end"]
    add(t, max(t, duration), "X")
    return cues


class LipsyncParams(BaseModel):
    file_id: str = Field(description="Voice audio (or a video with speech)")
    model: Literal["tiny", "base", "small", "medium", "large-v3"] = "base"
    language: str = Field("en", max_length=8)


@operation("lipsync", LipsyncParams, "Work out mouth shapes over time for a voice track (Rhubarb-style cues A-H and X) so a 3D or 2D character's mouth moves with the words. Returns a .json cue file.")
def lipsync(p: LipsyncParams) -> OpResult:
    segments, words, _ = words_of(p.file_id, p.model, p.language)
    duration = media_info(p.file_id)["duration_s"] or (words[-1]["end"] if words else 0)
    cues = mouth_cues(words, duration, "en-gb" if p.language == "en-gb" else "en-us")
    doc = {"metadata": {"soundFile": _stem(p.file_id), "duration": duration, "generator": "Builder lipsync (Whisper + espeak)"},
           "mouthCues": cues, "words": words}
    return _guarded("lipsync", [p.file_id], f"{_stem(p.file_id)}_mouth.json",
                    lambda out: out.write_text(json.dumps(doc, indent=1), encoding="utf-8"),
                    data={"cues": len(cues), "duration_s": duration, "text": " ".join(s["text"] for s in segments)})


# ---------------------------------------------------------------- audio polish


def _audio_job(op: str, file_id: str, chain: str, label: str, extra_inputs: list[str] | None = None, complex_: bool = False) -> OpResult:
    src = _src(file_id)
    info = media_info(file_id)
    if not info["has_audio"]:
        raise OpError(f"{op} needs a file with sound")
    ins = ["-i", str(src), *(extra_inputs or [])]
    if info["has_video"]:
        name = f"{_stem(file_id)}_{label}.mp4"
        filt = ["-filter_complex", chain, "-map", "0:v", "-map", "[a]"] if complex_ else ["-af", chain, "-map", "0:v", "-map", "0:a"]
        args = [*ins, *filt, "-c:v", "copy", *AUDIO_ENC]
    else:
        name = f"{_stem(file_id)}_{label}.wav"
        filt = ["-filter_complex", chain, "-map", "[a]"] if complex_ else ["-af", chain]
        args = [*ins, *filt, *AUDIO_CODECS["wav"]]
    return _guarded(op, [file_id], name, lambda out: _ffmpeg(*args, str(out)))


class DenoiseParams(BaseModel):
    file_id: str
    strength: Literal["light", "medium", "strong"] = "medium"


@operation("denoise_audio", DenoiseParams, "Remove steady background noise (hiss, hum, fans, room tone) from a voice recording or a video's sound.")
def denoise_audio(p: DenoiseParams) -> OpResult:
    nr = {"light": 8, "medium": 14, "strong": 22}[p.strength]
    chain = f"highpass=f=60,afftdn=nr={nr}:nf=-35:tn=1,anlmdn=s={0.0003 if p.strength == 'light' else 0.0008}"
    return _audio_job("denoise_audio", p.file_id, chain, "clean")


class VoicePolishParams(BaseModel):
    file_id: str
    style: Literal["broadcast", "podcast", "warm", "phone", "radio"] = Field("broadcast", description="broadcast/podcast: clear, even, loud; warm: fuller; phone/radio: band-limited effect")
    loudness: float = Field(-16, ge=-30, le=-9, description="Target loudness in LUFS (-16 web/podcast, -14 social, -23 TV)")


@operation("voice_polish", VoicePolishParams, "Make a voice sound finished: rumble cut, de-essing, compression and loudness for broadcast/podcast, or a phone/radio effect.")
def voice_polish(p: VoicePolishParams) -> OpResult:
    base = "highpass=f=80,deesser=i=0.4,acompressor=threshold=-20dB:ratio=3:attack=5:release=120:makeup=2"
    chain = {
        "broadcast": f"{base},equalizer=f=3000:t=q:w=1.2:g=2.5",
        "podcast": f"{base},equalizer=f=180:t=q:w=1:g=1.5,equalizer=f=3500:t=q:w=1.4:g=2",
        "warm": f"{base},equalizer=f=150:t=q:w=1:g=3,equalizer=f=8000:t=q:w=1:g=-2",
        "phone": "highpass=f=400,lowpass=f=3200,acompressor=threshold=-18dB:ratio=6,acrusher=bits=10:mix=0.25",
        "radio": "highpass=f=250,lowpass=f=5000,acompressor=threshold=-18dB:ratio=5:makeup=3,aecho=0.8:0.4:12:0.15",
    }[p.style] + f",loudnorm=I={p.loudness}:TP=-1.5:LRA=11"
    return _audio_job("voice_polish", p.file_id, chain, p.style)


ROOMS = {"small room": (0.35, 0.18), "living room": (0.6, 0.22), "studio": (0.25, 0.12), "hall": (1.8, 0.3),
         "church": (3.2, 0.35), "stairwell": (1.2, 0.32), "outdoors": (0.12, 0.06)}


class ReverbParams(BaseModel):
    file_id: str
    room: Literal["small room", "living room", "studio", "hall", "church", "stairwell", "outdoors"] = "living room"
    mix: float | None = Field(None, ge=0, le=1, description="How much room sound (default suits the room)")


@operation("room_reverb", ReverbParams, "Place a dry voice or sound in a space (small room, living room, studio, hall, church, stairwell, outdoors) so it matches the scene.")
def room_reverb(p: ReverbParams) -> OpResult:
    length, default_mix = ROOMS[p.room]
    mix = default_mix if p.mix is None else p.mix
    ir = ["-f", "lavfi", "-i", f"anoisesrc=d={length}:c=pink:r=48000:a=0.5,afade=t=out:st=0:d={length}:curve=exp,lowpass=f={9000 if length < 1 else 6000}"]
    chain = (f"[0:a]aresample=48000,asplit=2[d][w];[w][1:a]afir=gtype=gn[r];"
             f"[d][r]amix=inputs=2:weights='1 {mix:.2f}':normalize=0,alimiter=limit=0.95[a]")
    return _audio_job("room_reverb", p.file_id, chain, p.room.replace(" ", "-"), extra_inputs=ir, complex_=True)
