"""Unreal Engine bridge: render Level Sequences (MetaHumans included) to finished video, from Claude.

Builder drives the Unreal Editor installed on this computer:

- unreal_status: finds the editor and your projects and checks the plugins a render needs.
- unreal_render: renders a Level Sequence with Movie Render Queue (4K by default, path tracing
  optional) to PNG frames, then encodes them: ProRes for DaVinci Resolve, H.265 or H.264.
- unreal_script: runs an editor Python script headless (off unless UNREAL_ALLOW_SCRIPTS=on).

Settings (builder.env): UNREAL_EDITOR (UnrealEditor.exe, or the UE_5.x folder), UNREAL_PROJECT
(a .uproject file), UNREAL_ALLOW_SCRIPTS. Without them Builder looks in the usual Epic Games and
"Documents/Unreal Projects" folders.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from .config import NO_WINDOW, get_settings
from .ops import OpError, OpResult, _ffmpeg, _guarded, operation

NEEDED_PLUGINS = {"MovieRenderPipeline": "Movie Render Queue", "PythonScriptPlugin": "Python Editor Script Plugin"}


# ---------------------------------------------------------------- finding Unreal


def _version_key(p: Path) -> tuple:
    m = re.search(r"UE_(\d+)\.(\d+)", str(p))
    return (int(m.group(1)), int(m.group(2))) if m else (0, 0)


def _editor_in(folder: Path) -> Path | None:
    for rel in ("Engine/Binaries/Win64/UnrealEditor.exe", "Engine/Binaries/Linux/UnrealEditor",
                "Engine/Binaries/Mac/UnrealEditor.app/Contents/MacOS/UnrealEditor"):
        if (folder / rel).exists():
            return folder / rel
    return None


def find_editor() -> Path | None:
    env = os.environ.get("UNREAL_EDITOR", "").strip().strip('"')
    if env:
        p = Path(env).expanduser()
        return p if p.is_file() else _editor_in(p)
    roots = []
    for base in (os.environ.get("ProgramFiles", r"C:\Program Files"), r"C:\Program Files", r"D:\Program Files", "D:\\", "E:\\"):
        roots.append(Path(base) / "Epic Games")
    roots += [Path.home() / "UnrealEngine", Path("/Users/Shared/Epic Games")]
    found = []
    for r in roots:
        try:
            for d in r.glob("UE_5.*"):
                e = _editor_in(d)
                if e:
                    found.append(e)
        except OSError:
            continue
        e = _editor_in(r) if r.name == "UnrealEngine" else None
        if e:
            found.append(e)
    return max(found, key=_version_key) if found else None


def _cmd_editor(editor: Path) -> Path:
    """The console build (UnrealEditor-Cmd) for headless work, when it sits beside the editor."""
    cmd = editor.with_name(editor.stem + "-Cmd" + editor.suffix)
    return cmd if cmd.exists() else editor


def find_projects() -> list[Path]:
    out: list[Path] = []
    env = os.environ.get("UNREAL_PROJECT", "").strip().strip('"')
    if env:
        out.append(Path(env).expanduser())
    for docs in (Path.home() / "Documents" / "Unreal Projects", Path.home() / "OneDrive" / "Documents" / "Unreal Projects"):
        try:
            out += sorted(docs.glob("*/*.uproject"), key=lambda p: p.stat().st_mtime, reverse=True)
        except OSError:
            pass
    seen, uniq = set(), []
    for p in out:
        if p.exists() and p.resolve() not in seen:
            seen.add(p.resolve())
            uniq.append(p)
    return uniq


def project_plugins(uproject: Path) -> dict[str, bool]:
    try:
        data = json.loads(uproject.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}
    return {pl.get("Name", ""): bool(pl.get("Enabled", False)) for pl in data.get("Plugins", [])}


def _project(path: str | None) -> Path:
    if path:
        p = Path(path).expanduser()
        if p.is_dir():
            hits = sorted(p.glob("*.uproject"))
            if not hits:
                raise OpError(f"No .uproject file in {p}")
            p = hits[0]
        if not p.exists():
            raise OpError(f"Project not found: {p}")
        return p
    projects = find_projects()
    if not projects:
        raise OpError("No Unreal project found. Set UNREAL_PROJECT in builder.env, or pass project=<path to .uproject>.")
    return projects[0]


def _editor() -> Path:
    e = find_editor()
    if not e:
        raise OpError("Unreal Editor not found. Install Unreal Engine 5 with the Epic Games Launcher, or set UNREAL_EDITOR in builder.env.")
    return e


def scripts_allowed() -> bool:
    return os.environ.get("UNREAL_ALLOW_SCRIPTS", "off").lower() in ("on", "1", "true", "yes")


def status() -> dict[str, Any]:
    editor = find_editor()
    projects = []
    for p in find_projects()[:10]:
        plugins = project_plugins(p)
        missing = [label for name, label in NEEDED_PLUGINS.items() if not plugins.get(name)]
        metahuman = any("metahuman" in n.lower() and on for n, on in plugins.items())
        projects.append({"path": str(p), "name": p.stem, "missing_plugins": missing, "metahuman_plugin": metahuman})
    steps = []
    if not editor:
        steps.append("Install Unreal Engine 5 (Epic Games Launcher > Unreal Engine > Library), or set UNREAL_EDITOR in builder.env.")
    if not projects:
        steps.append("Create a project (Film/Video & Live Events > Blank is a good start), or set UNREAL_PROJECT in builder.env.")
    for p in projects[:1]:
        if p["missing_plugins"]:
            steps.append(f"In {p['name']}: Edit > Plugins, enable {', '.join(p['missing_plugins'])}, then restart the editor.")
    return {"editor": str(editor) if editor else None, "version": ".".join(map(str, _version_key(editor))) if editor else None,
            "projects": projects, "scripts_allowed": scripts_allowed(), "ready": bool(editor and projects and not projects[0]["missing_plugins"]),
            "next_steps": steps}


# ---------------------------------------------------------------- running the editor


def _arg_path(p: Path) -> str:
    """A path Unreal's command line can read: no spaces (Windows short name when needed)."""
    s = str(p)
    if " " in s and sys.platform == "win32":
        import ctypes

        buf = ctypes.create_unicode_buffer(1024)
        if ctypes.windll.kernel32.GetShortPathNameW(s, buf, 1024):
            return buf.value
    return s


def _run_editor(args: list[str], log_path: Path, timeout: int) -> int:
    with open(log_path, "w", encoding="utf-8", errors="replace") as log:
        try:
            proc = subprocess.run(args, stdout=log, stderr=subprocess.STDOUT, timeout=timeout, **NO_WINDOW)
        except FileNotFoundError as e:
            raise OpError(f"Couldn't start Unreal: {args[0]}") from e
        except subprocess.TimeoutExpired as e:
            raise OpError(f"Unreal didn't finish within {timeout // 60} minutes (log: {log_path})") from e
    return proc.returncode


def _tail(path: Path, n: int = 25) -> str:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    keep = [line for line in lines if re.search(r"error|warning|python|moviepipeline|LogPython", line, re.I)] or lines
    return "\n".join(keep[-n:])


def _work_dir(kind: str) -> Path:
    d = get_settings().data_dir / "unreal" / f"{kind}-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
    d.mkdir(parents=True, exist_ok=True)
    return d


RENDER_SCRIPT = r'''# Written by Builder: render one Level Sequence with Movie Render Queue, then close the editor.
import json
import unreal

SEQUENCE = {sequence!r}
MAP = {map!r}
OUT_DIR = {out_dir!r}
STATUS = {status!r}
WIDTH, HEIGHT, FPS = {width}, {height}, {fps}
PATH_TRACING = {path_tracing}
SAMPLES = {samples}
WARMUP = {warmup}


def finish(ok, message=""):
    with open(STATUS, "w") as f:
        json.dump({{"ok": bool(ok), "message": message}}, f)
    unreal.SystemLibrary.quit_editor()


def on_finished(executor, success):
    finish(success, "" if success else "Movie Render Queue reported a failed render; see the log.")


try:
    subsystem = unreal.get_editor_subsystem(unreal.MoviePipelineQueueSubsystem)
    queue = subsystem.get_queue()
    queue.delete_all_jobs()
    job = queue.allocate_new_job(unreal.MoviePipelineExecutorJob)
    job.job_name = "Builder"
    job.sequence = unreal.SoftObjectPath(SEQUENCE)
    world = MAP or unreal.EditorLevelLibrary.get_editor_world().get_path_name().split(".")[0]
    job.map = unreal.SoftObjectPath(world)
    config = job.get_configuration()
    out = config.find_or_add_setting_by_class(unreal.MoviePipelineOutputSetting)
    out.output_directory = unreal.DirectoryPath(OUT_DIR)
    out.output_resolution = unreal.IntPoint(WIDTH, HEIGHT)
    out.file_name_format = "{{sequence_name}}.{{frame_number}}"
    out.use_custom_frame_rate = True
    out.output_frame_rate = unreal.FrameRate(FPS, 1)
    if PATH_TRACING:
        config.find_or_add_setting_by_class(unreal.MoviePipelineDeferredPass_PathTracer)
    else:
        config.find_or_add_setting_by_class(unreal.MoviePipelineDeferredPassBase)
    config.find_or_add_setting_by_class(unreal.MoviePipelineImageSequenceOutput_PNG)
    aa = config.find_or_add_setting_by_class(unreal.MoviePipelineAntiAliasingSetting)
    aa.override_anti_aliasing = True
    aa.anti_aliasing_method = unreal.AntiAliasingMethod.AAM_NONE
    aa.spatial_sample_count = SAMPLES
    aa.temporal_sample_count = 1
    aa.engine_warm_up_count = WARMUP
    executor = unreal.MoviePipelinePIEExecutor()
    executor.on_executor_finished_delegate.add_callable_unique(on_finished)
    unreal.BuilderExecutor = executor  # keep it alive while it renders
    subsystem.render_queue_with_executor_instance(executor)
except Exception as e:
    finish(False, repr(e))
'''


def _encode(frames: list[Path], fps: int, encode: str, name: str) -> OpResult:
    m = re.match(r"^(.*?)(\d+)\.png$", frames[0].name)
    if not m:
        raise OpError(f"Unexpected frame names ({frames[0].name})")
    prefix, digits = m.group(1), m.group(2)
    pattern = str(frames[0].parent / f"{prefix}%0{len(digits)}d.png")
    common = ["-framerate", str(fps), "-start_number", str(int(digits)), "-i", pattern]
    if encode == "prores":
        enc, ext = ["-c:v", "prores_ks", "-profile:v", "3", "-pix_fmt", "yuv422p10le", "-vendor", "apl0"], "mov"
    elif encode == "h265":
        enc, ext = ["-c:v", "libx265", "-preset", "medium", "-crf", "16", "-pix_fmt", "yuv420p10le", "-tag:v", "hvc1", "-movflags", "+faststart"], "mp4"
    else:
        enc, ext = ["-c:v", "libx264", "-preset", "slow", "-crf", "14", "-pix_fmt", "yuv420p", "-movflags", "+faststart"], "mp4"
    return _guarded("unreal_render", [], f"{name}.{ext}", lambda out: _ffmpeg(*common, *enc, str(out)),
                    data={"frames": len(frames), "fps": fps, "frames_dir": str(frames[0].parent)})


# ---------------------------------------------------------------- operations


class UnrealStatusParams(BaseModel):
    pass


@operation("unreal_status", UnrealStatusParams, "Check Unreal Engine on this computer: where the editor and projects are, and which plugins a render still needs (Movie Render Queue, Python, MetaHuman).", read_only=True)
def unreal_status(_: UnrealStatusParams) -> OpResult:
    return OpResult(data=status())


class UnrealRenderParams(BaseModel):
    sequence: str = Field(description="Level Sequence asset path, e.g. /Game/Cinematics/TeenWave (right-click it in the Content Browser > Copy Reference)")
    map: str | None = Field(None, description="Level (map) to render in, e.g. /Game/Maps/Kitchen (default: the project's start-up level)")
    project: str | None = Field(None, description="Path to the .uproject (default: UNREAL_PROJECT, else the most recent project)")
    width: int = Field(3840, ge=256, le=7680, description="Output width (3840 = 4K UHD)")
    height: int = Field(2160, ge=144, le=4320, description="Output height (2160 = 4K UHD)")
    fps: int = Field(24, ge=12, le=120, description="Frames per second (24 is the film look)")
    path_tracing: bool = Field(False, description="Film-quality path-traced lighting (much slower; needs a ray-tracing GPU)")
    samples: int = Field(8, ge=1, le=256, description="Samples per pixel: higher = smoother light and motion blur, slower (path tracing likes 64+)")
    encode: Literal["prores", "h265", "h264", "frames"] = Field("prores", description="prores = best for DaVinci Resolve; h265/h264 = smaller files; frames = keep the PNGs only")
    timeout_minutes: int = Field(240, ge=5, le=1440, description="Give up after this long")


@operation("unreal_render", UnrealRenderParams, "Render an Unreal Engine Level Sequence (e.g. a MetaHuman scene) with Movie Render Queue on this computer, 4K by default, and encode it to a video (ProRes for DaVinci Resolve, H.265 or H.264). Takes minutes to hours: start it with wait=false and poll get_job.")
def unreal_render(p: UnrealRenderParams) -> OpResult:
    editor, project = _editor(), _project(p.project)
    plugins = project_plugins(project)
    missing = [label for name, label in NEEDED_PLUGINS.items() if not plugins.get(name)]
    if missing:
        raise OpError(f"{project.stem} needs these plugins enabled first (Edit > Plugins, then restart Unreal): {', '.join(missing)}")
    work = _work_dir("render")
    frames_dir, status_file, script = work / "frames", work / "status.json", work / "render.py"
    frames_dir.mkdir()
    script.write_text(RENDER_SCRIPT.format(
        sequence=p.sequence, map=p.map or "", out_dir=str(frames_dir), status=str(status_file), width=p.width,
        height=p.height, fps=p.fps, path_tracing=p.path_tracing, samples=p.samples, warmup=32 if p.path_tracing else 8), encoding="utf-8")
    log = work / "unreal.log"
    args = [str(editor), _arg_path(project), f"-ExecutePythonScript={_arg_path(script)}", "-windowed", "-ResX=1280", "-ResY=720",
            "-nosplash", "-log", "-stdout", "-FullStdOutLogOutput"]
    code = _run_editor(args, log, p.timeout_minutes * 60)
    try:
        st = json.loads(status_file.read_text())
    except (OSError, ValueError):
        st = {"ok": False, "message": f"Unreal closed without finishing the render (exit code {code})."}
    frames = sorted(frames_dir.rglob("*.png"))
    if not st.get("ok") or not frames:
        raise OpError(f"Render failed: {st.get('message') or 'no frames were written'}\nLog: {log}\n{_tail(log)}")
    name = re.sub(r"[^A-Za-z0-9_-]+", "_", p.sequence.rsplit("/", 1)[-1]) or "render"
    if p.encode == "frames":
        return OpResult(data={"frames": len(frames), "frames_dir": str(frames[0].parent), "log": str(log)})
    res = _encode(frames, p.fps, p.encode, f"{name}_{p.width}x{p.height}")
    res.data["log"] = str(log)
    return res


class UnrealScriptParams(BaseModel):
    code: str = Field(min_length=1, max_length=200_000, description="Python to run inside the Unreal Editor (import unreal ...). Print what you want back.")
    project: str | None = Field(None, description="Path to the .uproject (default: UNREAL_PROJECT, else the most recent project)")
    timeout_minutes: int = Field(30, ge=1, le=600)


@operation("unreal_script", UnrealScriptParams, "Run a Python script inside the Unreal Editor, headless (set up levels, cameras, lights, MetaHumans, sequences). Only works when the person has turned it on (UNREAL_ALLOW_SCRIPTS=on in builder.env).")
def unreal_script(p: UnrealScriptParams) -> OpResult:
    if not scripts_allowed():
        raise OpError("Unreal scripts are off. To allow them, set UNREAL_ALLOW_SCRIPTS=on in builder.env and restart Builder.")
    editor, project = _cmd_editor(_editor()), _project(p.project)
    work = _work_dir("script")
    script = work / "script.py"
    script.write_text(p.code, encoding="utf-8")
    log = work / "unreal.log"
    code = _run_editor([str(editor), _arg_path(project), "-run=pythonscript", f"-script={_arg_path(script)}",
                        "-unattended", "-nosplash", "-stdout", "-FullStdOutLogOutput"], log, p.timeout_minutes * 60)
    out = [line.split("LogPython: ", 1)[1] for line in log.read_text(encoding="utf-8", errors="replace").splitlines() if "LogPython: " in line]
    if code != 0:
        raise OpError(f"The script failed (exit code {code}).\n{_tail(log)}")
    return OpResult(data={"output": "\n".join(out[-200:]), "log": str(log)})
