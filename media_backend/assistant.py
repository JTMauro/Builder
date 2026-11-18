"""Built-in Claude assistant that can drive every media operation on the user's behalf.

It runs its own tool loop inside the backend (the tools act on this server's files), using the
Anthropic Messages API. Conversations are kept in memory per session id.
"""

from __future__ import annotations

import base64
import json
import threading
import uuid
from collections import OrderedDict
from typing import Any

import anthropic
from pydantic import BaseModel, ValidationError

from . import ops, storage
from .config import get_settings
from .jobs import get_queue

IMAGE_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp"}
MAX_IMAGE_BYTES = 4_500_000


def _schema(model: type[BaseModel]) -> dict[str, Any]:
    schema = model.model_json_schema()
    schema.pop("title", None)
    return schema


def build_tools() -> list[dict[str, Any]]:
    tools: list[dict[str, Any]] = [
        {
            "name": "list_files",
            "description": "List media files in the store (newest first) with id, name, type, size and which operation produced them.",
            "input_schema": {"type": "object", "properties": {"limit": {"type": "integer", "minimum": 1, "maximum": 200}}},
        },
        {
            "name": "look_at",
            "description": (
                "See a file with your own eyes. Images are shown directly; for a video, evenly spaced frames are "
                "shown. Use this to check a result before telling the user it's done."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "file_id": {"type": "string"},
                    "frames": {"type": "integer", "minimum": 1, "maximum": 8, "description": "Frames to show for a video (default 4)"},
                },
                "required": ["file_id"],
            },
        },
    ]
    for op in ops.REGISTRY.values():
        tools.append({"name": op.name, "description": op.description, "input_schema": _schema(op.params)})
    from . import craft_tools

    tools.extend(craft_tools.TOOLS)
    return tools


def _system_prompt() -> str:
    s = get_settings()
    return (
        f"You are {s.assistant_name}, the built-in assistant of Builder: {s.assistant_persona}.\n\n"
        "Builder has two parts you can drive:\n"
        "1. Craft builds (tools starting craft_): the Craft Anything engine studies a website, app or game (or starts "
        "from nothing), a coding agent builds the person's own version in a project, then checks it against the "
        "original (how alike, in percent) and refines it. Builds take minutes to hours: start one, report its run id "
        "and current step, and check on it with craft_build_status when asked. You may answer the builder's "
        "questions when the person told you what they want, but never approvals of actions: tell the person to "
        "approve or refuse those on the build screen.\n"
        "2. The audio/video workshop: ffmpeg operations on files in the media store. Inputs are referenced by file "
        "id and are never modified; every operation writes new file(s) and returns their ids.\n\n"
        "How to work:\n"
        "- If you don't know which file is meant, call list_files. Call probe before choosing settings that depend on "
        "duration, resolution or whether a file has audio.\n"
        "- Chain operations to finish the whole request (e.g. trim, then add_text, then replace_audio).\n"
        "- For visual work, call look_at on the result and fix problems before you report back.\n"
        "- If a tool fails, read the error, adjust the parameters and try again; tell the user if you can't recover.\n"
        "- Finish with a short plain-language summary: what you made, and the final file id(s) and names. Don't list "
        "intermediate files unless asked.\n"
        "- You cannot fetch from the internet or delete files. If someone asks you to recreate a copyrighted or "
        "trademarked character, say you can't and offer to help with an original one instead."
    )


class AssistantNotConfigured(RuntimeError):
    """No Anthropic credentials are available to the server."""

    def __init__(self):
        super().__init__(
            "The assistant has no Claude credentials. Set ANTHROPIC_API_KEY (or ANTHROPIC_AUTH_TOKEN) "
            "in the environment of the process running the backend."
        )


class Session(BaseModel):
    id: str
    owner: str = "local"
    title: str = ""
    messages: list[dict[str, Any]] = []


class ChatNotFound(KeyError):
    """No chat with that id belongs to this user."""


class ChatResult(BaseModel):
    session_id: str
    reply: str
    stop_reason: str | None
    tool_calls: list[dict[str, Any]] = []
    new_files: list[storage.FileInfo] = []


class Assistant:
    def __init__(self, client: anthropic.Anthropic | None = None, max_sessions: int = 200, store=None):
        self._client = client
        self._store = store  # users.UserStore for persistence; None keeps chats in memory only
        self._sessions: OrderedDict[str, Session] = OrderedDict()
        self._locks: dict[str, threading.Lock] = {}
        self._guard = threading.Lock()
        self._max_sessions = max_sessions
        self._tools = build_tools()

    @property
    def client(self) -> anthropic.Anthropic:
        if self._client is None:
            self._client = anthropic.Anthropic()
        return self._client

    # -------------------------------------------------------------- sessions

    def _session(self, session_id: str | None, owner: str) -> tuple[Session, threading.Lock]:
        with self._guard:
            if session_id is None:
                sess = Session(id=uuid.uuid4().hex[:12], owner=owner)
            else:
                sess = self._sessions.get(session_id)
                if sess is None and self._store is not None:
                    saved = self._store.load_chat(session_id)
                    if saved is not None:
                        sess = Session(id=saved["id"], owner=saved["owner"], title=saved["title"], messages=saved["messages"])
                if sess is None or sess.owner != owner:
                    raise ChatNotFound(session_id)
            if sess.id not in self._sessions:
                self._sessions[sess.id] = sess
                self._locks[sess.id] = threading.Lock()
                while len(self._sessions) > self._max_sessions:
                    old, _ = self._sessions.popitem(last=False)
                    self._locks.pop(old, None)
            self._sessions.move_to_end(sess.id)
            return sess, self._locks[sess.id]

    def _persist(self, sess: Session) -> None:
        if self._store is not None:
            self._store.save_chat(sess.id, sess.owner, sess.title, sess.messages)

    def reset(self, session_id: str, owner: str = "local") -> bool:
        with self._guard:
            sess = self._sessions.get(session_id)
            if sess is not None and sess.owner != owner:
                return False
            if sess is not None:
                self._sessions.pop(session_id, None)
                self._locks.pop(session_id, None)
        deleted = self._store.delete_chat(session_id, owner) if self._store is not None else False
        return sess is not None or deleted

    def list_chats(self, owner: str) -> list[dict[str, Any]]:
        if self._store is not None:
            return self._store.list_chats(owner)
        return [{"id": x.id, "title": x.title, "updated_at": None} for x in self._sessions.values() if x.owner == owner]

    def transcript(self, session_id: str, owner: str) -> list[dict[str, str]]:
        """The visible conversation (user text and assistant text only) for redisplaying a saved chat."""
        sess, lock = self._session(session_id, owner)
        out: list[dict[str, str]] = []
        with lock:
            for m in sess.messages:
                content = m["content"]
                if m["role"] == "user" and isinstance(content, str):
                    text = content.split("\n\n[Files attached", 1)[0]
                    out.append({"role": "user", "text": text})
                elif m["role"] == "assistant" and isinstance(content, list):
                    text = "\n".join(b.get("text", "") for b in content if b.get("type") == "text").strip()
                    if text:
                        out.append({"role": "assistant", "text": text})
        return out

    # -------------------------------------------------------------- tools

    def _execute(self, name: str, args: dict[str, Any], produced: list[storage.FileInfo], owner: str = "local") -> tuple[Any, bool]:
        """Run one tool call. Returns (tool_result content, is_error)."""
        from . import craft, craft_tools

        try:
            if name in craft_tools.NAMES:
                result = craft_tools.call(name, args, owner)
                return result if isinstance(result, str) else json.dumps(result, default=str), False
            if name == "list_files":
                files = storage.list_files()[: int(args.get("limit") or 50)]
                return json.dumps([f.model_dump(include={"id", "name", "mime_type", "size", "source"}) for f in files]), False
            if name == "look_at":
                return self._look_at(args["file_id"], int(args.get("frames") or 4), produced), False
            if name not in ops.REGISTRY:
                return f"Unknown tool {name}", True
            job = get_queue().run_and_wait(name, args)
            if job.status != "succeeded":
                return f"Operation failed: {job.error}", True
            produced.extend(job.outputs)
            return json.dumps({
                "outputs": [o.model_dump(include={"id", "name", "mime_type", "size"}) for o in job.outputs],
                "data": job.data,
            }), False
        except ValidationError as e:
            return f"Invalid parameters: {e}", True
        except craft.RunNotFound:
            return "Error: no such build for this person", True
        except (ops.OpError, storage.FileNotFound, KeyError, ValueError, PermissionError, craft.EngineMissing) as e:
            return f"Error: {e}", True

    def _look_at(self, file_id: str, frames: int, produced: list[storage.FileInfo]) -> list[dict[str, Any]]:
        info = storage.get(file_id)
        if info.mime_type in IMAGE_TYPES and info.size <= MAX_IMAGE_BYTES:
            images = [info]
        else:
            res = ops.run_operation("frames", {"file_id": file_id, "count": frames, "width": 640})
            images = res.outputs
        blocks: list[dict[str, Any]] = [{"type": "text", "text": f"{info.name} ({info.mime_type})"}]
        for img in images:
            data = storage.path_of(img.id).read_bytes()
            blocks.append({
                "type": "image",
                "source": {"type": "base64", "media_type": img.mime_type, "data": base64.standard_b64encode(data).decode()},
            })
        return blocks

    # -------------------------------------------------------------- chat

    def chat(self, message: str, session_id: str | None = None, file_ids: list[str] | None = None,
             owner: str = "local") -> ChatResult:
        s = get_settings()
        sess, lock = self._session(session_id, owner)
        with lock:
            if not sess.title:
                sess.title = " ".join(message.split())[:60]
            text = message
            if file_ids:
                text += "\n\n[Files attached to this message: " + ", ".join(file_ids) + "]"
            history = sess.messages
            checkpoint = len(history)
            history.append({"role": "user", "content": text})
            produced: list[storage.FileInfo] = []
            calls: list[dict[str, Any]] = []
            try:
                for _ in range(s.max_tool_turns):
                    resp = self.client.beta.messages.create(
                        model=s.model,
                        max_tokens=16000,
                        system=_system_prompt(),
                        tools=self._tools,
                        messages=history,
                        output_config={"effort": s.effort},
                        cache_control={"type": "ephemeral"},
                        # On a safety decline the API retries on a suitable fallback model in the same call.
                        betas=["server-side-fallback-2026-07-01"],
                        fallbacks="default",
                    )
                    # Keep every block (thinking, fallback, tool_use) so later turns stay valid.
                    history.append({"role": "assistant", "content": [b.to_dict() for b in resp.content]})

                    if resp.stop_reason == "refusal":
                        return self._result(sess, "Sorry, I can't help with that request.", resp.stop_reason, calls, produced)
                    if resp.stop_reason == "pause_turn":
                        continue
                    tool_uses = [b for b in resp.content if b.type == "tool_use"]
                    if resp.stop_reason != "tool_use" or not tool_uses:
                        reply = "\n".join(b.text for b in resp.content if b.type == "text").strip()
                        if resp.stop_reason == "max_tokens":
                            reply += "\n\n(The reply was cut off at the length limit.)"
                        return self._result(sess, reply, resp.stop_reason, calls, produced)

                    results = []
                    for tu in tool_uses:
                        content, is_error = self._execute(tu.name, dict(tu.input or {}), produced, sess.owner)
                        calls.append({"tool": tu.name, "input": tu.input, "ok": not is_error})
                        results.append({"type": "tool_result", "tool_use_id": tu.id, "content": content, "is_error": is_error})
                    history.append({"role": "user", "content": results})

                history.append({"role": "user", "content": "You've hit the step limit. Summarise where things stand."})
                resp = self.client.beta.messages.create(
                    model=s.model, max_tokens=4000, system=_system_prompt(), tools=self._tools, messages=history,
                    output_config={"effort": "low"}, betas=["server-side-fallback-2026-07-01"], fallbacks="default",
                )
                history.append({"role": "assistant", "content": [b.to_dict() for b in resp.content]})
                reply = "\n".join(b.text for b in resp.content if b.type == "text").strip()
                return self._result(sess, reply, "step_limit", calls, produced)
            except anthropic.AuthenticationError as e:
                del history[checkpoint:]
                raise AssistantNotConfigured() from e
            except TypeError as e:
                # The SDK raises TypeError when it finds no credentials at all.
                del history[checkpoint:]
                if "authentication" in str(e).lower():
                    raise AssistantNotConfigured() from e
                raise
            except anthropic.APIError:
                # Roll back this turn so the conversation stays well-formed for the next attempt.
                del history[checkpoint:]
                raise

    def _result(self, sess: Session, reply: str, stop: str | None, calls, produced) -> ChatResult:
        self._persist(sess)
        return ChatResult(session_id=sess.id, reply=reply, stop_reason=stop, tool_calls=calls, new_files=produced)


_assistant: Assistant | None = None


def get_assistant() -> Assistant:
    global _assistant
    if _assistant is None:
        from .users import get_store

        _assistant = Assistant(store=get_store())
    return _assistant
