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
    return tools


def _system_prompt() -> str:
    s = get_settings()
    return (
        f"You are {s.assistant_name}, the built-in assistant of a media backend: {s.assistant_persona}.\n\n"
        "You help people and other programs process audio and video. Your tools run real ffmpeg operations on files "
        "in this server's media store. Inputs are referenced by file id and are never modified; every operation "
        "writes new file(s) and returns their ids.\n\n"
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
    messages: list[dict[str, Any]] = []


class ChatResult(BaseModel):
    session_id: str
    reply: str
    stop_reason: str | None
    tool_calls: list[dict[str, Any]] = []
    new_files: list[storage.FileInfo] = []


class Assistant:
    def __init__(self, client: anthropic.Anthropic | None = None, max_sessions: int = 200):
        self._client = client
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

    def _session(self, session_id: str | None) -> tuple[Session, threading.Lock]:
        with self._guard:
            sid = session_id or uuid.uuid4().hex[:12]
            sess = self._sessions.get(sid)
            if sess is None:
                sess = Session(id=sid)
                self._sessions[sid] = sess
                self._locks[sid] = threading.Lock()
                while len(self._sessions) > self._max_sessions:
                    old, _ = self._sessions.popitem(last=False)
                    self._locks.pop(old, None)
            self._sessions.move_to_end(sid)
            return sess, self._locks[sid]

    def reset(self, session_id: str) -> bool:
        with self._guard:
            self._locks.pop(session_id, None)
            return self._sessions.pop(session_id, None) is not None

    # -------------------------------------------------------------- tools

    def _execute(self, name: str, args: dict[str, Any], produced: list[storage.FileInfo]) -> tuple[Any, bool]:
        """Run one tool call. Returns (tool_result content, is_error)."""
        try:
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
        except (ops.OpError, storage.FileNotFound, KeyError, ValueError) as e:
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

    def chat(self, message: str, session_id: str | None = None, file_ids: list[str] | None = None) -> ChatResult:
        s = get_settings()
        sess, lock = self._session(session_id)
        with lock:
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
                        content, is_error = self._execute(tu.name, dict(tu.input or {}), produced)
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

    @staticmethod
    def _result(sess: Session, reply: str, stop: str | None, calls, produced) -> ChatResult:
        return ChatResult(session_id=sess.id, reply=reply, stop_reason=stop, tool_calls=calls, new_files=produced)


_assistant: Assistant | None = None


def get_assistant() -> Assistant:
    global _assistant
    if _assistant is None:
        _assistant = Assistant()
    return _assistant
