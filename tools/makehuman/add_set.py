"""Download a ready-made set (a place with its sky/light and props) into a stage, all CC0 from Poly Haven.

    python3 -I add_set.py STAGE_DIR park|living_room|street [--res 1k]

Writes STAGE_DIR/sets/<name>/ with the HDRI (seen as the surroundings and used as the light), glTF props
with their textures, and set.json that studio.html reads. Credit isn't required (CC0) but Poly Haven is
named in each set.json.
"""
import argparse
import json
import os
import urllib.request

API = "https://api.polyhaven.com/files/"
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
}
# small things characters can hold (studio.html "hold" beats): saved to STAGE/props/<id>/
HANDHELD = ["football", "baseball_bat", "classic_laptop", "binder_notebook", "Megaphone_01", "plastic_bottle_gallon"]


def get(url, dest):
    if os.path.exists(dest):
        return
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    req = urllib.request.Request(url, headers={"User-Agent": "Builder-sets/0.1"})
    with urllib.request.urlopen(req, timeout=300) as r:
        data = r.read()
    open(dest, "wb").write(data)


def files(aid):
    req = urllib.request.Request(API + aid, headers={"User-Agent": "Builder-sets/0.1"})
    return json.load(urllib.request.urlopen(req, timeout=60))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("stage")
    ap.add_argument("name", choices=sorted(SETS) + ["props", "all"], help="a set, 'props' (handheld things) or 'all'")
    ap.add_argument("--res", default="1k")
    ap.add_argument("--hdri-res", default="2k")
    a = ap.parse_args()
    if a.name in ("props", "all"):
        for pid in HANDHELD:
            fetch_model(pid, os.path.join(a.stage, "props", pid), a.res)
        json.dump(HANDHELD, open(os.path.join(a.stage, "props", "index.json"), "w"))
        print("props ready")
        if a.name == "props":
            return
        for n in SETS:
            build_set(a.stage, n, a.res, a.hdri_res)
        return
    build_set(a.stage, a.name, a.res, a.hdri_res)


def fetch_model(pid, out, res):
    g = files(pid)["gltf"]
    g = g.get(res) or next(iter(g.values()))
    main_ = g["gltf"]
    rel = os.path.basename(main_["url"])
    get(main_["url"], os.path.join(out, rel))
    for path, inc in main_.get("include", {}).items():
        get(inc["url"], os.path.join(out, path))
    return rel


def build_set(stage, name, res, hdri_res):
    spec, out = SETS[name], os.path.join(stage, "sets", name)
    class A:  # keep the old body's names
        pass
    a = A(); a.name, a.res, a.hdri_res = name, res, hdri_res
    f = files(spec["hdri"])["hdri"]
    hd = f.get(a.hdri_res, f.get("1k"))["hdr"]
    get(hd["url"], os.path.join(out, "sky.hdr"))
    props = []
    for p in spec["props"]:
        rel = fetch_model(p["id"], os.path.join(out, p["id"]), a.res)
        props.append({"file": f"{p['id']}/{rel}", "at": p["at"], "rot": p["rot"], "scale": p.get("scale", 1)})
        print("got", p["id"])
    json.dump({"name": a.name, "sky": "sky.hdr", "ground": spec["ground"], "exposure": spec["exposure"], "props": props,
               "credit": "Poly Haven (CC0): " + ", ".join([spec["hdri"]] + [p["id"] for p in spec["props"]])},
              open(os.path.join(out, "set.json"), "w"), indent=1)
    print("set ready:", out)


if __name__ == "__main__":
    main()
