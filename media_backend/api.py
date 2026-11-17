"""HTTP API (FastAPI). Also mounts the MCP connector at /mcp."""

from __future__ import annotations

import contextlib
import hmac
from typing import Any

import anthropic
from fastapi import FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ValidationError

from . import __version__, ops, storage
from .assistant import AssistantNotConfigured, ChatResult, get_assistant
from .config import get_settings
from .jobs import Job, get_queue
from .mcp_server import build_server


class JobRequest(BaseModel):
    operation: str
    params: dict[str, Any] = {}


class ChatRequest(BaseModel):
    message: str
    session_id: str | None = None
    file_ids: list[str] | None = None


def create_app() -> FastAPI:
    settings = get_settings()
    settings.files_dir.mkdir(parents=True, exist_ok=True)
    mcp = build_server()
    mcp_app = mcp.streamable_http_app(streamable_http_path="/", host="0.0.0.0")

    @contextlib.asynccontextmanager
    async def lifespan(_app: FastAPI):
        async with mcp.session_manager.run():
            yield

    app = FastAPI(title="Media Backend", version=__version__, lifespan=lifespan)

    @app.middleware("http")
    async def auth(request: Request, call_next):
        token = get_settings().api_token
        if token and request.url.path not in ("/health",):
            given = request.headers.get("authorization", "")
            if not hmac.compare_digest(given, f"Bearer {token}"):
                return JSONResponse({"detail": "Missing or wrong bearer token"}, status_code=401)
        return await call_next(request)

    @app.exception_handler(storage.FileNotFound)
    async def _nf(_req, exc):
        return JSONResponse({"detail": f"No file with id {exc}"}, status_code=404)

    @app.exception_handler(ops.OpError)
    async def _op(_req, exc):
        return JSONResponse({"detail": str(exc)}, status_code=422)

    @app.exception_handler(ValidationError)
    async def _val(_req, exc):
        return JSONResponse({"detail": exc.errors(include_url=False, include_context=False)}, status_code=422)

    # ------------------------------------------------------------ meta

    @app.get("/health")
    def health():
        return {"ok": True, "version": __version__}

    @app.get("/operations")
    def list_operations():
        return ops.describe_operations()

    # ------------------------------------------------------------ files

    @app.post("/files", response_model=storage.FileInfo, status_code=201)
    def upload(file: UploadFile = File(...)):
        try:
            return storage.save_stream(file.filename or "upload", file.file)
        except ValueError as e:
            raise HTTPException(413, str(e))

    @app.get("/files", response_model=list[storage.FileInfo])
    def list_files(limit: int = Query(100, ge=1, le=1000)):
        return storage.list_files()[:limit]

    @app.get("/files/{file_id}", response_model=storage.FileInfo)
    def file_info(file_id: str):
        return storage.get(file_id)

    @app.get("/files/{file_id}/content")
    def download(file_id: str, inline: bool = False):
        info = storage.get(file_id)
        return FileResponse(
            storage.path_of(file_id), media_type=info.mime_type, filename=info.name,
            content_disposition_type="inline" if inline else "attachment",
        )

    @app.get("/files/{file_id}/probe")
    def probe(file_id: str):
        return ops.media_info(file_id) if storage.get(file_id) else None

    @app.delete("/files/{file_id}", status_code=204)
    def delete(file_id: str):
        storage.delete(file_id)

    # ------------------------------------------------------------ jobs

    @app.post("/jobs", response_model=Job, status_code=202)
    def create_job(req: JobRequest, wait: bool = False, timeout: float = Query(600, gt=0, le=7200)):
        """Start an operation. With ?wait=true the response comes back when it has finished."""
        q = get_queue()
        job = q.submit(req.operation, req.params)
        return (q.wait(job.id, timeout) or job) if wait else job

    @app.post("/ops/{operation}", response_model=Job)
    def run_op(operation: str, params: dict[str, Any], timeout: float = Query(600, gt=0, le=7200)):
        """Shortcut: run an operation and wait for it. Body is the operation's params."""
        q = get_queue()
        job = q.submit(operation, params)
        return q.wait(job.id, timeout) or job

    @app.get("/jobs", response_model=list[Job])
    def list_jobs(limit: int = Query(50, ge=1, le=500)):
        return get_queue().list(limit)

    @app.get("/jobs/{job_id}", response_model=Job)
    def get_job(job_id: str, wait: float = Query(0, ge=0, le=600)):
        q = get_queue()
        job = q.wait(job_id, wait) if wait else q.get(job_id)
        if job is None:
            raise HTTPException(404, "No such job")
        return job

    # ------------------------------------------------------------ assistant

    @app.post("/assistant/chat", response_model=ChatResult)
    def chat(req: ChatRequest):
        try:
            return get_assistant().chat(req.message, session_id=req.session_id, file_ids=req.file_ids)
        except AssistantNotConfigured as e:
            raise HTTPException(503, str(e))
        except anthropic.RateLimitError:
            raise HTTPException(429, "The assistant is rate limited; try again shortly.")
        except anthropic.APIStatusError as e:
            raise HTTPException(502, f"Claude API error ({e.status_code}): {e.message}")
        except anthropic.APIConnectionError:
            raise HTTPException(502, "Couldn't reach the Claude API.")

    @app.delete("/assistant/sessions/{session_id}", status_code=204)
    def reset_session(session_id: str):
        if not get_assistant().reset(session_id):
            raise HTTPException(404, "No such session")

    @app.get("/assistant/info")
    def assistant_info():
        s = get_settings()
        return {"name": s.assistant_name, "persona": s.assistant_persona, "model": s.model, "effort": s.effort}

    app.mount("/mcp", mcp_app)
    return app
