"""image_to_3d with TripoSR replaced by a stand-in worker (no torch, no downloads)."""
import importlib.util
import struct
from pathlib import Path

import pytest

from media_backend import image3d, ops, storage, voice

FIX = Path(__file__).parent / "fixtures"
TOOL = Path(__file__).parents[1] / "tools" / "image3d" / "triposr.py"


@pytest.fixture
def fake_model(tmp_path, monkeypatch):
    monkeypatch.setenv("IMAGE3D_WORKER", str(FIX / "fake_image3d.py"))
    monkeypatch.setattr(image3d, "ensure_model", lambda: (tmp_path / "code", tmp_path / "weights"))


def _image(tmp_path, name="pot.png"):
    p = tmp_path / name
    p.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\0" * 64)   # the stand-in never decodes it
    return storage.import_path(p).id


def test_image_to_3d_glb(fake_model, tmp_path):
    fid = _image(tmp_path)
    src = storage.path_of(fid).read_bytes()
    res = ops.run_operation("image_to_3d", {"file_id": fid, "resolution": 128, "max_faces": 5000, "foreground_ratio": 0.9})
    out = storage.path_of(res.outputs[0].id)
    b = out.read_bytes()
    assert out.name == "pot_3d.glb" and struct.unpack_from("<I", b)[0] == 0x46546C67     # glTF binary magic
    args = res.data["args"]
    assert args["--mc"] == "128" and args["--max-faces"] == "5000" and args["--fg"] == "0.9" and "--no-rembg" not in args
    assert res.data["faces"] == 1 and res.data["engine"].startswith("TripoSR")
    assert storage.path_of(fid).read_bytes() == src                                       # input untouched
    assert res.outputs[0].parent_ids == [fid]


def test_image_to_3d_obj_no_rembg(fake_model, tmp_path):
    res = ops.run_operation("image_to_3d", {"file_id": _image(tmp_path), "format": "obj", "remove_background": False})
    out = storage.path_of(res.outputs[0].id)
    assert out.suffix == ".obj" and out.read_text().startswith("v ")
    assert res.data["args"]["--no-rembg"] is True and "--max-faces" not in res.data["args"]


def test_image_to_3d_errors(fake_model, tmp_path):
    with pytest.raises(ops.OpError, match="needs an image"):
        ops.run_operation("image_to_3d", {"file_id": storage.import_path(_wav(tmp_path)).id})
    with pytest.raises(ops.OpError, match="needs torch: pip install"):
        ops.run_operation("image_to_3d", {"file_id": _image(tmp_path, "FAKE_MISSING.png")})
    with pytest.raises(ops.OpError, match="TripoSR failed:\nno object found"):
        ops.run_operation("image_to_3d", {"file_id": _image(tmp_path, "FAKE_FAIL.png")})
    assert [f.name for f in storage.list_files() if f.name.endswith(".glb")] == []           # failed runs leave nothing behind
    with pytest.raises(Exception):
        ops.run_operation("image_to_3d", {"file_id": "x", "resolution": 4096})


def _wav(tmp_path):
    p = tmp_path / "a.wav"
    p.write_bytes(b"RIFF")
    return p


def test_missing_packages_message(tmp_path, monkeypatch):
    monkeypatch.delenv("IMAGE3D_WORKER", raising=False)
    monkeypatch.setattr(image3d, "missing_packages", lambda: ["rembg", "torch"])
    monkeypatch.setattr(image3d, "ensure_model", lambda: pytest.fail("must not download without the packages"))
    with pytest.raises(ops.OpError, match=r"needs rembg, torch: pip install \"builder\[image3d\]\""):
        ops.run_operation("image_to_3d", {"file_id": _image(tmp_path)})


def test_code_download_is_checksummed(monkeypatch):
    monkeypatch.setattr(voice, "_fetch", lambda url, dest, tries=5: (dest.parent.mkdir(parents=True, exist_ok=True), dest.write_text("tampered")))
    with pytest.raises(ops.OpError, match="didn't match the expected checksum"):
        image3d.ensure_model()
    root = voice.models_dir() / "triposr"
    assert not (root / "code.part").exists() and not list(root.glob("code-*"))


def test_vit_key_rename():
    spec = importlib.util.spec_from_file_location("triposr_worker", TOOL)
    w = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(w)
    old = {"image_tokenizer.model.encoder.layer.3.attention.attention.query.weight": 1,
           "image_tokenizer.model.encoder.layer.3.attention.output.dense.bias": 2,
           "image_tokenizer.model.encoder.layer.3.output.dense.weight": 3,
           "image_tokenizer.model.encoder.layer.3.intermediate.dense.weight": 4,
           "image_tokenizer.model.encoder.layer.3.layernorm_before.weight": 5, "decoder.x": 6}
    new = w.vit_keys(old, {"image_tokenizer.model.layers.0.attention.q_proj.weight"})
    assert new == {"image_tokenizer.model.layers.3.attention.q_proj.weight": 1, "image_tokenizer.model.layers.3.attention.o_proj.bias": 2,
                   "image_tokenizer.model.layers.3.mlp.fc2.weight": 3, "image_tokenizer.model.layers.3.mlp.fc1.weight": 4,
                   "image_tokenizer.model.layers.3.layernorm_before.weight": 5, "decoder.x": 6}
    assert w.vit_keys(old, {"image_tokenizer.model.encoder.layer.0.attention.attention.query.weight"}) is old   # transformers 4: as is
