"""review_video and describe_audio on generated media (still vs moving pictures, clicks, tones, noise)."""
import json
import subprocess

import pytest

from media_backend import ops, storage
from media_backend.ops import OpError

pytest.importorskip("numpy")


def _ff(*args):
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args], check=True)


def _make(tmp_path, name, *args):
    out = tmp_path / name
    _ff(*args, str(out))
    return storage.import_path(out).id


def _video(file_id, **kw):
    return ops.run_operation("review_video", {"file_id": file_id, **kw})


def _audio(file_id, **kw):
    return ops.run_operation("describe_audio", {"file_id": file_id, **kw})


def test_registered():
    assert "review_video" in ops.REGISTRY and "describe_audio" in ops.REGISTRY


def test_review_video_static_vs_moving(media, tmp_path):
    still = _make(tmp_path, "still.mp4", "-f", "lavfi", "-i", "color=c=steelblue:s=320x240:r=25:d=3",
                  "-c:v", "libx264", "-pix_fmt", "yuv420p")
    res = _video(still, frames=6, columns=3)
    names = [o.name for o in res.outputs]
    assert names == ["still_contact_sheet.png", "still_motion_map.png", "still_review.json"]
    rep = res.data
    assert rep["motion"]["mean_pct"] < 0.01 and max(rep["motion"]["per_second_pct"]) < 0.01
    assert rep["frozen"] and any("no motion" in i for i in rep["issues"])
    assert json.loads(storage.path_of(res.outputs[2].id).read_text())["motion"]["mean_pct"] == rep["motion"]["mean_pct"]
    sheet = ops.media_info(res.outputs[0].id)["video"]
    assert sheet["width"] > 3 * 300 and sheet["height"] > 2 * 200  # 3 x 2 tiles

    moving = _video(media["video"]).data
    assert moving["motion"]["mean_pct"] > 0.05 and len(moving["motion"]["per_second_pct"]) == 3
    assert not moving["frozen"] and not moving["black"]
    assert len(moving["sharpness"]["samples"]) == 12 and moving["sharpness"]["median"] > 100
    assert 0 < moving["brightness"]["mean"] < 1 and moving["saturation"]["mean"] > 0.3
    # inputs are never modified
    assert storage.get(media["video"]).name == "clip.mp4"


def test_review_video_jitter_cuts_black_letterbox(tmp_path):
    img = tmp_path / "pic.png"
    _ff("-f", "lavfi", "-i", "testsrc2=s=320x240", "-frames:v", "1", str(img))
    jitter = _make(tmp_path, "jitter.mp4", "-loop", "1", "-i", str(img), "-t", "3", "-r", "25",
                   "-vf", "crop=300:220:'if(mod(n,2),0,8)':0,scale=320:240", "-c:v", "libx264", "-pix_fmt", "yuv420p")
    smooth = _make(tmp_path, "smooth.mp4", "-loop", "1", "-i", str(img), "-t", "3", "-r", "25",
                   "-vf", "crop=280:220:'n*0.5':0,scale=320:240", "-c:v", "libx264", "-pix_fmt", "yuv420p")
    j, s = _video(jitter).data, _video(smooth).data
    assert j["jitter"]["ratio"] > 0.5 and any("jitter" in i for i in j["issues"])
    assert s["jitter"]["ratio"] < 0.2 and s["motion"]["mean_pct"] > 0.1

    # a cut to black, then a different picture inside letterbox bars
    cut = _make(tmp_path, "cut.mp4", "-f", "lavfi", "-i", "testsrc=s=320x240:r=25:d=2",
                "-f", "lavfi", "-i", "color=black:s=320x240:r=25:d=1",
                "-f", "lavfi", "-i", "testsrc2=s=320x180:r=25:d=2",
                "-filter_complex", "[2]pad=320:240:0:30[c];[0][1][c]concat=n=3", "-c:v", "libx264", "-pix_fmt", "yuv420p")
    c = _video(cut).data
    assert any(abs(x["time"] - 2) < 0.2 for x in c["scene_cuts"]) and any(abs(x["time"] - 3) < 0.2 for x in c["scene_cuts"])
    assert c["black"] and abs(c["black"][0]["start"] - 2) < 0.2 and abs(c["black"][0]["end"] - 3) < 0.2
    assert not c["letterbox"]["detected"]  # bars only part of the time are not letterboxing

    boxed = _make(tmp_path, "boxed.mp4", "-f", "lavfi", "-i", "testsrc2=s=320x180:r=25:d=1",
                  "-vf", "pad=320:240:0:30", "-c:v", "libx264", "-pix_fmt", "yuv420p")
    lb = _video(boxed, frames=4).data["letterbox"]
    assert lb["detected"] and 20 <= lb["top_px"] <= 36 and 20 <= lb["bottom_px"] <= 36 and lb["left_px"] == 0


def test_review_video_needs_video(media):
    with pytest.raises(OpError):
        _video(media["audio"])


def test_describe_audio_click_track_tempo(tmp_path):
    clicks = _make(tmp_path, "clicks.wav", "-f", "lavfi", "-i",
                   "aevalsrc='if(lt(mod(t,0.5),0.01),0.8*sin(2*PI*1500*t),0)':s=44100:d=10")
    res = _audio(clicks)
    assert [o.name for o in res.outputs] == ["clicks_spectrogram.png", "clicks_waveform.png", "clicks_audio_review.json"]
    rep = res.data
    assert abs(rep["rhythm"]["bpm"] - 120) < 4 and rep["rhythm"]["regularity"] > 0.5
    assert rep["clipping"]["clipped_samples"] == 0 and "no clipping" in rep["description"]
    assert "120 BPM" in rep["description"]
    assert ops.media_info(res.outputs[0].id)["has_video"]


def test_describe_audio_sine_vs_noise(tmp_path):
    sine = _audio(_make(tmp_path, "sine.wav", "-f", "lavfi", "-i", "sine=f=300:d=4")).data
    noise = _audio(_make(tmp_path, "noise.wav", "-f", "lavfi", "-i", "anoisesrc=d=4:c=white:a=0.3")).data
    assert noise["spectrum"]["harshness"] > sine["spectrum"]["harshness"] + 0.1
    assert noise["spectrum"]["centroid_hz"] > 3 * sine["spectrum"]["centroid_hz"]
    assert noise["spectrum"]["flatness"] > 0.3 > sine["spectrum"]["flatness"]
    assert noise["spectrum"]["brightness"] == "bright" and sine["spectrum"]["brightness"] == "dark/warm"
    assert noise["content"]["other"] > 0.5 and noise["voice"]["median_f0_hz"] is None


def test_describe_audio_vibrato_pitch(tmp_path):
    # a voice-like tone: 220 Hz with 5 Hz vibrato (+-15 Hz) and a second harmonic
    vib = _make(tmp_path, "vib.wav", "-f", "lavfi", "-i",
                "aevalsrc='0.5*sin(2*PI*220*t+3*sin(2*PI*5*t))+0.25*sin(4*PI*220*t+6*sin(2*PI*5*t))':s=44100:d=4")
    v = _audio(vib).data["voice"]
    assert abs(v["median_f0_hz"] - 220) < 8 and v["voiced_share"] > 0.8
    assert 1.0 < v["pitch_spread_semitones"] < 4


def test_describe_audio_of_video_and_stereo(media, tmp_path):
    rep = _audio(media["video"]).data  # the fixture clip carries a 440 Hz tone
    assert abs(rep["voice"]["median_f0_hz"] - 440) < 10 and rep["description"]
    wide = _make(tmp_path, "wide.wav", "-f", "lavfi", "-i", "anoisesrc=d=3:a=0.2:seed=1", "-f", "lavfi", "-i", "anoisesrc=d=3:a=0.2:seed=2",
                 "-filter_complex", "[0][1]join=inputs=2:channel_layout=stereo")
    st = _audio(wide).data["stereo"]
    assert st["channels"] == 2 and abs(st["correlation"]) < 0.2 and st["label"] == "wide"
    with pytest.raises(OpError):
        _audio(media["image"])
