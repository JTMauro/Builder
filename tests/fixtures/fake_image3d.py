"""Stand-in for tools/image3d/triposr.py: writes a one-triangle .glb/.obj and prints stats (no torch, no model)."""
import json
import struct
import sys
from pathlib import Path

args = sys.argv[1:]
image, out = args[0], Path(args[1])
opts = {args[i]: args[i + 1] if i + 1 < len(args) and not args[i + 1].startswith("--") else True
        for i in range(2, len(args)) if args[i].startswith("--")}
if "FAKE_MISSING" in image:
    print(json.dumps({"missing": ["torch"]}))
    sys.exit(3)
if "FAKE_FAIL" in image:
    print("no object found", file=sys.stderr)
    sys.exit(1)
tri = [(0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 1.0, 0.0)]
if out.suffix == ".obj":
    out.write_text("".join(f"v {x} {y} {z} 1 0 0\n" for x, y, z in tri) + "f 1 2 3\n")
else:
    bin_ = b"".join(struct.pack("<3f", *v) for v in tri)
    js = json.dumps({"asset": {"version": "2.0"}, "buffers": [{"byteLength": len(bin_)}],
                     "bufferViews": [{"buffer": 0, "byteLength": len(bin_)}],
                     "accessors": [{"bufferView": 0, "componentType": 5126, "count": 3, "type": "VEC3", "min": [0, 0, 0], "max": [1, 1, 0]}],
                     "meshes": [{"primitives": [{"attributes": {"POSITION": 0}}]}], "nodes": [{"mesh": 0}], "scenes": [{"nodes": [0]}]}).encode()
    js += b" " * (-len(js) % 4)
    out.write_bytes(struct.pack("<III", 0x46546C67, 2, 28 + len(js) + len(bin_)) + struct.pack("<II", len(js), 0x4E4F534A) + js
                    + struct.pack("<II", len(bin_), 0x004E4942) + bin_)
print("loading...")
print(json.dumps({"vertices": 3, "faces": 1, "device": "cpu", "total_s": 0.1, "args": opts}))
