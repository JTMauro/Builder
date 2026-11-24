"""Original sound pack for the game "Unhappy Woks" (grumpy furry creatures launched at robot forts).

    python3 tools/gamesfx/unhappy_woks.py OUT_DIR [--only name,name]

Everything is synthesized here (numpy, Builder's DSP helpers): creature voices from a formant
synthesizer (pitch glides through vowel shapes, scaled by creature size), impacts from modal
synthesis and shaped noise, and two original tunes. Writes mono MP3s plus manifest.json.
No recordings, samples or existing music are used.
"""
from __future__ import annotations

import argparse
import json
import math
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import numpy as np  # noqa: E402

from media_backend import sound_design as sd  # noqa: E402
from media_backend.audio_plus import VOWELS, _Fx, _metal_click, _wood_thud  # noqa: E402

SR = sd.SR
rng = np.random.default_rng(20261124)
fx = _Fx(rng, 1.0)


def n_(s):
    return max(1, int(round(s * SR)))


def t_(n):
    return np.arange(n) / SR


def env_adsr(n, a=0.01, d=0.05, s=0.8, r=0.08):
    t = t_(n)
    dur = n / SR
    e = np.where(t < a, t / max(a, 1e-4), np.where(t < a + d, 1 - (1 - s) * (t - a) / max(d, 1e-4), s))
    rel = np.clip((dur - t) / max(r, 1e-4), 0, 1)
    return (e * rel).astype(np.float32)


def curve(points, n, log=False):
    """points: [(frac 0..1, value)] -> n samples (log-interpolated for pitch)."""
    xs = np.array([p[0] for p in points]) * (n - 1)
    ys = np.array([p[1] for p in points], dtype=float)
    if log:
        return np.exp(np.interp(np.arange(n), xs, np.log(ys))).astype(np.float32)
    return np.interp(np.arange(n), xs, ys).astype(np.float32)


# ---------------------------------------------------------------- creature voice synthesizer

SIZES = {   # f0 range (Hz), formant scale, growl (roughness), breath
    "pip": (650, 1250, 2.05, 0.0, 0.10),
    "soot": (430, 820, 1.65, 0.0, 0.12),
    "bramble": (300, 560, 1.38, 0.05, 0.12),
    "ash": (105, 220, 1.0, 0.45, 0.16),
    "thornback": (68, 140, 0.85, 0.8, 0.2),
}


def _formant_gain(f, vowel_a, vowel_b, mix, scale):
    """Gain at frequencies f for a blend of two vowels (formant freqs interpolated), scaled by creature size."""
    A, B = VOWELS[vowel_a], VOWELS[vowel_b]
    k = min(len(A), len(B))
    g = 0.02 + 0 * f
    for i in range(k):
        fq = ((1 - mix) * A[i][0] + mix * B[i][0]) * scale
        bw = ((1 - mix) * A[i][1] + mix * B[i][1]) * max(1.0, scale * 0.9)
        a = (1 - mix) * A[i][2] + mix * B[i][2]
        g = g + a / (1 + ((f - fq) / (bw / 2)) ** 2)
    return g / np.sqrt(1 + (f / (1800 * scale)) ** 2)


def voice(dur, pitch, vowels, amp=None, who="bramble", breath=None, growl=None, vib=0.02, vib_rate=6.5, sub=0.0):
    """pitch: [(frac, multiplier of the creature's low..high range 0..1.4)]; vowels: [(frac, 'u'|'i'|'a'|'o'|'e'|'h')]."""
    lo, hi, scale, g0, b0 = SIZES[who]
    breath = b0 if breath is None else breath
    growl = g0 if growl is None else growl
    n = n_(dur)
    pos = curve(pitch, n)
    f0 = lo * (hi / lo) ** pos
    f0 = f0 * (1 + vib * np.sin(2 * math.pi * vib_rate * t_(n) + rng.uniform(0, 6)) * np.clip(t_(n) / 0.15, 0, 1))
    f0 = f0 * (1 + 0.006 * rng.standard_normal(n).cumsum() / np.sqrt(np.arange(1, n + 1)))   # slight wander
    src = fx.saw(f0.astype(np.float32))
    if sub:
        src = src + sub * fx.saw((f0 / 2).astype(np.float32))
    if growl:   # rough, gravelly voice: fast irregular amplitude modulation + a little distortion
        am = 1 + growl * 0.6 * fx.sd._lfo(rng, n, 33, -1, 1) + growl * 0.3 * rng.standard_normal(n).astype(np.float32) * 0.3
        src = np.tanh(src * am * (1 + growl)) / (1 + 0.3 * growl)
    shimmer = 1 + 0.12 * fx.sd._lfo(rng, n, 17, -1, 1) + 0.05 * fx.sd._lfo(rng, n, 41, -1, 1)   # living, uneven voice
    src = src.astype(np.float32) * shimmer + (breath + 0.08) * fx.noise(n, 300, 9000)
    a = curve(amp or [(0, 0), (0.08, 1), (0.85, 0.9), (1, 0)], n)
    src = src * a
    # time-varying formant filter (STFT, frames interpolate between vowel keypoints)
    nfft, hop = 1024, 256
    pad = nfft
    xp = np.concatenate([np.zeros(pad, np.float32), src, np.zeros(pad + nfft, np.float32)])
    win = np.hanning(nfft + 1)[:-1]
    nfr = 1 + (len(xp) - nfft) // hop
    idx = np.arange(nfft)[None, :] + hop * np.arange(nfr)[:, None]
    X = np.fft.rfft(xp[idx] * win, axis=1)
    f = np.fft.rfftfreq(nfft, 1 / SR)
    vx = [v[0] for v in vowels]
    for i in range(nfr):
        tf = min(1.0, max(0.0, (i * hop + nfft // 2 - pad) / max(1, n - 1)))
        j = max(0, np.searchsorted(vx, tf, side="right") - 1)
        j2 = min(len(vowels) - 1, j + 1)
        span = (vx[j2] - vx[j]) or 1
        mix = 0.0 if j2 == j else min(1.0, max(0.0, (tf - vx[j]) / span))
        mix = mix * mix * (3 - 2 * mix)
        X[i] *= _formant_gain(f, vowels[j][1], vowels[j2][1], mix, scale)
    frames = np.fft.irfft(X, nfft, axis=1) * win
    y = np.zeros(len(xp))
    for i in range(nfr):
        y[i * hop: i * hop + nfft] += frames[i]
    y = y[pad: pad + n].astype(np.float32)
    y = fx.band(y, 70 * scale, 5200 + 900 * scale, order=2)   # soften the tinny top end, keep some body
    return (y / (np.max(np.abs(y)) + 1e-9)).astype(np.float32)


def seq(parts, gap=0.0):
    """Join (signal, gain) parts with small gaps."""
    out = []
    for p in parts:
        sig, g = p if isinstance(p, tuple) else (p, 1.0)
        out += [sig * g, np.zeros(n_(gap), np.float32)]
    return np.concatenate(out)


def giggle(who, count=4, start=0.9, step=-0.08, syl=0.09):
    return seq([voice(syl, [(0, start + step * i), (1, start + step * i - 0.12)], [(0, "h"), (0.3, "i"), (1, "e")], who=who,
                      amp=[(0, 0), (0.15, 1), (0.6, 0.7), (1, 0)], vib=0.0) for i in range(count)], gap=0.035)


def oof(who):
    return seq([voice(0.06, [(0, 0.5), (1, 0.45)], [(0, "h"), (1, "h")], who=who, breath=0.9, amp=[(0, 0), (0.3, 0.6), (1, 0.2)]),
                voice(0.32, [(0, 0.62), (0.3, 0.5), (1, 0.12)], [(0, "u"), (0.4, "o"), (1, "u")], who=who,
                      amp=[(0, 0), (0.06, 1), (0.5, 0.6), (1, 0)], vib=0.0)])


def whee(who, dur=0.7, up=1.25, vowels=None, wobble=0.0):
    p = [(0, 0.35), (0.22, up), (0.75, up - 0.1), (1, up - 0.55)]
    v = vowels or [(0, "u"), (0.14, "i"), (0.85, "i"), (1, "e")]
    return voice(dur, p, v, who=who, vib=0.03 + wobble, vib_rate=7.5 if wobble else 6.5,
                 amp=[(0, 0), (0.1, 1), (0.8, 0.85), (1, 0)])


def creature_set(who):
    big = who in ("ash", "thornback")
    out = {}
    if who == "bramble":
        out["launch_1"] = whee(who, 0.75)
        out["launch_2"] = seq([voice(0.18, [(0, 0.6), (1, 0.85)], [(0, "i"), (1, "a")], who=who), voice(0.08, [(0, 0.7), (1, 0.7)], [(0, "h"), (1, "h")], who=who, breath=0.9, amp=[(0, 0), (0.3, 0.5), (1, 0.2)]),
                               voice(0.42, [(0, 1.0), (0.3, 1.15), (1, 0.6)], [(0, "u"), (1, "u")], who=who)], gap=0.01)
        out["launch_3"] = seq([voice(0.28, [(0, 0.5), (1, 0.75)], [(0, "u"), (0.5, "o"), (1, "a")], who=who), voice(0.5, [(0, 0.9), (0.3, 1.2), (1, 0.8)], [(0, "e"), (0.3, "i"), (1, "i")], who=who)], gap=0.03)
        out["power"] = seq([voice(0.13, [(0, 0.5), (1, 0.45)], [(0, "h"), (0.35, "u"), (1, "u")], who=who, amp=[(0, 0), (0.2, 0.8), (0.7, 0.8), (1, 0)]),
                            voice(0.35, [(0, 0.95), (0.3, 1.1), (1, 0.8)], [(0, "h"), (0.25, "a"), (1, "a")], who=who, amp=[(0, 0), (0.12, 1), (0.6, 0.9), (1, 0)])], gap=0.06)
    elif who == "soot":
        out["launch_1"] = seq([voice(0.14, [(0, 0.8), (1, 1.0)], [(0, "u"), (1, "i")], who=who), voice(0.3, [(0, 1.1), (0.4, 1.25), (1, 0.85)], [(0, "h"), (0.25, "i"), (1, "e")], who=who)], gap=0.02)
        out["launch_2"] = seq([voice(0.09, [(0, 1.0), (1, 1.05)], [(0, "i"), (1, "i")], who=who, vib=0), voice(0.09, [(0, 1.05), (1, 1.1)], [(0, "i"), (1, "i")], who=who, vib=0),
                               whee(who, 0.55, 1.3)], gap=0.04)
        out["launch_3"] = voice(1.0, [(0, 0.95), (0.15, 1.3), (0.8, 1.25), (1, 0.9)], [(0, "e"), (0.1, "i"), (1, "i")], who=who, vib=0.045, vib_rate=8)
        out["power"] = giggle(who, 3, 1.05, -0.06, 0.1)
    elif who == "pip":
        out["launch_1"] = whee(who, 1.05, 1.3, wobble=0.06)
        out["launch_2"] = seq([voice(0.12, [(0, 0.8), (1, 0.85)], [(0, "u"), (1, "i")], who=who, vib=0), voice(0.12, [(0, 0.95), (1, 1.0)], [(0, "u"), (1, "i")], who=who, vib=0),
                               voice(0.35, [(0, 1.15), (0.3, 1.38), (1, 1.1)], [(0, "u"), (0.2, "i"), (1, "i")], who=who)], gap=0.04)
        out["launch_3"] = seq([whee(who, 0.6, 1.25), giggle(who, 4, 1.15, -0.07, 0.075)], gap=0.05)
        out["power"] = seq([voice(0.14, [(0, 0.6), (1, 0.6)], [(0, "h"), (1, "u")], who=who, breath=0.7), voice(0.22, [(0, 1.0), (0.4, 1.25), (1, 0.9)], [(0, "i"), (0.4, "a"), (1, "a")], who=who)], gap=0.02)
    elif who == "ash":
        grr = voice(0.55, [(0, 0.2), (0.5, 0.25), (1, 0.15)], [(0, "u"), (1, "u")], who=who, growl=0.9, breath=0.25, amp=[(0, 0), (0.15, 0.6), (0.85, 0.6), (1, 0)])
        out["launch_1"] = seq([(grr, 0.55), voice(0.75, [(0, 0.55), (0.3, 1.05), (1, 0.7)], [(0, "u"), (0.25, "o"), (0.7, "a"), (1, "a")], who=who)], gap=0.12)
        out["launch_2"] = seq([(voice(0.32, [(0, 0.25), (1, 0.3)], [(0, "e"), (1, "u")], who=who, growl=1.0, breath=0.3), 0.6),
                               voice(0.7, [(0, 0.6), (0.3, 1.1), (1, 0.8)], [(0, "u"), (0.3, "o"), (1, "o")], who=who)], gap=0.05)
        out["launch_3"] = voice(0.95, [(0, 0.35), (0.3, 0.6), (1, 0.25)], [(0, "u"), (0.35, "o"), (0.8, "a"), (1, "a")], who=who)
        out["power"] = seq([voice(0.12, [(0, 0.4), (1, 0.4)], [(0, "h"), (1, "h")], who=who, breath=0.9, amp=[(0, 0), (0.3, 0.4), (1, 0.1)]),
                            voice(0.6, [(0, 0.55), (0.5, 0.85), (1, 0.6)], [(0, "u"), (0.15, "e"), (1, "e")], who=who, growl=0.9, amp=[(0, 0), (0.1, 0.8), (0.75, 1), (1, 0)], vib=0.06, vib_rate=11)], gap=0.03)
    else:   # thornback
        out["launch_1"] = voice(1.15, [(0, 0.35), (0.25, 1.0), (0.7, 0.95), (1, 0.55)], [(0, "u"), (0.2, "o"), (0.5, "a"), (1, "a")], who=who, sub=0.35)
        out["launch_2"] = seq([voice(0.25, [(0, 0.3), (1, 0.5)], [(0, "e"), (1, "a")], who=who, growl=1.3, breath=0.3),
                               voice(0.85, [(0, 0.7), (0.3, 1.0), (1, 0.5)], [(0, "a"), (1, "a")], who=who, growl=1.2, sub=0.4)])
        out["launch_3"] = seq([voice(0.3, [(0, 0.5), (1, 0.35)], [(0, "o"), (1, "o")], who=who), voice(0.9, [(0, 0.45), (0.3, 1.0), (1, 0.6)], [(0, "u"), (0.25, "o"), (0.7, "a"), (1, "a")], who=who, sub=0.3)], gap=0.08)
        out["power"] = voice(1.1, [(0, 0.3), (0.2, 0.85), (0.7, 0.8), (1, 0.3)], [(0, "a"), (0.4, "o"), (1, "a")], who=who, growl=1.6, sub=0.6, breath=0.35, vib=0.07, vib_rate=9)
    out["oof"] = oof(who)
    if big:
        out["oof"] = seq([oof(who), (voice(0.25, [(0, 0.2), (1, 0.1)], [(0, "u"), (1, "u")], who=who, growl=1.0), 0.4)], gap=0.04)
    return out


def grumble(i):
    who = ["ash", "thornback", "ash", "bramble"][i % 4]
    sylls = []
    r = np.random.default_rng(100 + i)
    for k in range(int(r.integers(4, 7))):
        d = float(r.uniform(0.12, 0.24))
        p0 = float(r.uniform(0.05, 0.3))
        v1, v2 = r.choice(["u", "o", "a", "e", "u"], 2)
        sylls.append((voice(d, [(0, p0), (0.5, p0 + r.uniform(-0.1, 0.15)), (1, p0 - 0.12)], [(0, "u"), (0.35, str(v1)), (1, str(v2))], who=who,
                            growl=0.7 if who in ("ash", "thornback") else 0.4, breath=0.32,
                            amp=[(0, 0), (0.2, float(r.uniform(0.5, 1))), (0.8, 0.6), (1, 0)], vib=0.0), float(r.uniform(0.55, 1.0))))
    return fx.band(seq(sylls, gap=0.03)[: n_(1.5)], 60, 2600, order=2)   # lips mostly closed: a mumble


# ---------------------------------------------------------------- impacts

def burst(dur, lo, hi, tau, attack=0.0008):
    k = n_(dur)
    return fx.noise(k, lo, hi) * fx.decay(k, tau, attack)


def grains(dur, count, lo, hi, gdur=(0.004, 0.02), spread=0.6, decay=0.35):
    out = np.zeros(n_(dur), np.float32)
    for _ in range(count):
        at = float(rng.exponential(spread * dur / 3))
        if at >= dur - 0.03:
            continue
        g = burst(rng.uniform(*gdur), lo * rng.uniform(0.8, 1.2), hi, rng.uniform(0.002, 0.01))
        fx.put(out, at, g, float(rng.uniform(0.2, 1.0)) * math.exp(-at / max(decay, 1e-3)))
    return out


def tinkles(dur, count, lo, hi):
    out = np.zeros(n_(dur), np.float32)
    for _ in range(count):
        at = float(rng.exponential(dur / 4))
        if at >= dur - 0.05:
            continue
        f0 = rng.uniform(lo, hi)
        m = fx.modes(n_(0.12), [(f0, 0.03, 1.0), (f0 * 2.76, 0.02, 0.5), (f0 * 5.4, 0.012, 0.25)])
        fx.put(out, at, m, float(rng.uniform(0.2, 0.9)) * math.exp(-at / (dur / 2)))
    return out


def low_boom(dur, f0=55, tau=0.5):
    k = n_(dur)
    t = t_(k)
    f = f0 * (1 + 1.2 * np.exp(-t / 0.05))
    s = np.sin(2 * math.pi * np.cumsum(f) / SR)
    return (s * fx.decay(k, tau, 0.002) + 0.6 * fx.noise(k, 20, 220) * fx.decay(k, tau * 1.6, 0.004)).astype(np.float32)


def explosion(dur=1.0, size=1.0):
    k = n_(dur)
    body = fx.noise(k, 30, 2400 / size) * fx.decay(k, 0.25 * size, 0.003)
    crack = burst(0.08, 900, 7000, 0.015)
    out = np.zeros(k, np.float32)
    fx.put(out, 0, body, 1.0)
    fx.put(out, 0, crack, 0.6)
    fx.put(out, 0, low_boom(dur, 48 / size ** 0.3, 0.35 * size), 1.1)
    fx.put(out, 0.05, grains(dur - 0.05, int(30 * size), 700, 5000, spread=0.9, decay=0.4 * size), 0.35)
    return fx.soft(out, 1.4)


def square(f, k):
    ph = np.cumsum(np.full(k, f) if np.isscalar(f) else f) / SR
    return np.sign(np.sin(2 * math.pi * ph)).astype(np.float32)


def bitcrush(x, steps=12, hold=6):
    y = np.round(x * steps) / steps
    return np.repeat(y[::hold], hold)[: len(x)].astype(np.float32)


def chirp(k, f_from, f_to, shape="sin"):
    f = curve([(0, f_from), (1, f_to)], k, log=True)
    ph = 2 * math.pi * np.cumsum(f) / SR
    return (np.sin(ph) if shape == "sin" else np.sign(np.sin(ph)) * 0.6 + 0.4 * np.sin(ph)).astype(np.float32)


def impacts():
    out = {}
    out["wood_hit"] = _wood_thud(fx, 1.0, 1.0)
    w = np.zeros(n_(0.9), np.float32)
    fx.put(w, 0, _wood_thud(fx, 1.2, 1.3), 0.9)
    fx.put(w, 0.005, burst(0.12, 700, 5000, 0.02), 0.9)                       # the crack
    fx.put(w, 0.02, grains(0.85, 45, 1500, 7000, spread=0.5, decay=0.25), 0.5)  # splinters
    fx.put(w, 0.12, _wood_thud(fx, 0.6, 0.8), 0.35)
    fx.put(w, 0.27, _wood_thud(fx, 0.5, 0.7), 0.25)
    out["wood_break"] = w
    k = n_(0.45)
    out["stone_hit"] = (fx.modes(k, [(130, 0.05, 1.0), (205, 0.04, 0.6), (330, 0.03, 0.4)]) + burst(0.45, 200, 2600, 0.03) * 0.9).astype(np.float32)
    s = np.zeros(n_(1.5), np.float32)
    fx.put(s, 0, out["stone_hit"], 1.0)
    fx.put(s, 0.01, burst(0.1, 500, 4000, 0.02), 0.7)
    fx.put(s, 0.03, fx.noise(n_(1.4), 25, 320) * fx.decay(n_(1.4), 0.45, 0.06), 1.1)    # rumble
    fx.put(s, 0.05, grains(1.4, 70, 600, 4200, gdur=(0.006, 0.03), spread=0.8, decay=0.6), 0.55)
    out["stone_break"] = s
    k = n_(0.6)
    out["ice_hit"] = (fx.modes(k, [(2150, 0.22, 1.0), (3320, 0.15, 0.6), (4790, 0.1, 0.4), (6180, 0.07, 0.25)], detune=6) + burst(0.6, 3000, 12000, 0.004) * 0.6).astype(np.float32)
    g = np.zeros(n_(1.3), np.float32)
    fx.put(g, 0, burst(0.15, 1500, 12000, 0.03), 1.0)
    fx.put(g, 0, out["ice_hit"], 0.5)
    fx.put(g, 0.01, tinkles(1.25, 90, 2500, 7800), 0.9)
    out["ice_break"] = g
    k = n_(1.3)
    clang = fx.modes(k, [(310, 0.35, 1.0), (742, 0.28, 0.8), (1213, 0.2, 0.6), (1834, 0.14, 0.45), (2650, 0.09, 0.3), (3990, 0.05, 0.2)], detune=9)
    clang = clang + burst(1.3, 1500, 9000, 0.012) * 0.5
    out["metal_hit"] = (clang * 0.8 + np.pad(_metal_click(fx, 1.0), (0, k))[:k] * 0.8).astype(np.float32)
    m = np.zeros(n_(1.9), np.float32)
    fx.put(m, 0, out["metal_hit"], 1.0)
    fx.put(m, 0, low_boom(0.6, 70, 0.2), 0.6)
    ks = n_(1.0)
    scrape = fx.tv_band(fx.noise(ks, 200, 9000), curve([(0, 3200), (0.5, 1800), (1, 900)], ks, log=True), 0.25)
    scrape = scrape * (0.6 + 0.4 * fx.sd._lfo(rng, ks, 23, 0, 1)) * curve([(0, 0), (0.1, 1), (0.8, 0.7), (1, 0)], ks)
    fx.put(m, 0.18, scrape, 0.9)
    fx.put(m, 1.05, fx.modes(n_(0.8), [(415, 0.5, 1.0), (980, 0.35, 0.6), (1620, 0.2, 0.35)]), 0.5)
    out["metal_break"] = m
    # robots
    k = n_(0.28)
    buzz = square(180 * (1 + 0.08 * np.sin(2 * math.pi * 30 * t_(k))), k) * fx.decay(k, 0.09, 0.002)
    out["bot_hit"] = (bitcrush(fx.band(buzz, 120, 4000), 10, 4) * 0.8 + _metal_click_pad(k) * 0.6).astype(np.float32)
    b = np.zeros(n_(1.1), np.float32)
    kz = n_(0.3)
    zap = fx.band(chirp(kz, 2400, 180, "sq"), 150, 6000) * fx.decay(kz, 0.12, 0.002)
    fx.put(b, 0, zap, 0.9)
    fx.put(b, 0.22, explosion(0.85, 0.55), 1.0)
    out["bot_break"] = b
    kb = n_(0.09)
    out["sentry_hit"] = seq([square(880, kb) * env_adsr(kb, 0.004, 0.02, 0.7, 0.02) * 0.6, square(587, kb) * env_adsr(kb, 0.004, 0.02, 0.7, 0.03) * 0.6], gap=0.025)
    a = np.zeros(n_(1.8), np.float32)
    ka = n_(0.12)
    for i in range(4):
        fx.put(a, i * 0.13, square(1180 if i % 2 == 0 else 880, ka) * env_adsr(ka, 0.003, 0.01, 0.8, 0.01), 0.5)
    fx.put(a, 0.58, explosion(1.2, 0.85), 1.0)
    out["sentry_break"] = a
    k = n_(1.1)
    out["boss_hit"] = (fx.modes(k, [(92, 0.4, 1.0), (178, 0.32, 0.8), (321, 0.25, 0.6), (566, 0.18, 0.5), (1046, 0.1, 0.3)], detune=2) + low_boom(1.1, 60, 0.18) * 0.7
                       + np.pad(_metal_click(fx, 0.7), (0, k))[:k] * 0.5).astype(np.float32)
    bb = np.zeros(n_(2.9), np.float32)
    for at, size, gn in [(0.0, 1.0, 1.0), (0.45, 0.8, 0.8), (0.85, 0.9, 0.85), (1.3, 1.3, 1.0)]:
        fx.put(bb, at, explosion(1.5, size), gn)
    fx.put(bb, 0.2, tinkles(2.4, 40, 900, 3500), 0.25)
    fx.put(bb, 1.3, low_boom(1.5, 38, 0.7), 0.9)
    out["boss_break"] = bb
    k = n_(2.3)
    f = curve([(0, 42), (0.4, 55), (0.75, 48), (1, 36)], k)
    growl = fx.saw(f * (1 + 0.05 * fx.sd._lfo(rng, k, 7, -1, 1))) + 0.5 * fx.saw(f * 1.5)
    growl = np.tanh(2.2 * growl * (1 + 0.5 * fx.sd._lfo(rng, k, 18, 0, 1)))
    growl = fx.band(growl.astype(np.float32), 30, 1600, order=2) * curve([(0, 0), (0.12, 1), (0.8, 0.9), (1, 0)], k)
    servo = chirp(k, 300, 900) * 0.15 * curve([(0, 0), (0.2, 0.4), (0.6, 1), (1, 0)], k)
    clunk = np.zeros(k, np.float32)
    for at in (0.05, 1.6):
        fx.put(clunk, at, fx.modes(n_(0.4), [(110, 0.12, 1), (260, 0.08, 0.6), (900, 0.04, 0.3)]), 0.6)
    out["boss_intro"] = (growl + servo + clunk + 0.25 * fx.noise(k, 40, 300) * curve([(0, 0), (0.2, 1), (1, 0)], k)).astype(np.float32)
    # launch: crossbow string twang (Karplus-Strong) + an energy "pew"
    kt = n_(0.5)
    period = int(SR / 165)
    buf = rng.uniform(-1, 1, period).astype(np.float32)
    tw = np.zeros(kt, np.float32)
    for i in range(kt):
        v = buf[i % period]
        tw[i] = v
        buf[i % period] = 0.5 * (v + buf[(i + 1) % period]) * 0.994
    tw = fx.band(tw, 90, 5000) * fx.decay(kt, 0.18, 0.001)
    kp = n_(0.22)
    pew = (chirp(kp, 1900, 260, "sq") * 0.6 + chirp(kp, 3800, 520) * 0.25) * fx.decay(kp, 0.09, 0.002)
    l = np.zeros(n_(0.55), np.float32)
    fx.put(l, 0, burst(0.05, 2000, 9000, 0.006), 0.5)
    fx.put(l, 0, tw, 1.0)
    fx.put(l, 0.012, fx.band(pew.astype(np.float32), 150, 8000), 0.8)
    out["launch"] = l
    return out


def _metal_click_pad(k):
    c = _metal_click(fx, 1.2)
    return np.pad(c, (0, max(0, k - len(c))))[:k]


# ---------------------------------------------------------------- music

def midi(m):
    return 440.0 * 2 ** ((m - 69) / 12)


def flute(freq, dur, amp=1.0):
    k = n_(dur)
    t = t_(k)
    vib = 1 + 0.008 * np.sin(2 * math.pi * 5.5 * t) * np.clip((t - 0.08) / 0.1, 0, 1)
    ph = 2 * math.pi * np.cumsum(freq * vib) / SR
    tone = np.sin(ph) + 0.18 * np.sin(2 * ph) + 0.06 * np.sin(3 * ph)
    breath = fx.tv_band(rng.standard_normal(k).astype(np.float32), np.full(k, freq * 2, np.float32), 1.0) * 0.5
    e = env_adsr(k, 0.025, 0.06, 0.8, min(0.08, dur * 0.4))
    chiff = burst(0.03, freq * 2, freq * 8, 0.01)
    out = ((tone + breath) * e).astype(np.float32)
    out[: len(chiff)] += chiff[: len(out)] * 0.25
    return out * amp


def log_drum(freq, amp=1.0):
    k = n_(0.35)
    t = t_(k)
    f = freq * (1 + 0.25 * np.exp(-t / 0.02))
    s = np.sin(2 * math.pi * np.cumsum(f) / SR) + 0.35 * np.sin(2 * math.pi * np.cumsum(f * 2.7) / SR) * np.exp(-t / 0.03)
    return (s * fx.decay(k, 0.11, 0.001) + burst(0.35, 600, 3000, 0.006) * 0.3).astype(np.float32) * amp


def shaker(amp=1.0):
    k = n_(0.07)
    return fx.noise(k, 4500, 12000) * curve([(0, 0), (0.25, 1), (1, 0)], k) * amp


def clap(amp=1.0):
    out = np.zeros(n_(0.2), np.float32)
    for i, a in enumerate((0.6, 0.8, 1.0)):
        fx.put(out, i * 0.008, burst(0.12, 900, 6000, 0.012 if i < 2 else 0.05), a)
    return out * amp


def pluck(freq, dur, amp=1.0):
    k = n_(dur)
    return (fx.modes(k, [(freq, 0.25, 1.0), (freq * 2, 0.12, 0.4), (freq * 3, 0.07, 0.2)]) * amp).astype(np.float32)


def bass(freq, dur, amp=1.0):
    k = n_(dur)
    s = np.sin(2 * math.pi * freq * t_(k)) + 0.3 * np.sin(4 * math.pi * freq * t_(k))
    return (s * env_adsr(k, 0.005, 0.1, 0.6, 0.06) * amp).astype(np.float32)


def victory():
    bpm, beat = 138, 60 / 138
    total = 8.2
    out = np.zeros(n_(total + 0.6), np.float32)
    # original melody in G major (beats, midi, length in beats): bouncy village dance, ends on a held tonic
    mel = [(0, 67, .5), (.5, 71, .5), (1, 74, .5), (1.5, 76, .5), (2, 74, 1), (3, 71, .5), (3.5, 72, .5),
           (4, 74, .5), (4.5, 72, .25), (4.75, 71, .25), (5, 69, .5), (5.5, 71, .5), (6, 72, 1), (7, 69, 1),
           (8, 67, .5), (8.5, 71, .5), (9, 74, .5), (9.5, 79, .5), (10, 78, .5), (10.5, 76, .5), (11, 74, .5), (11.5, 72, .5),
           (12, 71, .5), (12.5, 69, .25), (12.75, 71, .25), (13, 72, .5), (13.5, 74, .5), (14, 79, 2.6)]
    for b, m, L in mel:
        fx.put(out, b * beat, flute(midi(m), L * beat * 0.95), 0.32)
        if b >= 8:   # second time round, a pan-pipe harmony a sixth below
            fx.put(out, b * beat, flute(midi(m - 9 if m - 9 >= 55 else m - 5), L * beat * 0.9), 0.12)
    chords = [(55, [67, 71, 74]), (55, [67, 71, 74]), (60, [64, 67, 72]), (62, [66, 69, 74]),
              (55, [67, 71, 74]), (52, [64, 67, 71]), (60, [64, 67, 72]), (62, [66, 69, 74])]
    for bar in range(4):
        for half in range(2):
            root, notes = chords[bar * 2 + half]
            at = (bar * 4 + half * 2) * beat
            fx.put(out, at, bass(midi(root - 12), beat * 1.8), 0.3)
            for j, nt in enumerate(notes):   # plucked strum (a kora/harp-like texture)
                fx.put(out, at + j * 0.018, pluck(midi(nt - 12), 0.6), 0.09)
                fx.put(out, at + beat + j * 0.018, pluck(midi(nt - 12), 0.5), 0.07)
    lo, hi = midi(43), midi(50)
    for s in range(int(16 * 2)):   # log drums on a bouncy pattern, shakers on eighths, claps on 2 and 4
        at = s * beat / 2
        if at > total - 0.3:
            break
        pos = s % 8
        if pos in (0, 3, 6):
            fx.put(out, at, log_drum(lo if pos != 3 else hi), 0.45)
        if pos in (5,):
            fx.put(out, at, log_drum(hi * 1.12), 0.3)
        fx.put(out, at, shaker(), 0.12 if s % 2 else 0.2)
        if pos in (2, 6):
            fx.put(out, at, clap(), 0.3)
    # the creatures cheer at the end: "hey!" (twice) then a "yaaay!"
    cheer_at = 14 * beat
    for i, who in enumerate(["bramble", "soot", "pip", "ash", "bramble", "soot"]):
        hey = voice(0.22, [(0, 0.7), (0.3, 1.0), (1, 0.8)], [(0, "h"), (0.25, "e"), (1, "i")], who=who, amp=[(0, 0), (0.15, 1), (0.6, 0.8), (1, 0)])
        fx.put(out, cheer_at + 0.02 * i, hey, 0.11)
        fx.put(out, cheer_at + beat + 0.02 * i, hey, 0.11)
    for i, who in enumerate(["bramble", "soot", "pip", "ash", "thornback", "pip", "soot"]):
        yay = voice(1.0, [(0, 0.55), (0.2, 1.05), (0.75, 1.0), (1, 0.65)], [(0, "i"), (0.15, "a"), (0.6, "e"), (1, "i")], who=who,
                    amp=[(0, 0), (0.1, 1), (0.8, 0.8), (1, 0)])
        fx.put(out, cheer_at + 2 * beat + 0.03 * i, yay, 0.1)
    fx.put(out, 14 * beat, log_drum(lo * 0.9), 0.6)
    out = out[: n_(total + 0.4)]
    return out * curve([(0, 1), (0.95, 1), (1, 0)], len(out))


def menu_loop():
    beat = 60 / 92
    bars = 8
    total = bars * 4 * beat   # ~22.9 s, loops cleanly
    out = np.zeros(n_(total + 2.0), np.float32)
    prog = [(55, [62, 67, 71]), (48, [64, 67, 72]), (55, [62, 67, 71]), (50, [62, 66, 69])] * 2
    for bar, (root, notes) in enumerate(prog):
        at = bar * 4 * beat
        k = n_(4 * beat + 0.8)
        for nt in notes:   # soft pad: detuned saws, low-passed
            f = np.full(k, midi(nt - 12), np.float32)
            pad = fx.band(fx.saw(f) + fx.saw(f * 1.004), 80, 1400, order=2) * env_adsr(k, 0.6, 0.3, 0.8, 0.9)
            fx.put(out, at, pad, 0.035)
        fx.put(out, at, bass(midi(root - 12), 4 * beat), 0.16)
        for q in range(8):   # gentle plucks
            if q in (0, 3, 5, 6):
                fx.put(out, at + q * beat / 2, pluck(midi(notes[q % 3]), 0.8), 0.06)
        for q in (0, 2):    # soft hand drum
            fx.put(out, at + q * beat, log_drum(midi(45) if q == 0 else midi(52)), 0.12)
    mel = [(0, 74, 1.5), (1.5, 71, .5), (2, 72, 1), (3, 71, 1), (4, 69, 2), (6, 67, 2),
           (8, 71, 1.5), (9.5, 72, .5), (10, 74, 1), (11, 76, 1), (12, 74, 3), (16, 79, 1.5), (17.5, 78, .5),
           (18, 76, 1), (19, 74, 1), (20, 72, 2), (22, 71, 2), (24, 69, 1.5), (25.5, 71, .5), (26, 72, 2), (28, 71, 3.5)]
    for b, m, L in mel:
        fx.put(out, b * beat, flute(midi(m), L * beat * 0.95), 0.12)
    for i in range(10):   # a few soft bird chirps
        at = float(rng.uniform(0.5, total - 1))
        kc = n_(0.09)
        fx.put(out, at, chirp(kc, rng.uniform(3000, 4200), rng.uniform(4500, 6000)) * env_adsr(kc, 0.01, 0.02, 0.6, 0.04), 0.03)
    # seamless loop: fold the tail that rings past the end back onto the start
    n = n_(total)
    tail = out[n:]
    out = out[:n].copy()
    out[: len(tail)] += tail
    return out


# ---------------------------------------------------------------- write

def write(out_dir: Path, name: str, x, kind: str, manifest: list):
    x = np.asarray(x, np.float32)
    x = x - float(np.mean(x))
    nz = np.nonzero(np.abs(x) > 0.002 * (np.max(np.abs(x)) + 1e-9))[0]   # trim silence
    if len(nz):
        x = x[max(0, nz[0] - n_(0.005)): min(len(x), nz[-1] + n_(0.03))]
    fade = min(len(x) // 4, n_(0.012))
    x[-fade:] *= np.linspace(1, 0, fade)
    # loudness: match RMS of the loud part per kind, never above -1 dBFS peak
    target = {"voice": -17.0, "impact": -15.0, "music": -16.0}[kind]
    a = np.abs(x)
    loud = x[a > 0.1 * a.max()] if a.max() > 0 else x
    rms_db = 20 * math.log10(float(np.sqrt(np.mean(loud ** 2))) + 1e-9)
    x = x * 10 ** ((target - rms_db) / 20)
    pk = float(np.max(np.abs(x)))
    if pk > 0.89:
        x = np.tanh(x / 0.89 * 1.2) / math.tanh(1.2) * 0.89 if pk > 1.2 else x * (0.89 / pk)
    wav = out_dir / f".{name}.wav"
    _wav(wav, x)
    want = {"voice": -18.0, "impact": -17.0, "music": -16.0}[kind]
    for _ in range(2):
        try:
            lufs = sd._loudness(["-i", str(wav)])["lufs"]
        except Exception:
            lufs = None
        if lufs is None:
            break
        x = x * 10 ** ((want - lufs) / 20)
        pk = float(np.max(np.abs(x)))
        if pk > 0.84:   # keep true peak under about -1.5 dBFS
            x = (np.tanh(x / 0.84 * 1.1) / math.tanh(1.1) * 0.84).astype(np.float32)
        _wav(wav, x)
    br = "128k" if kind == "music" else "80k"
    mp3 = out_dir / f"{name}.mp3"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(wav), "-ac", "1", "-ar", "44100", "-c:a", "libmp3lame", "-b:a", br, str(mp3)], check=True)
    wav.unlink()
    manifest.append({"file": mp3.name, "kind": kind, "seconds": round(len(x) / SR, 2), "bytes": mp3.stat().st_size})
    print(f"{mp3.name:28s} {len(x) / SR:5.2f}s {mp3.stat().st_size // 1024:4d} KB")


def _wav(path, x):
    import wave
    pcm = (np.clip(x, -1, 1) * 32767).astype("<i2").tobytes()
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(pcm)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("out")
    ap.add_argument("--only", default="")
    a = ap.parse_args()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    only = set(filter(None, a.only.split(",")))
    manifest: list = []
    want = lambda n: not only or n in only  # noqa: E731
    for who in ["bramble", "soot", "pip", "ash", "thornback"]:
        if only and not any(o.startswith(who) for o in only):
            continue
        for k, sig in creature_set(who).items():
            if want(f"{who}_{k}"):
                write(out, f"{who}_{k}", sig, "voice", manifest)
    for i in range(4):
        if want(f"grumble_{i + 1}"):
            write(out, f"grumble_{i + 1}", grumble(i), "voice", manifest)
    if not only or any(not o.startswith(("bramble", "soot", "pip", "ash", "thornback", "grumble", "victory", "menu")) for o in only):
        for k, sig in impacts().items():
            if want(k):
                write(out, k, sig, "impact", manifest)
    if want("victory"):
        write(out, "victory", victory(), "music", manifest)
    if want("menu_loop"):
        write(out, "menu_loop", menu_loop(), "music", manifest)
    if not only:
        (out / "manifest.json").write_text(json.dumps({"game": "Unhappy Woks", "made_with": "Builder (synthesized, original)",
                                                       "files": manifest}, indent=1))


if __name__ == "__main__":
    main()
