"""audio_plus: precise timing, who_spoke, enhance_voice, find_music and make_sfx. Heavy models and the network are stubbed."""
import json
import math
import subprocess
import wave

import pytest

from media_backend import audio_plus as ap
from media_backend import ops, storage, voice

np = pytest.importorskip("numpy")

WORDS = [{"word": "Hi,", "start": 0.0, "end": 0.4}, {"word": "there", "start": 0.4, "end": 0.9}, {"word": "42", "start": 1.0, "end": 1.2}]


def _ff(*args):
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args], check=True)


def _wav(path, x, sr):
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes((np.clip(x, -1, 1) * 32767).astype("<i2").tobytes())
    return storage.import_path(path).id


# ---------------------------------------------------------------- precise timing


def test_ctc_align_finds_token_spans():
    vocab = 5   # 0 blank, 1..4 letters
    T = 40
    lp = np.full((T, vocab), math.log(0.01))
    lp[:, 0] = math.log(0.9)
    for t in range(8, 11):
        lp[t] = math.log(0.01)
        lp[t, 1] = math.log(0.95)
    for t in range(20, 23):
        lp[t] = math.log(0.01)
        lp[t, 2] = math.log(0.95)
    for t in range(30, 31):
        lp[t] = math.log(0.01)
        lp[t, 2] = math.log(0.95)   # the same letter twice needs a blank between
    spans = ap.ctc_align(lp, [1, 2, 2])
    assert [s[0] for s in spans] == [8, 20, 30]
    assert spans[0][1] == 10 and all(0 < s[2] <= 1 for s in spans)
    assert ap.ctc_align(lp[:2], [1, 2, 3]) is None


def test_align_words_moves_words_onto_letters(monkeypatch):
    vocab = {"<pad>": 0, "|": 4, "H": 1, "I": 2, "T": 3, "E": 5, "R": 6}
    monkeypatch.setattr(ap, "_w2v", lambda: (None, vocab))

    def fake_emissions(seg):
        T = int(len(seg) / 16000 / 0.02)
        lp = np.full((T, 7), math.log(0.001))
        lp[:, 0] = math.log(0.99)
        # "HI" at 0.2-0.3 s, "THERE" at 0.6-0.8 s of the chunk
        for t, tok in ((10, 1), (14, 2), (16, 4), (30, 3), (33, 1), (35, 5), (37, 6), (39, 5)):
            lp[t] = math.log(0.001)
            lp[t, tok] = math.log(0.99)
        return lp, 0.02

    monkeypatch.setattr(ap, "_emissions", fake_emissions)
    x16 = np.zeros(16000 * 2, np.float32)
    x16[3200:4800] = 0.3   # some sound so word ends can extend
    out, info = ap.align_words(x16, WORDS)
    assert info["aligned_words"] == 2 and info["words"] == 3
    assert out[0]["start"] == pytest.approx(0.2, abs=0.03) and out[1]["start"] == pytest.approx(0.6, abs=0.03)
    assert out[0]["whisper"] == [0.0, 0.4] and "score" in out[0]
    assert "score" not in out[2] and out[2]["start"] >= out[1]["end"]   # "42" keeps Whisper's time, without overlap


@pytest.fixture
def stub_words(monkeypatch):
    monkeypatch.setattr(voice, "words_of", lambda fid, model="base", language="en": (
        [{"start": 0.0, "end": 1.2, "text": "Hi, there 42"}], [dict(w) for w in WORDS], "en"))


def test_transcribe_precise(stub_words, monkeypatch, media):
    def fake_align(x16, words):
        return [dict(w, start=w["start"] + 0.05, end=w["end"] + 0.05, score=0.9) for w in words], {"aligned_words": len(words)}

    monkeypatch.setattr(ap, "align_words", fake_align)
    res = ops.run_operation("transcribe", {"file_id": media["audio"], "precise": True})
    assert res.data["timing"]["aligned_words"] == 3 and res.data["words"][0]["start"] == 0.05
    srt = next(storage.path_of(o.id) for o in res.outputs if o.name.endswith(".srt")).read_text()
    assert "00:00:00,050 --> " in srt
    plain = ops.run_operation("transcribe", {"file_id": media["audio"]})
    assert "timing" not in plain.data and plain.data["words"][0]["start"] == 0.0


def test_precise_other_language_keeps_times():
    out, info = ap.precise_words("unused", WORDS, "fr")
    assert out == WORDS and "English-only" in info["note"]


def test_lipsync_precise(stub_words, monkeypatch, media):
    pytest.importorskip("phonemizer")
    pytest.importorskip("espeakng_loader")
    monkeypatch.setattr(ap, "align_words", lambda x16, words: ([dict(w, score=1.0) for w in words], {"aligned_words": 3}))
    res = ops.run_operation("lipsync", {"file_id": media["audio"], "precise": True})
    doc = json.loads(storage.path_of(res.outputs[0].id).read_text())
    assert "wav2vec2" in doc["metadata"]["generator"] and res.data["timing"]["aligned_words"] == 3


# ---------------------------------------------------------------- who_spoke


def test_who_spoke_needs_token(monkeypatch, media):
    for k in ("HF_TOKEN", "HUGGINGFACE_TOKEN", "HUGGING_FACE_HUB_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    with pytest.raises(ops.OpError, match="HF_TOKEN"):
        ops.run_operation("who_spoke", {"file_id": media["audio"]})


def test_who_spoke_labels_words(monkeypatch, stub_words, media):
    monkeypatch.setenv("HF_TOKEN", "test-token")
    monkeypatch.setattr(ap, "_diarize", lambda x16, n, lo, hi: [(0.0, 0.45, "SPEAKER_01"), (0.5, 1.3, "SPEAKER_00")])
    res = ops.run_operation("who_spoke", {"file_id": media["audio"]})
    assert res.data["speakers"] == ["Speaker 1", "Speaker 2"] and res.data["talk_time_s"]["Speaker 1"] == 0.45
    names = sorted(o.name for o in res.outputs)
    assert names == ["tone_speakers.json", "tone_speakers.srt"]
    doc = json.loads(storage.path_of(next(o.id for o in res.outputs if o.name.endswith(".json"))).read_text())
    assert [w["speaker"] for w in doc["words"]] == ["Speaker 1", "Speaker 2", "Speaker 2"]
    srt = storage.path_of(next(o.id for o in res.outputs if o.name.endswith(".srt"))).read_text()
    assert "Speaker 1: Hi," in srt and "Speaker 2: there 42" in srt


# ---------------------------------------------------------------- enhance_voice


def _phone_voice(tmp_path):
    """A buzzy 'voice' (harmonics of 120 Hz with a syllable rhythm) through a phone band, with hiss."""
    sr = 16000
    t = np.arange(int(sr * 2.0)) / sr
    f0 = 120 * (1 + 0.05 * np.sin(2 * math.pi * 1.3 * t))
    ph = np.cumsum(f0) / sr
    x = sum(np.sin(2 * math.pi * h * ph) / h for h in range(1, 60))
    x *= 0.5 + 0.5 * np.sin(2 * math.pi * 3 * t) ** 2
    x = 0.2 * x / np.max(np.abs(x)) + 0.004 * np.random.default_rng(0).standard_normal(len(t))
    raw = tmp_path / "raw.wav"
    _wav(raw, x, sr)
    out = tmp_path / "phone.wav"
    _ff("-i", str(raw), "-af", "highpass=f=300,highpass=f=300,highpass=f=300,lowpass=f=3400,lowpass=f=3400,aresample=8000", str(out))
    return storage.import_path(out).id


def test_enhance_voice_rebuilds_band(monkeypatch, tmp_path):
    monkeypatch.setattr(ap, "deepfilter", lambda x, s=1.0: x * 0.9)
    fid = _phone_voice(tmp_path)
    res = ops.run_operation("enhance_voice", {"file_id": fid, "loudness": -16})
    d = res.data
    assert res.outputs[0].name == "phone_enhanced.wav"
    assert d["before"]["top_hz"] <= 4600 and d["after"]["top_hz"] > 7000
    assert any("bandwidth extension" in s for s in d["steps"]) and any("DeepFilterNet" in s for s in d["steps"])
    assert d["after"]["lufs"] == pytest.approx(-16, abs=1.5) and d["after"]["true_peak_db"] <= -1.0
    assert ops.media_info(res.outputs[0].id)["audio"]["sample_rate"] == 48000


def test_enhance_voice_without_deepfilternet(monkeypatch, tmp_path):
    def missing(*a, **k):
        raise ops.OpError("DeepFilterNet is not installed")

    monkeypatch.setattr(ap, "deepfilter", missing)
    fid = _phone_voice(tmp_path)
    res = ops.run_operation("enhance_voice", {"file_id": fid, "bandwidth": "off", "format": "mp3"})
    assert res.outputs[0].name.endswith(".mp3") and any("afftdn" in s for s in res.data["steps"])
    assert not any("bandwidth" in s for s in res.data["steps"])


def test_enhance_voice_keeps_video_picture(monkeypatch, media):
    monkeypatch.setattr(ap, "deepfilter", lambda x, s=1.0: x)
    res = ops.run_operation("enhance_voice", {"file_id": media["video"], "dereverb": 0})
    info = ops.media_info(res.outputs[0].id)
    assert res.outputs[0].name == "clip_enhanced.mp4" and info["has_video"] and info["has_audio"]


def test_dereverb_shortens_a_tail():
    sr = 48000
    x = np.zeros(sr, np.float32)
    x[1000:3000] = np.sin(np.arange(2000) / 5.0)
    tail = np.random.default_rng(1).standard_normal(sr // 2) * np.exp(-np.arange(sr // 2) / (0.1 * sr)) * 0.3
    wet = x + np.convolve(x, tail)[:sr].astype(np.float32) * 0.05
    y = ap.dereverb(wet, sr, 1.0)
    late = slice(int(0.25 * sr), int(0.5 * sr))
    assert np.sqrt(np.mean(y[late] ** 2)) < 0.7 * np.sqrt(np.mean(wet[late] ** 2))


# ---------------------------------------------------------------- find_music


def test_parse_music_words():
    p = ap.parse_music_words("Triumphant march, brass, fast")
    assert p["moods"] == ["triumphant"] and p["instruments"] == ["brass"] and "march" in p["other"]
    assert p["target"]["bpm"] == (118, 180) and p["target"]["mode"] == "major" and "fanfare" in p["terms"]
    assert ap.parse_music_words("calm piano 70 bpm")["target"]["bpm"] == pytest.approx((63, 77))
    assert ap.parse_music_words("funny")["moods"] == ["playful"]


@pytest.fixture
def fake_archive(monkeypatch, tmp_path):
    """Three items: a 1915 march (trusted by date), a Musopen nocturne, and a modern score wrongly marked CC0."""
    sr = 22050

    def tune(path, bpm, bright, seconds=40):
        t = np.arange(sr * seconds) / sr
        beat = (np.mod(t, 60 / bpm) < 0.05).astype(float)
        x = 0.3 * np.sin(2 * math.pi * 220 * t) + 0.3 * beat * np.sin(2 * math.pi * bright * t)
        with wave.open(str(path), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(sr)
            w.writeframes((x * 32767 * 0.5).astype("<i2").tobytes())
        return path

    items = {
        "march1915": ({"title": "Grand March", "creator": "Band", "date": "1915", "licenseurl": "https://creativecommons.org/publicdomain/mark/1.0/",
                       "collection": ["78rpm"]}, [{"name": "01 Grand March.mp3", "source": "original", "length": "40", "size": "400000"},
                                                  {"name": "02 Grand March - Trumpet Section.mp3", "source": "derivative", "length": "40", "size": "400000"}], (124, 1800)),
        "musopen_nocturne": ({"title": "Nocturne", "creator": "Chopin", "collection": ["musopen-chopin"]},
                             [{"name": "nocturne.mp3", "source": "original", "length": "40", "size": "400000"}], (66, 400)),
        "modern_score": ({"title": "Film Score", "creator": "Someone", "date": "2004", "licenseurl": "http://creativecommons.org/publicdomain/zero/1.0/",
                          "collection": ["opensource_audio"]}, [{"name": "score.mp3", "source": "original", "length": "40", "size": "400000"}], (124, 1800)),
    }
    calls = []

    def cached_json(key, url, ttl_s=0):
        calls.append(key.split(":")[0])
        if key.startswith("search:"):
            return {"response": {"docs": [{"identifier": k} for k in items]}}
        ident = key.split(":", 1)[1]
        md, files, _ = items[ident]
        return {"metadata": md, "files": files}

    def preview(ident, fmeta, seconds=30.0):
        bpm, bright = items[ident][2]
        return tune(tmp_path / f"{ident}.wav", bpm, bright)

    def download(url, dest):
        ident = url.split("/download/")[1].split("/")[0]
        bpm, bright = items[ident][2]
        tune(tmp_path / "full.wav", bpm, bright, 12)
        _ff("-i", str(tmp_path / "full.wav"), "-f", "mp3", str(dest))

    monkeypatch.setattr(ap, "_cached_json", cached_json)
    monkeypatch.setattr(ap, "_preview", preview)
    from media_backend import assets
    monkeypatch.setattr(assets, "_download", download)
    return calls


def test_find_music_ranks_and_filters(fake_archive):
    res = ops.run_operation("find_music", {"words": "triumphant march", "candidates": 4, "top": 3})
    picks = res.data["picks"]
    ids = [p["id"] for p in picks]
    assert "modern_score/score.mp3" not in ids                    # marked CC0 but recorded in 2004: skipped
    assert ids[0] == "march1915/01 Grand March.mp3"              # the full performance, not the trumpet part
    assert picks[0]["match"] > picks[1]["match"] and "public domain" in picks[0]["license_line"]
    assert picks[0]["measured"]["bpm"] and 0 <= picks[0]["measured"]["energy"] <= 1
    calm = ops.run_operation("find_music", {"words": "calm sad", "candidates": 4}).data["picks"]
    assert calm[0]["id"] == "musopen_nocturne/nocturne.mp3"


def test_find_music_use_loops_with_licence(fake_archive):
    res = ops.run_operation("find_music", {"words": "triumphant march", "candidates": 4, "use": "1", "length_s": 20, "format": "wav"})
    out = res.outputs[0]
    assert out.name == "01_Grand_March_20s.wav" and res.data["looped"] and res.data["duration_s"] == 20
    assert ops.media_info(out.id)["duration_s"] == pytest.approx(20, abs=0.1)
    lic = json.loads((storage.path_of(out.id).parent / "license.json").read_text())
    assert lic["license"] == "Public domain" and lic["id"] == "march1915/01 Grand March.mp3"
    with pytest.raises(ops.OpError, match="public-domain recording"):
        ops.run_operation("find_music", {"words": "march", "use": "modern_score/score.mp3"})


# ---------------------------------------------------------------- make_sfx


@pytest.mark.parametrize("kind,variant,length", [
    ("door", "slam", 1.0), ("crowd", "cheer", 1.5), ("crowd", "laugh", 1.5), ("car", "pass_by", 2.0), ("ui", "success", None),
    ("weather", "rain_window", 1.0), ("impact", "sword_clash", 1.0), ("impact", "shield_hit", None), ("swoosh", "sword", None),
    ("fanfare", "short", None), ("gong", "small", 2.0)])
def test_make_sfx_kinds(kind, variant, length):
    params = {"kind": kind, "variant": variant, "seed": 3, "intensity": 0.8}
    if length:
        params["length_s"] = length
    res = ops.run_operation("make_sfx", params)
    info = ops.media_info(res.outputs[0].id)
    want = length or ap.SFX[kind][variant]
    assert info["duration_s"] == pytest.approx(want, abs=0.05) and info["audio"]["channels"] == 2
    assert res.data["variant"] == variant and res.data["seed"] == 3
    with wave.open(str(storage.path_of(res.outputs[0].id))) as w:
        pcm = np.frombuffer(w.readframes(w.getnframes()), "<i2").astype(float) / 32768
    assert 0.5 < np.max(np.abs(pcm)) < 0.95   # peak-normalised below 0 dBFS
    assert np.sqrt(np.mean(pcm ** 2)) > 0.003  # not silent


def test_make_sfx_seed_repeats_and_variant_check():
    a = ops.run_operation("make_sfx", {"kind": "ui", "variant": "click", "seed": 5})
    b = ops.run_operation("make_sfx", {"kind": "ui", "variant": "click", "seed": 5})
    assert storage.path_of(a.outputs[0].id).read_bytes() == storage.path_of(b.outputs[0].id).read_bytes()
    assert ops.run_operation("make_sfx", {"kind": "door"}).data["variant"] == "open"
    assert ops.run_operation("make_sfx", {"kind": "car", "variant": "Horn Double", "format": "mp3"}).outputs[0].name.endswith(".mp3")
    with pytest.raises(ops.OpError, match="sword_clash"):
        ops.run_operation("make_sfx", {"kind": "impact", "variant": "explosion"})


def test_registered():
    for name in ("who_spoke", "enhance_voice", "find_music", "make_sfx"):
        assert name in ops.REGISTRY
    assert "precise" in ops.REGISTRY["transcribe"].params.model_fields and "precise" in ops.REGISTRY["lipsync"].params.model_fields
