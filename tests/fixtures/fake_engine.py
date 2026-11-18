#!/usr/bin/env python3
"""Stand-in for craft-anything-cli in tests: speaks the `rebuild --events-json` protocol."""
import json
import os
import sys
import time
from pathlib import Path


def emit(obj):
    print(json.dumps(obj), flush=True)


args = sys.argv[1:]
if args[:1] == ["--version"]:
    print("craft-anything-cli 0.0.0-test")
    sys.exit(0)
if args[:1] in (["plugins-add"], ["plugins-refresh"]):
    log = os.environ.get("FAKE_ENGINE_LOG")
    if log:
        with open(log, "a") as f:
            f.write(json.dumps(args) + "\n")
    print("{}")
    sys.exit(0)
if args[:1] == ["ai"]:
    print("builder-claude  anthropic" if args[1:2] == ["list"] else "added")
    sys.exit(0)
if args[:1] != ["rebuild"] or "--events-json" not in args:
    print("unexpected: " + " ".join(args), file=sys.stderr)
    sys.exit(2)

target, request = args[1], args[2]
if "--continue-in" in args:
    project = Path(args[args.index("--continue-in") + 1])
else:
    project = Path(args[args.index("--projects-dir") + 1]) / "made-by-test"
project.mkdir(parents=True, exist_ok=True)
(project / "checks").mkdir(exist_ok=True)
import base64
(project / "checks" / "shot.png").write_bytes(base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="))
emit({"type": "project", "path": str(project)})
emit({"type": "step", "step": "study", "text": f"Studying {target}"})
emit({"type": "text", "text": "Looking at "})
emit({"type": "text", "text": "the page.\n"})
print("a log line", file=sys.stderr, flush=True)
emit({"type": "ask", "request": {"id": "q1", "kind": "approval", "title": "Run `npm install`", "detail": "",
                                 "choices": [], "allow_text": False, "action": "bash:npm", "created_ms": 1}})
answer = json.loads(sys.stdin.readline() or "{}")
allowed = answer.get("answer", {}).get("allow")
emit({"type": "activity", "tool": "build_file_write", "words": "Writing app/index.html"})
emit({"type": "check", "check": {"round": 1, "check": "web_visual", "passed": True, "similarity": 87.5,
                                 "note": "Header matches", "artifacts": ["checks/shot.png"], "at_ms": 2}})
emit({"type": "preview", "url": "http://127.0.0.1:5199/"})
(project / "craft.json").write_text(json.dumps({
    "version": 3, "name": "Made by test", "request": request, "target": {"kind": "website", "url": target},
    "stack": "React", "status": "done", "note": "", "created_ms": 1, "updated_ms": int(time.time() * 1000),
    "checks": [{"round": 1, "check": "web_visual", "passed": True, "similarity": 87.5, "note": "ok",
                "artifacts": ["checks/shot.png"], "at_ms": 2}],
}))
if os.environ.get("FAKE_ENGINE_HANG"):
    time.sleep(60)
emit({"type": "finished", "ok": bool(allowed), "message": "Built" if allowed else "npm was refused", "paused": False})
