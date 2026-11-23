"""Photoreal stills of a MakeHuman character with Blender Cycles (free; tools/makehuman/cycles.py).

The character is a .blend made by tools/makehuman/run_job.py, found by name in
<data>/makehuman/mhgen (or BUILDER_MAKEHUMAN_DIR). CPU rendering is slow (1080p: minutes; 4K: about
half an hour); a GPU machine is much faster. Start with wait=false.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from .config import NO_WINDOW, get_settings
from .ops import OpError, OpResult, _guarded, operation

CYCLES = Path(__file__).resolve().parents[1] / "tools" / "makehuman" / "cycles.py"


def makehuman_dir() -> Path:
    return Path(os.environ.get("BUILDER_MAKEHUMAN_DIR") or (get_settings().data_dir / "makehuman")).expanduser()


def characters() -> list[str]:
    g = makehuman_dir() / "mhgen"
    return sorted(p.stem for p in g.glob("*.blend") if "__" not in p.stem) if g.exists() else []


class StillParams(BaseModel):
    character: str = Field(description="Character name (a .blend made by the MakeHuman tools)")
    shot: Literal["full", "medium", "closeup", "portrait3q"] = "full"
    resolution: Literal["960x540", "1920x1080", "1080x1350", "1080x1920", "3840x2160"] = "1920x1080"
    samples: int = Field(64, ge=4, le=1024)
    pose: Literal["none", "relaxed"] = "relaxed"
    smile: float | None = Field(None, ge=0, le=1)
    dof: bool = Field(False, description="Depth of field focused on the eyes")
    angle: float = Field(0, ge=-180, le=180, description="Camera angle around the person (degrees)")
    backdrop: Literal["grey", "white", "none", "shadow"] = Field("grey", description="shadow: transparent PNG with only the shadow")


@operation("render_still", StillParams, "Render a photoreal still of a MakeHuman character with Blender Cycles (skin subsurface, studio HDRI light, depth of field, up to 4K). Slow on CPU: start with wait=false.")
def render_still(p: StillParams) -> OpResult:
    mh = makehuman_dir()
    blend = mh / "mhgen" / f"{p.character}.blend"
    py = mh / "mhenv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if not py.exists():
        raise OpError("Blender + MakeHuman aren't installed (run tools/setup_studio.sh)")
    if not blend.exists():
        raise OpError(f"no character called {p.character!r} (have: {', '.join(characters()) or 'none'})")
    args = [str(py), "-I", str(CYCLES), str(blend), "OUT", "--shot", p.shot, "--res", p.resolution, "--samples", str(p.samples),
            "--pose", p.pose, "--angle", str(p.angle), "--backdrop", p.backdrop]
    if p.smile is not None:
        args += ["--smile", str(p.smile)]
    if p.dof:
        args.append("--dof")
    env = dict(os.environ, BLENDER_USER_RESOURCES=str(mh / "bhome"))

    def build(out: Path) -> None:
        a = [str(out) if x == "OUT" else x for x in args]
        proc = subprocess.run(a, capture_output=True, text=True, env=env, **NO_WINDOW)
        if proc.returncode != 0 or not out.exists():
            raise OpError("Cycles render failed:\n" + "\n".join((proc.stderr or proc.stdout).strip().splitlines()[-8:]))

    return _guarded("render_still", [], f"{p.character}_{p.shot}_{p.resolution}.png", build,
                    data={"engine": "Blender Cycles", "character": p.character, "samples": p.samples})
