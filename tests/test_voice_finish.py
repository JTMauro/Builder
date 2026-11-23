"""Voice, captions, lip-sync, audio polish and video finishing. Speech engines are stubbed."""
import json
import math
import struct

import pytest

from media_backend import ops, storage, voice


class FakeKokoro:
    def create(self, text, voice, speed=1.0, lang="en-us"):
        np = pytest.importorskip("numpy")
        sr = 24000
        t = np.arange(int(sr * 0.8)) / sr
        return (0.3 * np.sin(2 * math.pi * 220 * t)).astype("float32"), sr


WORDS = [{"word": "Hi,", "start": 0.0, "end": 0.22}, {"word": "I'm", "start": 0.42, "end": 0.58},
         {"word": "photo", "start": 1.2, "end": 1.48}, {"word": "talk.", "start": 3.18, "end": 3.42}]


@pytest.fixture
def stub_speech(monkeypatch):
    monkeypatch.setattr(voice, "_kokoro_engine", lambda: FakeKokoro())
    monkeypatch.setattr(voice, "words_of", lambda fid, model="base", language="en": (
        [{"start": 0.0, "end": 3.42, "text": "Hi, I'm photo talk."}], WORDS, "en"))


def test_speak(stub_speech):
    pytest.importorskip("numpy")
    res = ops.run_operation("speak", {"text": "Hello there, Builder", "voice": "am_puck", "format": "mp3"})
    assert res.outputs[0].name == "Hello_there_Builder.mp3" and 0.7 < res.data["duration_s"] < 0.9
    assert ops.media_info(res.outputs[0].id)["has_audio"]


def test_transcribe_and_burn(stub_speech, media):
    res = ops.run_operation("transcribe", {"file_id": media["video"]})
    names = sorted(o.name for o in res.outputs)
    assert names == ["clip.srt", "clip.vtt"]
    srt = next(storage.path_of(o.id) for o in res.outputs if o.name.endswith(".srt")).read_text()
    assert "00:00:00,000 --> " in srt and "Hi," in srt
    vtt = next(storage.path_of(o.id) for o in res.outputs if o.name.endswith(".vtt")).read_text()
    assert vtt.startswith("WEBVTT") and "00:00:00.000" in vtt
    srt_id = next(o.id for o in res.outputs if o.name.endswith(".srt"))
    out = ops.run_operation("burn_captions", {"file_id": media["video"], "captions_id": srt_id, "style": "boxed"}).outputs[0]
    assert ops.media_info(out.id)["video"]["width"] == 320


def test_mouth_cues_mapping():
    pytest.importorskip("phonemizer")
    pytest.importorskip("espeakng_loader")
    cues = voice.mouth_cues([{"word": "mama", "start": 0.2, "end": 0.6}, {"word": "food", "start": 1.0, "end": 1.4}], 1.6)
    values = [c["value"] for c in cues]
    assert values[0] == "X" and values[-1] == "X"
    assert "A" in values and "G" in values and "F" in values          # m, f, oo
    assert all(cues[i]["end"] <= cues[i + 1]["start"] + 1e-6 for i in range(len(cues) - 1))
    assert cues[-1]["end"] == 1.6


def test_lipsync_file(stub_speech, media, monkeypatch):
    monkeypatch.setattr(voice, "mouth_cues", lambda words, dur, lang="en-us": [{"start": 0, "end": dur, "value": "X"}])
    res = ops.run_operation("lipsync", {"file_id": media["audio"]})
    doc = json.loads(storage.path_of(res.outputs[0].id).read_text())
    assert doc["mouthCues"][0]["value"] == "X" and doc["words"] == WORDS


@pytest.mark.parametrize("op,params", [
    ("denoise_audio", {"strength": "medium"}),
    ("voice_polish", {"style": "broadcast"}),
    ("voice_polish", {"style": "phone"}),
    ("room_reverb", {"room": "hall"}),
])
def test_audio_polish(media, op, params):
    out = ops.run_operation(op, {"file_id": media["audio"], **params}).outputs[0]
    assert out.name.endswith(".wav") and ops.media_info(out.id)["has_audio"]
    v = ops.run_operation(op, {"file_id": media["video"], **params}).outputs[0]
    i = ops.media_info(v.id)
    assert i["has_video"] and i["has_audio"]


def test_film_look_and_lut(media, tmp_path):
    out = ops.run_operation("film_look", {"file_id": media["video"], "look": "film", "letterbox": True}).outputs[0]
    assert ops.media_info(out.id)["has_audio"]
    cube = tmp_path / "warm.cube"
    lines = ["LUT_3D_SIZE 2"] + [f"{min(1, r * 1.1):.3f} {g:.3f} {b * 0.9:.3f}" for b in (0, 1) for g in (0, 1) for r in (0, 1)]
    cube.write_text("\n".join(lines) + "\n")
    lut = storage.import_path(cube).id
    out = ops.run_operation("film_look", {"file_id": media["video"], "lut_id": lut, "grain": 0}).outputs[0]
    assert out.name == "clip_lut.mp4"


def test_upscale_fallback(media, monkeypatch):
    monkeypatch.delenv("REALESRGAN_BIN", raising=False)
    monkeypatch.setenv("PATH", "/usr/bin:/bin:/usr/local/bin")
    res = ops.run_operation("upscale", {"file_id": media["video"], "factor": 2})
    assert ops.media_info(res.outputs[0].id)["video"]["width"] == 640 and "lanczos" in res.data["engine"]
    img = ops.run_operation("upscale", {"file_id": media["image"], "factor": 2}).outputs[0]
    assert img.name.endswith("_x2.png")


def test_smooth_motion(media):
    res = ops.run_operation("smooth_motion", {"file_id": media["video"], "fps": 50})
    assert abs(ops.media_info(res.outputs[0].id)["video"]["fps"] - 50) < 0.5
    slow = ops.run_operation("smooth_motion", {"file_id": media["video"], "fps": 50, "slow_motion": 2}).outputs[0]
    assert ops.media_info(slow.id)["duration_s"] > 5
