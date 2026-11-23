"""Stand-in for tools/model3d/autorig.py: writes a tiny skinned GLB with Mixamo bone names (no Blender)."""
import json
import struct
import sys
from pathlib import Path

src, out = sys.argv[1], Path(sys.argv[2])
if "FAKE_FAIL" in src:
    print("no mesh in the file", file=sys.stderr)
    sys.exit(1)
names = ["Hips", "Spine", "Spine1", "Spine2", "Neck", "Head"] + [f"{s}{b}" for s in ("Left", "Right") for b in
                                                                ("Shoulder", "Arm", "ForeArm", "Hand", "UpLeg", "Leg", "Foot", "ToeBase")]
if "FAKE_NOHEAD" in src:
    names.remove("Head")
nodes = [{"name": "mixamorig:" + n} for n in names] + [{"name": "Body", "mesh": 0, "skin": 0}]
doc = {"asset": {"version": "2.0"}, "nodes": nodes, "skins": [{"joints": list(range(len(names)))}], "meshes": [{"primitives": []}],
       "scenes": [{"nodes": [0, len(nodes) - 1]}]}
js = json.dumps(doc).encode()
js += b" " * (-len(js) % 4)
out.write_bytes(struct.pack("<III", 0x46546C67, 2, 20 + len(js)) + struct.pack("<II", len(js), 0x4E4F534A) + js)
print("export done")
print(json.dumps({"height": 1.75, "crotch": 0.47, "neck": 0.84, "front": "-y", "weights": "heat", "args": sys.argv[3:],
                  "joints_m": {"Hips": [0, 0, 0.95]}}))
