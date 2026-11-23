"""Download a set (a place with its sky/light and props) into a stage, all CC0 from Poly Haven.

    python3 -I add_set.py STAGE_DIR park|arena|...            a ready-made preset (see --list)
    python3 -I add_set.py STAGE_DIR NAME --hdri ID_OR_WORDS    any Poly Haven HDRI, by id or search words
        [--prop ID@X,Z[,ROT[,SCALE]] ...] [--height M] [--radius M] [--exposure X]
    python3 -I add_set.py STAGE_DIR props | all | refresh       handheld props / everything / update set.json data
    python3 -I add_set.py --list | --search WORDS

Writes STAGE_DIR/sets/<name>/ with the HDRI (seen as the surroundings and used as the light), glTF props
with their textures, and set.json that studio.html reads. Ground height/radius and exposure come from the
HDRI's own data (indoor or outdoor, time of day, contrast) unless given. Each prop in set.json carries an
"id" and, for things people use, a "use" block (studio.html: sit on a seat, lean on a table or wall, open a
door; see USES). Credit isn't required (CC0) but Poly Haven is named in each set.json.
"""
import argparse
import json
import math
import os
import re
import urllib.parse
import urllib.request

API = "https://api.polyhaven.com/"
UA = {"User-Agent": "Builder-sets/0.2"}
SETS = {
    "park": {"hdri": "autumn_park", "ground": {"height": 1.7, "radius": 60}, "exposure": 0.9,
             "props": [{"id": "painted_wooden_bench", "at": [-1.6, -1.2], "rot": 25}, {"id": "planter_box_01", "at": [1.9, -1.6], "rot": -10},
                       {"id": "potted_plant_02", "at": [2.3, 0.6], "rot": 0}]},
    "living_room": {"hdri": "anniversary_lounge", "ground": {"height": 1.6, "radius": 18}, "exposure": 1.0,
                    "props": [{"id": "Sofa_01", "at": [0, -2.2], "rot": 0}, {"id": "CoffeeTable_01", "at": [0, -1.0], "rot": 0},
                              {"id": "ArmChair_01", "at": [-2.0, -0.6], "rot": 60}, {"id": "potted_plant_01", "at": [2.0, -2.0], "rot": 0}]},
    "street": {"hdri": "cobblestone_street_night", "ground": {"height": 1.7, "radius": 40}, "exposure": 1.1,
               "props": [{"id": "Barrel_01", "at": [-2.0, -1.5], "rot": 0}, {"id": "plastic_crate_01", "at": [1.8, -1.4], "rot": 20}]},
    "beach": {"hdri": "secluded_beach", "ground": {"height": 1.7, "radius": 60}, "exposure": 0.85,
              "props": [{"id": "coast_rocks_01", "at": [-2.6, -2.4], "rot": 0}, {"id": "island_tree_01", "at": [3.2, -3.5], "rot": 0}]},
    "gym": {"hdri": "gym_01", "ground": {"height": 1.6, "radius": 25}, "exposure": 1.0,
            "props": [{"id": "WetFloorSign_01", "at": [2.0, -1.2], "rot": -30}]},
    "school": {"hdri": "school_quad", "ground": {"height": 1.7, "radius": 40}, "exposure": 0.9,
               "props": [{"id": "SchoolDesk_01", "at": [-1.8, -1.4], "rot": 15}, {"id": "SchoolChair_01", "at": [-1.8, -0.8], "rot": 195}]},
    "cafe": {"hdri": "comfy_cafe", "ground": {"height": 1.6, "radius": 18}, "exposure": 1.0,
             "props": [{"id": "round_wooden_table_01", "at": [0, -1.4], "rot": 0}, {"id": "WoodenChair_01", "at": [-0.8, -1.6], "rot": 60},
                       {"id": "CoffeeCart_01", "at": [2.4, -2.2], "rot": -20}]},
    "stadium": {"hdri": "stadium_01", "ground": {"height": 1.7, "radius": 70}, "exposure": 0.9,
                "props": [{"id": "football", "at": [1.2, 0.6], "rot": 0}]},
    "snow": {"hdri": "snow_field", "ground": {"height": 1.7, "radius": 60}, "exposure": 0.75,
             "props": [{"id": "fir_tree_01", "at": [-3.0, -3.2], "rot": 0}, {"id": "fir_tree_01", "at": [3.4, -4.0], "rot": 90}]},
    # a sandy round arena inside old walls: the gladiator-arena set
    "arena": {"hdri": "old_outdoor_theater", "ground": {"height": 1.7, "radius": 35}, "exposure": 0.8,
              "props": [{"id": "Barrel_01", "at": [-2.8, -2.4], "rot": 20}, {"id": "Barrel_01", "at": [-3.5, -1.9], "rot": 70}, {"id": "wooden_crate_01", "at": [2.6, -2.4], "rot": -15},
                        {"id": "painted_wooden_stool", "at": [1.8, -1.2], "rot": -20}, {"id": "door", "at": [0.0, -4.2], "rot": 0}]},
    "colosseum": {"hdri": "colosseum", "ground": {"height": 1.7, "radius": 45}, "exposure": 0.8,
                  "props": [{"id": "boulder_01", "at": [-2.8, -2.6], "rot": 0}]},
    "desert": {"hdri": "goegap", "ground": {"height": 1.7, "radius": 80}, "exposure": 0.8, "props": []},
    "ruins": {"hdri": "roofless_ruins", "ground": {"height": 1.6, "radius": 20}, "exposure": 0.95, "props": []},
    "circus": {"hdri": "circus_arena", "ground": {"height": 1.6, "radius": 22}, "exposure": 1.1, "props": []},
}
# More good HDRIs to try with --hdri (any of the ~1,000 Poly Haven HDRIs works): id -> what it looks like
GOOD = {
    "old_outdoor_theater": "sandy round arena in old walls, midday sun (preset 'arena')",
    "colosseum": "outside the Colosseum in Rome, morning sun (preset 'colosseum')",
    "amphitheatre_zanzibar_fort": "stone amphitheatre with seats, soft daylight",
    "goegap": "open desert, hard sun (preset 'desert')", "roofless_ruins": "roofless stone ruins (preset 'ruins')",
    "circus_arena": "indoor circus ring with spotlights (preset 'circus')", "venice_sunset": "Venice canal at sunset",
    "kloppenheim_06": "open field, soft sky", "brown_photostudio_02": "photo studio, soft boxes",
    "industrial_sunset_puresky": "pure sky at sunset (no ground objects)", "rooftop_night": "city rooftop at night",
    "small_empty_room_1": "small empty room", "lebombo": "hilltop lodge, bright sun",
}
# handheld things characters can hold (studio.html "prop" beats): saved to STAGE/props/<id>/
HANDHELD = ["football", "baseball_bat", "classic_laptop", "binder_notebook", "Megaphone_01", "plastic_bottle_gallon"]

# What people can do with a prop, in the prop's own space (metres, before its rotation; facing 0 = the prop's +z).
#   sit:  seats [[x, y, z], ...] where the hips go (y = seat height), face = which way a seated person looks
#   lean: at [x, z] where the feet go, face = which way they face, kind "table" (lean forward on it) | "wall" (back to it)
#   open: hinge [x, z] the door turns about, angle in degrees, seconds, stand [x, z] where the opener stands
USES = {
    "painted_wooden_bench": {"sit": {"seats": [[-0.28, 0.45, 0.02], [0.28, 0.45, 0.02]], "face": 0}, "lean": {"at": [0, 0.55], "face": 0, "kind": "wall"}},
    "painted_wooden_stool": {"sit": {"seats": [[0, 0.58, 0]], "face": 0}},
    "wooden_stool_01": {"sit": {"seats": [[0, 0.45, 0]], "face": 0}},
    "WoodenChair_01": {"sit": {"seats": [[0, 0.46, 0.02]], "face": 0}},
    "SchoolChair_01": {"sit": {"seats": [[0, 0.46, 0.04]], "face": 0}},
    "ArmChair_01": {"sit": {"seats": [[0, 0.42, 0.06]], "face": 0}},
    "Sofa_01": {"sit": {"seats": [[-0.4, 0.42, 0.04], [0.4, 0.42, 0.04]], "face": 0}},
    "round_wooden_table_01": {"lean": {"at": [0, 0.95], "face": 180, "kind": "table"}},
    "SchoolDesk_01": {"lean": {"at": [0, 0.5], "face": 180, "kind": "table"}},
    "CoffeeCart_01": {"lean": {"at": [0, 0.75], "face": 180, "kind": "table"}},
    "wooden_crate_01": {"sit": {"seats": [[0, 0.36, 0]], "face": 0}},
    "Barrel_01": {"lean": {"at": [0, 0.6], "face": 180, "kind": "table"}},
    "wooden_barrels_01": {"lean": {"at": [0, 1.65], "face": 0, "kind": "wall"}},
    # a simple built-in door (no download): frame + panel, hinged on its left edge
    "door": {"open": {"hinge": [-0.45, 0], "angle": 100, "seconds": 1.2, "stand": [0.15, 0.75]}, "lean": {"at": [0.95, 0.3], "face": 0, "kind": "wall"}},
}
BUILTIN = {"door": {"kind": "door", "size": [0.9, 2.05], "color": "#7a5636"}}


def get(url, dest):
    if os.path.exists(dest):
        return
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=300) as r:
        data = r.read()
    open(dest, "wb").write(data)


def files(aid):
    return json.load(urllib.request.urlopen(urllib.request.Request(API + "files/" + urllib.parse.quote(aid), headers=UA), timeout=60))


def info(aid):
    return json.load(urllib.request.urlopen(urllib.request.Request(API + "info/" + urllib.parse.quote(aid), headers=UA), timeout=60))


def hdris():
    return json.load(urllib.request.urlopen(urllib.request.Request(API + "assets?t=hdris", headers=UA), timeout=60))


def search_hdri(words, assets=None, limit=8):
    """Rank Poly Haven HDRIs for search words: [(id, score, name)] best first."""
    assets = assets if assets is not None else hdris()
    q = words.strip().lower().replace(" ", "_")
    toks = [t for t in re.split(r"[\s_,-]+", words.lower()) if t]
    out = []
    for aid, a in assets.items():
        idt = set(re.split(r"[_-]+", aid.lower()))
        tags = [t.lower() for t in a.get("tags", [])]
        cats = " ".join(a.get("categories", [])).lower() + " " + str(a.get("category", "")).lower()
        name, desc = str(a.get("name", "")).lower(), str(a.get("description", "")).lower()
        s = 100.0 if aid.lower() == q else 0.0
        for t in toks:
            stem = t[:-1] if len(t) > 4 and t.endswith("s") else t
            s += 30 if t in idt or stem in idt else 12 if stem in aid.lower() else 0
            s += 20 if any(stem in g for g in tags) else 0
            s += 14 if stem in name else 0
            s += 10 if stem in cats else 0
            s += 6 if stem in desc else 0
        if s > 0:
            out.append((aid, round(s + math.log10(1 + a.get("download_count", 0)), 2), a.get("name", aid)))
    out.sort(key=lambda x: -x[1])
    return out[:limit]


def defaults_for(a):
    """Sensible ground height, radius and exposure from an HDRI's data (indoor/outdoor, time of day, contrast)."""
    at = a.get("attributes") or {}
    cats = " ".join(a.get("categories", [])).lower() + " " + str(a.get("category", "")).lower()
    indoor = at.get("environment") == "indoor" or "indoor" in cats or "interior" in cats
    tod, contrast = str(at.get("time_of_day", "")), str(at.get("contrast", ""))
    if indoor:
        ground = {"height": 1.6, "radius": 18}
    elif any(w in cats for w in ("street", "square", "arena", "urban", "courtyard")):
        ground = {"height": 1.7, "radius": 35}
    else:
        ground = {"height": 1.7, "radius": 60}
    exposure = 1.25 if "night" in tod else 1.0 if tod in ("sunrise", "sunset", "golden hour") else 0.8 if contrast == "high" else 0.9
    if indoor:
        exposure = max(exposure, 1.0)
    return ground, exposure


def resolve_hdri(word, assets=None):
    """An HDRI id from an id or search words (the best match)."""
    assets = assets if assets is not None else hdris()
    if word in assets:
        return word, assets[word]
    hits = search_hdri(word, assets, 1)
    if not hits:
        raise SystemExit(f"no Poly Haven HDRI matches {word!r}")
    return hits[0][0], assets[hits[0][0]]


def parse_prop(text):
    """'id@x,z[,rot[,scale]]' -> a prop spec."""
    pid, _, rest = text.partition("@")
    nums = [float(x) for x in rest.split(",") if x.strip()] if rest else []
    nums += [0.0] * (2 - len(nums)) if len(nums) < 2 else []
    return {"id": pid.strip(), "at": nums[:2], "rot": nums[2] if len(nums) > 2 else 0, "scale": nums[3] if len(nums) > 3 else 1}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("stage", nargs="?")
    ap.add_argument("name", nargs="?", help="a preset, a new set's name (with --hdri), 'props', 'all' or 'refresh'")
    ap.add_argument("--hdri", help="Poly Haven HDRI id, or search words (the best match is used)")
    ap.add_argument("--prop", action="append", default=[], help="ID@X,Z[,ROT[,SCALE]] a Poly Haven model (or 'door') on the set")
    ap.add_argument("--height", type=float)
    ap.add_argument("--radius", type=float)
    ap.add_argument("--exposure", type=float)
    ap.add_argument("--res", default="1k")
    ap.add_argument("--hdri-res", default="2k")
    ap.add_argument("--list", action="store_true", help="show the presets and other good HDRIs")
    ap.add_argument("--search", help="search Poly Haven HDRIs and print the best matches")
    a = ap.parse_args()
    if a.list:
        for n, s in SETS.items():
            print(f"{n:12} {s['hdri']}")
        print("\nmore good HDRIs (--hdri):")
        for k, v in GOOD.items():
            print(f"  {k:30} {v}")
        return
    if a.search:
        for aid, score, name in search_hdri(a.search):
            print(f"{aid:34} {score:7.1f}  {name}")
        return
    if not (a.stage and a.name):
        ap.error("STAGE_DIR and NAME are needed")
    if a.name in ("props", "all"):
        add_handheld(a.stage, HANDHELD, a.res)
        if a.name == "props":
            return
        for n in SETS:
            build_set(a.stage, n, a.res, a.hdri_res)
        return
    if a.name == "refresh":
        for n in sorted(os.listdir(os.path.join(a.stage, "sets"))):
            if refresh_set(os.path.join(a.stage, "sets", n)):
                print("refreshed", n)
        return
    spec = None
    if a.hdri or a.prop or a.name not in SETS:
        spec = custom_spec(a.name, a.hdri or a.name, [parse_prop(x) for x in a.prop], a.height, a.radius, a.exposure)
    build_set(a.stage, a.name, a.res, a.hdri_res, spec)


def custom_spec(name, hdri_word, props, height=None, radius=None, exposure=None, assets=None):
    base = SETS.get(name, {})
    hid, meta = resolve_hdri(hdri_word, assets)
    ground, expo = defaults_for(meta)
    if base and hid == base.get("hdri"):
        ground, expo = dict(base["ground"]), base["exposure"]
    if height is not None:
        ground["height"] = height
    if radius is not None:
        ground["radius"] = radius
    return {"hdri": hid, "ground": ground, "exposure": exposure if exposure is not None else expo,
            "props": props or base.get("props", [])}


def add_handheld(stage, ids, res="1k"):
    """Fetch handheld props into STAGE/props and merge their ids into props/index.json (keeps others' entries)."""
    for pid in ids:
        fetch_model(pid, os.path.join(stage, "props", pid), res)
    ix = os.path.join(stage, "props", "index.json")
    have = json.load(open(ix)) if os.path.exists(ix) else []
    have += [p for p in ids if p not in have]
    json.dump(have, open(ix, "w"))
    print("props ready:", ", ".join(ids))
    return have


def fetch_model(pid, out, res):
    g = files(pid)["gltf"]
    g = g.get(res) or next(iter(g.values()))
    main_ = g["gltf"]
    rel = os.path.basename(main_["url"])
    get(main_["url"], os.path.join(out, rel))
    for path, inc in main_.get("include", {}).items():
        get(inc["url"], os.path.join(out, path))
    return rel


def prop_entry(p, file=None):
    e = {"id": p["id"], "at": p["at"], "rot": p.get("rot", 0), "scale": p.get("scale", 1)}
    if file:
        e["file"] = file
    e.update(BUILTIN.get(p["id"], {}))
    if p["id"] in USES:
        e["use"] = USES[p["id"]]
    return e


def build_set(stage, name, res, hdri_res, spec=None):
    spec, out = spec or SETS[name], os.path.join(stage, "sets", name)
    f = files(spec["hdri"])["hdri"]
    hd = f.get(hdri_res, f.get("1k"))["hdr"]
    get(hd["url"], os.path.join(out, "sky.hdr"))
    props = []
    for p in spec["props"]:
        if p["id"] in BUILTIN:
            props.append(prop_entry(p))
            continue
        rel = fetch_model(p["id"], os.path.join(out, p["id"]), res)
        props.append(prop_entry(p, f"{p['id']}/{rel}"))
        print("got", p["id"])
    data = {"name": name, "hdri": spec["hdri"], "sky": "sky.hdr", "ground": spec["ground"], "exposure": spec["exposure"], "props": props,
            "credit": "Poly Haven (CC0): " + ", ".join([spec["hdri"]] + [p["id"] for p in spec["props"] if p["id"] not in BUILTIN])}
    json.dump(data, open(os.path.join(out, "set.json"), "w"), indent=1)
    print("set ready:", out)
    return data


def refresh_set(sdir):
    """Add prop ids and use data (sit/lean/open) to an older set.json, without downloading anything."""
    f = os.path.join(sdir, "set.json")
    if not os.path.exists(f):
        return False
    data = json.load(open(f))
    for p in data.get("props", []):
        p.setdefault("id", (p.get("file") or "").split("/")[0])
        if p["id"] in USES:
            p["use"] = USES[p["id"]]
    if data.get("name") in SETS:
        data.setdefault("hdri", SETS[data["name"]]["hdri"])
    json.dump(data, open(f, "w"), indent=1)
    return True


if __name__ == "__main__":
    main()
