"""make_character, the character library and export_character, with Claude, MakeHuman and the exporter stubbed."""
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from media_backend import characters, ops, storage

FIX = Path(__file__).parent / "fixtures"


class FakeClaude:
    def __init__(self, *answers):
        self.answers, self.calls = list(answers), []

    def __call__(self, system, content, max_tokens=2000):
        self.calls.append((system, content))
        return self.answers.pop(0)


@pytest.fixture
def studio(tmp_path, monkeypatch):
    """A stage with the teen, stand-in MakeHuman job and exporter, an empty library."""
    stages = tmp_path / "stages"
    s = stages / "teen"
    (s / "models").mkdir(parents=True)
    (s / "index.html").write_text("<title>stage</title>")
    (s / "studio.html").write_text("<title>studio</title>")
    for m in ("teen", "friend", "light"):
        (s / "models" / f"{m}.json").write_text("{}")
    (s / "sets" / "park").mkdir(parents=True)
    (s / "sets" / "park" / "set.json").write_text("{}")
    monkeypatch.setenv("BUILDER_STAGES_DIR", str(stages))
    monkeypatch.setenv("BUILDER_MAKEHUMAN_DIR", str(tmp_path / "mh"))
    monkeypatch.setenv("BUILDER_LIBRARY_DIR", str(tmp_path / "library"))
    monkeypatch.setenv("BUILDER_MAKEHUMAN_JOB", str(FIX / "fake_makehuman_job.py"))
    monkeypatch.setenv("CHARACTER_EXPORTER", str(FIX / "fake_exporter.py"))
    return s


def seen(tmp_path, name):
    return json.loads((tmp_path / "mh" / "mhgen" / f"{name}_params_seen.json").read_text())


# ---------------------------------------------------------------- validation


def test_validation_falls_back(studio):
    clean, notes = characters.validate_params({"age": 140, "gender": 0, "hair": "mohawk", "eyes": "blue", "outfit": "gym clothes",
                                               "shoes": "boots", "topRGB": [0.1, 0.2, 0.3], "skinRGB": [1.4, 0.5], "weight": "heavy",
                                               "shape": {"neck/neck-scale-vert-incr": 0.4, "../../etc": 1}, "secret": "x"})
    assert clean["age"] == 90 and clean["hair"] == "long01"          # unknown hair -> a sensible default for a woman
    assert clean["eyes"] == "blue_eye" and clean["outfit"] == "female_sportsuit01" and clean["shoes"] == "shoes05"
    assert "topRGB" not in clean and "skinRGB" not in clean and "weight" not in clean and "secret" not in clean
    assert clean["shape"] == {"neck/neck-scale-vert-incr": 0.4} and clean["morphs"] == "talk"
    assert any("mohawk" in n for n in notes) and any("boots" in n for n in notes)
    near, _ = characters.validate_params({"gender": 1, "hair": "braids", "outfit": "work suit"})
    assert near["hair"] == "braid01" and near["outfit"] == "male_worksuit01"
    tee, _ = characters.validate_params({"gender": 1, "topRGB": [0.1, 0.2, 0.3]})
    assert tee["outfit"] == "male_casualsuit06" and tee["topRGB"] == [0.1, 0.2, 0.3]


def test_validation_uses_installed_assets(studio, tmp_path):
    a = tmp_path / "mh" / "mhassets"
    for d in ("hair/short02", "hair/bob01"):
        (a / d).mkdir(parents=True)
        (a / d / (Path(d).name + ".mhclo")).write_text("")
    for c in ("male_casualsuit06", "female_casualsuit01", "shoes05", "fedora01"):
        (a / "clothes" / c).mkdir(parents=True)
    (a / "eyes" / "materials").mkdir(parents=True)
    (a / "eyes" / "materials" / "green_eye.png").write_bytes(b"")
    have = characters.assets()
    assert have["hair"] == ["bob01", "short02"] and have["outfits"] == ["female_casualsuit01", "male_casualsuit06"]
    assert have["shoes"] == ["shoes05"] and have["eyes"] == ["green_eye"]
    clean, notes = characters.validate_params({"gender": 0, "hair": "braid01", "eyes": "blue", "outfit": "fedora01"})
    assert clean["hair"] == "bob01" and clean["eyes"] == "green_eye" and clean["outfit"] == "female_casualsuit01"


# ---------------------------------------------------------------- make_character


def test_make_character_from_a_description(studio, tmp_path, monkeypatch):
    answer = {"name": "grandpa", "voice": "bm_george", "persona": "Grumpy but kind.",
              "params": {"age": 78, "gender": 1, "hair": "short04", "outfit": "male_worksuit01", "eyes": "grey_eye", "shoes": "shoes02"}}
    fake = FakeClaude("Here you go: " + json.dumps(answer))
    monkeypatch.setattr(characters, "_ask", fake)
    res = ops.run_operation("make_character", {"description": "a grumpy grandpa in a work suit"})
    assert "short04" in fake.calls[0][0] and "am_onyx" in fake.calls[0][0]       # assets and voices offered to Claude
    assert res.data["name"] == "grandpa" and res.data["voice"] == "bm_george" and res.data["stages"] == ["teen"]
    assert (studio / "models" / "grandpa.json").exists()
    assert seen(tmp_path, "grandpa")["outfit"] == "male_worksuit01" and seen(tmp_path, "grandpa")["morphs"] == "talk"
    assert storage.path_of(res.outputs[0].id).read_bytes().startswith(b"glTF")
    lib = json.loads((tmp_path / "library" / "characters" / "grandpa.json").read_text())
    assert lib["persona"] == "Grumpy but kind." and lib["params"]["hair"] == "short04"
    with pytest.raises(ops.OpError, match="already a character"):
        ops.run_operation("make_character", {"name": "grandpa", "params": {"age": 70}})


def test_make_character_with_params_skips_claude(studio, tmp_path, monkeypatch):
    monkeypatch.setattr(characters, "_ask", lambda *a, **k: pytest.fail("Claude was asked"))
    res = ops.run_operation("make_character", {"name": "coach", "voice": "am_onyx",
                                               "params": {"age": 45, "gender": 1, "hair": "buzzcut", "outfit": "male_casualsuit06"}})
    assert res.data["voice"] == "am_onyx" and seen(tmp_path, "coach")["hair"] == "short01"
    assert any("buzzcut" in n for n in res.data["notes"])
    only = ops.run_operation("make_character", {"name": "aunt", "params": {"age": 40, "gender": 0}, "build": False})
    assert only.outputs == [] and only.data["voice"] == "af_heart" and not (studio / "models" / "aunt.json").exists()


def test_make_character_refuses_real_people(studio, monkeypatch):
    monkeypatch.setattr(characters, "_ask", lambda *a, **k: pytest.fail("Claude was asked"))
    for d in ("a guy who looks just like Tom Hanks", "make me as Harry Potter", "a famous pop star"):
        with pytest.raises(ops.OpError, match="real people or famous"):
            ops.run_operation("make_character", {"description": d})
    monkeypatch.setattr(characters, "_ask", FakeClaude('{"refuse": "that is a real footballer"}'))
    with pytest.raises(ops.OpError, match="real footballer"):
        ops.run_operation("make_character", {"description": "the number 7 from my favourite team"})


def test_make_character_errors(studio, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(ops.OpError, match="ANTHROPIC_API_KEY"):
        ops.run_operation("make_character", {"description": "a cheerful baker"})
    with pytest.raises(ops.OpError, match="description or params"):
        ops.run_operation("make_character", {"name": "x"})
    with pytest.raises(ops.OpError, match="lower-case"):
        ops.run_operation("make_character", {"name": "Bad Name", "params": {}})
    with pytest.raises(ops.OpError, match="light"):
        ops.run_operation("make_character", {"name": "light", "params": {}})
    with pytest.raises(ops.OpError, match="no stage"):
        ops.run_operation("make_character", {"name": "x", "params": {}, "stage": "nope"})
    with pytest.raises(ops.OpError, match="unknown voice"):
        ops.run_operation("make_character", {"name": "x", "params": {}, "voice": "zz_nobody"})
    with pytest.raises(ops.OpError, match="already a character"):    # the stage's own teen is protected
        ops.run_operation("make_character", {"name": "teen", "params": {}})
    with pytest.raises(ops.OpError, match="building the character failed"):
        ops.run_operation("make_character", {"name": "crashy", "params": {"age": 1}})


def test_make_character_needs_makehuman(studio, monkeypatch):
    monkeypatch.delenv("BUILDER_MAKEHUMAN_JOB")
    with pytest.raises(ops.OpError, match="aren't installed"):
        ops.run_operation("make_character", {"name": "x", "params": {}})


# ---------------------------------------------------------------- library


def test_library_round_trip(studio, tmp_path):
    ops.run_operation("make_character", {"name": "coach", "params": {"age": 45}, "voice": "am_onyx", "persona": "Calm."})
    lst = ops.run_operation("library_list", {}).data
    by = {c["name"]: c for c in lst["characters"]}
    assert by["teen"]["voice"] == "am_puck" and by["teen"]["source"].startswith("studio/characters")   # the repo seed
    assert by["coach"]["voice"] == "am_onyx" and by["coach"]["on_stages"] == ["teen"]
    assert lst["stage_only_models"] == ["friend"] and lst["stages"]["teen"]["sets"] == ["park"] and "am_onyx" in lst["voices"]
    scene = {"stage": "teen", "set": "park", "cast": [{"id": "c", "model": "coach"}, {"id": "t", "model": "teen", "voice": "am_liam"}],
             "beats": [{"actor": "c", "say": "Great job today."}, {"actor": "t", "move": "wave"}]}
    saved = ops.run_operation("library_save_scene", {"name": "pep-talk", "params": scene, "description": "Coach cheers the teen"})
    assert saved.data["warnings"] == []
    stored = json.loads((tmp_path / "library" / "scenes" / "pep-talk.json").read_text())
    assert "voice" not in stored["params"]["cast"][0] and "resolution" not in stored["params"]      # only what was given
    got = ops.run_operation("library_get", {"name": "pep-talk"}).data
    cast = {c["id"]: c for c in got["scene_video_params"]["cast"]}
    assert cast["c"]["voice"] == "am_onyx" and cast["t"]["voice"] == "am_liam"   # library voice filled, explicit kept
    from media_backend.scene import SceneParams

    SceneParams.model_validate(got["scene_video_params"])
    assert ops.run_operation("library_list", {}).data["scenes"][0]["cast"] == ["coach", "teen"]
    assert ops.run_operation("library_get", {"name": "coach", "kind": "character"}).data["persona"] == "Calm."
    with pytest.raises(ops.OpError, match="exists"):
        ops.run_operation("library_save_scene", {"name": "pep-talk", "params": scene})
    with pytest.raises(ops.OpError, match="not valid scene_video params"):
        ops.run_operation("library_save_scene", {"name": "bad", "params": {"beats": []}})
    with pytest.raises(ops.OpError, match="nothing in the library"):
        ops.run_operation("library_get", {"name": "nobody"})


def test_resolve_cast(studio):
    from media_backend.scene import CastMember

    characters.save_character({"name": "coach", "voice": "am_onyx", "params": {}, "model": "coach"})
    dicts = characters.resolve_cast([{"id": "a", "model": "coach"}, {"id": "b", "character": "teen"}, {"id": "c", "model": "coach", "voice": "am_liam"},
                                     {"id": "d", "model": "stranger"}])
    assert [d.get("voice") for d in dicts] == ["am_onyx", "am_puck", "am_liam", None] and dicts[1]["model"] == "teen" and "character" not in dicts[1]
    models = characters.resolve_cast([CastMember(id="a", model="coach"), CastMember(id="b", model="teen", voice="af_heart")])
    assert isinstance(models[0], CastMember) and [m.voice for m in models] == ["am_onyx", "af_heart"]
    assert characters.resolve_cast([]) == []


# ---------------------------------------------------------------- export


def test_export_character(studio):
    res = ops.run_operation("export_character", {"character": "teen", "moves": ["walk", "wave"], "morphs": False})
    assert [o.name for o in res.outputs] == ["teen_game.glb", "teen_walk_preview.png"]
    assert res.data["moves"] == ["idle", "walk", "wave"] and [c["name"] for c in res.data["clips"]] == ["idle", "walk", "wave"]
    assert res.data["morph_targets"] == 0 and res.data["preview"]["move"] == "walk"
    plain = ops.run_operation("export_character", {"character": "friend", "moves": ["idle"], "preview": None})
    assert len(plain.outputs) == 1 and plain.data["morph_targets"] == 13


def test_export_character_errors(studio, monkeypatch):
    for params, msg in [({"stage": "nope"}, "no stage"), ({"character": "ghost"}, "no character"), ({"character": "light"}, "no character"),
                        ({"moves": ["moonwalk-backflip"]}, "unknown moves"), ({"moves": ["walk"], "preview": "run"}, "preview move")]:
        with pytest.raises(ops.OpError, match=msg):
            ops.run_operation("export_character", params)
    (studio / "models" / "broken.json").write_text("{}")
    with pytest.raises(ops.OpError, match="export failed"):
        ops.run_operation("export_character", {"character": "broken"})
    (studio / "models" / "lossy.json").write_text("{}")
    with pytest.raises(ops.OpError, match="without the moves"):
        ops.run_operation("export_character", {"character": "lossy", "moves": ["walk", "run"]})
    assert storage.list_files() == []     # failed exports leave nothing behind
    monkeypatch.setenv("CHARACTER_EXPORTER", str(FIX / "nowhere.cjs"))
    with pytest.raises(ops.OpError, match="exporter not found"):
        ops.run_operation("export_character", {})
    monkeypatch.delenv("CHARACTER_EXPORTER")
    monkeypatch.setattr(shutil, "which", lambda name: None)
    with pytest.raises(ops.OpError, match="Node.js"):
        ops.run_operation("export_character", {})


def _has_playwright() -> bool:
    if not shutil.which("node"):
        return False
    return subprocess.run(["node", "-e", "require('playwright')"], capture_output=True).returncode == 0


REAL_STAGE = Path(os.path.expanduser("~/.builder/stages/teen"))


@pytest.mark.skipif(not _has_playwright() or not (REAL_STAGE / "models" / "teen.json").exists(),
                    reason="needs Node.js + Playwright and a real teen stage (~/.builder/stages/teen)")
def test_export_character_real(monkeypatch):
    monkeypatch.setenv("BUILDER_STAGES_DIR", str(REAL_STAGE.parent))
    try:
        res = ops.run_operation("export_character", {"character": "teen", "moves": ["walk", "wave", "dance"], "preview": "wave"})
    except ops.OpError as e:
        if "page failed to load" in str(e):   # three.js comes from a CDN; offline machines can't load it
            pytest.skip(str(e))
        raise
    glb = storage.path_of(res.outputs[0].id).read_bytes()
    assert glb[:4] == b"glTF" and len(glb) > 1_000_000
    clips = {c["name"]: c["duration"] for c in res.data["clips"]}
    assert set(clips) == {"idle", "walk", "wave", "dance"} and clips["wave"] == pytest.approx(2.8, abs=0.05)
    assert res.data["morph_targets"] >= 10 and storage.path_of(res.outputs[1].id).stat().st_size > 10_000
