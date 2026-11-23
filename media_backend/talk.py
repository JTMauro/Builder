"""Talk to a character live: you speak or type, Claude answers in character, Kokoro voices the answer
and Builder's lipsync works out the mouth shapes, so the stage page can play it with a moving mouth.

Nothing is stored between turns: the browser sends the last few turns (`history`) with each request,
so one person's conversation never reaches anyone else.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

import anthropic

from . import ops, storage
from .config import get_settings

GESTURES = ("wave", "point", "thumbs up", "agree", "headShake", "shrug", "laugh", "hello", "bow",
            "arms crossed", "dance", "jump", "none")
DEFAULT_PERSONA = (
    "the teen on Builder's 3D stage: a friendly, upbeat 16-year-old guy in a pale grey tee, light faded jeans "
    "and olive sneakers, made by Builder from a photo with free tools (MakeHuman, Kokoro voice, CMU motion "
    "capture). Kind, curious and clean; never claims to be a real person"
)
MAX_TURNS = 8
# For trying the page without a Claude key: TALK_STUB_REPLY='{"say": "...", "gesture": "wave"}' (tests, demos).
STUB_ENV = "TALK_STUB_REPLY"

REPLY_SCHEMA = {
    "type": "object",
    "properties": {"say": {"type": "string"}, "gesture": {"type": "string", "enum": list(GESTURES)}},
    "required": ["say", "gesture"],
    "additionalProperties": False,
}


STAGE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")


def persona_for(character: str) -> str:
    """The default persona for a stage: the teen from studio/BIBLE.md, or a friendly character by name."""
    if character == "teen":
        return DEFAULT_PERSONA
    return f"{character.replace('_', ' ').replace('-', ' ')}, a friendly character on Builder's 3D stage"


def stage_file(name: str, path: str) -> Path | None:
    """A file inside a stage folder, or None. Refuses anything outside the stage (.., absolute, links out)."""
    from .scene import stages_dir

    if not STAGE_NAME.fullmatch(name or "") or "\\" in path or "\0" in path:
        return None
    root = (stages_dir() / name).resolve()
    if not (root / "index.html").is_file():
        return None
    target = (root / (path or "index.html")).resolve()
    if target.is_dir():
        target = target / "index.html"
    if not target.is_relative_to(root) or not target.is_file():
        return None
    return target


class TalkError(RuntimeError):
    """Something the person can fix or retry (shown as the error message)."""


def system_prompt(persona: str | None) -> str:
    who = (persona or "").strip() or DEFAULT_PERSONA
    return (
        f"You are a character on a 3D stage, talking live with the person in front of you. You are {who}.\n"
        "Stay in character, keep it clean and kind, and never claim to be a real person; if asked, you're a "
        "3D character made with Builder. Never recreate someone else's copyrighted character.\n"
        "Reply with at most two short spoken sentences (no lists, no emoji, no stage directions).\n"
        f"Pick one gesture that fits the reply from: {', '.join(GESTURES)}.\n"
        'Answer only JSON: {"say": "...", "gesture": "..."}.'
    )


def clean_history(history: Any) -> list[dict[str, str]]:
    """The last few turns as Messages API turns: [{role: user|assistant, content: text}], alternating."""
    if isinstance(history, str):
        try:
            history = json.loads(history) if history.strip() else []
        except json.JSONDecodeError as e:
            raise TalkError("history must be a JSON list of {role, text}") from e
    out: list[dict[str, str]] = []
    for turn in (history or [])[-MAX_TURNS:]:
        if not isinstance(turn, dict):
            continue
        role = turn.get("role")
        text = str(turn.get("text") or turn.get("content") or "").strip()[:600]
        if role not in ("user", "assistant") or not text:
            continue
        if out and out[-1]["role"] == role:
            out[-1]["content"] += "\n" + text
        else:
            out.append({"role": role, "content": text})
    while out and out[0]["role"] != "user":
        out.pop(0)
    if out and out[-1]["role"] == "user":   # the new message is the user turn; drop a dangling one
        out.pop()
    return out


def _parse_reply(text: str) -> dict[str, str]:
    m = re.search(r"\{.*\}", text or "", re.S)
    try:
        data = json.loads(m.group(0)) if m else {}
    except json.JSONDecodeError:
        data = {}
    say = str(data.get("say") or "").strip() or (text or "").strip()
    gesture = data.get("gesture") if data.get("gesture") in GESTURES else "none"
    return {"say": " ".join(say.split())[:400], "gesture": gesture}


def reply(message: str, history: list[dict[str, str]], persona: str | None = None) -> dict[str, str]:
    """Claude's in-character answer as {say, gesture}, through the assistant's client and model."""
    stub = os.environ.get(STUB_ENV)
    if stub:
        return _parse_reply(stub)
    from .assistant import AssistantNotConfigured, get_assistant

    s = get_settings()
    try:
        resp = get_assistant().client.beta.messages.create(
            model=s.model,
            max_tokens=2000,
            system=system_prompt(persona),
            messages=[*history, {"role": "user", "content": message}],
            output_config={"effort": "low", "format": {"type": "json_schema", "schema": REPLY_SCHEMA}},
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        )
    except anthropic.AuthenticationError as e:
        raise AssistantNotConfigured() from e
    except TypeError as e:  # the SDK raises TypeError when it finds no credentials at all
        if "authentication" in str(e).lower():
            raise AssistantNotConfigured() from e
        raise
    if resp.stop_reason == "refusal":
        return {"say": "Let's talk about something else.", "gesture": "shrug"}
    text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
    out = _parse_reply(text)
    if not out["say"]:
        raise TalkError("The character didn't come up with an answer; try again.")
    return out


def hear(file_id: str) -> str:
    """What was said in a recording (Whisper)."""
    from . import voice

    segments, words, _ = voice.words_of(file_id)
    return " ".join(s["text"] for s in segments).strip() or " ".join(w["word"] for w in words).strip()


def speak_line(say: str, voice_name: str) -> dict[str, Any]:
    """Voice a line (Kokoro) and work out its mouth shapes: {file_id, duration, cues, words}."""
    audio = ops.run_operation("speak", {"text": say, "voice": voice_name, "name": "talk_reply"}).outputs[0]
    lip = ops.run_operation("lipsync", {"file_id": audio.id}).outputs[0]
    doc = json.loads(storage.path_of(lip.id).read_text(encoding="utf-8"))
    duration = float(doc.get("metadata", {}).get("duration") or 0)
    cues = [[c["start"], c["value"]] for c in doc.get("mouthCues", [])]
    words = [{"w": w["word"].strip(), "s": round(w["start"], 3), "e": round(w["end"], 3)} for w in doc.get("words", [])]
    return {"file_id": audio.id, "duration": duration, "cues": cues, "words": words}
