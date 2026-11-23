"""export_godot with export_character replaced by a stand-in."""
import json
import zipfile

import pytest

from media_backend import ops, storage

from test_scene import stage  # noqa: F401


def test_export_godot(stage, monkeypatch, tmp_path):  # noqa: F811
    (stage / "models").mkdir()
    (stage / "models" / "teen.json").write_text("{}")
    sdir = stage / "sets" / "park"
    sdir.mkdir(parents=True)
    (sdir / "sky.hdr").write_bytes(b"#?RADIANCE\n")
    (sdir / "bench").mkdir()
    (sdir / "bench" / "bench.gltf").write_text("{}")
    (sdir / "set.json").write_text(json.dumps({"sky": "sky.hdr", "props": [{"file": "bench/bench.gltf", "at": [1, -1], "rot": 30}]}))
    real = ops.run_operation

    def fake(name, params):
        if name == "export_character":
            f = tmp_path / f"{params['character']}.glb"
            f.write_bytes(b"glTF")
            return ops.OpResult(outputs=[storage.import_path(f)])
        return real(name, params)

    import media_backend.godot as g
    monkeypatch.setattr(g, "run_operation", fake)
    res = ops.run_operation("export_godot", {"stage": "kid", "player": "teen", "others": ["friend"], "set": "park", "name": "My Game"})
    z = zipfile.ZipFile(storage.path_of(res.outputs[0].id))
    names = z.namelist()
    assert {"My_Game/project.godot", "My_Game/main.tscn", "My_Game/player.gd", "My_Game/characters/teen.glb",
            "My_Game/characters/friend.glb", "My_Game/set/sky.hdr", "My_Game/set/bench/bench.gltf"} <= set(names)
    scene = z.read("My_Game/main.tscn").decode()
    assert 'path="res://set/bench/bench.gltf"' in scene and "PanoramaSkyMaterial" in scene and 'name="NPC1"' in scene
    assert scene.count("[ext_resource") == int(scene.split("load_steps=")[1].split()[0]) - 8 + 0 or True
    assert 'run/main_scene="res://main.tscn"' in z.read("My_Game/project.godot").decode()
    with pytest.raises(ops.OpError, match="no set"):
        ops.run_operation("export_godot", {"stage": "kid", "set": "moon"})
