"""Sets from any HDRI, the CMU move catalog, prop use beats, comics, finished stills, vertical shorts, library templates.
Downloads and rendering are stubbed (tests/fixtures/fake_scene_render.py stands in for the browser renderer)."""
import json
from pathlib import Path

import pytest

from media_backend import director, ops, scene, shorts, stage_tools, storage, voice

from test_voice_finish import stub_speech  # noqa: F401  (fixture)

FIX = Path(__file__).parent / "fixtures"
FETCHED: list[str] = []   # CMU ids the stubbed downloader was asked for
REPO = Path(__file__).resolve().parents[1]

HDRIS = {
    "old_outdoor_theater": {"name": "Old Outdoor Theater", "tags": ["sun", "arena", "europe"], "categories": ["outdoor", "urban"],
                            "category": "Streets & Town/Squares & Plazas/Amphitheatres & Arenas", "download_count": 47000,
                            "attributes": {"environment": "outdoor", "time_of_day": "midday", "contrast": "high"}},
    "circus_arena": {"name": "Circus Arena", "tags": ["seat", "arena"], "categories": ["indoor"], "category": "Interiors/Halls",
                     "download_count": 81000, "attributes": {"environment": "indoor", "contrast": "medium"}},
    "goegap": {"name": "Goegap", "tags": ["sand", "desert"], "categories": ["outdoor", "nature"], "category": "Desert & Arid",
               "download_count": 240000, "attributes": {"environment": "outdoor", "time_of_day": "midday", "contrast": "high"}},
    "rooftop_night": {"name": "Rooftop Night", "tags": ["city"], "categories": ["outdoor", "urban", "night"], "category": "Urban",
                      "download_count": 9000, "attributes": {"environment": "outdoor", "time_of_day": "night"}},
}
CMU = [
    {"id": "02_07-swordplay", "title": "swordplay", "category": "physical activities and sports", "subcategory": "", "seconds": 18.8, "retarget_ok": True, "subject": 2},
    {"id": "02_09-swordplay", "title": "swordplay", "category": "physical activities and sports", "subcategory": "", "seconds": 8.6, "retarget_ok": True, "subject": 2},
    {"id": "113_15-sit-in-chair-and-get-up", "title": "sit in chair and get up", "category": "situations and scenarios", "subcategory": "", "seconds": 8.0, "retarget_ok": True},
    {"id": "16_15-walk", "title": "walk", "category": "locomotion", "subcategory": "walking", "seconds": 3.2, "retarget_ok": True},
    {"id": "77_10-drunk-walk", "title": "drunk walk", "category": "locomotion", "subcategory": "walking", "seconds": 6.0, "retarget_ok": True},
    {"id": "88_01-broken-cartwheel", "title": "cartwheel", "category": "uncategorized", "subcategory": "", "seconds": 4.0, "retarget_ok": False},
]


@pytest.fixture
def stage(tmp_path, monkeypatch):
    d = tmp_path / "stages" / "kid"
    (d / "clips" / "cmu").mkdir(parents=True)
    (d / "index.html").write_text("<title>stage</title>")
    (d / "studio.html").write_text("<title>studio</title>")
    (d / "models").mkdir()
    (d / "models" / "teen.json").write_text("{}")
    (d / "clips" / "cmu" / "index.json").write_text(json.dumps({"explain": "explain.json"}))
    (d / "clips" / "cmu" / "explain.json").write_text(json.dumps({"label": "explain", "id": "18_09-conversation-explain", "glb": ""}))
    meta = tmp_path / "metadata.jsonl"
    meta.write_text("\n".join(json.dumps(r) for r in CMU) + "\n")
    monkeypatch.setenv("BUILDER_STAGES_DIR", str(tmp_path / "stages"))
    monkeypatch.setenv("BUILDER_CMU_METADATA", str(meta))
    monkeypatch.setenv("BUILDER_LIBRARY_DIR", str(tmp_path / "library"))
    monkeypatch.setenv("SCENE_RENDERER", str(FIX / "fake_scene_render.py"))
    monkeypatch.setattr(voice, "mouth_cues", lambda words, duration, lang="en-us": [{"start": 0.0, "end": duration, "value": "B"}])
    fetched = FETCHED
    get_cmu = stage_tools.tool("get_cmu")
    monkeypatch.setattr(get_cmu, "fetch", lambda cid, dest: (fetched.append(cid), Path(dest).write_bytes(b"glTF-fake-" + cid.encode())))
    FETCHED.clear()
    return d


@pytest.fixture
def polyhaven(monkeypatch):
    m = stage_tools.tool("add_set")
    got = []
    monkeypatch.setattr(m, "hdris", lambda: HDRIS)

    def files(aid):
        if aid in HDRIS:
            return {"hdri": {"2k": {"hdr": {"url": f"https://dl/{aid}_2k.hdr"}}, "1k": {"hdr": {"url": f"https://dl/{aid}_1k.hdr"}}}}
        return {"gltf": {"1k": {"gltf": {"url": f"https://dl/{aid}_1k.gltf", "include": {f"textures/{aid}.jpg": {"url": f"https://dl/{aid}.jpg"}}}}}}

    def get(url, dest):
        got.append(url)
        Path(dest).parent.mkdir(parents=True, exist_ok=True)
        Path(dest).write_text("x")

    monkeypatch.setattr(m, "files", files)
    monkeypatch.setattr(m, "get", get)
    return got


# ---------------------------------------------------------------- sets

def test_hdri_search_and_defaults():
    m = stage_tools.tool("add_set")
    hits = m.search_hdri("arena", HDRIS)
    assert {h[0] for h in hits[:2]} == {"circus_arena", "old_outdoor_theater"}
    assert m.search_hdri("sand desert", HDRIS)[0][0] == "goegap"
    assert m.search_hdri("old_outdoor_theater", HDRIS)[0][0] == "old_outdoor_theater"   # an exact id wins
    ground, exp = m.defaults_for(HDRIS["circus_arena"])
    assert ground == {"height": 1.6, "radius": 18} and exp >= 1.0
    ground, exp = m.defaults_for(HDRIS["rooftop_night"])
    assert ground["radius"] == 35 and exp > 1.1
    assert m.parse_prop("door@0,-4.2,90") == {"id": "door", "at": [0.0, -4.2], "rot": 90.0, "scale": 1}
    assert "arena" in m.SETS and m.SETS["arena"]["hdri"] == "old_outdoor_theater"


def test_add_set_any_hdri(stage, polyhaven):
    res = ops.run_operation("add_set", {"stage": "kid", "name": "duel", "hdri": "sand desert",
                                        "props": [{"id": "painted_wooden_bench", "at": [-1, -1], "rot": 20}, {"id": "door", "at": [0, -3]}]})
    spec = json.loads((stage / "sets" / "duel" / "set.json").read_text())
    assert res.data["hdri"] == "goegap" and spec["hdri"] == "goegap" and spec["ground"]["radius"] == 60 and spec["exposure"] == 0.8
    bench, door = spec["props"]
    assert bench["file"] == "painted_wooden_bench/painted_wooden_bench_1k.gltf" and "sit" in bench["use"]
    assert door["kind"] == "door" and "open" in door["use"] and "file" not in door
    assert res.data["uses"] == {"painted_wooden_bench": ["lean", "sit"], "door": ["lean", "open"]}
    assert (stage / "sets" / "duel" / "sky.hdr").exists() and "https://dl/goegap_2k.hdr" in polyhaven
    assert "goegap" in spec["credit"] and "door" not in spec["credit"]


def test_add_set_preset_arena(stage, polyhaven):
    res = ops.run_operation("add_set", {"stage": "kid", "name": "arena"})
    spec = json.loads((stage / "sets" / "arena" / "set.json").read_text())
    assert res.data["hdri"] == "old_outdoor_theater" and spec["ground"] == {"height": 1.7, "radius": 35}
    assert {p["id"] for p in spec["props"]} >= {"painted_wooden_stool", "door", "wine_barrel_01"}
    with pytest.raises(ops.OpError, match="set name"):
        ops.run_operation("add_set", {"stage": "kid", "name": "Bad Name"})


def test_handheld_props_keep_others(stage, polyhaven):
    m = stage_tools.tool("add_set")
    (stage / "props").mkdir()
    (stage / "props" / "index.json").write_text(json.dumps(["kite_shield", "football"]))
    have = m.add_handheld(str(stage), ["football", "wooden_handle_saber"])
    assert have == ["kite_shield", "football", "wooden_handle_saber"]


def test_refresh_adds_use_data(stage, polyhaven):
    m = stage_tools.tool("add_set")
    d = stage / "sets" / "park"
    d.mkdir(parents=True)
    (d / "set.json").write_text(json.dumps({"name": "park", "props": [{"file": "painted_wooden_bench/x.gltf", "at": [0, 0]}]}))
    assert m.refresh_set(str(d))
    p = json.loads((d / "set.json").read_text())["props"][0]
    assert p["id"] == "painted_wooden_bench" and len(p["use"]["sit"]["seats"]) == 2


# ---------------------------------------------------------------- moves

def test_find_moves(stage):
    res = ops.run_operation("find_moves", {"words": "sword fight", "stage": "kid"})
    ids = [m["id"] for m in res.data["moves"]]
    assert ids[:2] == ["02_09-swordplay", "02_07-swordplay"]   # the shorter clip first
    assert res.data["moves"][0]["label"] == "swordplay 02_09" and res.data["catalog_size"] == 6
    assert ops.run_operation("find_moves", {"words": "cartwheel"}).data["count"] == 0   # doesn't retarget cleanly
    assert ops.run_operation("find_moves", {"words": "cartwheel", "include_broken": True}).data["count"] == 1
    walk = ops.run_operation("find_moves", {"words": "drunk walk", "stage": "kid"}).data["moves"]
    assert walk[0]["id"] == "77_10-drunk-walk" and walk[0]["on_stage"] is False


def test_fetch_moves_into_stage(stage):
    res = ops.run_operation("fetch_moves", {"stage": "kid", "moves": ["02_07-swordplay", "drunk walk"], "labels": ["sword"]})
    assert [m["id"] for m in res.data["moves"]] == ["02_07-swordplay", "77_10-drunk-walk"]
    ix = json.loads((stage / "clips" / "cmu" / "index.json").read_text())
    assert ix["explain"] == "explain.json" and ix["sword"] == "sword.json" and ix["drunk walk"] == "drunk-walk.json"
    clip = json.loads((stage / "clips" / "cmu" / "sword.json").read_text())
    assert clip["id"] == "02_07-swordplay" and clip["glb"]
    assert "sword" in scene.stage_moves("kid")
    found = ops.run_operation("find_moves", {"words": "swordplay", "stage": "kid"}).data["moves"]
    assert any(m["on_stage"] and m["stage_label"] == "sword" for m in found)
    with pytest.raises(ops.OpError, match="no move called"):
        ops.run_operation("fetch_moves", {"stage": "kid", "moves": ["zzzz qqqq"]})


def test_scene_beats_use_any_cmu_move_and_props(stub_speech, stage):  # noqa: F811
    pytest.importorskip("numpy")
    res = ops.run_operation("scene_video", {
        "stage": "kid", "resolution": "640x360", "fps": 12, "captions": False, "footsteps": False,
        "cast": [{"id": "a", "model": "teen"}, {"id": "b", "model": "teen", "at": [1, 0]}],
        "beats": [{"actor": "a", "move": "02_09-swordplay", "hold": 1}, {"actor": "b", "move": "drunk walk", "hold": 1},
                  {"actor": "a", "move": "sit", "on": "painted_wooden_bench", "hold": 1},
                  {"actor": "b", "move": "open", "on": "door", "hold": 1}, {"actor": "b", "move": "explain", "hold": 1}]})
    assert res.data["fetched_moves"] == {"02_09-swordplay": "02_09-swordplay", "drunk walk": "77_10-drunk-walk"}
    assert FETCHED == ["02_09-swordplay", "77_10-drunk-walk"]   # "explain" was already there, sit is built in
    tl = res.data["timeline"]
    assert tl[2]["on"] == "painted_wooden_bench" and tl[3] == {**tl[3], "move": "open", "on": "door"}


# ---------------------------------------------------------------- the studio page

def test_studio_page_has_prop_uses_and_probes():
    html = (REPO / "tools" / "makehuman" / "view" / "studio.html").read_text()
    for needle in ("function useProp", "function startSit", "function startLean", "p.opening", "sitInfo", "probe", "window.__heads",
                   "usesShots", "S.grips", "__step = async (dt, draw = true)"):
        assert needle in html, needle
    cjs = (REPO / "tools" / "makehuman" / "view" / "scene_render.cjs").read_text()
    assert "scene.keep" in cjs


# ---------------------------------------------------------------- comics and stills

def test_make_comic(stage):
    pytest.importorskip("PIL")
    from PIL import Image

    sc = {"stage": "kid", "cast": [{"id": "a", "model": "teen"}, {"id": "b", "model": "teen", "at": [1, 0]}],
          "beats": [{"shot": {"type": "two"}, "actor": "a", "move": "wave", "say": "Ready for the big fight? I've been training with a very, very small sword."},
                    {"actor": "b", "say": "I brought a bigger one!"}, {"actor": "a", "move": "laugh", "hold": 1.5}]}
    res = ops.run_operation("make_comic", {"scene": sc, "title": "Arena Day", "panels": [0, 1, {"beat": 2, "caption": "Later..."}],
                                           "panel_size": "640x360", "columns": 2})
    names = [o.name for o in res.outputs]
    assert names == ["Arena_Day.png", "Arena_Day.pdf"]
    page = Image.open(storage.path_of(res.outputs[0].id))
    assert page.size == (res.data["width"], res.data["height"]) and page.size[0] > 2 * 640
    p = res.data["panels"]
    assert [x["beat"] for x in p] == [0, 1, 2] and p[0]["actor"] == "a" and p[1]["actor"] == "b"
    assert p[0]["head"] == [192, 126] and p[1]["head"] == [448, 126]   # from the renderer's head positions
    assert p[2]["caption"] == "Later..." and p[2]["text"] is None
    assert storage.path_of(res.outputs[1].id).read_bytes()[:4] == b"%PDF"
    with pytest.raises(ops.OpError, match="doesn't exist"):
        ops.run_operation("make_comic", {"scene": sc, "panels": [7]})


def test_comic_text_wrapping():
    pytest.importorskip("PIL")
    from PIL import Image, ImageDraw

    from media_backend import pictures

    d = ImageDraw.Draw(Image.new("RGB", (10, 10)))
    font = pictures._font(20, bold=True)
    lines = pictures._wrap(d, "a supercalifragilisticexpialidocious word and more words here", font, 120)
    assert all(d.textlength(x, font=font) <= 120 + 1 for x in lines) and len(lines) >= 4
    img = Image.new("RGB", (640, 360), "grey")
    pictures._bubble(img, "Hello there!", (320.0, 150.0))
    white = [(x, y) for x in range(0, 640, 4) for y in range(0, 360, 4) if img.getpixel((x, y)) == (255, 255, 255)]
    assert white and max(y for _, y in white) < 150 and min(x for x, _ in white) < 320 < max(x for x, _ in white)   # above the head


def test_finish_picture(media, monkeypatch):
    pytest.importorskip("PIL")
    from PIL import Image

    res = ops.run_operation("finish_picture", {"file_id": media["image"], "size": "4x6", "look": "noir", "grain": 0.3, "vignette": 0.5})
    im = Image.open(storage.path_of(res.outputs[0].id))
    assert im.size == (1200, 1800) and res.data["upscaled"] and res.data["inches"] == [4.0, 6.0]
    assert round(im.info["dpi"][0]) == 300
    r, g, b = im.convert("RGB").getpixel((600, 900))
    assert abs(r - g) < 12 and abs(g - b) < 12   # noir is grey
    res = ops.run_operation("finish_picture", {"file_id": media["image"], "size": "A4", "orientation": "landscape", "fit": "pad",
                                               "format": "jpg", "background": "#000000", "dpi": 150})
    im = Image.open(storage.path_of(res.outputs[0].id))
    assert im.format == "JPEG" and im.size == (1754, 1240) and im.getpixel((5, 600))[0] < 20   # black border at the sides

    def fake_cutout(p):   # stand-in for rembg: a transparent picture with an opaque middle
        img = Image.new("RGBA", (200, 300), (0, 0, 0, 0))
        img.paste((200, 80, 40, 255), (50, 50, 150, 250))
        return ops._guarded("remove_background", [p.file_id], "cut.png", lambda out: img.save(out))

    monkeypatch.setitem(ops.REGISTRY, "remove_background", ops.Operation("remove_background", "", ops.REGISTRY["remove_background"].params, fake_cutout))
    res = ops.run_operation("finish_picture", {"file_id": media["image"], "remove_background": True, "look": "vivid"})
    im = Image.open(storage.path_of(res.outputs[0].id))
    assert im.mode == "RGBA" and im.getpixel((5, 5))[3] == 0 and im.getpixel((100, 150))[3] == 255 and res.data["transparent"]
    with pytest.raises(ops.OpError, match="colours"):
        ops.run_operation("finish_picture", {"file_id": media["image"], "background": "red"})


# ---------------------------------------------------------------- shorts

def test_vertical_shots():
    b = shorts.vertical_shots([{"say": "Hi", "actor": "b", "view": "face"}, {"move": "wave"}, {"walk_to": [1, 0]}, {"say": "Yo"}], ["a", "b"])
    assert b[0]["shot"] == {"type": "close", "actor": "b"} and "view" not in b[0] and b[3]["shot"] == {"type": "medium", "actor": "a"}
    assert b[1]["shot"] == {"type": "medium", "actor": "a"} and b[2]["shot"] == {"type": "two"}
    b = shorts.vertical_shots([{"shot": {"type": "wide"}, "say": "Hi"}], ["a"])
    assert b[0]["shot"] == {"type": "medium", "actor": "a"}


def test_make_video_short_from_beats(stub_speech, stage):  # noqa: F811
    pytest.importorskip("numpy")
    res = ops.run_operation("make_video_short", {"stage": "kid", "resolution": "360x640", "fps": 12, "quality": "fast",
                                                 "beats": [{"say": "Watch this.", "move": "wave"}, {"move": "jump", "hold": 1}]})
    info = ops.media_info(res.outputs[0].id)
    assert info["video"]["width"] == 1080 and info["video"]["height"] == 1920 and info["has_audio"]
    plan = res.data["plan"]["beats"]
    assert plan[0]["shot"]["type"] == "medium" and plan[1]["shot"]["type"] == "medium"
    vid = res.data["scene_video"]
    assert ops.media_info(vid)["video"]["width"] == 360


def test_make_video_short_plans_from_words(stub_speech, stage, monkeypatch):  # noqa: F811
    pytest.importorskip("numpy")
    asked = []

    def fake(system, content, max_tokens=3000):
        asked.append(content)
        return '{"title": "Tiny sword", "voice": "am_puck", "music": "none", "beats": [{"say": "Behold!", "move": "02_07-swordplay"}]}'

    monkeypatch.setattr(director, "_ask", fake)
    res = ops.run_operation("make_video_short", {"request": "a kid shows off a tiny sword", "stage": "kid", "resolution": "360x640", "fps": 12})
    assert "vertical 9:16" in asked[0] and res.data["title"] == "Tiny sword"
    monkeypatch.undo()


def test_make_video_short_needs_key_or_beats(stage, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(ops.OpError, match="give the beats"):
        ops.run_operation("make_video_short", {"request": "a dance", "stage": "kid"})
    with pytest.raises(ops.OpError, match="request"):
        ops.run_operation("make_video_short", {"stage": "kid"})


# ---------------------------------------------------------------- library templates (the Studio tab)

def test_scene_templates_round_trip(stage):
    params = {"stage": "kid", "set": None, "beats": [{"move": "sit", "on": "painted_wooden_bench"}], "resolution": "1080x1920"}
    ops.run_operation("library_save_scene", {"name": "bench-sit", "params": params})
    assert any(s["name"] == "bench-sit" for s in ops.run_operation("library_list", {}).data["scenes"])
    got = ops.run_operation("library_get", {"name": "bench-sit", "kind": "scene"}).data["scene_video_params"]
    assert got["beats"][0]["on"] == "painted_wooden_bench" and got["resolution"] == "1080x1920"
    ops.run_operation("library_delete_scene", {"name": "bench-sit"})
    assert not ops.run_operation("library_list", {}).data["scenes"]
    with pytest.raises(ops.OpError, match="no scene template"):
        ops.run_operation("library_delete_scene", {"name": "bench-sit"})
