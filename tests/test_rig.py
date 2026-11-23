"""rig_model (Blender replaced by a stand-in) and bake_texture (real xatlas, tiny mesh)."""
import json
import struct
from pathlib import Path

import pytest

from media_backend import glbtools, ops, storage

FIX = Path(__file__).parent / "fixtures"


@pytest.fixture
def fake_rig(monkeypatch):
    monkeypatch.setenv("BUILDER_AUTORIG", str(FIX / "fake_autorig.py"))


def _model(tmp_path, name="hero.glb"):
    p = tmp_path / name
    p.write_bytes(struct.pack("<III", 0x46546C67, 2, 12))
    return storage.import_path(p).id


def test_rig_model(fake_rig, tmp_path):
    fid = _model(tmp_path)
    src = storage.path_of(fid).read_bytes()
    res = ops.run_operation("rig_model", {"file_id": fid, "height_m": 1.75, "facing": "back", "check": False})
    out = res.outputs[0]
    assert out.name == "hero_rigged.glb" and out.parent_ids == [fid] and len(res.outputs) == 1
    s = glbtools.summary(storage.path_of(out.id).read_bytes())
    assert s["skins"] == 1 and "mixamorig:LeftForeArm" in s["bones"] and len(s["bones"]) == 22
    assert res.data["height_m"] == 1.75 and res.data["weights"] == "heat" and res.data["joints_m"]["Hips"] == [0, 0, 0.95]
    assert storage.path_of(fid).read_bytes() == src


def test_rig_model_passes_options(fake_rig, tmp_path, monkeypatch):
    import subprocess

    seen = {}
    real = subprocess.run

    def spy(args, **kw):
        seen["args"] = args
        return real(args, **kw)

    monkeypatch.setattr(subprocess, "run", spy)
    ops.run_operation("rig_model", {"file_id": _model(tmp_path), "facing": "front", "weights": "nearest", "check": False})
    a = seen["args"]
    assert a[a.index("--facing") + 1] == "-y" and a[a.index("--weights") + 1] == "nearest" and "--height" not in a


def test_rig_model_errors(fake_rig, tmp_path):
    with pytest.raises(ops.OpError, match="needs a 3D model"):
        p = tmp_path / "a.wav"
        p.write_bytes(b"RIFF")
        ops.run_operation("rig_model", {"file_id": storage.import_path(p).id, "check": False})
    with pytest.raises(ops.OpError, match="rigging failed:\nno mesh"):
        ops.run_operation("rig_model", {"file_id": _model(tmp_path, "FAKE_FAIL.glb"), "check": False})
    with pytest.raises(ops.OpError, match="Head"):
        ops.run_operation("rig_model", {"file_id": _model(tmp_path, "FAKE_NOHEAD.glb"), "check": False})
    assert [f.name for f in storage.list_files() if "rigged" in f.name] == []          # failed runs leave nothing


def test_rig_check_skips_without_tools(fake_rig, tmp_path, monkeypatch):
    import shutil

    monkeypatch.setattr(shutil, "which", lambda name: None)
    res = ops.run_operation("rig_model", {"file_id": _model(tmp_path)})
    assert "skipped" in res.data["check"] and len(res.outputs) == 1


def _coloured_box(tmp_path):
    trimesh = pytest.importorskip("trimesh")
    pytest.importorskip("xatlas")
    import numpy as np

    m = trimesh.creation.box(extents=(1, 2, 1))
    m = m.subdivide()
    cols = np.zeros((len(m.vertices), 4), np.uint8)
    cols[:, 3] = 255
    cols[m.vertices[:, 1] > 0, 0] = 255          # top half red, bottom half blue
    cols[m.vertices[:, 1] <= 0, 2] = 255
    m.visual = trimesh.visual.ColorVisuals(m, vertex_colors=cols)
    p = tmp_path / "box.glb"
    m.export(p)
    return storage.import_path(p).id


def test_bake_texture(tmp_path):
    import io

    import numpy as np
    from PIL import Image

    fid = _coloured_box(tmp_path)
    res = ops.run_operation("bake_texture", {"file_id": fid, "size": 256})
    out = storage.path_of(res.outputs[0].id)
    doc, bin_ = glbtools.read_glb(out.read_bytes())
    assert out.name == "box_textured.glb" and doc["images"][0]["mimeType"] == "image/jpeg"
    assert "TEXCOORD_0" in doc["meshes"][0]["primitives"][0]["attributes"] and "COLOR_0" not in doc["meshes"][0]["primitives"][0]["attributes"]
    bv = doc["bufferViews"][doc["images"][0]["bufferView"]]
    img = np.asarray(Image.open(io.BytesIO(bin_[bv.get("byteOffset", 0): bv.get("byteOffset", 0) + bv["byteLength"]])).convert("RGB")).astype(int)
    red = ((img[..., 0] > 180) & (img[..., 2] < 80)).mean()
    blue = ((img[..., 2] > 180) & (img[..., 0] < 80)).mean()
    assert red > 0.2 and blue > 0.2 and res.data["coverage"] > 0.3                       # both colours baked, charts fill the atlas


def test_bake_texture_needs_colours(tmp_path):
    trimesh = pytest.importorskip("trimesh")
    pytest.importorskip("xatlas")
    p = tmp_path / "plain.glb"
    trimesh.creation.box().export(p)
    with pytest.raises(ops.OpError, match="no vertex colours"):
        ops.run_operation("bake_texture", {"file_id": storage.import_path(p).id})


def test_bake_rasteriser_covers_triangle():
    np = pytest.importorskip("numpy")
    import importlib.util

    spec = importlib.util.spec_from_file_location("bake", Path(__file__).parents[1] / "tools" / "image3d" / "bake.py")
    b = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(b)
    uv = np.array([[0.0, 0.0], [1.0, 0.0], [0.0, 1.0]])
    r, c, t, w = b.rasterise(uv, np.array([[0, 1, 2]]), 16)
    assert 120 <= len(r) <= 140 and (t == 0).all() and np.allclose(w.sum(1), 1)          # about half of 16x16 texels
    img = np.zeros((4, 4, 3))
    filled = np.zeros((4, 4), bool)
    img[0, 0], filled[0, 0] = 200, True
    assert (b.dilate(img, filled, steps=8) == 200).all()
