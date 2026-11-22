"""Build one MakeHuman person from a settings file (as Craft Studio's "Real human" sends them).

    python3 -I run_job.py params.json OUT_NAME [WORKDIR]

params.json keys (all optional): age, gender, height, weight, muscle (numbers), skinRGB, hairRGB,
topRGB, shoesRGB ([r, g, b] sRGB 0..1), hair, eyes, outfit, shoes (asset names).
Writes WORKDIR/mhgen/OUT_NAME.glb and OUT_NAME.json (one-file glTF) and prints their paths.
"""
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
p = json.load(open(sys.argv[1]))
out = sys.argv[2]
S = sys.argv[3] if len(sys.argv) > 3 else os.path.expanduser("~/.builder/makehuman")
rgb = lambda v, d: ",".join(str(round(float(x), 3)) for x in v) if isinstance(v, list) and len(v) == 3 else d

env = dict(os.environ)
env.update({
    "OUT": out,
    "AGE": str(p.get("age", 25)), "GENDER": str(p.get("gender", 1)),
    "HEIGHT": str(p.get("height", 0.5)), "WEIGHT": str(p.get("weight", 0.5)), "MUSCLE": str(p.get("muscle", 0.5)),
    "SKIN_RGB": rgb(p.get("skinRGB"), ".73,.53,.39"), "HAIR_RGB": rgb(p.get("hairRGB"), ".075,.05,.035"),
    "TOP_RGB": rgb(p.get("topRGB"), "keep"), "SHOES_RGB": rgb(p.get("shoesRGB"), ""), "JEANS_LIGHT": str(p.get("jeansLight", "")), "SMILE": str(p.get("smile", 0)),
    "HAIR": p.get("hair") or "short02", "EYE": p.get("eyes") or "brown_eye",
    "CLOTHES": p.get("outfit") or "male_casualsuit06", "SHOES": p.get("shoes") or "shoes05",
    "BLENDER_USER_RESOURCES": S + "/bhome",
})
def run(cmd):
    r = subprocess.run(cmd, env=env, capture_output=True, text=True)
    if r.returncode:
        sys.stderr.write(r.stdout[-3000:] + r.stderr[-3000:])
        sys.exit(f"step failed: {' '.join(cmd[:3])}")
    return r.stdout

run(["python3", "-I", HERE + "/tex.py", S + "/mhassets", S + "/mhtex"])
run([S + "/mhenv/bin/python", "-I", HERE + "/gen.py", S])
glb = f"{S}/mhgen/{out}.glb"
run(["python3", "-I", HERE + "/glb2json.py", glb, f"{S}/mhgen/{out}.json"])
print(glb)
print(f"{S}/mhgen/{out}.json")
