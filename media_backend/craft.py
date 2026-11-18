"""Craft runs: Builder's front end for the bundled Craft Anything engine (`engine/`).

A run is one `craft-anything-cli rebuild ... --events-json` process: study a target (a website,
an app or game, or nothing), have a coding agent build the person's own version, check it against
the original and refine. Its JSON events are kept per run and streamed to the screen; questions
and approvals wait for an answer from the person who started it.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import signal
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from .config import FROZEN, IS_WINDOWS, NO_WINDOW, app_dir, builder_command, exe_name, get_settings

ENGINE_DIR = Path(__file__).resolve().parent.parent / "engine"


class EngineMissing(RuntimeError):
    def __init__(self):
        super().__init__(
            "The Craft engine isn't built yet. Run `builder setup` (it builds engine/ and installs it in "
            "~/.builder/bin), or set CRAFT_ENGINE_BIN to a craft-anything-cli program."
        )


def engine_bin() -> Path:
    env = os.environ.get("CRAFT_ENGINE_BIN")
    candidates = [Path(env)] if env else []
    name = exe_name("craft-anything-cli")
    if FROZEN:
        candidates.append(app_dir() / "engine" / name)  # shipped inside the Windows app
    candidates.append(get_settings().bin_dir / name)  # where `builder setup` installs it
    target = os.environ.get("CARGO_TARGET_DIR")
    for base in ([Path(target)] if target else []) + [ENGINE_DIR / "target"]:
        candidates += [base / "release" / name, base / "debug" / name]
    found = shutil.which("craft-anything-cli")
    if found:
        candidates.append(Path(found))
    for c in candidates:
        if c.is_file() and os.access(c, os.X_OK):
            return c
    raise EngineMissing()


def engine_env() -> dict[str, str]:
    """The engine keeps its settings in Builder's data folder, never in the server user's own."""
    s = get_settings()
    env = dict(os.environ)
    env["CRAFT_ANYTHING_CONFIG_DIR"] = str(s.data_dir / "engine-config")
    return env


_setup_lock = threading.Lock()
_ai_ready = False
PLUGIN_ID = "builder_media"


# Each build runs in its own process group so Stop reaches the engine and everything it started.
_GROUP: dict = ({**NO_WINDOW, "creationflags": 0x08000000 | 0x00000200} if IS_WINDOWS  # NO_WINDOW | NEW_PROCESS_GROUP
                else {"start_new_session": True})


def _end_tree(proc: subprocess.Popen, grace: float = 5.0) -> None:
    try:
        proc.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True, **NO_WINDOW)


def _engine(*args: str, timeout: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run([str(engine_bin()), *args], capture_output=True, text=True, env=engine_env(),
                          timeout=timeout, stdin=subprocess.DEVNULL, **NO_WINDOW)


def media_plugin_spec() -> dict[str, Any]:
    """Builder's own audio/video connector, as a plugin of the engine: Craft's builder can then trim,
    caption, score and animate media while it builds, reading and writing inside the projects folder."""
    s = get_settings()
    cmd = builder_command("mcp")
    return {
        "id": PLUGIN_ID,
        "name": "Builder audio & video",
        "command": cmd[0],
        "args": cmd[1:],
        "env": {
            "MEDIA_BACKEND_DATA": str(s.data_dir),
            "MEDIA_BACKEND_ALLOWED_DIRS": str(s.data_dir / "projects"),
            "MEDIA_BACKEND_MCP_MEDIA_ONLY": "1",
            "PYTHONPATH": str(Path(__file__).resolve().parent.parent),
        },
        "replace": True,
    }


def ensure_ai() -> None:
    """Once per server start: give the engine a Claude backend from the server's ANTHROPIC_API_KEY,
    and plug Builder's audio/video tools into it."""
    global _ai_ready
    with _setup_lock:
        if _ai_ready:
            return
        if os.environ.get("ANTHROPIC_API_KEY"):
            # Without a key the engine may still find a coding-agent app on this computer.
            if "builder-claude" not in _engine("ai", "list").stdout:
                _engine("ai", "add", "builder-claude", "--kind", "anthropic", "--key-env", "ANTHROPIC_API_KEY")
        added = _engine("plugins-add", "--json", json.dumps(media_plugin_spec()), "--allow-run")
        if added.returncode == 0:
            _engine("plugins-refresh", "--json", json.dumps({"id": PLUGIN_ID}), "--allow-run", timeout=180)
        _ai_ready = True


class Run(BaseModel):
    id: str
    owner: str
    target: str
    request: str
    status: str = "starting"  # starting, running, waiting, done, failed, stopped
    project: str | None = None
    preview: str | None = None
    started_at: float
    finished_at: float | None = None
    message: str = ""
    pending: dict[str, Any] | None = None  # the question or approval waiting for an answer
    checks: list[dict[str, Any]] = []
    step: str | None = None


class RunNotFound(KeyError):
    pass


class _Live:
    def __init__(self, run: Run, proc: subprocess.Popen, log_path: Path):
        self.run = run
        self.proc = proc
        self.events: list[dict[str, Any]] = []
        self.lock = threading.Lock()
        self.changed = threading.Condition(self.lock)
        self.log_path = log_path


class Runs:
    def __init__(self):
        self._runs: dict[str, _Live] = {}
        self._lock = threading.Lock()

    def _dir(self) -> Path:
        d = get_settings().data_dir / "craft-runs"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def projects_dir(self, owner: str) -> Path:
        d = get_settings().data_dir / "projects" / owner
        d.mkdir(parents=True, exist_ok=True)
        return d

    def start(self, owner: str, target: str, request: str, rounds: int = 2, build_with: str | None = None,
              continue_in: str | None = None) -> Run:
        exe = engine_bin()
        ensure_ai()
        target = target.strip() or "-"
        args = [str(exe), "rebuild", target, request, "--events-json", "--rounds", str(rounds)]
        if continue_in:
            project = Path(continue_in).resolve()
            if self.projects_dir(owner).resolve() not in project.parents:
                raise PermissionError("That project isn't yours")
            args = [str(exe), "rebuild", "-", request, "--events-json", "--rounds", str(rounds), "--continue-in", str(project)]
        else:
            args += ["--projects-dir", str(self.projects_dir(owner))]
            if build_with:
                args += ["--build-with", build_with]
        run = Run(id=uuid.uuid4().hex[:12], owner=owner, target=target, request=request, started_at=time.time(),
                  project=continue_in)
        log_path = self._dir() / f"{run.id}.jsonl"
        proc = subprocess.Popen(
            args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1,
            env=engine_env(), **_GROUP,
        )
        live = _Live(run, proc, log_path)
        with self._lock:
            self._runs[run.id] = live
        threading.Thread(target=self._read, args=(live,), daemon=True, name=f"craft-run-{run.id}").start()
        threading.Thread(target=self._read_err, args=(live,), daemon=True).start()
        return run

    # ------------------------------------------------------------ event handling

    def _read(self, live: _Live) -> None:
        assert live.proc.stdout is not None
        with live.log_path.open("a") as log:
            for line in live.proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    ev = {"type": "text", "text": line + "\n"}
                log.write(json.dumps(ev) + "\n")
                log.flush()
                self._apply(live, ev)
        code = live.proc.wait()
        with live.lock:
            r = live.run
            if r.status not in ("done", "failed", "stopped"):
                r.status = "failed" if code else "done"
                if code and not r.message:
                    r.message = "The engine stopped. See the run's log for details."
            r.finished_at = r.finished_at or time.time()
            r.pending = None
            live.events.append({"type": "closed", "code": code})
            live.changed.notify_all()

    def _read_err(self, live: _Live) -> None:
        assert live.proc.stderr is not None
        for line in live.proc.stderr:
            line = line.rstrip()
            if line:
                self._apply(live, {"type": "log", "text": line})

    def _apply(self, live: _Live, ev: dict[str, Any]) -> None:
        with live.lock:
            r = live.run
            t = ev.get("type")
            if r.status == "starting":
                r.status = "running"
            if t == "log":
                # The engine explains a failed start on stderr ("rebuild: …" plus an indented remedy).
                text = ev.get("text", "")
                if text.startswith("rebuild: "):
                    r.message = text.removeprefix("rebuild: ")
                elif text.startswith("  ") and r.message and live.proc.poll() is None:
                    r.message += " " + text.strip()
            elif t == "project":
                r.project = ev.get("path")
            elif t == "step":
                r.step = ev.get("step")
            elif t == "ask":
                r.pending = ev.get("request")
                r.status = "waiting"
            elif t == "check":
                r.checks.append(ev.get("check") or {})
            elif t == "preview":
                r.preview = ev.get("url")
            elif t == "finished":
                r.status = "done" if ev.get("ok") else ("stopped" if ev.get("paused") else "failed")
                r.message = ev.get("message", "")
                r.finished_at = time.time()
            if t == "text" and live.events and live.events[-1].get("type") == "text":
                # Merge streamed text so the history stays small.
                live.events[-1] = {"type": "text", "text": live.events[-1]["text"] + ev.get("text", "")}
            elif t != "agent":
                live.events.append(ev)
            live.changed.notify_all()

    # ------------------------------------------------------------ access

    def _live(self, run_id: str, owner: str) -> _Live:
        live = self._runs.get(run_id)
        if live is None or live.run.owner != owner:
            raise RunNotFound(run_id)
        return live

    def get(self, run_id: str, owner: str) -> Run:
        live = self._live(run_id, owner)
        with live.lock:
            return live.run.model_copy(deep=True)

    def list(self, owner: str) -> list[Run]:
        with self._lock:
            mine = [x for x in self._runs.values() if x.run.owner == owner]
        return sorted((x.run.model_copy(deep=True) for x in mine), key=lambda r: r.started_at, reverse=True)

    def events(self, run_id: str, owner: str, after: int = 0, wait: float = 0) -> tuple[list[dict[str, Any]], int, Run]:
        live = self._live(run_id, owner)
        deadline = time.time() + wait
        with live.lock:
            while len(live.events) <= after and live.run.finished_at is None and time.time() < deadline:
                live.changed.wait(timeout=max(0.0, deadline - time.time()))
            return live.events[after:], len(live.events), live.run.model_copy(deep=True)

    def answer(self, run_id: str, owner: str, answer: dict[str, Any]) -> Run:
        live = self._live(run_id, owner)
        with live.lock:
            pending = live.run.pending
            if not pending:
                raise ValueError("Nothing is waiting for an answer")
            line = json.dumps({"id": pending["id"], "answer": answer})
            live.run.pending = None
            live.run.status = "running"
            live.events.append({"type": "answered", "id": pending["id"], "answer": answer})
            live.changed.notify_all()
        assert live.proc.stdin is not None
        live.proc.stdin.write(line + "\n")
        live.proc.stdin.flush()
        return self.get(run_id, owner)

    def stop(self, run_id: str, owner: str) -> Run:
        live = self._live(run_id, owner)
        if live.proc.poll() is None:
            try:
                # The engine stops its builder cleanly on Ctrl-C (Ctrl-Break on Windows).
                if IS_WINDOWS:
                    live.proc.send_signal(signal.CTRL_BREAK_EVENT)
                    # A windowed parent can't always reach the engine's hidden console: if it hasn't
                    # stopped in a few seconds, end it and everything it started.
                    threading.Thread(target=_end_tree, args=(live.proc,), daemon=True).start()
                else:
                    os.killpg(live.proc.pid, signal.SIGINT)
            except (ProcessLookupError, OSError):
                pass
        with live.lock:
            if live.run.finished_at is None:
                live.run.status = "stopped"
                live.run.message = "Stopped."
        return self.get(run_id, owner)

    def owned_project(self, owner: str, path: str) -> Path:
        p = Path(path).resolve()
        if self.projects_dir(owner).resolve() not in p.parents:
            raise RunNotFound(path)
        return p


def engine_version() -> str:
    try:
        out = subprocess.run([str(engine_bin()), "--version"], capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL, **NO_WINDOW)
        return out.stdout.strip()
    except (OSError, subprocess.SubprocessError, EngineMissing):
        return ""


def list_projects(owner: str) -> list[dict[str, Any]]:
    """The person's projects, newest first, read from each project's craft.json."""
    out = []
    for manifest in get_runs().projects_dir(owner).glob("*/craft.json"):
        try:
            m = json.loads(manifest.read_text())
        except (OSError, ValueError):
            continue
        checks = m.get("checks") or []
        last = checks[-1] if checks else None
        out.append({
            "path": str(manifest.parent),
            "name": m.get("name") or manifest.parent.name,
            "request": m.get("request", ""),
            "target": m.get("target"),
            "stack": m.get("stack", ""),
            "status": m.get("status", ""),
            "note": m.get("note", ""),
            "last_reply": m.get("last_reply", ""),
            "updated_ms": m.get("updated_ms", 0),
            "checks": len(checks),
            "last_check": last,
        })
    return sorted(out, key=lambda p: p["updated_ms"], reverse=True)


_runs: Runs | None = None


def get_runs() -> Runs:
    global _runs
    if _runs is None:
        _runs = Runs()
    return _runs
