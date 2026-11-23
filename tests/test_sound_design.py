"""Music, ambience, footsteps and the audio check. Whisper is stubbed."""
import json
import subprocess

import pytest

from media_backend import ops, sound_design, storage, voice
from media_backend.ops import OpError

pytest.importorskip("numpy")


def _check(file_id, **kw):
    return ops.run_operation("audio_check", {"file_id": file_id, **kw})


@pytest.mark.parametrize("mood", ["calm", "upbeat", "tense", "sad", "epic", "playful", "lofi"])
def test_make_music_moods(mood):
    res = ops.run_operation("make_music", {"mood": mood, "length_s": 6, "seed": 7})
    out = res.outputs[0]
    assert out.name == f"music_{mood}_7.wav" and res.data["seed"] == 7 and res.data["progression"]
    info = ops.media_info(out.id)
    assert abs(info["duration_s"] - 6) < 0.1 and info["audio"]["channels"] == 2
    rep = _check(out.id).data
    assert abs(rep["lufs"] - -16) < 1.0 and rep["true_peak_db"] <= -1.0 and rep["clipped_samples"] == 0


def test_make_music_repeatable_key_and_mp3():
    a = ops.run_operation("make_music", {"mood": "upbeat", "length_s": 5, "seed": 3, "key": "F# minor", "tempo_bpm": 100})
    b = ops.run_operation("make_music", {"mood": "upbeat", "length_s": 5, "seed": 3, "key": "F# minor", "tempo_bpm": 100})
    assert a.data["key"] == "F# minor" and a.data["tempo_bpm"] == 100
    assert storage.path_of(a.outputs[0].id).read_bytes() == storage.path_of(b.outputs[0].id).read_bytes()
    mp3 = ops.run_operation("make_music", {"mood": "lofi", "length_s": 5, "seed": 3, "format": "mp3"}).outputs[0]
    assert mp3.name.endswith(".mp3") and ops.media_info(mp3.id)["audio"]["codec"] == "mp3"
    with pytest.raises(OpError):
        ops.run_operation("make_music", {"mood": "calm", "length_s": 5, "key": "H dorian"})


@pytest.mark.parametrize("kind", ["room", "office", "outdoors", "city", "rain", "wind", "crowd_murmur", "forest"])
def test_make_ambience(kind):
    res = ops.run_operation("make_ambience", {"kind": kind, "length_s": 4, "seed": 1})
    out = res.outputs[0]
    assert out.name == f"ambience_{kind}_1.wav"
    rep = _check(out.id).data
    assert abs(rep["duration_s"] - 4) < 0.1 and abs(rep["lufs"] - sound_design.AMBIENCE_LEVEL[kind]) < 1.5


def test_footsteps_count_and_times():
    res = ops.run_operation("footsteps", {"surface": "gravel", "count": 5, "seed": 2})
    assert res.data["steps"] == 5 and res.outputs[0].name == "footsteps_gravel_2.wav"
    rep = _check(res.outputs[0].id).data
    assert rep["peak_dbfs"] < -2 and rep["clipped_samples"] == 0
    timed = ops.run_operation("footsteps", {"surface": "wood", "times": [0.5, 1.1, 1.6, 2.4], "length_s": 3})
    assert timed.data["times"] == [0.5, 1.1, 1.6, 2.4] and abs(ops.media_info(timed.outputs[0].id)["duration_s"] - 3) < 0.05
    run = ops.run_operation("footsteps", {"surface": "snow", "pace": "run", "length_s": 4, "format": "mp3"})
    assert 9 <= run.data["steps"] <= 11 and run.outputs[0].name.endswith(".mp3")


def test_footsteps_place_steps_where_asked():
    np = pytest.importorskip("numpy")
    out = ops.run_operation("footsteps", {"surface": "tile", "times": [1.0, 2.0], "length_s": 3, "reverb": 0}).outputs[0]
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(storage.path_of(out.id)), "-f", "f32le", "-ac", "1", "-"],
                         capture_output=True, check=True).stdout
    x = np.abs(np.frombuffer(raw, np.float32))
    sr = 44100
    assert x[: int(0.95 * sr)].max() < 0.01                      # nothing before the first contact
    assert x[int(0.99 * sr): int(1.1 * sr)].max() > 0.3          # a step right at 1.0 s
    assert x[int(1.99 * sr): int(2.1 * sr)].max() > 0.3


def test_audio_check_finds_problems(media, tmp_path):
    loud = tmp_path / "hot.wav"
    # clipped, offset, with a 3 s silent gap in the middle
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=f=220:d=2", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono:d=3",
                    "-f", "lavfi", "-i", "sine=f=220:d=2", "-filter_complex",
                    "[0:a]volume=30dB,dcshift=0.02[a];[1:a]aformat=sample_rates=44100[b];[2:a]volume=30dB,dcshift=0.02[c];[a][b][c]concat=n=3:v=0:a=1[o]",
                    "-map", "[o]", "-c:a", "pcm_s16le", str(loud)], check=True)
    fid = storage.import_path(loud).id
    res = _check(fid, min_silence_s=1.5)
    rep = res.data
    assert rep["clipped_samples"] > 100 and rep["clip_events"] > 0
    assert rep["silences"] and 2.9 < rep["silences"][0]["duration"] < 3.2 and 1.9 < rep["silences"][0]["start"] < 2.1
    assert rep["dc_offset"][0] > 0.005 and not rep["ok"]
    assert any("Clipping" in i for i in rep["issues"]) and any("silence" in i for i in rep["issues"])
    saved = json.loads(storage.path_of(res.outputs[0].id).read_text())
    assert res.outputs[0].name == "hot_check.json" and saved["clipped_samples"] == rep["clipped_samples"]
    v = _check(media["video"]).data   # works on video files too
    assert v["lufs"] is not None and v["channels"] >= 1


def test_audio_check_words(media, monkeypatch):
    monkeypatch.setattr(voice, "words_of", lambda fid, model="base", language="en": (
        [{"start": 0.0, "end": 2.0, "text": "Hello there, my old friend."}], [], "en"))
    sp = _check(media["audio"], expected_text="Hello there my dear old friend").data["speech"]
    assert sp["missing"] == ["dear"] and sp["extra"] == [] and sp["matched_words"] == 5
    assert sp["word_accuracy"] == round(5 / 6, 3) and 0.8 < sp["match_score"] < 1


def test_word_match_alignment():
    m = sound_design.word_match("The quick brown fox jumps", "the quick red fox jumps high")
    assert m["missing"] == ["brown"] and m["extra"] == ["red", "high"] and m["wer"] == 0.4


def test_audio_check_without_whisper(media, monkeypatch):
    def no_whisper(*a, **k):
        try:
            import faster_whisper_not_here  # noqa: F401
        except ImportError as e:
            raise OpError("Captions need the speech extra") from e

    monkeypatch.setattr(voice, "_whisper_model", no_whisper)
    assert _check(media["audio"]).data["lufs"] is not None          # fine without a script
    with pytest.raises(OpError, match="needs Whisper"):
        _check(media["audio"], expected_text="hello")
