"""Stand-in for tools/makehuman/view/export_glb.cjs: writes a GLB-sized file, a preview PNG and the report."""
import json
import sys
from pathlib import Path

stage, job_path = sys.argv[1], sys.argv[2]
job = json.loads(Path(job_path).read_text(encoding="utf-8"))
assert (Path(stage) / "models" / f"{job['model']}.json").exists()
if job["model"] == "broken":
    sys.exit("stage failed to load: boom")
Path(job["out"]).write_bytes(b"glTF" + b"\0" * 4000)
drop = job["moves"][-1] if job["model"] == "lossy" else None
clips = [{"name": m, "duration": 1.0, "tracks": 53} for m in job["moves"] if m != drop]
if job.get("check") and job["check"].get("png"):
    Path(job["check"]["png"]).write_bytes(b"\x89PNG\r\n\x1a\n" + b"\0" * 100)
Path(job["report"]).write_text(json.dumps({"exported": {"clips": clips, "bytes": 4004},
                                           "loaded": {"clips": clips, "morphTargets": 13 if job["morphs"] else 0}, "job": job}))
