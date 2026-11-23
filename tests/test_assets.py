"""Asset finder with the network stubbed out (no real downloads in tests)."""
import json

import pytest

from media_backend import assets, ops, storage

PH_LIST = {"rock_wall_08": {"name": "Rock Wall 08", "categories": ["rock"], "tags": ["wall", "stone"], "authors": {"Rob": "All"}, "download_count": 5},
           "brick_floor": {"name": "Brick Floor", "categories": ["floor"], "tags": ["brick"], "authors": {"Amy": "All"}, "download_count": 9}}
PH_HDRI = {"sunset_sky": {"name": "Sunset Sky", "categories": ["skies"], "tags": ["sunset", "clouds"], "authors": {"Greg": "All"}}}


@pytest.fixture
def net(monkeypatch):
    calls = []

    def get_json(url, headers=None):
        calls.append(url)
        if "assets?t=textures" in url:
            return PH_LIST
        if "assets?t=hdris" in url:
            return PH_HDRI
        if "assets?t=models" in url:
            return {}
        if "/files/sunset_sky" in url:
            return {"hdri": {"2k": {"hdr": {"url": "https://dl.example/sunset_2k.hdr"}}}}
        if "/info/sunset_sky" in url:
            return {"authors": {"Greg": "All"}}
        if "ambientcg" in url:
            return {"foundAssets": [{"assetId": "Wood099", "displayName": "Wood 099", "previewImage": {"256-PNG": "https://x/p.png"},
                                     "downloadFolders": {"default": {"downloadFiletypeCategories": {"zip": {"downloads": [
                                         {"attribute": "2K-JPG", "fileName": "Wood099_2K-JPG.zip", "downloadLink": "https://dl.example/w.zip"}]}}}}}]}
        if "openverse" in url and "/images/?" in url:
            return {"results": [{"id": "abc", "title": "forest", "url": "https://img.example/f.jpg", "license": "by", "license_version": "4.0",
                                 "creator": "Ana", "foreign_landing_url": "https://flickr/x", "attribution": "forest by Ana, CC BY 4.0"}]}
        if "openverse" in url:
            return {"results": []}
        if "images-api.nasa.gov/search" in url:
            return {"collection": {"items": [{"data": [{"nasa_id": "as11-40-5903", "title": "Moon walk", "media_type": "image", "center": "JSC"}],
                                              "links": [{"href": "https://images-assets.nasa.gov/p.jpg", "render": "image"}]}]}}
        if "api.si.edu" in url:
            return {"response": {"rows": []}}
        if "archive.org/advancedsearch" in url:
            return {"response": {"docs": [{"identifier": "musopen-bach", "title": "Bach Prelude", "mediatype": "audio", "licenseurl": "http://creativecommons.org/publicdomain/mark/1.0/"}]}}
        raise AssertionError(url)

    def download(url, dest):
        calls.append(url)
        dest.write_bytes(b"#?RADIANCE\nfake" if url.endswith(".hdr") else b"PK fake bytes")

    monkeypatch.setattr(assets, "_get_json", get_json)
    monkeypatch.setattr(assets, "_download", download)
    assets._ph_cache.clear()
    return calls


def test_search_ranks_and_licenses(net, monkeypatch):
    monkeypatch.delenv("FREESOUND_API_KEY", raising=False)
    d = ops.run_operation("asset_search", {"query": "brick wall", "kind": "texture"}).data
    ids = [r["id"] for r in d["results"]]
    assert ids[0] in ("rock_wall_08", "brick_floor") and "Wood099" in ids
    assert all(r["license"] == "CC0 1.0" for r in d["results"])
    assert "FREESOUND_API_KEY" in d["note"]
    d = ops.run_operation("asset_search", {"query": "forest", "kind": "image"}).data
    assert d["results"][0]["source"] == "openverse" and d["results"][0]["license"] == "CC BY 4.0"
    assert any("license_type=commercial" in c for c in net)


def test_import_hdri_saves_license(net):
    res = ops.run_operation("asset_import", {"source": "polyhaven", "asset_id": "sunset_sky", "resolution": "2k"})
    out = res.outputs[0]
    assert out.name == "sunset_sky_2k.hdr"
    lic = assets.license_of(out.id)
    assert lic["license"] == "CC0 1.0" and "Greg" in lic["attribution"] and lic["kind"] == "hdri"


def test_import_ambientcg(net):
    out = ops.run_operation("asset_import", {"source": "ambientcg", "asset_id": "Wood099", "resolution": "2k"}).outputs[0]
    assert out.name == "Wood099_2K-JPG.zip" and assets.license_of(out.id)["creator"] == "ambientCG"


def test_private_addresses_refused():
    for bad in ("http://127.0.0.1/x.png", "http://localhost:8000/files", "file:///etc/passwd", "http://10.0.0.5/a.jpg"):
        with pytest.raises(ops.OpError):
            assets._check_public(bad)


def test_page_references(monkeypatch):
    html = ('<html><head><title>Kitchen ideas</title><meta property="og:image" content="/hero.jpg"></head><body>'
            '<img src="a.png"><img srcset="s1.jpg 1x, s2.jpg 2x"><img src="data:image/png;base64,xx"><img src="/img/logo.png">'
            '<img src="a.png"></body></html>')
    monkeypatch.setattr(assets, "_get_text", lambda url, limit=0: (html, "https://site.example/page/"))
    d = ops.run_operation("page_references", {"url": "https://site.example/page/"}).data
    assert d["title"] == "Kitchen ideas"
    assert d["images"] == ["https://site.example/hero.jpg", "https://site.example/page/a.png", "https://site.example/page/s2.jpg"]


def test_import_url_needs_url():
    with pytest.raises(ops.OpError):
        ops.run_operation("asset_import", {"source": "url"})


def test_public_domain_sources(net):
    space = ops.run_operation("asset_search", {"query": "moon", "kind": "image", "source": "nasa"}).data["results"]
    assert space[0]["source"] == "nasa" and space[0]["license"] == "Public domain" and "endorsement" in space[0]["note"]
    music = ops.run_operation("asset_search", {"query": "bach", "kind": "sound", "source": "archive"}).data["results"]
    assert music[0]["id"] == "musopen-bach" and music[0]["kind"] == "sound"
