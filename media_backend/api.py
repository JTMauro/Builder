"""HTTP API (FastAPI). Also mounts the MCP connector at /mcp."""

from __future__ import annotations

import contextlib
import hmac
from typing import Any

from pathlib import Path

import anthropic
from fastapi import Depends, FastAPI, File, HTTPException, Query, Request, Response, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ValidationError

from . import __version__, ops, storage
from .assistant import AssistantNotConfigured, ChatNotFound, ChatResult, get_assistant
from .config import get_settings
from .jobs import Job, get_queue
from .mcp_server import build_server
from .users import AuthError, User, get_store

COOKIE = "builder_session"
PUBLIC_PATHS = {"/", "/health", "/favicon.ico", "/auth/status", "/auth/login", "/auth/setup", "/auth/signup"}
# Stand-in identities when a request isn't from a signed-in person.
LOCAL_USER = User(id=0, username="local", display_name="Local", is_admin=True, created_at=0)
SERVICE_USER = User(id=-1, username="service", display_name="Service token", is_admin=False, created_at=0)


class JobRequest(BaseModel):
    operation: str
    params: dict[str, Any] = {}


class Credentials(BaseModel):
    username: str
    password: str
    display_name: str = ""


class NewUser(Credentials):
    is_admin: bool = False


class PasswordChange(BaseModel):
    current_password: str
    new_password: str


class TokenRequest(BaseModel):
    name: str = "Claude Code"


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

    def identify(request: Request) -> tuple[User | None, str]:
        """Return (user, how) for a request: signed-in cookie, personal token, or server token."""
        settings = get_settings()
        if not settings.auth_enabled:
            return LOCAL_USER, "open"
        user = get_store().user_for_session(request.cookies.get(COOKIE, ""))
        if user:
            return user, "cookie"
        bearer = request.headers.get("authorization", "")
        if not bearer and request.method == "GET" and request.url.path.endswith("/content"):
            # <img>/<video> tags can't send headers, so downloads may pass ?access_token= instead.
            bearer = f"Bearer {request.query_params.get('access_token', '')}"
        token = bearer.removeprefix("Bearer ").strip()
        if token:
            user = get_store().user_for_api_token(token)
            if user:
                return user, "token"
            if settings.api_token and hmac.compare_digest(token, settings.api_token):
                return SERVICE_USER, "token"
        return None, ""

    @app.middleware("http")
    async def auth(request: Request, call_next):
        path = request.url.path
        if path in PUBLIC_PATHS or path.startswith("/static/"):
            return await call_next(request)
        user, how = identify(request)
        if user is None:
            return JSONResponse({"detail": "Please sign in"}, status_code=401)
        # Cookie-authenticated writes must come from our own page (a custom header can't be sent cross-site).
        if how == "cookie" and request.method not in ("GET", "HEAD", "OPTIONS") and request.headers.get("x-builder") != "1":
            return JSONResponse({"detail": "Missing X-Builder header"}, status_code=403)
        request.state.user = user
        return await call_next(request)

    def current_user(request: Request) -> User:
        return request.state.user

    def admin_user(user: User = Depends(current_user)) -> User:
        if not user.is_admin:
            raise HTTPException(403, "Only admins can do that")
        return user

    def real_user(user: User = Depends(current_user)) -> User:
        if user.id <= 0:
            raise HTTPException(400, "Sign in as a person to do that")
        return user

    def start_session(request: Request, response: Response, user: User) -> dict:
        token = get_store().new_session(user.id)
        response.set_cookie(
            COOKIE, token, max_age=30 * 86400, httponly=True, samesite="lax", path="/",
            secure=request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https",
        )
        return {"user": user}

    # ------------------------------------------------------------ accounts

    @app.get("/auth/status")
    def auth_status(request: Request):
        settings = get_settings()
        user, _ = identify(request)
        return {
            "auth_enabled": settings.auth_enabled,
            "needs_setup": settings.auth_enabled and get_store().count() == 0,
            "signup_open": settings.open_signup,
            "user": user,
        }

    @app.post("/auth/setup")
    def setup(creds: Credentials, request: Request, response: Response):
        """Create the first account (an admin). Only works while there are no accounts."""
        store = get_store()
        if store.count() > 0:
            raise HTTPException(409, "Setup is already done; sign in instead")
        try:
            user = store.create_user(creds.username, creds.password, creds.display_name, is_admin=True)
        except AuthError as e:
            raise HTTPException(422, str(e))
        return start_session(request, response, user)

    @app.post("/auth/signup")
    def signup(creds: Credentials, request: Request, response: Response):
        if not get_settings().open_signup:
            raise HTTPException(403, "Ask an admin to create your account")
        try:
            user = get_store().create_user(creds.username, creds.password, creds.display_name)
        except AuthError as e:
            raise HTTPException(422, str(e))
        return start_session(request, response, user)

    @app.post("/auth/login")
    def login(creds: Credentials, request: Request, response: Response):
        try:
            user = get_store().verify(creds.username, creds.password)
        except AuthError as e:
            raise HTTPException(401 if "Wrong" in str(e) else 429, str(e))
        return start_session(request, response, user)

    @app.post("/auth/logout", status_code=204)
    def logout(request: Request, response: Response):
        get_store().end_session(request.cookies.get(COOKIE, ""))
        response.delete_cookie(COOKIE, path="/")

    @app.post("/auth/password", status_code=204)
    def change_password(body: PasswordChange, user: User = Depends(real_user)):
        store = get_store()
        if not store.check_password(user.id, body.current_password):
            raise HTTPException(401, "Your current password is wrong")
        try:
            store.set_password(user.id, body.new_password)
        except AuthError as e:
            raise HTTPException(422, str(e))

    @app.get("/auth/users", response_model=list[User])
    def list_users(_: User = Depends(admin_user)):
        return get_store().list_users()

    @app.post("/auth/users", response_model=User, status_code=201)
    def add_user(body: NewUser, _: User = Depends(admin_user)):
        try:
            return get_store().create_user(body.username, body.password, body.display_name, body.is_admin)
        except AuthError as e:
            raise HTTPException(422, str(e))

    @app.delete("/auth/users/{user_id}", status_code=204)
    def remove_user(user_id: int, admin: User = Depends(admin_user)):
        if user_id == admin.id:
            raise HTTPException(400, "You can't delete your own account")
        if get_store().get_user(user_id) is None:
            raise HTTPException(404, "No such user")
        get_store().delete_user(user_id)

    @app.get("/auth/tokens")
    def list_tokens(user: User = Depends(real_user)):
        return get_store().list_api_tokens(user.id)

    @app.post("/auth/tokens", status_code=201)
    def create_token(body: TokenRequest, user: User = Depends(real_user)):
        """Make a personal access token (shown once) for Claude Code or scripts."""
        info, token = get_store().new_api_token(user.id, body.name)
        return {**info, "token": token}

    @app.delete("/auth/tokens/{token_id}", status_code=204)
    def revoke_token(token_id: int, user: User = Depends(real_user)):
        if not get_store().delete_api_token(user.id, token_id):
            raise HTTPException(404, "No such token")

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
    def chat(req: ChatRequest, user: User = Depends(current_user)):
        try:
            return get_assistant().chat(req.message, session_id=req.session_id, file_ids=req.file_ids, owner=user.username)
        except ChatNotFound:
            raise HTTPException(404, "No such chat")
        except AssistantNotConfigured as e:
            raise HTTPException(503, str(e))
        except anthropic.RateLimitError:
            raise HTTPException(429, "The assistant is rate limited; try again shortly.")
        except anthropic.APIStatusError as e:
            raise HTTPException(502, f"Claude API error ({e.status_code}): {e.message}")
        except anthropic.APIConnectionError:
            raise HTTPException(502, "Couldn't reach the Claude API.")

    @app.get("/assistant/sessions")
    def my_chats(user: User = Depends(current_user)):
        return get_assistant().list_chats(user.username)

    @app.get("/assistant/sessions/{session_id}")
    def chat_transcript(session_id: str, user: User = Depends(current_user)):
        try:
            return {"id": session_id, "messages": get_assistant().transcript(session_id, user.username)}
        except ChatNotFound:
            raise HTTPException(404, "No such chat")

    @app.delete("/assistant/sessions/{session_id}", status_code=204)
    def reset_session(session_id: str, user: User = Depends(current_user)):
        if not get_assistant().reset(session_id, user.username):
            raise HTTPException(404, "No such chat")

    @app.get("/assistant/info")
    def assistant_info():
        s = get_settings()
        return {"name": s.assistant_name, "persona": s.assistant_persona, "model": s.model, "effort": s.effort}

    static_dir = Path(__file__).parent / "static"

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(static_dir / "index.html", headers={"Cache-Control": "no-cache"})

    app.mount("/static", StaticFiles(directory=static_dir), name="static")
    app.mount("/mcp", mcp_app)
    return app
