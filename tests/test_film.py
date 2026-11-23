"""make_film, dub_scene, dub_video and make_short (speech, Claude and the renderer stubbed)."""
import json

import pytest

from media_backend import director, ops, voice

from test_scene import stage  # noqa: F401
from test_voice_finish import stub_speech  # noqa: F401


def fake_ask(system, content, max_tokens=3000):
    lines = json.loads(content.split("\n")[-1]) if isinstance(content, str) else []
    return json.dumps({"lines": [f"[es] {x}" for x in lines]})


def test_make_film(stub_speech, stage):  # noqa: F811
    pytest.importorskip("numpy")
    scene = {"stage": "kid", "beats": [{"say": "Hi there."}, {"move": "wave", "hold": 1.0}]}
    res = ops.run_operation("make_film", {"scenes": [scene, scene], "title": "Two Friends", "resolution": "640x360", "fps": 12,
                                          "music": "calm", "transition": "dissolve"})
    info = ops.media_info(res.outputs[0].id)
    assert info["has_video"] and info["has_audio"] and info["video"]["width"] == 640
    assert res.data["scenes"] == 2 and res.outputs[1].name.endswith(".fcpxml")
    assert res.data["duration_s"] > 3 + 4.5                       # title + credits + scenes


def test_dub_scene(stub_speech, stage, monkeypatch):  # noqa: F811
    pytest.importorskip("numpy")
    monkeypatch.setattr(director, "_ask", fake_ask)
    sc = {"stage": "kid", "voice": "am_puck", "resolution": "640x360", "fps": 12, "beats": [{"say": "Hello friend."}, {"move": "wave"}]}
    res = ops.run_operation("dub_scene", {"scene": sc, "language": "es"})
    assert res.data["lines"] == ["[es] Hello friend."] and res.data["language"] == "es"


def test_dub_video_and_short(stub_speech, media, monkeypatch):
    pytest.importorskip("numpy")
    monkeypatch.setattr(director, "_ask", fake_ask)
    res = ops.run_operation("dub_video", {"file_id": media["video"], "language": "fr"})
    assert res.data["sentences"] == 1 and ops.media_info(res.outputs[0].id)["has_audio"]
    short = ops.run_operation("make_short", {"file_id": media["video"], "captions": True})
    v = ops.media_info(short.outputs[0].id)["video"]
    assert v["height"] == 1920 and v["width"] == 1080
