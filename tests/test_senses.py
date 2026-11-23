"""watch_video, caption_audio, quality_score with the models stubbed (no downloads, fast)."""
import json

import pytest

from media_backend import ops, senses, storage
from media_backend.ops import OpError

np = pytest.importorskip("numpy")


def test_registered():
    for name in ("watch_video", "caption_audio", "quality_score"):
        assert name in ops.REGISTRY and ops.REGISTRY[name].read_only


def test_parse_timeline_formats():
    moments = [1.0, 3.0, 5.0]
    text = "1.0 s: he waves\n- 0:03 s - she turns\n[5 s]: they talk\n7 s: invented\n9.0 s: he waves"
    notes = senses._parse_timeline(text, moments)
    assert [n["t"] for n in notes] == [1.0, 3.0, 5.0]
    assert notes[1]["note"] == "she turns"
    # times dropped by the model: lines are paired with the moments in order
    notes = senses._parse_timeline("Two people stand.\n\n- One waves.\n", moments)
    assert notes == [{"t": 1.0, "note": "Two people stand."}, {"t": 3.0, "note": "One waves."}]


def test_watch_video_samples_frames_and_reports(media, monkeypatch):
    calls = []

    def fake(frames, times, prompt, max_new_tokens):
        calls.append((frames.shape, list(times), prompt))
        if prompt.startswith("For each moment"):
            return "0.4 s: a test pattern\n1.1 s: still a test pattern"
        return "A colourful test pattern with a moving bar."

    monkeypatch.setattr(senses, "_generate_vlm", fake)
    res = ops.run_operation("watch_video", {"file_id": media["video"], "frames": 5, "max_side": 160})
    shape, times, prompt = calls[0]
    assert shape[0] == 6 and shape[3] == 3  # rounded up to an even count (the model pairs frames)
    assert shape[2] % 32 == 0 and shape[1] % 32 == 0 and max(shape[1:3]) <= 160
    assert times == sorted(times) and 0 < times[0] < times[-1] < 3
    assert "Describe what happens" in prompt
    d = res.data
    assert d["answer"].startswith("A colourful") and len(d["timeline"]) == 2
    assert res.outputs[0].name == "clip_watch.json"
    saved = json.loads(storage.path_of(res.outputs[0].id).read_text())
    assert saved["model"] == senses.VLM_REPO and len(calls) == 2


def test_watch_video_question_window_and_errors(media, monkeypatch):
    seen = {}
    monkeypatch.setattr(senses, "_generate_vlm", lambda f, t, p, m: seen.update(t=t, p=p) or "No.")
    res = ops.run_operation("watch_video", {"file_id": media["video"], "question": "Is it raining?", "start": 1, "end": 2,
                                            "frames": 4, "timeline": False, "max_side": 128})
    assert res.data["answer"] == "No." and res.data["timeline"] == []
    assert all(1 <= t <= 2 for t in seen["t"]) and seen["p"].startswith("Is it raining?")
    with pytest.raises(OpError, match="past the end"):
        ops.run_operation("watch_video", {"file_id": media["video"], "start": 10})
    with pytest.raises(OpError):
        ops.run_operation("watch_video", {"file_id": media["audio"]})


def _vec(torch, text, dim=256):
    g = torch.Generator().manual_seed(abs(hash(text)) % (2 ** 31))
    v = torch.randn(dim, generator=g)
    return v / v.norm()


@pytest.fixture
def fake_clap(monkeypatch):
    torch = pytest.importorskip("torch")
    target = _vec(torch, "This is a recording of environmental ambience.") + _vec(torch, "This is a sound of rain.") \
        + _vec(torch, "This sound was recorded in outdoors in an open space.") + _vec(torch, "This sound feels calm.")
    target = target / target.norm()
    monkeypatch.setattr(senses, "_clap_embed_text", lambda texts: torch.stack([_vec(torch, t) for t in texts]))
    monkeypatch.setattr(senses, "_clap_embed_audio", lambda windows: torch.stack([target for _ in windows]))
    monkeypatch.setattr(senses, "_clap_scale", lambda: 100.0)
    return target


def test_caption_audio_words_and_timeline(media, fake_clap):
    res = ops.run_operation("caption_audio", {"file_id": media["audio"], "window_s": 2, "numbers": False,
                                              "extra_labels": ["a basketball bouncing"]})
    d = res.data
    assert d["tags"]["content"][0]["label"] == "environmental ambience"
    assert d["tags"]["sound"][0]["label"] == "rain"
    assert d["caption"].startswith("Sounds like environmental ambience (rain")
    assert "outdoors" in d["caption"] and "mood calm" in d["caption"]
    assert len(d["timeline"]) == 1 and d["timeline"][0]["content"] == "environmental ambience"
    assert res.outputs[-1].name == "tone_caption.json"


def test_caption_audio_adds_measurements(media, fake_clap):
    res = ops.run_operation("caption_audio", {"file_id": media["video"], "numbers": True})
    d = res.data
    assert "Measured:" in d["caption"] and "LUFS" in d["caption"]
    names = [o.name for o in res.outputs]
    assert names[-1] == "clip_caption.json" and any(n.endswith("_spectrogram.png") for n in names)
    with pytest.raises(OpError):
        ops.run_operation("caption_audio", {"file_id": media["image"]})


@pytest.fixture
def fake_quality(monkeypatch):
    torch = pytest.importorskip("torch")

    class FakeLpips:
        def __call__(self, a, b):
            return (a - b).abs().mean(dim=(1, 2, 3), keepdim=True) / 2

    monkeypatch.setattr(senses, "_lpips_model", lambda: FakeLpips())
    monkeypatch.setattr(senses, "aesthetic_scores", lambda frames: [5.0 + float(f.mean()) / 255 for f in frames])
    return torch


def test_quality_score_reference_and_aesthetic(media, fake_quality):
    same = ops.run_operation("quality_score", {"file_id": media["image"], "reference_id": media["image"]})
    assert same.data["lpips"]["mean"] == 0 and same.data["lpips"]["meaning"] == "practically the same"
    diff = ops.run_operation("quality_score", {"file_id": media["image"], "reference_id": media["image2"]})
    assert diff.data["lpips"]["mean"] > 0.15
    assert 5 <= diff.data["aesthetic"]["mean"] <= 6 and "reference_mean" in diff.data["aesthetic"]
    vid = ops.run_operation("quality_score", {"file_id": media["video"], "frames": 3, "aesthetic": True})
    assert len(vid.data["times"]) == 3 and "lpips" not in vid.data
    assert vid.outputs[0].name == "clip_quality.json"


def test_visual_diff_perceptual(media, fake_quality):
    plain = ops.run_operation("visual_diff", {"file_id": media["image"], "reference_id": media["image2"]})
    assert "lpips" not in plain.data
    res = ops.run_operation("visual_diff", {"file_id": media["image"], "reference_id": media["image2"], "perceptual": True})
    assert res.data["lpips"] > 0 and res.data["lpips_meaning"]


def test_status_and_missing_model_message(monkeypatch, tmp_path):
    monkeypatch.setenv("BUILDER_MODELS_DIR", str(tmp_path / "models"))
    st = senses.status()
    assert st["vlm"] is False and st["clap"] is False
    assert senses.senses_dir() == tmp_path / "models" / "vlm"
