"""Stand-in for UnrealEditor / UnrealEditor-Cmd in tests.

Render mode (-ExecutePythonScript=render.py): reads OUT_DIR / STATUS / WIDTH / HEIGHT from Builder's
script and writes a few PNG frames named like Movie Render Queue does, then the status file.
Script mode (-run=pythonscript -script=x.py): prints the script's lines as LogPython output.
FAKE_UNREAL_FAIL=1 makes a render report failure.
"""
import os
import re
import subprocess
import sys

args = sys.argv[1:]
render = next((a.split("=", 1)[1] for a in args if a.startswith("-ExecutePythonScript=")), None)
script = next((a.split("=", 1)[1] for a in args if a.startswith("-script=")), None)
if render:
    src = open(render).read()
    get = lambda k: eval(re.search(rf"^{k} = (.+)$", src, re.M).group(1))  # noqa: E731
    out, status = get("OUT_DIR"), get("STATUS")
    w, h = re.search(r"^WIDTH, HEIGHT, FPS = (\d+), (\d+), (\d+)$", src, re.M).groups()[:2]
    if os.environ.get("FAKE_UNREAL_FAIL"):
        open(status, "w").write('{"ok": false, "message": "fake failure"}')
        sys.exit(0)
    seq = get("SEQUENCE").rsplit("/", 1)[-1]
    for i in range(6):
        subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", f"testsrc=size={w}x{h}",
                        "-frames:v", "1", os.path.join(out, f"{seq}.{i + 10:04d}.png")], check=True)
    print("LogMovieRenderPipeline: Display: fake render done")
    open(status, "w").write('{"ok": true, "message": ""}')
elif script:
    for line in open(script).read().splitlines():
        print(f"LogPython: {line}")
else:
    sys.exit(2)
