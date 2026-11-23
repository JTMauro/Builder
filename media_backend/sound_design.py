"""Sound design, all free and offline: original music, ambiences, footsteps and an audio check.

- make_music: original background music composed on the spot (generic chord progressions, bass,
  pads/keys/plucks, synthesized drums, reverb), for a mood and a length. Never imitates a real song.
- make_ambience: room tone and backgrounds (room, office, outdoors, city, rain, wind, crowd murmur,
  forest) from shaped noise plus event layers (birds, raindrops, typing, passing cars).
- footsteps: a footstep track on wood, concrete, grass, gravel, carpet, tile or snow, at a pace or at
  exact times (e.g. an animation's foot contacts).
- audio_check: loudness (LUFS), true peak, clipping, long silences, DC offset and, given the script,
  how well the speech matches it (Whisper).

Synthesis uses numpy (in the speech extra, `pip install "builder[voice]"`); ffmpeg does loudness and
encoding. Everything is seeded, so the same seed gives the same sound.
"""

from __future__ import annotations

import difflib
import json
import math
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from . import storage
from .config import NO_WINDOW, get_settings
from .ops import AUDIO_CODECS, OpError, OpResult, _ffmpeg, _guarded, _src, _stem, media_info, operation

SR = 44100


def _np():
    try:
        import numpy as np
    except ImportError as e:
        raise OpError('Sound design needs numpy: pip install "builder[voice]" (or pip install numpy)') from e
    return np


def _rng(seed: int | None):
    np = _np()
    seed = int(np.random.SeedSequence().entropy % (2**31)) if seed is None else seed
    return np.random.default_rng(seed), seed


# ---------------------------------------------------------------- DSP helpers


def _convolve(x, h):
    """Linear convolution by FFT overlap-add (fast for long signals and long impulse responses)."""
    np = _np()
    n, m = len(x), len(h)
    if n == 0 or m == 0:
        return np.zeros(n + max(m, 1) - 1, np.float32)
    block = max(1 << 14, 1 << int(math.ceil(math.log2(max(m, 2)))))
    nfft = 1 << int(math.ceil(math.log2(block + m - 1)))
    H = np.fft.rfft(h, nfft)
    y = np.zeros(n + m - 1, np.float32)
    for i in range(0, n, block):
        seg = x[i:i + block]
        out = np.fft.irfft(np.fft.rfft(seg, nfft) * H, nfft)[:len(seg) + m - 1]
        y[i:i + len(out)] += out.astype(np.float32)
    return y


def _fir(gain_fn, taps: int = 2048, sr: int = SR):
    """Linear-phase FIR from a magnitude response gain_fn(freqs_hz), scaled so white noise stays at RMS 1."""
    np = _np()
    f = np.fft.rfftfreq(taps, 1 / sr)
    g = np.asarray(gain_fn(np.maximum(f, 1.0)), float)
    h = np.roll(np.fft.irfft(g, taps), taps // 2) * np.hanning(taps)
    return (h / (np.sqrt(np.sum(h * h)) or 1)).astype(np.float32)


def _band(f, lo: float | None = None, hi: float | None = None, tilt_db: float = 0.0, order: int = 2):
    """Smooth band shape: high-pass at lo, low-pass at hi, and a slope in dB per octave around 1 kHz."""
    np = _np()
    g = (f / 1000.0) ** (tilt_db / 6.02)
    if lo:
        g = g / np.sqrt(1 + (lo / f) ** (2 * order))
    if hi:
        g = g / np.sqrt(1 + (f / hi) ** (2 * order))
    return g


def _filter(x, gain_fn, taps: int = 1024, sr: int = SR):
    h = _fir(gain_fn, taps, sr)
    return _convolve(x, h)[taps // 2: taps // 2 + len(x)]


def _noise(rng, n: int, gain_fn, taps: int = 4096):
    """Coloured noise: white noise through a FIR with the given shape (RMS about 1)."""
    np = _np()
    return _filter(rng.standard_normal(n).astype(np.float32), gain_fn, taps)


def _lfo(rng, n: int, rate_hz: float, lo: float, hi: float, sr: int = SR):
    """A smooth random curve between lo and hi that changes about rate_hz times a second."""
    np = _np()
    k = int(n / sr * rate_hz) + 3
    pts = rng.uniform(lo, hi, k)
    x = np.arange(n) / sr * rate_hz
    i = x.astype(int)
    fr = x - i
    s = fr * fr * (3 - 2 * fr)
    return (pts[i] * (1 - s) + pts[i + 1] * s).astype(np.float32)


def _put(buf, start_s: float, sig, gain: float = 1.0) -> None:
    i0 = int(round(start_s * SR))
    if i0 >= len(buf) or i0 + len(sig) <= 0:
        return
    a = max(0, -i0)
    i0 = max(0, i0)
    end = min(len(buf), i0 + len(sig) - a)
    buf[i0:end] += sig[a:a + end - i0] * gain


def _put_st(L, R, start_s: float, sig, gain: float = 1.0, pan: float = 0.0) -> None:
    """Place a mono sound in the stereo field (pan -1 left .. +1 right, constant power)."""
    ang = (pan + 1) * math.pi / 4
    _put(L, start_s, sig, gain * math.cos(ang) * math.sqrt(2))
    _put(R, start_s, sig, gain * math.sin(ang) * math.sqrt(2))


def _reverb_ir(rng, seconds: float, damp_hz: float, predelay: float = 0.012):
    """Stereo impulse response: decaying, darkened noise (unit energy per channel)."""
    np = _np()
    n = int(seconds * SR)
    t = np.arange(n) / SR
    out = []
    for _ in range(2):
        tail = _noise(rng, n, lambda f: _band(f, 80, damp_hz), 512) * np.exp(-6.9 * t / seconds)
        tail[: int(0.004 * SR)] *= np.linspace(0, 1, int(0.004 * SR))
        ir = np.concatenate([np.zeros(int(predelay * SR), np.float32), tail.astype(np.float32)])
        out.append(ir / (np.sqrt(np.sum(ir * ir)) or 1))
    return out


def _add_reverb(L, R, ir, wet: float):
    if wet <= 0:
        return L, R
    n = len(L)
    wl = _convolve(L, ir[0])[:n]
    wr = _convolve(R, ir[1])[:n]
    return L + wet * wl, R + wet * wr


def _fades(L, R, fade_in: float, fade_out: float) -> None:
    np = _np()
    for buf in (L, R):
        a = min(len(buf), int(fade_in * SR))
        if a > 0:
            buf[:a] *= (np.sin(np.linspace(0, math.pi / 2, a)) ** 2).astype(np.float32)
        b = min(len(buf), int(fade_out * SR))
        if b > 0:
            buf[-b:] *= (np.cos(np.linspace(0, math.pi / 2, b)) ** 2).astype(np.float32)


def _loudness(args: list[str]) -> dict[str, float | None]:
    """EBU R128 measurement (ffmpeg ebur128) of the given input args: integrated LUFS, true peak, LRA."""
    cmd = [get_settings().ffmpeg, "-hide_banner", "-nostats", "-y", *args, "-af", "ebur128=peak=true:framelog=quiet", "-f", "null", "-"]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=3600, **NO_WINDOW)
    except FileNotFoundError as e:
        raise OpError("ffmpeg is not installed or not on PATH") from e
    err = proc.stderr or ""
    summary = err[err.rfind("Summary:"):] if "Summary:" in err else ""
    if proc.returncode != 0 or not summary:
        raise OpError("Couldn't measure loudness:\n" + "\n".join(err.strip().splitlines()[-6:]))

    def num(pattern: str) -> float | None:
        m = re.search(pattern, summary)
        if not m:
            return None
        try:
            v = float(m.group(1))
        except ValueError:
            return None
        return v if math.isfinite(v) else None

    lufs = num(r"I:\s*(-?[\d.]+|-?inf)\s*LUFS")
    peak = num(r"True peak:\s*Peak:\s*(-?[\d.]+|-?inf)\s*dBFS")
    return {"lufs": None if lufs is None or lufs <= -70 else lufs, "true_peak_db": peak, "lra": num(r"LRA:\s*(-?[\d.]+)\s*LU")}


def _encode(out: Path, L, R, fmt: str, loudness: float | None, chain: list[str], peak_db: float = -3.0) -> dict[str, Any]:
    """Write stereo float audio to out (wav/mp3). With loudness: measure, set gain, limit peaks to -1.5 dBTP."""
    np = _np()
    work = Path(tempfile.mkdtemp(prefix="sound-"))
    try:
        st = np.stack([L, R], axis=1).astype(np.float32)
        peak = float(np.max(np.abs(st))) or 1.0
        st *= (0.5 if loudness is not None else 10 ** (peak_db / 20)) / peak
        raw = work / "mix.f32"
        st.tofile(raw)
        ins = ["-f", "f32le", "-ar", str(SR), "-ac", "2", "-i", str(raw)]
        filters = list(chain)
        if loudness is not None:
            if chain:   # measure what the chain produces
                pre = work / "pre.wav"
                _ffmpeg(*ins, "-af", ",".join(chain), "-c:a", "pcm_f32le", str(pre))
                ins, filters = ["-i", str(pre)], []
            m = _loudness(ins)
            if m["lufs"] is not None:
                gain = loudness - m["lufs"]
                filters.append(f"volume={gain:.2f}dB")
                if (m["true_peak_db"] if m["true_peak_db"] is not None else -99) + gain > -1.5:
                    filters.append("alimiter=limit=0.79:attack=4:release=60:level=0")   # about -2 dBFS
        args = [*ins, *(["-af", ",".join(filters)] if filters else []), "-ar", str(SR), *AUDIO_CODECS[fmt]]
        _ffmpeg(*args, str(out))
        return {}
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _hz(m: float) -> float:
    return 440.0 * 2 ** ((m - 69) / 12)


# ---------------------------------------------------------------- make_music

NOTE_PC = {"C": 0, "D": 2, "E": 4, "F": 5, "G": 7, "A": 9, "B": 11}
SCALES = {"major": [0, 2, 4, 5, 7, 9, 11], "minor": [0, 2, 3, 5, 7, 8, 10]}
# Generic, public-domain-style progressions as scale degrees (0 = tonic). Used everywhere in music.
PROGRESSIONS = {
    "calm": [[0, 4, 5, 3], [0, 3, 0, 4], [3, 0, 4, 5], [0, 5, 3, 4]],
    "upbeat": [[0, 4, 5, 3], [0, 5, 3, 4], [3, 4, 0, 5], [0, 3, 5, 4]],
    "playful": [[0, 3, 4, 0], [0, 5, 1, 4], [0, 3, 0, 4]],
    "lofi": [[1, 4, 0, 5], [3, 2, 1, 0], [0, 5, 1, 4], [3, 4, 2, 5]],
    "sad": [[0, 5, 2, 6], [0, 3, 5, 4], [5, 6, 0, 0], [0, 5, 3, 4]],
    "tense": [[0, 0, 5, 4], [0, 1, 0, 5], [0, 5, 0, 6]],
    "epic": [[0, 5, 2, 6], [0, 6, 5, 6], [5, 6, 0, 0], [0, 3, 5, 6]],
}
MOODS: dict[str, dict[str, Any]] = {
    "calm": {"bpm": 76, "mode": "major", "bpc": 2, "roots": ["C", "D", "F", "G"], "reverb": (2.6, 0.45), "chain": ["highpass=f=35", "lowpass=f=11000"]},
    "upbeat": {"bpm": 118, "mode": "major", "bpc": 1, "roots": ["C", "D", "E", "G", "A"], "reverb": (1.4, 0.22), "chain": ["highpass=f=30"]},
    "playful": {"bpm": 124, "mode": "major", "bpc": 1, "roots": ["C", "F", "G", "D"], "reverb": (1.1, 0.2), "chain": ["highpass=f=35"]},
    "lofi": {"bpm": 78, "mode": "major", "bpc": 1, "roots": ["C", "D", "F", "A"], "reverb": (1.3, 0.25), "chain": ["highpass=f=40", "lowpass=f=5200"]},
    "sad": {"bpm": 66, "mode": "minor", "bpc": 2, "roots": ["A", "D", "E", "C"], "reverb": (2.8, 0.45), "chain": ["highpass=f=35", "lowpass=f=10000"]},
    "tense": {"bpm": 96, "mode": "minor", "bpc": 2, "roots": ["D", "E", "C", "F"], "reverb": (2.0, 0.35), "chain": ["highpass=f=30"]},
    "epic": {"bpm": 84, "mode": "minor", "bpc": 1, "roots": ["D", "C", "E", "G"], "reverb": (2.6, 0.38), "chain": ["highpass=f=28"]},
}


def _parse_key(key: str | None, mood: dict[str, Any], rng) -> tuple[int, str, str]:
    if not key:
        name = mood["roots"][int(rng.integers(len(mood["roots"])))]
        return NOTE_PC[name], mood["mode"], f"{name} {mood['mode']}"
    m = re.fullmatch(r"\s*([A-Ga-g])\s*([#b♯♭]?)\s*(m|min|minor|maj|major)?\s*", key)
    if not m:
        raise OpError(f"key should look like 'C', 'A minor', 'F# major' or 'Ebm', not '{key}'")
    pc = (NOTE_PC[m.group(1).upper()] + {"#": 1, "♯": 1, "b": -1, "♭": -1}.get(m.group(2), 0)) % 12
    mode = "minor" if m.group(3) in ("m", "min", "minor") else "major" if m.group(3) else mood["mode"]
    return pc, mode, f"{m.group(1).upper()}{m.group(2)} {mode}"


def _table(np, amps: list[float], size: int = 4096):
    ph = np.arange(size) / size * 2 * math.pi
    w = sum(a * np.sin((h + 1) * ph) for h, a in enumerate(amps))
    return (w / np.max(np.abs(w))).astype(np.float32)


def _wt(np, table, f: float, n: int, vib: float = 0.0, phase0: float = 0.0):
    t = np.arange(n) / SR
    if vib:
        depth = vib * np.clip((t - 0.25) / 0.3, 0, 1)
        ph = phase0 + np.cumsum(f * (1 + depth * np.sin(2 * math.pi * 5.2 * t))) / SR
    else:
        ph = phase0 + f * t
    idx = (ph % 1.0) * len(table)
    i = idx.astype(np.int64)
    fr = (idx - i).astype(np.float32)
    return table[i % len(table)] * (1 - fr) + table[(i + 1) % len(table)] * fr


def _env(np, n: int, attack: float, dur: float, release: float, decay: float | None = None, sustain: float = 1.0):
    t = np.arange(n) / SR
    a = np.clip(t / max(attack, 1e-3), 0, 1)
    e = a * a * (3 - 2 * a)
    if decay:
        e = e * (sustain + (1 - sustain) * np.exp(-np.maximum(t - attack, 0) / decay))
    after = t > dur
    e[after] *= np.exp(-(t[after] - dur) / max(release / 4.6, 1e-3))
    return e.astype(np.float32)


class _Synth:
    """Small instrument set: additive pads/bass/lead from wavetables, FM keys, plucks, bells, drums."""

    def __init__(self, np, rng):
        self.np, self.rng = np, rng
        self.t_pad = _table(np, [1, 0.42, 0.26, 0.14, 0.09, 0.05, 0.03])
        self.t_bass = _table(np, [1, 0.35, 0.12, 0.05])
        self.t_bass_round = _table(np, [1, 0.15, 0.03])
        self.t_lead = _table(np, [1, 0.5, 0.28, 0.16, 0.08, 0.04])
        self.t_brass = _table(np, [1, 0.7, 0.5, 0.35, 0.22, 0.12, 0.07, 0.04])
        self._drums = self._drum_kit()

    def pad(self, f: float, dur: float, table=None):
        np = self.np
        rel = min(1.6, 0.5 + dur * 0.25)
        n = int((dur + rel) * SR)
        env = _env(np, n, min(0.9, dur * 0.35), dur, rel)
        tb = self.t_pad if table is None else table
        a = _wt(np, tb, f * 1.0025, n, phase0=self.rng.random())
        b = _wt(np, tb, f / 1.0025, n, phase0=self.rng.random())
        return a * env, b * env

    def bass(self, f: float, dur: float, round_: bool = False):
        np = self.np
        n = int((dur + 0.08) * SR)
        env = _env(np, n, 0.006, dur, 0.08, decay=0.35, sustain=0.7)
        return _wt(np, self.t_bass_round if round_ else self.t_bass, f, n) * env

    def keys(self, f: float, dur: float, index: float = 1.2):
        """Electric-piano-like FM tone."""
        np = self.np
        n = int((dur + 0.3) * SR)
        t = np.arange(n) / SR
        idx = index * np.exp(-t / 0.35) + 0.12
        y = np.sin(2 * math.pi * f * t + idx * np.sin(2 * math.pi * f * t))
        env = _env(np, n, 0.004, dur, 0.3, decay=1.1, sustain=0.0)
        return (y * env).astype(np.float32)

    def pluck(self, f: float, dur: float, bright: float = 1.0):
        np = self.np
        n = int((min(dur, 1.2) + 0.25) * SR)
        t = np.arange(n) / SR
        y = np.zeros(n)
        for h in range(1, 8):
            if f * h > 9000:
                break
            y += np.sin(2 * math.pi * f * h * t) / h ** 1.2 * np.exp(-t * (2.5 + h * 3.0 / bright))
        env = _env(np, n, 0.003, dur, 0.2)
        return (y * env / 1.6).astype(np.float32)

    def marimba(self, f: float, dur: float):
        np = self.np
        n = int(0.7 * SR)
        t = np.arange(n) / SR
        y = np.sin(2 * math.pi * f * t) * np.exp(-t / 0.32) + 0.22 * np.sin(2 * math.pi * f * 3.93 * t) * np.exp(-t / 0.06)
        return (y * _env(np, n, 0.002, 0.7, 0.05)).astype(np.float32)

    def bell(self, f: float, dur: float):
        np = self.np
        n = int(2.2 * SR)
        t = np.arange(n) / SR
        y = (np.sin(2 * math.pi * f * t) * np.exp(-t / 1.1) + 0.25 * np.sin(2 * math.pi * f * 2.0 * t) * np.exp(-t / 0.5)
             + 0.08 * np.sin(2 * math.pi * f * 3.01 * t) * np.exp(-t / 0.25))
        return (y * _env(np, n, 0.003, 2.2, 0.05)).astype(np.float32)

    def lead(self, f: float, dur: float, table=None, attack: float = 0.02):
        np = self.np
        n = int((dur + 0.25) * SR)
        env = _env(np, n, attack, dur, 0.25, decay=0.5, sustain=0.75)
        return _wt(np, self.t_lead if table is None else table, f, n, vib=0.004) * env

    # drums
    def _drum_kit(self):
        np, rng = self.np, self.rng
        kit: dict[str, list] = {}

        def t_(sec):
            return np.arange(int(sec * SR)) / SR

        kicks = []
        for _ in range(3):
            t = t_(0.5)
            fr = 46 + 90 * np.exp(-t / 0.032) * rng.uniform(0.95, 1.05)
            body = np.sin(2 * math.pi * np.cumsum(fr) / SR) * np.exp(-t / 0.26)
            click = _filter(rng.standard_normal(len(t)).astype(np.float32), lambda f: _band(f, 1500, 6000), 256) * np.exp(-t / 0.004) * 0.12
            kicks.append((body + click).astype(np.float32))
        kit["kick"] = kicks
        snares = []
        for _ in range(4):
            t = t_(0.35)
            tone = (np.sin(2 * math.pi * 185 * t) + 0.5 * np.sin(2 * math.pi * 330 * t)) * np.exp(-t / 0.05) * 0.45
            noise = _filter(rng.standard_normal(len(t)).astype(np.float32), lambda f: _band(f, 1200, 7500), 512) * np.exp(-t / 0.11) * 0.35
            snares.append((tone + noise).astype(np.float32))
        kit["snare"] = snares
        kit["rim"] = [(_filter(rng.standard_normal(int(0.12 * SR)).astype(np.float32), lambda f: _band(f, 1500, 5000), 256)
                       * np.exp(-t_(0.12) / 0.025) * 0.35 + np.sin(2 * math.pi * 420 * t_(0.12)) * np.exp(-t_(0.12) / 0.03) * 0.3).astype(np.float32)
                      for _ in range(3)]
        kit["hat"] = [(_filter(rng.standard_normal(int(0.12 * SR)).astype(np.float32), lambda f: _band(f, 7000, 13000), 256)
                       * np.exp(-t_(0.12) / 0.022) * 0.16).astype(np.float32) for _ in range(4)]
        kit["ohat"] = [(_filter(rng.standard_normal(int(0.5 * SR)).astype(np.float32), lambda f: _band(f, 6500, 12000), 256)
                        * np.exp(-t_(0.5) / 0.14) * 0.12).astype(np.float32) for _ in range(2)]
        kit["shaker"] = [(_filter(rng.standard_normal(int(0.09 * SR)).astype(np.float32), lambda f: _band(f, 5000, 10000), 256)
                          * np.sin(np.linspace(0, math.pi, int(0.09 * SR))) ** 3 * 0.12).astype(np.float32) for _ in range(4)]
        toms = []
        for base in (110, 82, 62):
            t = t_(0.7)
            fr = base * (1 + 0.35 * np.exp(-t / 0.05))
            toms.append(((np.sin(2 * math.pi * np.cumsum(fr) / SR) * np.exp(-t / 0.32))
                         + _filter(rng.standard_normal(len(t)).astype(np.float32), lambda f: _band(f, 100, 900), 256) * np.exp(-t / 0.05) * 0.2).astype(np.float32))
        kit["tom"] = toms
        t = t_(2.5)
        kit["crash"] = [(_filter(rng.standard_normal(len(t)).astype(np.float32), lambda f: _band(f, 3500, 11000), 256) * np.exp(-t / 0.7) * 0.1).astype(np.float32)]
        return kit

    def drum(self, kind: str, variant: int | None = None):
        opts = self._drums[kind]
        return opts[int(self.rng.integers(len(opts))) if variant is None else variant % len(opts)]


def _voicing(pcs: list[int], center: int) -> list[int]:
    """Place each pitch class at the octave nearest to center (a close, smooth voicing)."""
    out = []
    for pc in pcs:
        m = center - ((center - pc) % 12)
        out.append(m + 12 if center - m > 6 else m)
    return sorted(out)


def _compose(p: "MusicParams", rng) -> tuple[Any, Any, dict[str, Any]]:
    np = _np()
    mood = MOODS[p.mood]
    bpm = float(p.tempo_bpm or mood["bpm"])
    root, mode, key_name = _parse_key(p.key, mood, rng)
    scale = SCALES[mode]
    beat = 60.0 / bpm
    bar = 4 * beat
    step = beat / 4
    n_bars = int(math.ceil(p.length_s / bar)) + 1
    bpc = mood["bpc"]
    prog = PROGRESSIONS[p.mood][int(rng.integers(len(PROGRESSIONS[p.mood])))]
    alt = PROGRESSIONS[p.mood][int(rng.integers(len(PROGRESSIONS[p.mood])))]
    total = n_bars * bar + 3.0
    n = int(total * SR)
    L = np.zeros(n, np.float32)
    R = np.zeros(n, np.float32)
    syn = _Synth(np, rng)
    sevenths = p.mood in ("lofi",) or (p.mood in ("calm", "sad") and rng.random() < 0.5)

    def chord_at(bar_i: int) -> tuple[int, list[int]]:
        slot = bar_i // bpc
        last_slot = (n_bars - 2) // bpc
        if slot >= last_slot:          # end on the home chord
            deg = 0
        else:
            seq = prog if (slot // len(prog)) % 4 != 2 else alt   # a contrasting pass every so often
            deg = seq[slot % len(seq)]
        tones = [deg, deg + 2, deg + 4] + ([deg + 6] if sevenths else [])
        pcs = [(root + scale[d % 7]) % 12 for d in tones]
        if mode == "minor" and deg == 4 and p.mood in ("tense", "epic"):
            pcs[1] = (pcs[1] + 1) % 12   # major V (harmonic minor) for pull back home
        return pcs[0], pcs

    intro = 0 if n_bars <= 5 else (1 if n_bars <= 10 else 2)
    outro_from = n_bars - 2 if n_bars > 10 else n_bars + 1
    scale_notes = [m for m in range(36, 100) if (m - root) % 12 in scale]

    def nearest(m: float) -> int:
        return min(range(len(scale_notes)), key=lambda i: abs(scale_notes[i] - m))

    # -------- harmony, bass and rhythm, bar by bar
    prev_pad_center = 60 + (root % 12 if root < 6 else root - 12)
    for b in range(n_bars):
        t0 = b * bar
        bass_pc, pcs = chord_at(b)
        bass_m = 36 + bass_pc if bass_pc < 10 else 24 + bass_pc
        fifth = bass_m + 7
        first_of_chord = b % bpc == 0
        drums_on = intro <= b < outro_from
        groove_on = b >= intro

        # pads
        if first_of_chord:
            dur = bar * bpc
            pad_gain = {"calm": 0.07, "sad": 0.055, "epic": 0.07, "tense": 0.06, "upbeat": 0.055, "playful": 0.05, "lofi": 0.035}[p.mood]
            center = prev_pad_center if p.mood != "epic" else 57
            for m in _voicing(pcs, center):
                a, c = syn.pad(_hz(m), dur)
                _put(L, t0, a, pad_gain)
                _put(R, t0, c, pad_gain)
            if p.mood in ("epic", "sad", "calm"):   # low octave doubling for warmth
                a, c = syn.pad(_hz(bass_m + 12), dur)
                _put(L, t0, a, pad_gain * 0.6)
                _put(R, t0, c, pad_gain * 0.6)

        # bass
        if groove_on or p.mood in ("calm", "sad"):
            if p.mood == "upbeat":
                for s in range(0, 16, 2):
                    m = bass_m + (12 if s in (6, 14) and rng.random() < 0.5 else 0)
                    _put_st(L, R, t0 + s * step, syn.bass(_hz(m), step * 1.6), 0.16 if s % 4 == 0 else 0.12)
            elif p.mood == "playful":
                for s, m, d in ((0, bass_m, 3), (6, bass_m, 1), (8, fifth, 3), (12, bass_m + 12, 2)):
                    _put_st(L, R, t0 + s * step, syn.bass(_hz(m), step * d), 0.16)
            elif p.mood == "lofi":
                for s, m, d in ((0, bass_m, 6), (10, fifth if rng.random() < 0.5 else bass_m, 3), (14, bass_m, 1.5)):
                    _put_st(L, R, t0 + s * step, syn.bass(_hz(m), step * d, round_=True), 0.18)
            elif p.mood == "tense":
                for s in range(0, 16, 2):
                    _put_st(L, R, t0 + s * step, syn.bass(_hz(bass_m), step * 1.2), 0.11 if s % 8 else 0.15)
            elif first_of_chord:   # calm, sad, epic: long notes
                d = bar * bpc
                _put_st(L, R, t0, syn.bass(_hz(bass_m), d * 0.95, round_=True), {"calm": 0.1, "sad": 0.1, "epic": 0.17}[p.mood])

        # chordal / arpeggio part
        tones = _voicing(pcs, 69 if p.mood != "tense" else 57)
        tones = tones[:3] + [tones[0] + 12]
        if p.mood == "upbeat":
            for s in (2, 6, 10, 14):
                for m in _voicing(pcs, 64):
                    _put_st(L, R, t0 + s * step, syn.keys(_hz(m), step * 1.3, 0.8), 0.09, pan=-0.25)
            pat = [0, 2, 1, 3, 0, 2, 1, 3]
            for k, s in enumerate(range(0, 16, 2)):
                _put_st(L, R, t0 + s * step, syn.pluck(_hz(tones[pat[k]] + 12), step * 2), 0.13, pan=0.35)
        elif p.mood == "playful":
            pat = [0, 2, 3, 2, 1, 2, 3, 2]
            for k, s in enumerate(range(0, 16, 2)):
                _put_st(L, R, t0 + s * step, syn.marimba(_hz(tones[pat[k]] + 12), step * 2), 0.11 if k % 2 == 0 else 0.08, pan=0.3)
        elif p.mood == "lofi":
            hits = [(0, 6), (7, 5)] if rng.random() < 0.6 else [(0, 10), (10, 6)]
            for s, d in hits:
                for i, m in enumerate(_voicing(pcs, 62)):
                    _put_st(L, R, t0 + s * step + i * 0.012, syn.keys(_hz(m), step * d, 0.6), 0.075, pan=-0.15)
        elif p.mood == "calm":
            for k in range(4):
                _put_st(L, R, t0 + k * beat, syn.pluck(_hz(tones[k] + 12), beat * 2, bright=0.9), 0.2, pan=0.3 if k % 2 else -0.3)
        elif p.mood == "sad":
            pat = [0, 1, 2, 1, 3, 1, 2, 1]
            for k in range(8):
                _put_st(L, R, t0 + k * beat / 2, syn.keys(_hz(tones[pat[k]]), beat * 1.5, 0.5), 0.085, pan=-0.1)
        elif p.mood == "tense":
            pat = [0, 0, 1, 0, 2, 0, 1, 0]
            for k in range(8):
                _put_st(L, R, t0 + k * beat / 2, syn.pluck(_hz(tones[pat[k]]), beat / 2, bright=0.6), 0.17, pan=0.25)
        elif p.mood == "epic" and groove_on:
            low = _voicing(pcs, 50)
            pat = [0, 0, 1, 0, 2, 0, 1, 2]
            for k in range(8):
                _put_st(L, R, t0 + k * beat / 2, syn.pluck(_hz(low[pat[k]]), beat / 2, bright=0.8), 0.17, pan=-0.3)

        # drums
        if drums_on:
            _drums_bar(p.mood, syn, L, R, t0, step, b - intro, rng)

    # -------- melody: a short motif repeated over the chords, in alternate sections
    if p.mood != "tense":
        _melody(p.mood, syn, L, R, rng, n_bars, intro, outro_from, bar, step, chord_at, scale_notes, nearest, root)

    if p.mood == "lofi":   # vinyl crackle
        imp = np.zeros(n, np.float32)
        k = int(total * 6)
        imp[rng.integers(0, n, k)] = (rng.exponential(1.0, k) * rng.choice([-1, 1], k)).astype(np.float32)
        click = (rng.standard_normal(40) * np.exp(-np.arange(40) / 6)).astype(np.float32)
        cr = _convolve(imp, click)[:n] * 0.006 + _noise(rng, n, lambda f: _band(f, 2000, 8000), 512) * 0.0015
        L += cr
        R += cr

    rev_len, wet = mood["reverb"]
    L, R = _add_reverb(L, R, _reverb_ir(rng, rev_len, 6500 if p.mood != "lofi" else 4000), wet)
    end = int(p.length_s * SR)
    L, R = L[:end].copy(), R[:end].copy()
    _fades(L, R, min(1.5, p.length_s * 0.1) if intro else 0.05, min(4.0, p.length_s * 0.15))
    return L, R, {"key": key_name, "tempo_bpm": round(bpm, 2), "bars": n_bars - 1,
                  "progression": [["I", "ii", "iii", "IV", "V", "vi", "vii"][d] if mode == "major" else ["i", "ii°", "III", "iv", "v", "VI", "VII"][d] for d in prog]}


def _drums_bar(mood: str, syn: _Synth, L, R, t0: float, step: float, b: int, rng) -> None:
    def hit(kind, s, g, pan=0.0, swing=0.0):
        _put_st(L, R, t0 + s * step + swing + rng.normal(0, 0.002), syn.drum(kind), g * rng.uniform(0.88, 1.0), pan)

    fill = b % 8 == 7
    if mood == "upbeat":
        for s in (0, 8) + ((10,) if b % 4 == 3 else ()) + ((6,) if b % 2 else ()):
            hit("kick", s, 0.42)
        for s in (4, 12):
            hit("snare", s, 0.42)
        if fill:
            hit("snare", 14, 0.25)
            hit("snare", 15, 0.3)
        for s in range(0, 16, 2):
            hit("ohat" if s == 14 and b % 4 == 1 else "hat", s, 0.5 if s % 4 == 2 else 0.32, pan=0.3)
        if b % 8 == 0:
            hit("crash", 0, 0.6, pan=-0.2)
    elif mood == "playful":
        for s in (0, 8):
            hit("kick", s, 0.45)
        for s in (4, 12):
            hit("rim", s, 0.5, pan=-0.1)
        for s in range(16):
            hit("shaker", s, 0.5 if s % 2 == 0 else 0.3, pan=0.35)
    elif mood == "lofi":
        sw = step * 0.33
        for s in (0, 10) + ((7,) if b % 2 else ()):
            hit("kick", s, 0.42)
        for s in (4, 12):
            hit("snare", s, 0.26)
            hit("rim", s, 0.12)
        for s in range(0, 16, 2):
            hit("hat", s, 0.32 if s % 4 == 0 else 0.22, pan=0.25, swing=sw if s % 4 == 2 else 0.0)
    elif mood == "tense":
        for s in (0, 3):
            hit("kick", s, 0.42 if s == 0 else 0.28)
        for s in range(0, 16, 2):
            hit("hat", s, 0.14, pan=0.4)
        if b % 4 == 3:
            hit("tom", 12, 0.25, pan=-0.2)
    elif mood == "epic":
        hit("kick", 0, 0.6)
        hit("tom", 0, 0.35, pan=0.1)
        hit("kick", 10, 0.45)
        hit("snare", 8, 0.45)
        hit("tom", 6, 0.22, pan=-0.3)
        if fill or b % 4 == 3:
            for s, k in ((12, 0), (13, 0), (14, 1), (15, 2)):
                _put_st(L, R, t0 + s * step, syn.drum("tom", k), 0.3, (k - 1) * 0.3)
        if b % 8 == 0:
            hit("crash", 0, 0.7, pan=0.2)


MOTIFS = {
    "busy": [[(0, 3), (4, 2), (6, 2), (8, 4), (14, 2), (16, 3), (20, 2), (22, 2), (24, 6)],
             [(0, 2), (2, 2), (4, 4), (10, 2), (12, 4), (16, 2), (18, 2), (20, 4), (24, 8)],
             [(0, 4), (6, 2), (8, 2), (10, 6), (16, 4), (22, 2), (24, 8)]],
    "sparse": [[(0, 8), (8, 4), (12, 4), (16, 12)], [(0, 6), (6, 2), (8, 8), (20, 4), (24, 8)], [(2, 6), (8, 8), (18, 6), (24, 8)]],
    "long": [[(0, 12), (12, 4), (16, 16)], [(0, 8), (8, 8), (16, 8), (24, 8)]],
}


def _melody(mood, syn, L, R, rng, n_bars, intro, outro_from, bar, step, chord_at, scale_notes, nearest, root) -> None:
    density = {"upbeat": "busy", "playful": "busy", "epic": "long"}.get(mood, "sparse")
    motifs = MOTIFS[density]
    rhythm = motifs[int(rng.integers(len(motifs)))]
    contour = [0] + [int(rng.choice([-2, -1, -1, 1, 1, 2, 0])) for _ in rhythm[1:]]
    center = 72 + ((root % 12) if root % 12 < 6 else (root % 12) - 12)
    lo, hi = nearest(center - 7), nearest(center + 9)
    voice = {"upbeat": ("lead", 0.17), "playful": ("marimba", 0.13), "epic": ("brass", 0.16),
             "calm": ("bell", 0.12), "sad": ("keys", 0.11), "lofi": ("bell", 0.1)}[mood]
    playing = list(range(intro + 2, outro_from)) if n_bars - intro < 14 else [
        b for b in range(intro, outro_from) if ((b - intro) // 8) % 2 == 1]
    idx = nearest(center)
    for b in playing:
        if (b - (intro if n_bars - intro >= 14 else intro + 2)) % 2:
            continue
        if b + 1 >= outro_from and n_bars - intro >= 14:
            break
        _, pcs = chord_at(b)
        i = idx
        for k, ((s, d), c) in enumerate(zip(rhythm, contour)):
            i = min(max(i + c, lo), hi)
            cur_bar = b + s // 16
            _, cp = chord_at(cur_bar)
            m = scale_notes[i]
            if s % 8 == 0 or k == len(rhythm) - 1:   # strong beats land on chord tones
                cands = [x for x in range(m - 4, m + 5) if x % 12 in cp]
                if cands:
                    m = min(cands, key=lambda x: abs(x - m))
                    i = nearest(m)
            dur = d * step
            t = b * bar + s * step
            name, g = voice
            if name == "lead":
                sig = syn.lead(_hz(m), dur * 0.9)
            elif name == "brass":
                sig = syn.lead(_hz(m - 12), dur * 0.95, syn.t_brass, attack=0.08)
            elif name == "marimba":
                sig = syn.marimba(_hz(m), dur)
            elif name == "bell":
                sig = syn.bell(_hz(m), dur)
            else:
                sig = syn.keys(_hz(m), dur, 0.7)
            _put_st(L, R, t, sig, g * rng.uniform(0.9, 1.0), pan=0.1)
        idx = i


class MusicParams(BaseModel):
    mood: Literal["calm", "upbeat", "tense", "sad", "epic", "playful", "lofi"] = Field("calm", description="Feel of the music")
    length_s: float = Field(30, ge=5, le=600, description="Length in seconds")
    tempo_bpm: float | None = Field(None, ge=50, le=180, description="Tempo; default suits the mood")
    key: str | None = Field(None, max_length=12, description="Musical key such as 'C', 'A minor', 'F# major', 'Ebm'; default chosen by the seed")
    seed: int | None = Field(None, ge=0, le=2**31 - 1, description="Same seed + settings = the same piece; leave empty for a new one")
    format: Literal["wav", "mp3"] = "wav"


@operation("make_music", MusicParams, "Compose ORIGINAL background music (free, offline): a mood (calm, upbeat, tense, sad, epic, playful, lofi), a length, optional tempo/key and a seed for repeatability. Chords, bass, pads/keys, melody and drums are synthesized here, never copied from a song; loudness about -16 LUFS. Mix under a video with replace_audio.")
def make_music(p: MusicParams) -> OpResult:
    rng, seed = _rng(p.seed)
    L, R, meta = _compose(p, rng)
    data = {"mood": p.mood, "seed": seed, "duration_s": round(len(L) / SR, 3), **meta, "license": "Original, generated by Builder: free to use"}

    def build(out: Path) -> None:
        data.update(_encode(out, L, R, p.format, -16.0, MOODS[p.mood]["chain"]))

    return _guarded("make_music", [], f"music_{p.mood}_{seed}.{p.format}", build, data=data)


# ---------------------------------------------------------------- make_ambience

AMBIENCE_LEVEL = {"room": -45, "office": -36, "outdoors": -32, "city": -28, "rain": -24, "wind": -26, "crowd_murmur": -30, "forest": -30}


def _stereo_bed(rng, n: int, shape, taps: int = 4096, width: float = 1.0):
    """Two partly independent noise channels with the same colour."""
    a = _noise(rng, n, shape, taps)
    b = _noise(rng, n, shape, taps)
    mix = (1 - width) * 0.5
    return a * (1 - mix) + b * mix, b * (1 - mix) + a * mix


def _birds(np, rng, L, R, dur: float, rate: float, gain: float) -> None:
    species = []
    for _ in range(3):
        species.append({"f0": rng.uniform(2300, 5200), "sweep": rng.uniform(0.6, 1.6), "len": rng.uniform(0.05, 0.22),
                        "gap": rng.uniform(0.05, 0.2), "count": (int(rng.integers(1, 3)), int(rng.integers(3, 8))),
                        "trill": rng.uniform(0, 45), "pan": rng.uniform(-0.8, 0.8)})
    t = rng.exponential(1 / rate)
    while t < dur - 0.5:
        sp = species[int(rng.integers(len(species)))]
        g = gain * rng.uniform(0.3, 1.0)
        pan = float(np.clip(sp["pan"] + rng.normal(0, 0.15), -1, 1))
        tt = t
        for _ in range(int(rng.integers(*sp["count"]))):
            ln = sp["len"] * rng.uniform(0.85, 1.15)
            k = int(ln * SR)
            x = np.arange(k) / k
            fr = sp["f0"] * rng.uniform(0.95, 1.05) * sp["sweep"] ** x * (1 + 0.03 * np.sin(2 * math.pi * sp["trill"] * x * ln))
            sig = (np.sin(2 * math.pi * np.cumsum(fr) / SR) * np.sin(math.pi * x) ** 2).astype(np.float32)
            _put_st(L, R, tt, sig, g, pan)
            tt += ln + sp["gap"] * rng.uniform(0.8, 1.2)
        t = tt + rng.exponential(1 / rate)


def _impulses(np, rng, n: int, rate: float, amp_scale: float = 1.0):
    imp = np.zeros(n, np.float32)
    k = rng.poisson(rate * n / SR)
    imp[rng.integers(0, n, k)] += (rng.exponential(amp_scale, k) * rng.choice([-1.0, 1.0], k)).astype(np.float32)
    return imp


def _drop_kernel(np, rng, f: float, tau: float):
    k = int(tau * 6 * SR)
    t = np.arange(k) / SR
    return (np.sin(2 * math.pi * np.cumsum(f * (1 + 0.6 * t / (tau * 6))) / SR) * np.exp(-t / tau)).astype(np.float32)


def _murmur(np, rng, n: int, voices: int, lp: float):
    """Many distant talkers: pitched buzz plus breath through vowel-like bands, gated by syllables and phrases.

    Synthesized at a quarter of the sample rate (it's all below 4 kHz), then upsampled."""
    q = 4
    sr = SR // q
    m = n // q + 2
    L = np.zeros(m, np.float32)
    R = np.zeros(m, np.float32)
    lp = min(lp, sr * 0.42)
    for _ in range(voices):
        pitch = rng.uniform(95, 230) * _lfo(rng, m, 3.0, 0.85, 1.18, sr)
        ph = np.cumsum(pitch) / sr
        buzz = (2 * (ph % 1.0) - 1).astype(np.float32)
        breath = rng.standard_normal(m).astype(np.float32) * 0.6
        f1, f2 = rng.uniform(400, 800), rng.uniform(1100, 2200)
        src = _filter(buzz * 0.5 + breath, lambda f: (np.exp(-((f - f1) / 180) ** 2) + 0.6 * np.exp(-((f - f2) / 300) ** 2) + 0.1) * _band(f, 150, lp), 256, sr)
        syll = np.clip(_lfo(rng, m, rng.uniform(4, 7), -0.6, 1.0, sr), 0, None)
        phrase = np.clip(_lfo(rng, m, 0.4, -0.5, 1.0, sr) * 4, 0, 1)   # talking, then a pause
        sig = src * syll * phrase
        pan = rng.uniform(-0.9, 0.9)
        L += sig * math.cos((pan + 1) * math.pi / 4)
        R += sig * math.sin((pan + 1) * math.pi / 4)
    up = _fir(lambda f: _band(f, None, lp, order=4), 512) * q
    h = up / (np.sum(up) or 1) * q   # unity gain in the pass band for zero-stuffed input
    out = []
    for buf in (L, R):
        z = np.zeros(m * q, np.float32)
        z[::q] = buf
        out.append(_convolve(z, h)[256: 256 + n] / math.sqrt(voices))
    return out[0], out[1]


def _ambience(kind: str, length_s: float, rng):
    np = _np()
    n = int(length_s * SR)
    dur = length_s
    if kind == "room":
        L, R = _stereo_bed(rng, n, lambda f: _band(f, 25, 700, -4), width=0.7)
        aL, aR = _stereo_bed(rng, n, lambda f: _band(f, 200, 6000, -3))
        L, R = L + 0.08 * aL, R + 0.08 * aR
    elif kind == "office":
        L, R = _stereo_bed(rng, n, lambda f: _band(f, 40, 900, -3), width=0.6)   # air handling
        mL, mR = _murmur(np, rng, n, 5, 1800)
        L, R = L + 0.35 * mL, R + 0.35 * mR
        typing = np.zeros(n, np.float32)
        t = rng.exponential(6)
        while t < dur - 1:
            burst = rng.uniform(1.5, 5)
            m = int(burst * rng.uniform(5, 9))
            for k in range(m):
                i = int((t + k / (m / burst) + rng.normal(0, 0.02)) * SR)
                if 0 <= i < n:
                    typing[i] += rng.uniform(0.5, 1.0)
            t += burst + rng.exponential(7)
        click = _filter(rng.standard_normal(200).astype(np.float32), lambda f: _band(f, 1500, 6000), 128) * np.exp(-np.arange(200) / 25)
        ty = _convolve(typing, click)[:n] * 0.25
        L, R = L + ty * 0.8, R + ty * 0.5
    elif kind in ("outdoors", "forest"):
        gust = _lfo(rng, n, 0.15, 0.3, 1.0)
        L, R = _stereo_bed(rng, n, lambda f: _band(f, 30, 500, -4))
        L, R = L * gust * 0.5, R * gust * 0.5
        lvs = _filter(rng.standard_normal(n).astype(np.float32), lambda f: _band(f, 1200, 9000), 1024)
        lvs2 = _filter(rng.standard_normal(n).astype(np.float32), lambda f: _band(f, 1200, 9000), 1024)
        leaves = 0.45 if kind == "forest" else 0.18
        g2 = _lfo(rng, n, 0.25, 0.1, 1.0) * gust
        L, R = L + lvs * g2 * leaves, R + lvs2 * g2 * leaves
        if kind == "outdoors":   # far-off traffic
            tL, tR = _stereo_bed(rng, n, lambda f: _band(f, 40, 400, -5))
            L, R = L + 0.25 * tL * _lfo(rng, n, 0.1, 0.5, 1.0), R + 0.25 * tR * _lfo(rng, n, 0.1, 0.5, 1.0)
            _birds(np, rng, L, R, dur, 0.06, 0.25)
        else:
            _birds(np, rng, L, R, dur, 0.35, 0.6)
    elif kind == "city":
        L, R = _stereo_bed(rng, n, lambda f: _band(f, 30, 900, -5), width=0.8)
        L, R = L * 0.6, R * 0.6
        t = rng.exponential(3)
        while t < dur:
            ln = rng.uniform(3, 7)
            k = int(ln * SR)
            x = np.arange(k) / k
            car = _filter(rng.standard_normal(k).astype(np.float32), lambda f: _band(f, 50, rng.uniform(900, 2500), -3), 1024)
            env = np.exp(-((x - 0.5) / 0.18) ** 2).astype(np.float32)
            p0 = rng.choice([-1, 1]) * rng.uniform(0.5, 0.9)
            pans = p0 - 2 * p0 * x
            g = rng.uniform(0.4, 1.0)
            _put(L, t, car * env * np.cos((pans + 1) * math.pi / 4) * math.sqrt(2), g)
            _put(R, t, car * env * np.sin((pans + 1) * math.pi / 4) * math.sqrt(2), g)
            t += rng.exponential(4.5)
        t = rng.exponential(40)
        while t < dur - 1:   # a distant horn now and then
            ln = rng.uniform(0.25, 0.6)
            tt = np.arange(int(ln * SR)) / SR
            f = rng.uniform(380, 480)
            horn = sum(np.sin(2 * math.pi * f * h * tt) / h for h in range(1, 6)) + 0.6 * np.sin(2 * math.pi * f * 1.26 * tt)
            horn = _filter((horn * np.minimum(1, np.minimum(tt / 0.03, (ln - tt) / 0.05))).astype(np.float32), lambda fr: _band(fr, 200, 1800), 512)
            _put_st(L, R, t, horn, 0.05, rng.uniform(-0.8, 0.8))
            t += rng.exponential(40)
        mL, mR = _murmur(np, rng, n, 4, 1500)
        L, R = L + 0.2 * mL, R + 0.2 * mR
    elif kind == "rain":
        swell = _lfo(rng, n, 0.08, 0.85, 1.1)
        L, R = _stereo_bed(rng, n, lambda f: _band(f, 300, 11000, -2))
        L, R = L * swell * 0.5, R * swell * 0.5
        lL, lR = _stereo_bed(rng, n, lambda f: _band(f, 30, 300, -3))
        L, R = L + 0.3 * lL, R + 0.3 * lR
        for buf in (L, R):
            for _ in range(5):
                k = _drop_kernel(np, rng, rng.uniform(1500, 4500), rng.uniform(0.002, 0.006))
                buf += _convolve(_impulses(np, rng, n, 40), k)[:n] * 0.05
        for _ in range(3):   # a few closer drips
            k = _drop_kernel(np, rng, rng.uniform(900, 2200), rng.uniform(0.006, 0.012))
            d = _convolve(_impulses(np, rng, n, 0.8), k)[:n] * 0.25
            pan = rng.uniform(-0.7, 0.7)
            L += d * math.cos((pan + 1) * math.pi / 4)
            R += d * math.sin((pan + 1) * math.pi / 4)
    elif kind == "wind":
        gust = _lfo(rng, n, 0.2, 0.15, 1.0) ** 1.5
        L, R = _stereo_bed(rng, n, lambda f: _band(f, 25, 350, -3))
        mL, mR = _stereo_bed(rng, n, lambda f: _band(f, 250, 1400, -2))
        g2 = _lfo(rng, n, 0.3, 0.2, 1.0) * gust
        L, R = L * (0.4 + gust) * 0.6 + mL * g2 * 0.5, R * (0.4 + gust) * 0.6 + mR * g2 * 0.5
        for c in (rng.uniform(420, 560), rng.uniform(600, 800), rng.uniform(850, 1100)):   # whistles moving between pitches
            w = _filter(rng.standard_normal(n).astype(np.float32), lambda f, c=c: np.exp(-((f - c) / 25) ** 2), 4096)
            wg = np.clip(_lfo(rng, n, 0.25, -0.6, 1.0), 0, None) * gust * 0.18
            pan = rng.uniform(-0.6, 0.6)
            L += w * wg * math.cos((pan + 1) * math.pi / 4)
            R += w * wg * math.sin((pan + 1) * math.pi / 4)
    elif kind == "crowd_murmur":
        L, R = _murmur(np, rng, n, 18, 3200)
        bL, bR = _stereo_bed(rng, n, lambda f: _band(f, 100, 2500, -3))
        L, R = L + 0.15 * bL, R + 0.15 * bR
    else:   # pragma: no cover - guarded by the params model
        raise OpError(f"Unknown ambience '{kind}'")
    return L.astype(np.float32), R.astype(np.float32)


def _ambience_blocks(kind: str, length_s: float, rng, block: float = 30.0, xfade: float = 2.0):
    """Long ambiences are rendered in blocks joined by equal-power crossfades (keeps memory and time in check)."""
    np = _np()
    if length_s <= block * 1.5:
        return _ambience(kind, length_s, rng)
    n = int(length_s * SR)
    L = np.zeros(n, np.float32)
    R = np.zeros(n, np.float32)
    x = int(xfade * SR)
    fin = np.sin(np.linspace(0, math.pi / 2, x)).astype(np.float32)
    fout = np.cos(np.linspace(0, math.pi / 2, x)).astype(np.float32)
    start = 0
    while start < n:
        seg_n = min(int(block * SR) + x, n - start)
        a, b = _ambience(kind, seg_n / SR, rng)
        a, b = a[:seg_n], b[:seg_n]
        for buf, seg in ((L, a), (R, b)):
            seg = seg.copy()
            if start > 0:
                k = min(x, seg_n)
                seg[:k] *= fin[:k]
            if start + seg_n < n:
                seg[-x:] *= fout
            buf[start:start + len(seg)] += seg
        if start + seg_n >= n:
            break
        start += seg_n - x
    return L, R


class AmbienceParams(BaseModel):
    kind: Literal["room", "office", "outdoors", "city", "rain", "wind", "crowd_murmur", "forest"] = Field("room", description="room: quiet room tone; forest: leaves + birds; rain: rain with drops; crowd_murmur: many people talking far off")
    length_s: float = Field(30, ge=1, le=600)
    loudness: float | None = Field(None, ge=-60, le=-10, description="Target LUFS; default suits the kind (room tone is very quiet)")
    seed: int | None = Field(None, ge=0, le=2**31 - 1)
    format: Literal["wav", "mp3"] = "wav"


@operation("make_ambience", AmbienceParams, "Make background sound / room tone (free, offline): room, office, outdoors, city, rain, wind, crowd_murmur or forest, any length, seeded. Lay it under dialogue so cuts don't sound dead.")
def make_ambience(p: AmbienceParams) -> OpResult:
    rng, seed = _rng(p.seed)
    L, R = _ambience_blocks(p.kind, p.length_s, rng)
    _fades(L, R, min(0.5, p.length_s / 4), min(0.5, p.length_s / 4))
    level = AMBIENCE_LEVEL[p.kind] if p.loudness is None else p.loudness
    data = {"kind": p.kind, "seed": seed, "duration_s": round(len(L) / SR, 3), "target_lufs": level}

    def build(out: Path) -> None:
        data.update(_encode(out, L, R, p.format, float(level), ["highpass=f=20"]))

    return _guarded("make_ambience", [], f"ambience_{p.kind}_{seed}.{p.format}", build, data=data)


# ---------------------------------------------------------------- footsteps

SURFACES: dict[str, dict[str, Any]] = {
    "wood": {"thump": (60, 900), "tau": 0.035, "modes": [(118, 0.06, 1.0), (245, 0.04, 0.6), (520, 0.022, 0.3)], "click": 0.25,
             "grains": 0, "band": (800, 5000), "spread": 0.05, "room": 0.18, "level": 1.0},
    "concrete": {"thump": (60, 2500), "tau": 0.014, "modes": [], "click": 0.8, "grains": 30, "band": (1500, 9000), "spread": 0.06, "room": 0.22, "level": 0.9},
    "tile": {"thump": (80, 3000), "tau": 0.012, "modes": [(1850, 0.02, 0.25), (2900, 0.014, 0.15)], "click": 1.0, "grains": 10,
             "band": (2000, 9000), "spread": 0.04, "room": 0.3, "level": 0.85},
    "carpet": {"thump": (40, 450), "tau": 0.045, "modes": [(85, 0.04, 0.4)], "click": 0.0, "grains": 25, "band": (200, 1500), "spread": 0.08, "room": 0.08, "level": 0.6},
    "grass": {"thump": (40, 400), "tau": 0.03, "modes": [], "click": 0.0, "grains": 260, "band": (900, 7000), "spread": 0.2, "room": 0.0, "level": 0.8},
    "gravel": {"thump": (50, 700), "tau": 0.03, "modes": [], "click": 0.1, "grains": 420, "band": (600, 9500), "spread": 0.22, "room": 0.03, "level": 1.0},
    "snow": {"thump": (40, 300), "tau": 0.04, "modes": [], "click": 0.0, "grains": 650, "band": (400, 4200), "spread": 0.3, "room": 0.0, "level": 0.8},
}


def _step(np, rng, s: dict[str, Any], strength: float, roll: float):
    """One footstep: heel impact (thump, click, resonances), texture grains, then the toe/roll."""
    n = int(0.6 * SR)
    t = np.arange(n) / SR
    out = np.zeros(n, np.float32)
    var = rng.uniform(0.9, 1.1)
    lo, hi = s["thump"]
    for when, g in ((0.0, 1.0), (roll * rng.uniform(0.85, 1.15), rng.uniform(0.35, 0.6))):
        k = int(0.25 * SR)
        tt = t[:k]
        thump = _filter(rng.standard_normal(k).astype(np.float32), lambda f: _band(f, lo * var, hi * var), 512) * np.exp(-tt / s["tau"])
        for fr, tau, a in s["modes"]:
            thump += a * np.sin(2 * math.pi * fr * var * tt + rng.uniform(0, 6.28)) * np.exp(-tt / tau)
        if s["click"]:
            thump += s["click"] * _filter(rng.standard_normal(k).astype(np.float32), lambda f: _band(f, 2500, 10000), 256) * np.exp(-tt / 0.0025)
        _put(out, when, thump.astype(np.float32), g)
    if s["grains"]:
        k = int((s["spread"] + roll + 0.1) * SR)
        imp = np.zeros(k, np.float32)
        m = int(s["grains"] * strength * rng.uniform(0.8, 1.2))
        # grains cluster at the heel and again at the roll
        pos = np.concatenate([np.abs(rng.normal(0, s["spread"] / 2.5, m // 2)), roll + np.abs(rng.normal(0, s["spread"] / 3, m - m // 2))])
        pos = (pos * SR).astype(int)
        pos = pos[pos < k]
        imp[pos] += (rng.exponential(1.0, len(pos)) * rng.choice([-1.0, 1.0], len(pos))).astype(np.float32)
        gl = 90
        grain = (rng.standard_normal(gl) * np.exp(-np.arange(gl) / 18)).astype(np.float32)
        tex = _filter(_convolve(imp, grain)[:k], lambda f: _band(f, s["band"][0] * var, s["band"][1] * var), 512)
        tex *= 1.4 / (np.max(np.abs(tex)) or 1)
        _put(out, 0.0, tex, 0.7)
    return out * s["level"] * strength * rng.uniform(0.8, 1.0)


class FootstepsParams(BaseModel):
    surface: Literal["wood", "concrete", "grass", "gravel", "carpet", "tile", "snow"] = "wood"
    pace: Literal["walk", "run"] = Field("walk", description="walk is about 1.8 steps a second, run about 2.8 (harder, quicker steps)")
    steps_per_s: float | None = Field(None, ge=0.3, le=6, description="Exact pace; overrides pace's speed")
    count: int | None = Field(None, ge=1, le=2000, description="Number of steps")
    length_s: float | None = Field(None, ge=0.5, le=600, description="Track length; steps fill it (default 10 s when neither count nor times is given)")
    times: list[float] | None = Field(None, max_length=5000, description="Exact step times in seconds (e.g. an animation's foot contacts); overrides pace and count")
    reverb: float | None = Field(None, ge=0, le=1, description="Room sound; default suits the surface (indoor floors get a little)")
    seed: int | None = Field(None, ge=0, le=2**31 - 1)
    format: Literal["wav", "mp3"] = "wav"


@operation("footsteps", FootstepsParams, "Make a footstep track (free, offline): wood, concrete, grass, gravel, carpet, tile or snow; walk or run, a step count or length, or exact step times from an animation's foot contacts. Every step varies slightly.")
def footsteps(p: FootstepsParams) -> OpResult:
    np = _np()
    rng, seed = _rng(p.seed)
    s = SURFACES[p.surface]
    run = p.pace == "run"
    sps = p.steps_per_s or (2.8 if run else 1.8)
    interval = 1.0 / sps
    if p.times:
        times = sorted(t for t in p.times if t >= 0)
        if p.length_s:
            times = [t for t in times if t < p.length_s]
        if not times:
            raise OpError("times has no step inside the track")
        length = p.length_s or times[-1] + 0.8
    else:
        if p.count:
            k = p.count
            length = p.length_s or 0.25 + (k - 1) * interval + 0.8
        else:
            length = p.length_s or 10.0
            k = max(1, int((length - 0.25 - 0.3) / interval) + 1)
        times = [0.25 + i * interval + rng.normal(0, 0.012 * interval / 0.55) for i in range(k)]
        times = [max(0.0, t) for t in times if t < length]
    n = int(length * SR)
    L = np.zeros(n, np.float32)
    R = np.zeros(n, np.float32)
    roll = 0.05 if run else 0.1
    for i, t in enumerate(times):
        sig = _step(np, rng, s, 1.25 if run else 1.0, roll)
        _put_st(L, R, t, sig, 1.0, pan=0.08 if i % 2 else -0.08)
    wet = s["room"] if p.reverb is None else p.reverb
    if wet > 0:
        L, R = _add_reverb(L, R, _reverb_ir(rng, 0.45, 5000, 0.006), wet)
    _fades(L, R, 0.0, min(0.05, length / 10))
    data = {"surface": p.surface, "steps": len(times), "times": [round(t, 3) for t in times[:2000]], "duration_s": round(length, 3), "seed": seed}
    return _guarded("footsteps", [], f"footsteps_{p.surface}_{seed}.{p.format}",
                    lambda out: _encode(out, L, R, p.format, None, ["highpass=f=30"], peak_db=-3.0), data=data)


# ---------------------------------------------------------------- audio_check


def _words(text: str) -> list[str]:
    return [w.strip("'") for w in re.findall(r"[a-z0-9']+", text.lower().replace("’", "'")) if w.strip("'")]


def word_match(expected: str, heard: str) -> dict[str, Any]:
    """Align the expected words with what was heard: accuracy, WER, missing and extra words."""
    exp, got = _words(expected), _words(heard)
    sm = difflib.SequenceMatcher(None, exp, got, autojunk=False)
    matched = subs = dels = ins = 0
    missing: list[str] = []
    extra: list[str] = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        a, b = i2 - i1, j2 - j1
        if tag == "equal":
            matched += a
        elif tag == "delete":
            dels += a
            missing += exp[i1:i2]
        elif tag == "insert":
            ins += b
            extra += got[j1:j2]
        else:
            subs += min(a, b)
            dels += max(0, a - b)
            ins += max(0, b - a)
            missing += exp[i1:i2]
            extra += got[j1:j2]
    total = len(exp) or 1
    return {"expected_words": len(exp), "heard_words": len(got), "matched_words": matched,
            "word_accuracy": round(matched / total, 3),
            "match_score": round(2 * matched / ((len(exp) + len(got)) or 1), 3),
            "wer": round((subs + dels + ins) / total, 3), "missing": missing[:200], "extra": extra[:200]}


def _scan(src: Path, channels: int, sr: int, silence_db: float, min_silence: float) -> dict[str, Any]:
    """Stream the decoded audio once: peak, RMS, DC offset, clipping and silent stretches."""
    np = _np()
    win = max(1, int(sr * 0.05))
    chunk_frames = win * 200
    cmd = [get_settings().ffmpeg, "-v", "error", "-i", str(src), "-map", "0:a:0", "-f", "f32le", "-acodec", "pcm_f32le",
           "-ac", str(channels), "-ar", str(sr), "-"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, **NO_WINDOW)
    total = 0
    sums = np.zeros(channels)
    sq = 0.0
    peak = 0.0
    clipped = near = 0
    runs = 0
    run_len = 0
    win_db: list[Any] = []
    assert proc.stdout is not None
    try:
        while True:
            buf = proc.stdout.read(chunk_frames * channels * 4)
            if not buf:
                break
            x = np.frombuffer(buf[: len(buf) - len(buf) % (channels * 4)], np.float32).reshape(-1, channels)
            if not len(x):
                break
            total += len(x)
            sums += x.sum(axis=0, dtype=np.float64)
            sq += float(np.sum(x.astype(np.float64) ** 2))
            ax = np.abs(x)
            peak = max(peak, float(ax.max()))
            clipped += int(np.sum(ax >= 0.999))
            near += int(np.sum(ax >= 0.989))
            hot = (ax >= 0.999).any(axis=1)
            # runs of 3+ consecutive full-scale frames are real clipping (carried across chunks)
            if hot.any():
                edges = np.flatnonzero(np.diff(np.concatenate([[0], hot.astype(np.int8), [0]])))
                starts, lens = edges[::2], edges[1::2] - edges[::2]
                if starts[0] == 0 and run_len:
                    runs += int(run_len < 3 <= run_len + lens[0])
                    lens = lens.copy()
                    lens[0] += run_len
                    runs += int(np.sum(lens[1:] >= 3))
                else:
                    runs += int(np.sum(lens >= 3))
                run_len = int(lens[-1]) if hot[-1] else 0
            else:
                run_len = 0
            m = len(x) // win * win
            if m:
                ms = np.mean(x[:m].astype(np.float64) ** 2, axis=1).reshape(-1, win).mean(axis=1)
                win_db.append(10 * np.log10(ms + 1e-20))
            if len(x) > m:
                ms = float(np.mean(x[m:].astype(np.float64) ** 2))
                win_db.append(np.array([10 * math.log10(ms + 1e-20)]))
    finally:
        err = proc.stderr.read().decode(errors="replace") if proc.stderr else ""
        proc.wait()
    if proc.returncode != 0 or total == 0:
        raise OpError("Couldn't read the audio:\n" + "\n".join(err.strip().splitlines()[-6:]))
    dbs = np.concatenate(win_db) if win_db else np.zeros(0)
    silences = []
    quiet = dbs < silence_db
    i = 0
    step_s = win / sr
    while i < len(quiet):
        if quiet[i]:
            j = i
            while j < len(quiet) and quiet[j]:
                j += 1
            if (j - i) * step_s >= min_silence:
                silences.append({"start": round(i * step_s, 2), "end": round(min(j * step_s, total / sr), 2), "duration": round(min(j * step_s, total / sr) - i * step_s, 2)})
            i = j
        else:
            i += 1
    dc = (sums / total).tolist()
    rms = math.sqrt(sq / (total * channels))
    return {"duration_s": round(total / sr, 3), "sample_rate": sr, "channels": channels,
            "peak_dbfs": round(20 * math.log10(peak), 2) if peak > 0 else None,
            "rms_dbfs": round(20 * math.log10(rms), 2) if rms > 0 else None,
            "dc_offset": [round(d, 5) for d in dc], "clipped_samples": clipped, "near_full_scale_samples": near,
            "clip_events": runs, "silences": silences[:200], "silence_total_s": round(sum(s["duration"] for s in silences), 2)}


class AudioCheckParams(BaseModel):
    file_id: str = Field(description="Audio or video to check")
    expected_text: str | None = Field(None, max_length=20000, description="The script; when given, the speech is transcribed (Whisper) and compared word by word")
    model: Literal["tiny", "base", "small", "medium", "large-v3"] = Field("base", description="Whisper model for the word check")
    language: str | None = Field("en", max_length=8)
    target_lufs: float = Field(-16, ge=-30, le=-9, description="Loudness you're aiming for (-16 web/podcast, -14 social, -23 TV)")
    silence_db: float = Field(-50, ge=-90, le=-20, description="Quieter than this counts as silence (dBFS)")
    min_silence_s: float = Field(2.0, ge=0.2, le=60, description="Report silences at least this long")


@operation("audio_check", AudioCheckParams, "Check a file's sound before delivery: loudness (LUFS) against a target, true peak, clipping, long silences, DC offset; with expected_text, transcribes the speech (Whisper) and scores it against the script with missing/extra words. Writes a .json report.")
def audio_check(p: AudioCheckParams) -> OpResult:
    src = _src(p.file_id)
    info = media_info(p.file_id)
    if not info["has_audio"]:
        raise OpError("audio_check needs a file with sound")
    a = info.get("audio") or {}
    sr = int(a.get("sample_rate") or 48000)
    ch = int(a.get("channels") or 2)
    report: dict[str, Any] = {"file": storage.get(p.file_id).name, **_loudness(["-i", str(src), "-map", "0:a:0"])}
    report.update(_scan(src, ch, sr, p.silence_db, p.min_silence_s))
    issues: list[str] = []
    if report["lufs"] is None:
        issues.append("No measurable sound (silent file)")
    else:
        report["lufs_off_target"] = round(report["lufs"] - p.target_lufs, 2)
        if abs(report["lufs_off_target"]) > 2:
            issues.append(f"Loudness {report['lufs']} LUFS is {abs(report['lufs_off_target']):.1f} dB {'above' if report['lufs_off_target'] > 0 else 'below'} the {p.target_lufs:g} LUFS target (voice_polish or normalize_audio can fix it)")
    if report["true_peak_db"] is not None and report["true_peak_db"] > -1.0:
        issues.append(f"True peak {report['true_peak_db']} dBTP is above -1 dBTP (may distort after encoding)")
    if report["clip_events"] or report["clipped_samples"] > max(10, report["duration_s"] * report["sample_rate"] * 1e-5):
        issues.append(f"Clipping: {report['clipped_samples']} samples at full scale in {report['clip_events']} places")
    if max(abs(d) for d in report["dc_offset"]) > 0.01:
        issues.append("DC offset above 1% (a high-pass filter removes it)")
    long_sil = [s for s in report["silences"] if s["start"] > 0.05 and s["end"] < report["duration_s"] - 0.05]
    if long_sil:
        issues.append(f"{len(long_sil)} silence(s) of {p.min_silence_s:g} s or more inside the file")
    if p.expected_text:
        from . import voice

        try:
            segments, _words_t, _lang = voice.words_of(p.file_id, p.model, p.language)
        except OpError as e:
            if isinstance(e.__cause__, ImportError):
                raise OpError('Checking the words needs Whisper: pip install "builder[voice]" (or leave out expected_text to check only the sound)') from e
            raise
        heard = " ".join(s["text"] for s in segments)
        report["speech"] = {"heard_text": heard, **word_match(p.expected_text, heard)}
        sp = report["speech"]
        if sp["word_accuracy"] < 0.95:
            issues.append(f"Speech matches {sp['word_accuracy'] * 100:.0f}% of the script ({len(sp['missing'])} missing, {len(sp['extra'])} extra words)")
    report["target_lufs"] = p.target_lufs
    report["issues"] = issues
    report["ok"] = not issues
    return _guarded("audio_check", [p.file_id], f"{_stem(p.file_id)}_check.json",
                    lambda out: out.write_text(json.dumps(report, indent=1), encoding="utf-8"), data=report)
