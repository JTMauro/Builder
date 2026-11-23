"""Resolve timeline export, reframe, watch folders and the Unreal bridge (with a stand-in editor)."""
import json
import stat
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from media_backend import ops, storage, unreal, watch

FAKE = Path(__file__).parent / "fixtures" / "fake_unreal.py"


def test_reframe_shapes(media):
    v = ops.run_operation("reframe", {"file_id": media["video"], "aspect": "9:16", "height": 640}).outputs[0]
    i = ops.media_info(v.id)["video"]
    assert (i["width"], i["height"]) == (360, 640)
    s = ops.run_operation("reframe", {"file_id": media["video"], "aspect": "1:1", "mode": "blur", "height": 320}).outputs[0]
    i = ops.media_info(s.id)
    assert (i["video"]["width"], i["video"]["height"]) == (320, 320) and i["has_audio"]


def test_timeline_export_fcpxml(media):
    res = ops.run_operation("timeline_export", {
        "name": "Teen cut", "fps": 25,
        "clips": [{"file_id": media["video"], "start": 0.5, "end": 2.0, "name": "walk in"}, {"file_id": media["video"]}],
        "music_id": media["audio"]})
    path = storage.path_of(res.outputs[0].id)
    assert path.suffix == ".fcpxml"
    root = ET.parse(path).getroot()
    assert root.tag == "fcpxml" and root.get("version") == "1.9"
    clips = root.findall(".//spine/asset-clip")
    assert [c.get("name") for c in clips] == ["walk in", "clip"]
    assert clips[0].get("start") == "13/25s" and clips[0].get("duration") == "38/25s"   # 0.5s = frame 12.5 -> 13; 1.5s = 37.5 -> 38
    assert clips[1].get("offset") == "38/25s"
    music = clips[0].find("asset-clip")
    assert music is not None and music.get("lane") == "-1"
    assets = root.findall(".//resources/asset")
    assert all(a.get("src").startswith("file:") for a in assets)
    assert res.data["clips"] == 2 and res.data["width"] == 320


def test_timeline_export_rejects_audio_clip(media):
    with pytest.raises(ops.OpError):
        ops.run_operation("timeline_export", {"clips": [{"file_id": media["audio"]}]})


def test_watch_folder_social(media, tmp_path):
    folder = tmp_path / "resolve_out"
    folder.mkdir()
    (folder / "old.mp4").write_bytes(storage.path_of(media["video"]).read_bytes())
    entry = watch.add_folder(str(folder), "social", owner="tester")
    watch.stop()
    assert watch.list_folders()[0]["id"] == entry["id"]
    assert watch.scan_once(settle_s=0) == []                    # files already there are left alone
    (folder / "final.mp4").write_bytes(storage.path_of(media["video"]).read_bytes())
    [result] = watch.scan_once(settle_s=0)
    assert not result["errors"], result["errors"]
    made = sorted(Path(m).name for m in result["made"])
    assert made == ["final_preview.gif", "final_square.mp4", "final_thumbnail.png", "final_vertical.mp4", "final_web.mp4"]
    assert watch.scan_once(settle_s=0) == []                    # each file only once
    assert watch.remove_folder(entry["id"]) and watch.list_folders() == []


def test_watch_folder_must_be_allowed(tmp_path):
    with pytest.raises(watch.WatchError):
        watch.add_folder("/", "social")
    with pytest.raises(watch.WatchError):
        watch.add_folder(str(tmp_path), "nope")


@pytest.fixture
def fake_unreal(tmp_path, monkeypatch):
    exe = tmp_path / "UnrealEditor"
    exe.write_text(f"#!/bin/sh\nexec {sys.executable} {FAKE} \"$@\"\n")
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    proj = tmp_path / "Film" / "Film.uproject"
    proj.parent.mkdir()
    proj.write_text(json.dumps({"Plugins": [{"Name": "MovieRenderPipeline", "Enabled": True},
                                            {"Name": "PythonScriptPlugin", "Enabled": True},
                                            {"Name": "MetaHumanCharacter", "Enabled": True}]}))
    monkeypatch.setenv("UNREAL_EDITOR", str(exe))
    monkeypatch.setenv("UNREAL_PROJECT", str(proj))
    return proj


def test_unreal_status(fake_unreal, monkeypatch):
    s = ops.run_operation("unreal_status", {}).data
    assert s["ready"] and s["projects"][0]["name"] == "Film" and s["projects"][0]["metahuman_plugin"]
    fake_unreal.write_text(json.dumps({"Plugins": []}))
    s = ops.run_operation("unreal_status", {}).data
    assert not s["ready"] and "Movie Render Queue" in s["projects"][0]["missing_plugins"]
    assert any("Edit > Plugins" in step for step in s["next_steps"])


@pytest.mark.parametrize("encode,ext", [("prores", ".mov"), ("h264", ".mp4")])
def test_unreal_render(fake_unreal, encode, ext):
    res = ops.run_operation("unreal_render", {"sequence": "/Game/Cine/TeenWave", "width": 320, "height": 180, "fps": 24, "encode": encode})
    out = res.outputs[0]
    assert out.name == f"TeenWave_320x180{ext}" and res.data["frames"] == 6
    info = ops.media_info(out.id)
    assert info["video"]["width"] == 320 and 0.2 < info["duration_s"] < 0.3


def test_unreal_render_errors(fake_unreal, monkeypatch):
    monkeypatch.setenv("FAKE_UNREAL_FAIL", "1")
    with pytest.raises(ops.OpError, match="fake failure"):
        ops.run_operation("unreal_render", {"sequence": "/Game/S", "width": 320, "height": 180})
    monkeypatch.delenv("FAKE_UNREAL_FAIL")
    fake_unreal.write_text(json.dumps({"Plugins": []}))
    with pytest.raises(ops.OpError, match="Movie Render Queue"):
        ops.run_operation("unreal_render", {"sequence": "/Game/S"})


def test_unreal_script_needs_permission(fake_unreal, monkeypatch):
    with pytest.raises(ops.OpError, match="UNREAL_ALLOW_SCRIPTS"):
        ops.run_operation("unreal_script", {"code": "print('hi')"})
    monkeypatch.setenv("UNREAL_ALLOW_SCRIPTS", "on")
    out = ops.run_operation("unreal_script", {"code": "import unreal\nprint('hi')"}).data["output"]
    assert "print('hi')" in out


def test_unreal_tools_not_in_plugin_mode():
    from media_backend.mcp_server import build_server

    names = lambda s: {t.name for t in s._tool_manager.list_tools()}  # noqa: E731
    assert "unreal_render" in names(build_server(media_only=False))
    assert "unreal_render" not in names(build_server(media_only=True))
    assert "watch_folder_add" in names(build_server(media_only=False))
