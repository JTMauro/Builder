"""MCP connector: lets Claude Code (or any MCP client) use the backend directly.

Two ways to connect:
  - stdio (local):  claude mcp add media -- media-backend mcp
  - HTTP (remote):  claude mcp add --transport http media http://HOST:8000/mcp
"""

from __future__ import annotations

import functools
import time
from pathlib import Path
from typing import Any

import httpx2
from mcp.server.mcpserver import Image, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import BaseModel, ValidationError

from . import ops, storage
from .assistant import AssistantNotConfigured, ChatNotFound, get_assistant
from .config import get_settings
from .jobs import get_queue

INSTRUCTIONS = """\
Audio/video processing backend (ffmpeg) with a built-in Claude assistant.

Typical flow: import_path (or import_url) to get a file id -> run media tools (each returns new file ids,
inputs are never changed) -> view_file to check visual results -> export_file to write the result back
into the project. Long encodes can be started with wait=false and polled with get_job.
Use ask_assistant to hand a whole multi-step media task to the built-in assistant.
"""


def _allowed(path: Path) -> Path:
    resolved = path.expanduser().resolve()
    for root in get_settings().allowed_dirs:
        if resolved == root or root in resolved.parents:
            return resolved
    roots = ", ".join(str(r) for r in get_settings().allowed_dirs)
    raise ValueError(f"{resolved} is outside the allowed directories ({roots}). Set MEDIA_BACKEND_ALLOWED_DIRS to change this.")


def _brief(info: storage.FileInfo) -> dict[str, Any]:
    return info.model_dump(include={"id", "name", "mime_type", "size", "source", "parent_ids"})


def _job_payload(job) -> dict[str, Any]:
    out = {"job_id": job.id, "status": job.status, "operation": job.operation}
    if job.status == "succeeded":
        out["outputs"] = [_brief(o) for o in job.outputs]
        if job.data:
            out["data"] = job.data
    if job.error:
        out["error"] = job.error
    return out


_EXPECTED = (AssistantNotConfigured, ValueError, ValidationError, ops.OpError, storage.FileNotFound, OSError, httpx2.HTTPError)


def _friendly(fn):
    """Turn expected failures into MCP tool errors whose message reaches the client."""

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except storage.FileNotFound as e:
            raise ToolError(f"No file with id {e}") from e
        except ChatNotFound as e:
            raise ToolError(f"No assistant session {e}; omit session_id to start a new one") from e
        except _EXPECTED as e:
            raise ToolError(str(e)) from e

    return wrapper


class _Server(MCPServer):
    def tool(self, *args, **kwargs):
        register = super().tool(*args, **kwargs)
        return lambda fn: register(_friendly(fn))

    def add_tool(self, fn, *args, **kwargs):
        return super().add_tool(_friendly(fn), *args, **kwargs)


def build_server(media_only: bool | None = None) -> MCPServer:
    """`media_only` (or MEDIA_BACKEND_MCP_MEDIA_ONLY=1) serves just the audio/video tools: the mode the
    Craft engine loads Builder in as a plugin, so a build can't start builds or call Rivet."""
    import os

    if media_only is None:
        media_only = os.environ.get("MEDIA_BACKEND_MCP_MEDIA_ONLY") == "1"
    mcp = _Server(name="media-backend", instructions=INSTRUCTIONS, version="0.1.0")

    # ---------------------------------------------------------------- files

    @mcp.tool()
    def list_files(limit: int = 50) -> list[dict[str, Any]]:
        """List files in the media store, newest first."""
        return [_brief(f) for f in storage.list_files()[:limit]]

    @mcp.tool()
    def import_path(path: str) -> dict[str, Any]:
        """Copy a local file (on the machine running this server) into the media store. Returns its file id."""
        src = _allowed(Path(path))
        if not src.is_file():
            raise ValueError(f"No such file: {src}")
        return _brief(storage.import_path(src))

    @mcp.tool()
    def import_url(url: str, filename: str | None = None) -> dict[str, Any]:
        """Download a media file from an http(s) URL into the media store. Returns its file id."""
        if not url.startswith(("http://", "https://")):
            raise ValueError("Only http(s) URLs are supported")
        name = filename or Path(httpx2.URL(url).path).name or "download"
        with httpx2.stream("GET", url, follow_redirects=True, timeout=60) as r:
            r.raise_for_status()

            class _Reader:
                def __init__(self, it):
                    self._it, self._buf = it, b""

                def read(self, n: int) -> bytes:
                    while len(self._buf) < n:
                        try:
                            self._buf += next(self._it)
                        except StopIteration:
                            break
                    out, self._buf = self._buf[:n], self._buf[n:]
                    return out

            return _brief(storage.save_stream(name, _Reader(r.iter_bytes()), source="url"))

    @mcp.tool()
    def export_file(file_id: str, dest_path: str, overwrite: bool = False) -> dict[str, Any]:
        """Write a file from the media store to a local path (on the machine running this server).
        If dest_path is an existing directory, the file keeps its own name inside it."""
        dest = _allowed(Path(dest_path))
        if dest.is_dir():
            dest = dest / storage.get(file_id).name
        if dest.exists() and not overwrite:
            raise ValueError(f"{dest} already exists; pass overwrite=true to replace it")
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(storage.path_of(file_id).read_bytes())
        return {"written": str(dest), "size": dest.stat().st_size}

    @mcp.tool()
    def delete_file(file_id: str) -> str:
        """Delete a file from the media store."""
        storage.delete(file_id)
        return f"deleted {file_id}"

    @mcp.tool()
    def view_file(file_id: str, frames: int = 4) -> list[Image]:
        """Look at an image, or at evenly spaced frames of a video (max 8)."""
        info = storage.get(file_id)
        if info.mime_type.startswith("image/"):
            ids = [file_id]
        else:
            ids = [o.id for o in ops.run_operation("frames", {"file_id": file_id, "count": max(1, min(frames, 8)), "width": 640}).outputs]
        return [Image(path=storage.path_of(i)) for i in ids]

    # ---------------------------------------------------------------- jobs

    @mcp.tool()
    def list_operations() -> list[dict[str, Any]]:
        """Every media operation with its description and parameter JSON schema."""
        return ops.describe_operations()

    @mcp.tool()
    def get_job(job_id: str, wait_seconds: float = 0) -> dict[str, Any]:
        """Check a background job; optionally wait up to wait_seconds for it to finish."""
        q = get_queue()
        job = q.wait(job_id, timeout=wait_seconds) if wait_seconds > 0 else q.get(job_id)
        if job is None:
            raise ValueError(f"No job {job_id}")
        return _job_payload(job)

    def register_op(op: ops.Operation) -> None:
        def run(params: BaseModel, wait: bool = True) -> dict[str, Any]:
            q = get_queue()
            job = q.submit(op.name, params.model_dump())
            if wait:
                job = q.wait(job.id) or job
            return _job_payload(job)

        run.__name__ = op.name
        run.__doc__ = op.description + "\nSet wait=false to start it in the background and poll get_job."
        run.__annotations__ = {"params": op.params, "wait": bool, "return": dict[str, Any]}
        mcp.add_tool(run, name=op.name, description=run.__doc__)

    for op in ops.REGISTRY.values():
        register_op(op)

    if media_only:
        return mcp

    # ---------------------------------------------------------------- Craft builds (engine/)
    # Builds started over MCP belong to the "claude-code" owner (their own projects folder).

    from . import craft_tools

    def _craft(name: str, args: dict[str, Any]) -> Any:
        from . import craft

        try:
            return craft_tools.call(name, {k: v for k, v in args.items() if v is not None}, "claude-code")
        except craft.RunNotFound as e:
            raise ToolError(f"No build {e}") from e
        except craft.EngineMissing as e:
            raise ToolError(str(e)) from e

    @mcp.tool()
    def craft_projects_list() -> Any:
        """List Craft projects made through this connector, newest first."""
        return _craft("craft_projects_list", {})

    @mcp.tool()
    def craft_build_start(request: str, target: str = "", file_id: str | None = None, rounds: int = 2) -> Any:
        """Start a Craft build: study a target (web address, uploaded file id, or nothing), build your own
        version with a coding agent, check it against the original and refine. Follow it with craft_build_status."""
        return _craft("craft_build_start", {"request": request, "target": target, "file_id": file_id, "rounds": rounds})

    @mcp.tool()
    def craft_build_followup(request: str, run_id: str | None = None, project_path: str | None = None) -> Any:
        """Ask for a change to an existing Craft project (by run id or project path)."""
        return _craft("craft_build_followup", {"request": request, "run_id": run_id, "project_path": project_path})

    @mcp.tool()
    def craft_build_status(run_id: str) -> Any:
        """A build's step, status, checks (how alike), preview, anything waiting, and recent activity."""
        return _craft("craft_build_status", {"run_id": run_id})

    @mcp.tool()
    def craft_build_answer(run_id: str, choice: str | None = None, text: str | None = None) -> Any:
        """Answer a question the builder asked. Approvals of actions are left to the person (Builder's build screen)."""
        return _craft("craft_build_answer", {"run_id": run_id, "choice": choice, "text": text})

    @mcp.tool()
    def craft_build_stop(run_id: str) -> Any:
        """Stop a running build."""
        return _craft("craft_build_stop", {"run_id": run_id})

    @mcp.tool()
    def craft_engine_tools_search(query: str) -> Any:
        """Search the Craft engine's analysis tools by words."""
        return _craft("craft_engine_tools_search", {"query": query})

    @mcp.tool()
    def craft_engine_tool(tool: str, params: dict[str, Any] | None = None) -> Any:
        """Run one Craft engine tool by id with JSON params (tools that run foreign code are refused)."""
        return _craft("craft_engine_tool", {"tool": tool, "params": params or {}})

    # ---------------------------------------------------------------- assistant

    @mcp.tool()
    def ask_assistant(message: str, session_id: str | None = None, file_ids: list[str] | None = None) -> dict[str, Any]:
        """Hand a media task to the built-in Claude assistant, which plans and runs the operations itself and
        checks its own results. Pass the returned session_id to continue the same conversation."""
        start = time.time()
        res = get_assistant().chat(message, session_id=session_id, file_ids=file_ids, owner="claude-code")
        return {
            "session_id": res.session_id,
            "reply": res.reply,
            "new_files": [_brief(f) for f in res.new_files],
            "steps": res.tool_calls,
            "seconds": round(time.time() - start, 1),
        }

    return mcp


def run_stdio() -> None:
    build_server().run("stdio")
