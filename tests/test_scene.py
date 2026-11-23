"""scene_video (speech stubbed, renderer replaced by a stand-in) and the morph-target merge tool."""
import json
import struct
import subprocess
import sys
from pathlib import Path

import pytest

from media_backend import ops, scene, voice

from test_voice_finish import stub_speech  # noqa: F401  (fixture)

FIX = Path(__file__).parent / "fixtures"


@pytest.fixture
def stage(tmp_path, monkeypatch):
    d = tmp_path / "stages" / "kid"
    d.mkdir(parents=True)
    (d / "index.html").write_text("<title>stage</title>")
    monkeypatch.setenv("BUILDER_STAGES_DIR", str(tmp_path / "stages"))
    monkeypatch.setenv("SCENE_RENDERER", str(FIX / "fake_scene_render.py"))
    monkeypatch.setattr(voice, "mouth_cues", lambda words, duration, lang="en-us": [{"start": 0.0, "end": duration, "value": "B"}])
    return d


def test_scene_video(stub_speech, stage):  # noqa: F811
    pytest.importorskip("numpy")
    res = ops.run_operation("scene_video", {"stage": "kid", "resolution": "640x360", "fps": 12, "look": "film", "captions": True,
                                            "beats": [{"view": "face", "say": "Hi there."}, {"move": "wave", "hold": 1.0},
                                                      {"walk_to": [1.0, 0.0]}]})
    info = ops.media_info(res.outputs[0].id)
    assert info["has_video"] and info["has_audio"] and info["video"]["width"] == 640
    tl = res.data["timeline"]
    assert tl[0]["say"] == "l0" and tl[0]["view"] == "face" and tl[1]["move"] == "wave" and tl[2]["walkTo"] == [1.0, 0.0]
    assert tl[1]["t"] > tl[0]["t"] and tl[2]["t"] >= tl[1]["t"] + 1.0
    assert res.data["lines"] == 1 and res.data["footsteps"] == 2
    assert abs(info["duration_s"] - res.data["duration_s"]) < 0.3


def test_scene_unknown_stage(stage):
    with pytest.raises(ops.OpError, match="no stage called"):
        ops.run_operation("scene_video", {"stage": "nobody", "beats": [{"move": "wave"}]})
    assert scene.list_stages() == ["kid"]


def _glb(path, positions):
    """A one-mesh GLB with the given triangle positions."""
    import numpy as np

    pos = np.asarray(positions, "float32")
    bin_ = pos.tobytes()
    js = {"asset": {"version": "2.0"}, "buffers": [{"byteLength": len(bin_)}], "bufferViews": [{"buffer": 0, "byteOffset": 0, "byteLength": len(bin_)}],
          "accessors": [{"bufferView": 0, "componentType": 5126, "count": len(pos), "type": "VEC3", "min": pos.min(0).tolist(), "max": pos.max(0).tolist()}],
          "meshes": [{"name": "face", "primitives": [{"attributes": {"POSITION": 0}}]}], "nodes": [{"mesh": 0}], "scenes": [{"nodes": [0]}]}
    j = json.dumps(js).encode()
    j += b" " * (-len(j) % 4)
    path.write_bytes(struct.pack("<III", 0x46546C67, 2, 28 + len(j) + len(bin_)) + struct.pack("<II", len(j), 0x4E4F534A) + j
                     + struct.pack("<II", len(bin_), 0x004E4942) + bin_)


def test_morphs_merge_sparse(tmp_path):
    np = pytest.importorskip("numpy")
    base = [[0, 0, 0], [1, 0, 0], [0, 1, 0], [5, 5, 5]]
    _glb(tmp_path / "base.glb", base)
    _glb(tmp_path / "open.glb", [[0, 0, 0], [1, -0.02, 0], [0, 1, 0], [5, 5, 5]])
    _glb(tmp_path / "same.glb", base)
    (tmp_path / "v.json").write_text(json.dumps({"viseme_D": str(tmp_path / "open.glb"), "blink_L": str(tmp_path / "same.glb")}))
    tool = Path(__file__).parents[1] / "tools" / "makehuman" / "morphs.py"
    subprocess.run([sys.executable, "-I", str(tool), str(tmp_path / "base.glb"), str(tmp_path / "v.json"), str(tmp_path / "out.glb")], check=True, capture_output=True)
    b = (tmp_path / "out.glb").read_bytes()
    jl = struct.unpack_from("<I", b, 12)[0]
    js = json.loads(b[20:20 + jl])
    mesh = js["meshes"][0]
    assert mesh["extras"]["targetNames"] == ["viseme_D"]          # an expression that moves nothing is left out
    acc = js["accessors"][mesh["primitives"][0]["targets"][0]["POSITION"]]
    assert acc["count"] == 4 and acc["sparse"]["count"] == 1      # only the one moved vertex is stored
    bin_ = b[20 + jl + 8:]
    sp = acc["sparse"]
    idx = np.frombuffer(bin_, "uint32", 1, js["bufferViews"][sp["indices"]["bufferView"]]["byteOffset"])
    val = np.frombuffer(bin_, "float32", 3, js["bufferViews"][sp["values"]["bufferView"]]["byteOffset"])
    assert idx[0] == 1 and np.allclose(val, [0, -0.02, 0])


def test_visual_diff(media):
    same = ops.run_operation("visual_diff", {"file_id": media["video"], "reference_id": media["video"], "at": 1.0})
    assert same.data["ssim"] > 0.999 and same.data["pass"]
    diff = ops.run_operation("visual_diff", {"file_id": media["image"], "reference_id": media["image2"]})
    assert not diff.data["pass"] and diff.data["ssim"] < 0.97
    assert ops.media_info(diff.outputs[0].id)["video"]["width"] == 200
    with pytest.raises(ops.OpError):
        ops.run_operation("visual_diff", {"file_id": media["audio"], "reference_id": media["image"]})


def test_render_still_needs_blender(tmp_path, monkeypatch):
    monkeypatch.setenv("BUILDER_MAKEHUMAN_DIR", str(tmp_path / "mh"))
    with pytest.raises(ops.OpError, match="aren't installed"):
        ops.run_operation("render_still", {"character": "teen"})
