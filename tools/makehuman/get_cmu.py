"""Download CMU motion-capture clips (skeleton-only GLBs) and pack them for the viewers.

    python3 -I get_cmu.py [WORKDIR] [--pack OUT.json] [--id 05_02-... --id ...]

Downloads every clip in cmu_clips.json (plus any --id) to WORKDIR/cmu/<id>.glb (default WORKDIR
~/.builder/makehuman), skipping ones already there. --pack writes the one-file bundle the talk viewer
loads: {"credit", "source": {label: id}, "glb": {label: base64}}. Any of the ~2,400 CMU clips can be
fetched by id; browse them in metadata.jsonl at the dataset below.
Credit: CMU Graphics Lab Motion Capture Database, mocap.cs.cmu.edu (NSF EIA-0196217).
"""
import argparse
import base64
import json
import os
import time
import urllib.request

BASE = "https://huggingface.co/datasets/codepawl/cmu-mocap-glb/resolve/main/skeleton/{0}/{0}_skeleton.glb"
HERE = os.path.dirname(os.path.abspath(__file__))


def fetch(cid: str, dest: str) -> None:
    for i in range(5):
        try:
            with urllib.request.urlopen(BASE.format(cid), timeout=120) as r:
                data = r.read()
            if data[:4] != b"glTF":
                raise ValueError("not a GLB")
            open(dest, "wb").write(data)
            return
        except Exception as e:  # flaky mirror: retry with backoff
            err = e
            time.sleep(2 * (i + 1))
    raise SystemExit(f"{cid}: {err}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("workdir", nargs="?", default=os.path.expanduser("~/.builder/makehuman"))
    ap.add_argument("--pack")
    ap.add_argument("--id", action="append", default=[])
    ap.add_argument("--labels", help="comma-separated labels to put in --pack (default: all)")
    ap.add_argument("--split", help="also write one small file per clip to this folder (<label>.json) plus index.json, for loading moves on demand")
    a = ap.parse_args()
    clips = {k: v for k, v in json.load(open(os.path.join(HERE, "cmu_clips.json"))).items() if not k.startswith("_")}
    clips.update({cid: cid for cid in a.id})
    d = os.path.join(a.workdir, "cmu")
    os.makedirs(d, exist_ok=True)
    for label, cid in clips.items():
        p = os.path.join(d, cid + ".glb")
        if not os.path.exists(p):
            fetch(cid, p)
            print("got", label, cid)
    if a.pack:
        credit = json.load(open(os.path.join(HERE, "cmu_clips.json")))["_credit"]
        keep = set(a.labels.split(",")) if a.labels else set(clips)
        glb = {k: base64.b64encode(open(os.path.join(d, v + ".glb"), "rb").read()).decode() for k, v in clips.items() if k in keep}
        json.dump({"credit": credit, "source": clips, "glb": glb}, open(a.pack, "w"))
        print("packed", len(glb), "clips ->", a.pack)
    if a.split:
        os.makedirs(a.split, exist_ok=True)
        index = {}
        for k, v in clips.items():
            f = "".join(c if c.isalnum() else "-" for c in k) + ".json"
            json.dump({"label": k, "id": v, "glb": base64.b64encode(open(os.path.join(d, v + ".glb"), "rb").read()).decode()}, open(os.path.join(a.split, f), "w"))
            index[k] = f
        json.dump(index, open(os.path.join(a.split, "index.json"), "w"), indent=1)
        print("split", len(index), "clips ->", a.split)


if __name__ == "__main__":
    main()
