import pytest

from media_backend import ops, storage
from media_backend.jobs import get_queue


def run(name, **params):
    res = ops.run_operation(name, params)
    for o in res.outputs:
        assert storage.path_of(o.id).stat().st_size > 0
    return res


def info(file_id):
    return ops.media_info(file_id)


def test_probe(media):
    d = run("probe", file_id=media["video"]).data
    assert d["has_video"] and d["has_audio"] and d["video"]["width"] == 320
    assert 2.9 < d["duration_s"] < 3.2


@pytest.mark.parametrize("fmt", ["mp4", "webm", "mp3", "wav", "flac", "ogg"])
def test_convert(media, fmt):
    out = run("convert", file_id=media["video"], format=fmt).outputs[0]
    assert out.name.endswith("." + fmt)
    assert info(out.id)["has_video"] == (fmt in ("mp4", "webm"))


def test_trim_precise_and_copy(media):
    a = run("trim", file_id=media["video"], start=0.5, end=2.0).outputs[0]
    assert 1.3 < info(a.id)["duration_s"] < 1.7
    b = run("trim", file_id=media["video"], start=1, duration=1, precise=False).outputs[0]
    assert info(b.id)["duration_s"] > 0


def test_trim_rejects_bad_range(media):
    with pytest.raises(Exception):
        run("trim", file_id=media["video"], start=2, end=1)


def test_audio_ops(media):
    mp3 = run("extract_audio", file_id=media["video"], format="mp3").outputs[0]
    assert not info(mp3.id)["has_video"]
    run("normalize_audio", file_id=media["audio"], target_lufs=-16)
    run("normalize_audio", file_id=media["video"])
    fast = run("change_speed", file_id=media["audio"], factor=2).outputs[0]
    assert 0.9 < info(fast.id)["duration_s"] < 1.1
    slow = run("change_speed", file_id=media["video"], factor=0.5).outputs[0]
    assert 5.5 < info(slow.id)["duration_s"] < 6.5
    run("waveform", file_id=media["audio"])
    run("fade", file_id=media["audio"], fade_in=0.3, fade_out=0.3)


def test_video_ops(media):
    r = run("resize", file_id=media["video"], width=160).outputs[0]
    assert info(r.id)["video"]["width"] == 160 and info(r.id)["video"]["height"] == 120
    assert run("thumbnail", file_id=media["video"], time=1).outputs[0].mime_type == "image/png"
    assert len(run("frames", file_id=media["video"], count=3).outputs) == 3
    m = run("mute", file_id=media["video"]).outputs[0]
    assert not info(m.id)["has_audio"]
    assert run("make_gif", file_id=media["video"], duration=1, width=160).outputs[0].mime_type == "image/gif"
    run("fade", file_id=media["video"])
    run("add_text", file_id=media["video"], text="It's a test: 100% \"quoted\"", position="top", start=0.5, end=2)


def test_concat_and_soundtrack(media):
    muted = run("mute", file_id=media["video"]).outputs[0].id
    joined = run("concat", file_ids=[media["video"], muted]).outputs[0]
    d = info(joined.id)
    assert d["has_audio"] and 5.5 < d["duration_s"] < 6.5
    aj = run("concat", file_ids=[media["audio"], media["audio"]]).outputs[0]
    assert 3.8 < info(aj.id)["duration_s"] < 4.2
    for mode in ("replace", "mix"):
        out = run("replace_audio", video_id=media["video"], audio_id=media["audio"], mode=mode, loop_audio=True).outputs[0]
        assert 2.8 < info(out.id)["duration_s"] < 3.3


def test_animation(media):
    frames = [media["image"], media["image2"]]
    anim = run("images_to_video", file_ids=frames, fps=4, loops=3, audio_id=media["audio"])
    d = info(anim.outputs[0].id)
    assert anim.data["frames"] == 6 and 1.4 < d["duration_s"] < 1.7 and d["has_audio"]
    show = run("images_to_video", file_ids=frames, seconds_per_image=1)
    assert 1.9 < info(show.outputs[0].id)["duration_s"] < 2.2
    for effect in ("zoom_in", "pan_left", "bounce", "wobble"):
        out = run("animate_image", file_id=media["image"], effect=effect, duration=1, width=320, height=240, fps=10)
        d = info(out.outputs[0].id)
        assert d["video"]["width"] == 320 and 0.9 < d["duration_s"] < 1.2


def test_errors_are_clean(media):
    with pytest.raises(ops.OpError, match="No file"):
        run("probe", file_id="000000000000")
    with pytest.raises(ops.OpError, match="no audio"):
        run("extract_audio", file_id=media["image"])
    with pytest.raises(ops.OpError, match="Unknown operation"):
        ops.run_operation("explode", {})
    before = len(storage.list_files())
    with pytest.raises(ops.OpError):
        run("concat", file_ids=[media["video"], media["audio"]])
    assert len(storage.list_files()) == before  # failed ops leave nothing behind


def test_job_queue(media):
    q = get_queue()
    job = q.run_and_wait("thumbnail", {"file_id": media["video"]})
    assert job.status == "succeeded" and job.outputs
    bad = q.run_and_wait("thumbnail", {"file_id": "000000000000"})
    assert bad.status == "failed" and "No file" in bad.error
