"""Build a stage: the folder Builder's scene_video renders (and that you can publish or open as a page).

    python3 -I make_stage.py PERSON_morph.glb STAGE_DIR [--hdri studio.hdr] [--xbot Xbot.glb] [--workdir ~/.builder/makehuman]

PERSON_morph.glb is a MakeHuman person with talk morphs (run_job.py with "morphs": "talk"). The stage gets:
index.html (view/talk.html), anim.js, models/teen.json, models/light.json (the HDRI), clips/mixamo-clips.json
(three.js's MIT sample moves from Xbot.glb), clips/cmu-clips.json + clips/cmu/ (CMU motion capture) and an
empty sound/lines.json, plus studio.html (several characters, sets, camera shots), extra characters
(--model friend=FRIEND_morph.glb) and sets (--set park). Default STAGE_DIR for Builder: ~/.builder/stages/<name>.
"""
import argparse
import base64
import json
import os
import shutil
import subprocess
import sys
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
XBOT = "https://cdn.jsdelivr.net/gh/mrdoob/three.js@r160/examples/models/gltf/Xbot.glb"
HDRI = "https://dl.polyhaven.org/file/ph-assets/HDRIs/hdr/1k/studio_small_09_1k.hdr"
CORE = "jump,sit,dance,shrug,bow,stretch,kick,pickup,hello,laugh"


def fetch(url, dest):
    if not os.path.exists(dest):
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with urllib.request.urlopen(url, timeout=300) as r:
            open(dest, "wb").write(r.read())
    return dest


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("glb")
    ap.add_argument("stage")
    ap.add_argument("--workdir", default=os.path.expanduser("~/.builder/makehuman"))
    ap.add_argument("--hdri")
    ap.add_argument("--xbot")
    ap.add_argument("--model", action="append", default=[], metavar="NAME=PERSON_morph.glb", help="another character for several-character scenes")
    ap.add_argument("--set", action="append", default=[], help="add a set: park, living_room, street (add_set.py)")
    a = ap.parse_args()
    S = a.stage
    for d in ("models", "clips", "sound"):
        os.makedirs(os.path.join(S, d), exist_ok=True)
    shutil.copy(os.path.join(HERE, "view", "talk.html"), os.path.join(S, "index.html"))
    shutil.copy(os.path.join(HERE, "view", "anim.js"), os.path.join(S, "anim.js"))
    shutil.copy(os.path.join(HERE, "view", "studio.html"), os.path.join(S, "studio.html"))
    py = [sys.executable, "-I"]
    subprocess.run(py + [os.path.join(HERE, "glb2json.py"), a.glb, os.path.join(S, "models", "teen.json")], check=True)
    xbot = a.xbot or fetch(XBOT, os.path.join(a.workdir, "dl", "Xbot.glb"))
    subprocess.run(py + [os.path.join(HERE, "glb2json.py"), xbot, os.path.join(S, "clips", "mixamo-clips.json")], check=True)
    hdr = a.hdri or fetch(HDRI, os.path.join(a.workdir, "dl", "studio.hdr"))
    json.dump({"name": os.path.basename(hdr), "by": "Poly Haven (CC0)", "hdr": base64.b64encode(open(hdr, "rb").read()).decode()},
              open(os.path.join(S, "models", "light.json"), "w"))
    subprocess.run(py + [os.path.join(HERE, "get_cmu.py"), a.workdir, "--pack", os.path.join(S, "clips", "cmu-clips.json"),
                         "--labels", CORE, "--split", os.path.join(S, "clips", "cmu")], check=True)
    for spec in a.model:
        name, glb = spec.split("=", 1)
        subprocess.run(py + [os.path.join(HERE, "glb2json.py"), glb, os.path.join(S, "models", name + ".json")], check=True)
    for name in a.set:
        subprocess.run(py + [os.path.join(HERE, "add_set.py"), S, name], check=True)
    lines = os.path.join(S, "sound", "lines.json")
    if not os.path.exists(lines):
        json.dump({"voice": "", "lines": []}, open(lines, "w"))
    print("stage ready:", S)


if __name__ == "__main__":
    main()
