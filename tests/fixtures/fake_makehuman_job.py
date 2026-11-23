"""Stand-in for tools/makehuman/run_job.py: writes WORKDIR/mhgen/NAME.glb and NAME.json and keeps the params it got."""
import json
import sys
from pathlib import Path

params, name, work = sys.argv[1], sys.argv[2], Path(sys.argv[3])
p = json.loads(Path(params).read_text(encoding="utf-8"))
if p.get("age") == 1:   # tests: a failing build
    sys.exit("step failed: gen.py")
g = work / "mhgen"
g.mkdir(parents=True, exist_ok=True)
(g / f"{name}.glb").write_bytes(b"glTF" + b"\0" * 64)
(g / f"{name}.json").write_text(json.dumps({"asset": {"version": "2.0"}, "fake": True}))
(g / f"{name}_params_seen.json").write_text(json.dumps(p))
print(g / f"{name}.glb")
