"""direct_scene, make_anything and style_check with Claude stubbed and the stand-in renderer."""
import json

import pytest

from media_backend import director, ops

from test_scene import stage  # noqa: F401  (fixture: a stage + stand-in renderer)
from test_voice_finish import stub_speech  # noqa: F401


class FakeClaude:
    def __init__(self, answers):
        self.answers, self.calls = list(answers), []

    def __call__(self, system, content, max_tokens=3000):
        self.calls.append((system, content))
        return self.answers.pop(0)


PLAN = {"title": "Hello", "voice": "am_liam", "music": "calm", "look": "warm",
        "beats": [{"view": "face", "say": "Hi there."}, {"move": "wave", "hold": 1}, {"move": "backflip"}, {"walk_to": [9, 9, 9]}]}


def test_direct_scene_revises(stub_speech, stage, monkeypatch):  # noqa: F811
    pytest.importorskip("numpy")
    fake = FakeClaude([json.dumps(PLAN), '{"ok": false, "problems": ["too short"]}',
                       json.dumps({**PLAN, "beats": PLAN["beats"][:2] + [{"move": "dance", "hold": 1}]}), 'ok: {"ok": true, "problems": []}'])
    monkeypatch.setattr(director, "_ask", fake)
    res = ops.run_operation("direct_scene", {"request": "say hi and wave", "stage": "kid", "resolution": "640x360", "fps": 12})
    assert [r["verdict"]["ok"] for r in res.data["reviews"]] == [False, True]
    moves = [b.get("move") for b in res.data["plan"]["beats"]]
    assert "backflip" not in moves and "dance" in moves                       # unknown moves are dropped
    assert "too short" in fake.calls[2][1]                                     # the fix was asked for with the problem
    assert any(c.get("type") == "image" for c in fake.calls[1][1])             # the review looked at frames
    assert ops.media_info(res.outputs[0].id)["has_audio"]


def test_direct_scene_needs_a_key(stage, monkeypatch):  # noqa: F811
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(ops.OpError, match="ANTHROPIC_API_KEY"):
        ops.run_operation("direct_scene", {"request": "say hi", "stage": "kid"})


def test_make_anything_routes(monkeypatch, media):
    pytest.importorskip("numpy")
    monkeypatch.setattr(director, "_ask", FakeClaude(['{"tool": "make_music", "params": {"mood": "calm", "length_s": 5}, "why": "music"}']))
    res = ops.run_operation("make_anything", {"request": "some calm music"})
    assert res.data["tool"] == "make_music" and ops.media_info(res.outputs[0].id)["has_audio"]
    monkeypatch.setattr(director, "_ask", FakeClaude(['{"tool": "craft", "params": {}, "why": "a game"}']))
    res = ops.run_operation("make_anything", {"request": "a snake game"})
    assert res.outputs == [] and "craft_build_start" in res.data["next"]


def test_style_check(stage, tmp_path, monkeypatch):  # noqa: F811
    monkeypatch.setenv("BUILDER_APPROVED_DIR", str(tmp_path / "approved"))
    with pytest.raises(ops.OpError, match="nothing approved"):
        ops.run_operation("style_check", {"stage": "kid"})
    ok = ops.run_operation("style_check", {"stage": "kid", "approve": True})
    assert ok.data["pass"] and sorted(p.name for p in (tmp_path / "approved" / "kid").iterdir()) == ["body.png", "face.png", "three_quarter.png"]
    res = ops.run_operation("style_check", {"stage": "kid"})
    assert res.data["pass"] and res.data["views"]["face"]["ssim"] > 0.999
