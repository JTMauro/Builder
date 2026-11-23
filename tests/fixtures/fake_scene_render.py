"""Stand-in for tools/makehuman/view/scene_render.cjs: writes grey PNG frames and two foot contacts."""
import json
import struct
import sys
import zlib
from pathlib import Path

stage, scene_path, out = sys.argv[1:4]
scene = json.loads(Path(scene_path).read_text(encoding="utf-8"))
w, h, n = scene["width"], scene["height"], round(scene["duration"] * scene["fps"])
assert (Path(stage) / "index.html").exists()
raw = b"".join(b"\0" + b"\x80\x80\x80" * w for _ in range(h))


def chunk(t, d):
    return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d))


png = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")
o = Path(out)
o.mkdir(parents=True, exist_ok=True)
for i in range(n):
    (o / f"{i + 1:06d}.png").write_bytes(png)
(o / "events.json").write_text(json.dumps([{"t": 0.5, "step": "L", "pace": "walk"}, {"t": 1.0, "step": "R", "pace": "walk"}]))
(o / "scene_seen.json").write_text(json.dumps(scene))
print(f"done: {n} frames")
