"""Kenney, Quaternius and Poly Pizza sources, glTF packing and stage props, with the network stubbed out."""
import io
import json
import struct
import zipfile

import pytest

from media_backend import asset_packs, assets, glbtools, ops, storage

PNG = (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\x0f"
       b"\x00\x00\x01\x01\x00\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82")


def tri_doc(height=1.0, image_uri=None, bin_uri=None):
    pos = struct.pack("<9f", 0, 0, 0, 1, 0, 0, 0, height, 0)
    doc = {"asset": {"version": "2.0"}, "buffers": [{"byteLength": len(pos), **({"uri": bin_uri} if bin_uri else {})}],
           "bufferViews": [{"buffer": 0, "byteLength": len(pos)}],
           "accessors": [{"bufferView": 0, "componentType": 5126, "count": 3, "type": "VEC3", "min": [0, 0, 0], "max": [1, height, 0]}],
           "meshes": [{"primitives": [{"attributes": {"POSITION": 0}, "material": 0}]}], "materials": [{"name": "m"}],
           "nodes": [{"mesh": 0, "name": "thing"}], "scenes": [{"nodes": [0]}], "scene": 0}
    if image_uri:
        doc.update({"images": [{"uri": image_uri}], "textures": [{"source": 0}]})
        doc["materials"][0]["pbrMetallicRoughness"] = {"baseColorTexture": {"index": 0}}
    return doc, pos


def tri_glb(**kw):
    doc, pos = tri_doc(**kw)
    return glbtools.write_glb(doc, pos)


def zip_bytes(files):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, data in files.items():
            z.writestr(name, data)
    return buf.getvalue()


KENNEY_ZIP = zip_bytes({"License.txt": "CC0", "Models/GLB format/barrel.glb": tri_glb(image_uri="Textures/colormap.png"),
                        "Models/GLB format/Textures/colormap.png": PNG, "Models/GLB format/column-damaged.glb": tri_glb(height=2.0),
                        "Models/FBX format/barrel.fbx": b"fbx", "Previews/barrel.png": PNG})
_gl_doc, _gl_bin = tri_doc(image_uri="T_Wood.png", bin_uri="Crate.bin")
QUAT_ZIP = zip_bytes({"Exports/glTF/Crate.gltf": json.dumps(_gl_doc), "Exports/glTF/Crate.bin": _gl_bin,
                      "Textures/T_Wood.png": PNG, "Textures/Normals-UnrealEngine/T_Wood.png": b"wrong",
                      "Exports/OBJ/Crate.obj": "v 0 0 0", "../evil.txt": "x"})
OGA_LIST = ('<div class="art-preview-title"><a href="/content/lowpoly-medieval-weapons">LowPoly Medieval Weapons</a></div>'
            '<div class="art-preview-title"><a href="/content/fantasy-props-megakit">Fantasy Props MegaKit</a></div>')


def oga_page(author="quaternius", lic="CC0"):
    return (f'<title>Fantasy Props MegaKit | OpenGameArt.org</title><div class="field-label">Author:&nbsp;</div><span class="username">'
            f'<a href="/users/{author}">{author}</a></span> License(s):&nbsp;<a>{lic}</a> '
            '<a href="https://opengameart.org/sites/default/files/fantasy_props_megakitsource.zip">src</a>'
            '<a href="https://opengameart.org/sites/default/files/fantasy_props_megakitstandard.zip">zip</a>')


@pytest.fixture
def net(monkeypatch):
    calls = {"json": [], "dl": [], "text": []}
    page = {"html": oga_page()}

    def get_json(url, headers=None):
        calls["json"].append((url, headers))
        if "kenney.nl/download-link/" in url:
            assert headers["Referer"].startswith("https://kenney.nl/assets/")
            assert url.endswith(b"assets/mini-arena".hex())
            return {"url": "https://kenney.nl/download/abc?expires=1"}
        if "api.poly.pizza/v1.1/search/" in url:
            assert headers == {"x-auth-token": "k"}
            return {"total": 1, "results": [{"ID": "p1", "Title": "Viking Helmet", "Creator": {"Username": "Quaternius"}, "Licence": "CC0 1.0",
                                             "Thumbnail": "https://x/t.webp", "Download": "https://static.poly.pizza/p1.glb", "Tri Count": 300}]}
        if "api.poly.pizza/v1.1/model/" in url:
            return {"ID": "p2", "Title": "Shield", "Creator": {"Username": "Ana"}, "Licence": "CC-BY 3.0", "Download": "https://static.poly.pizza/p2.glb",
                    "Attribution": "Shield by Ana [CC-BY] via Poly Pizza"}
        raise AssertionError(url)

    def get_text(url, limit=0):
        calls["text"].append(url)
        if "art-search-advanced" in url:
            assert "name=quaternius" in url
            return OGA_LIST, url
        return page["html"], url

    def download(url, dest):
        calls["dl"].append(url)
        if "kenney.nl/download/" in url:
            dest.write_bytes(KENNEY_ZIP)
        elif "megakitstandard.zip" in url:
            dest.write_bytes(QUAT_ZIP)
        elif "static.poly.pizza/p2" in url:
            dest.write_bytes(page.get("pp", tri_glb()))
        else:
            raise AssertionError(url)

    monkeypatch.setattr(assets, "_get_json", get_json)
    monkeypatch.setattr(assets, "_get_text", get_text)
    monkeypatch.setattr(assets, "_download", download)
    asset_packs._oga_cache.clear()
    calls["page"] = page
    return calls


def test_kenney_search_and_import_embeds_texture(net, monkeypatch):
    monkeypatch.delenv("POLY_PIZZA_API_KEY", raising=False)
    d = ops.run_operation("asset_search", {"query": "gladiator column", "kind": "model", "source": "kenney"}).data
    assert d["results"][0]["id"] == "mini-arena" and d["results"][0]["license"] == "CC0 1.0"
    listing = ops.run_operation("asset_import", {"source": "kenney", "asset_id": "mini-arena"})
    assert listing.outputs == [] and listing.data["items"] == ["barrel", "column-damaged"]
    res = ops.run_operation("asset_import", {"source": "kenney", "asset_id": "mini-arena", "item": "barrel, broken column damaged, unicorn"})
    names = [o.name for o in res.outputs]
    assert names == ["barrel.glb", "column-damaged.glb"] and res.data["not_found"] == ["unicorn"]
    doc, bin_ = glbtools.read_glb(storage.path_of(res.outputs[0].id).read_bytes())
    assert "uri" not in doc["images"][0] and bin_.find(PNG) >= 0                       # texture now inside the GLB
    lic = assets.license_of(res.outputs[0].id)
    assert lic["license"] == "CC0 1.0" and "Kenney" in lic["attribution"] and lic["id"] == "mini-arena/barrel"
    assert len(net["dl"]) == 1                                                          # pack downloaded once, reused
    # the pack's model names now help search
    d = ops.run_operation("asset_search", {"query": "barrel", "kind": "model", "source": "kenney"}).data
    hit = next(r for r in d["results"] if r["id"] == "mini-arena")
    assert hit["options"]["item"] == ["barrel"]
    with pytest.raises(ops.OpError, match="unknown Kenney pack"):
        ops.run_operation("asset_import", {"source": "kenney", "asset_id": "not-a-pack", "item": "x"})


def test_quaternius_from_opengameart(net):
    d = ops.run_operation("asset_search", {"query": "barrel torch", "kind": "model", "source": "quaternius"}).data
    assert d["results"][0]["id"] == "fantasy-props-megakit" and d["results"][0]["creator"] == "Quaternius"
    res = ops.run_operation("asset_import", {"source": "quaternius", "asset_id": "fantasy-props-megakit", "item": "crate"})
    assert net["dl"] == ["https://opengameart.org/sites/default/files/fantasy_props_megakitstandard.zip"]   # not the source zip
    doc, bin_ = glbtools.read_glb(storage.path_of(res.outputs[0].id).read_bytes())
    assert res.outputs[0].name == "Crate.glb" and res.data["imported"][0]["from"] == ".gltf"
    assert bin_.find(PNG) >= 0 and b"wrong" not in bin_                                  # shared Textures folder, glTF (not Unreal) normals
    assert assets.license_of(res.outputs[0].id)["attribution"].startswith("Fantasy Props MegaKit by Quaternius")
    assert not any(p.name == "evil.txt" for p in asset_packs.cache_dir().parent.rglob("*"))   # zip paths can't escape


@pytest.mark.parametrize("author,lic,msg", [("someone", "CC0", "isn't Quaternius"), ("quaternius", "CC-BY", "isn't marked CC0")])
def test_quaternius_refuses_other_uploads(net, author, lic, msg):
    net["page"]["html"] = oga_page(author, lic)
    with pytest.raises(ops.OpError, match=msg):
        ops.run_operation("asset_import", {"source": "quaternius", "asset_id": "fantasy-props-megakit", "item": "crate"})
    assert not (asset_packs.cache_dir() / "quaternius" / "fantasy-props-megakit").exists()


def test_poly_pizza(net, monkeypatch):
    monkeypatch.delenv("POLY_PIZZA_API_KEY", raising=False)
    d = ops.run_operation("asset_search", {"query": "helmet", "kind": "model", "source": "polypizza"}).data
    assert d["results"] == [] and "POLY_PIZZA_API_KEY" in d["note"]
    with pytest.raises(ops.OpError, match="POLY_PIZZA_API_KEY"):
        ops.run_operation("asset_import", {"source": "polypizza", "asset_id": "p2"})
    monkeypatch.setenv("POLY_PIZZA_API_KEY", "k")
    r = ops.run_operation("asset_search", {"query": "helmet", "kind": "model", "source": "polypizza"}).data["results"][0]
    assert r["id"] == "p1" and r["license"] == "CC0 1.0" and r["creator"] == "Quaternius"
    out = ops.run_operation("asset_import", {"source": "polypizza", "asset_id": "p2"}).outputs[0]
    lic = assets.license_of(out.id)
    assert out.name == "Shield.glb" and lic["license"] == "CC BY 3.0" and "Ana" in lic["attribution"]
    net["page"]["pp"] = b"<html>challenge</html>"
    with pytest.raises(ops.OpError, match="blocks many cloud addresses"):
        ops.run_operation("asset_import", {"source": "polypizza", "asset_id": "p2"})


def test_glb_tools_round_trip(tmp_path):
    (tmp_path / "Textures").mkdir()
    (tmp_path / "Textures" / "c.png").write_bytes(PNG)
    doc, pos = tri_doc(image_uri="Textures/c.png", bin_uri="m.bin")
    (tmp_path / "m.bin").write_bytes(pos)
    (tmp_path / "m.gltf").write_text(json.dumps(doc))
    glb = glbtools.pack_glb(tmp_path / "m.gltf")
    d, b = glbtools.read_glb(glb)
    assert d["images"][0]["mimeType"] == "image/png" and len(d["buffers"]) == 1 and "uri" not in d["buffers"][0]
    scaled = glbtools.wrap_scale(glb, 2.5, 0.1)
    d2, _ = glbtools.read_glb(scaled)
    assert d2["nodes"][-1]["scale"] == [2.5] * 3 and d2["scenes"][0]["nodes"] == [len(d2["nodes"]) - 1]
    out = glbtools.split_to_gltf(scaled, tmp_path / "prop", "x_1k")
    g = json.loads(out.read_text())
    assert g["buffers"][0]["uri"] == "x_1k.bin" and (tmp_path / "prop" / g["images"][0]["uri"]).read_bytes() == PNG
    bad = json.dumps(tri_doc(bin_uri="../../etc/passwd")[0])
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "deep").mkdir()
    (tmp_path / "sub" / "deep" / "e.gltf").write_text(bad)
    with pytest.raises(glbtools.GlbError, match="outside"):
        glbtools.pack_glb(tmp_path / "sub" / "deep" / "e.gltf")


def test_stage_prop(tmp_path, monkeypatch):
    stage = tmp_path / "stages" / "teen"
    (stage / "props").mkdir(parents=True)
    (stage / "index.html").write_text("<html>")
    (stage / "props" / "index.json").write_text('["football"]')
    monkeypatch.setenv("BUILDER_STAGES_DIR", str(tmp_path / "stages"))
    f = tmp_path / "column.glb"
    f.write_bytes(tri_glb(height=2.0))
    fid = storage.import_path(f).id
    src = storage.path_of(fid).read_bytes()
    res = ops.run_operation("stage_prop", {"file_id": fid, "prop_id": "arena_column", "height_m": 3.0})
    assert res.data["size_m"][1] == pytest.approx(3.0) and res.data["scale"] == pytest.approx(1.5)
    g = json.loads((stage / "props" / "arena_column" / "arena_column_1k.gltf").read_text())
    assert g["nodes"][-1]["scale"] == [1.5] * 3 and (stage / "props" / "arena_column" / "arena_column_1k.bin").exists()
    assert json.loads((stage / "props" / "index.json").read_text()) == ["football", "arena_column"]
    assert storage.path_of(fid).read_bytes() == src                                       # input untouched
    with pytest.raises(ops.OpError, match="already has a prop"):
        ops.run_operation("stage_prop", {"file_id": fid, "prop_id": "arena_column", "height_m": 3.0})
    with pytest.raises(ops.OpError, match="not both"):
        ops.run_operation("stage_prop", {"file_id": fid, "prop_id": "b", "height_m": 1, "length_m": 1})
    with pytest.raises(ops.OpError, match="no stage"):
        ops.run_operation("stage_prop", {"file_id": fid, "prop_id": "b", "height_m": 1, "stage": "nope"})
    ops.run_operation("stage_prop", {"file_id": fid, "prop_id": "arena_column", "length_m": 1.0, "overwrite": True})
    assert json.loads((stage / "props" / "index.json").read_text()) == ["football", "arena_column"]
