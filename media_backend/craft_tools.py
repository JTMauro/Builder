"""Craft tools shared by Rivet (the built-in assistant) and the MCP connector.

Each function takes the owner (the person the build belongs to) plus plain JSON arguments and
returns a JSON-able result. Approvals are deliberately not answerable here: a person approves
actions on the build screen.
"""

from __future__ import annotations

import json
import subprocess
from typing import Any

from . import craft, storage

MAX_FEED = 20

TOOLS: list[dict[str, Any]] = [
    {
        "name": "craft_projects_list",
        "description": "List this person's Craft projects (things built from a website, app or game, or from scratch), newest first, with status and the last check's likeness.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "craft_build_start",
        "description": (
            "Start a Craft build: the engine studies a target (a web address, an uploaded file id, or nothing for "
            "something new), a coding agent builds the person's own version in a new project, then it is checked "
            "against the original and refined. Returns a run id; follow it with craft_build_status."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "request": {"type": "string", "description": "What to make, in the person's words"},
                "target": {"type": "string", "description": "A web address to study (http/https), or empty"},
                "file_id": {"type": "string", "description": "An uploaded app, game or file to study instead"},
                "rounds": {"type": "integer", "minimum": 0, "maximum": 10, "description": "Refinement rounds after the first build (default 2)"},
            },
            "required": ["request"],
        },
    },
    {
        "name": "craft_build_followup",
        "description": "Ask for a change to an existing project (by run id or project path); it continues the same project.",
        "input_schema": {
            "type": "object",
            "properties": {
                "request": {"type": "string"},
                "run_id": {"type": "string"},
                "project_path": {"type": "string"},
            },
            "required": ["request"],
        },
    },
    {
        "name": "craft_build_status",
        "description": "Where a build is: its step, status, checks (how alike), preview address, anything waiting for an answer, and the latest activity.",
        "input_schema": {"type": "object", "properties": {"run_id": {"type": "string"}}, "required": ["run_id"]},
    },
    {
        "name": "craft_build_answer",
        "description": "Answer a question the builder asked (a choice and/or text). Approvals of actions can't be answered here: tell the person to approve or refuse on the build screen.",
        "input_schema": {
            "type": "object",
            "properties": {"run_id": {"type": "string"}, "choice": {"type": "string"}, "text": {"type": "string"}},
            "required": ["run_id"],
        },
    },
    {
        "name": "craft_build_stop",
        "description": "Stop a running build.",
        "input_schema": {"type": "object", "properties": {"run_id": {"type": "string"}}, "required": ["run_id"]},
    },
    {
        "name": "craft_engine_tools_search",
        "description": "Search the Craft engine's ~300 analysis tools (websites, programs, apps, files, comparing) by words; returns ids and titles to use with craft_engine_tool.",
        "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
    },
    {
        "name": "craft_engine_tool",
        "description": "Run one Craft engine tool by id with JSON parameters (find ids with craft_engine_tools_search). Tools that run foreign code or change a device are refused.",
        "input_schema": {
            "type": "object",
            "properties": {"tool": {"type": "string"}, "params": {"type": "object"}},
            "required": ["tool"],
        },
    },
]

NAMES = {t["name"] for t in TOOLS}


def _summary(run: craft.Run) -> dict[str, Any]:
    out = run.model_dump(include={"id", "status", "step", "project", "preview", "message", "request", "target"})
    out["checks"] = [
        {"round": c.get("round"), "passed": c.get("passed"), "similarity": c.get("similarity"), "note": c.get("note")}
        for c in run.checks
    ]
    if run.pending:
        p = run.pending
        out["waiting_for"] = {"kind": p.get("kind"), "title": p.get("title"), "detail": p.get("detail"), "choices": p.get("choices")}
    return out


def _feed(events: list[dict[str, Any]]) -> list[str]:
    lines = []
    for e in events:
        t = e.get("type")
        if t == "step":
            lines.append(f"[{e.get('step')}] {e.get('text', '')}")
        elif t == "activity":
            lines.append(f"· {e.get('words', '')}")
        elif t == "text":
            text = " ".join(e.get("text", "").split())
            if text:
                lines.append(text[-300:])
        elif t == "check":
            c = e.get("check") or {}
            lines.append(f"[check {c.get('round')}] {c.get('similarity')}% alike: {c.get('note', '')}")
        elif t == "error":
            lines.append(f"! {e.get('message', '')}")
        elif t == "finished":
            lines.append(f"finished: {e.get('message', '')}")
    return lines[-MAX_FEED:]


_tool_list_cache: list[dict[str, str]] | None = None


def engine_tools() -> list[dict[str, str]]:
    global _tool_list_cache
    if _tool_list_cache is None:
        out = subprocess.run([str(craft.engine_bin()), "commands", "--format", "json"], capture_output=True, text=True,
                             env=craft.engine_env(), timeout=120, stdin=subprocess.DEVNULL)
        tools: list[dict[str, str]] = []
        try:
            data = json.loads(out.stdout)
            if isinstance(data, dict):
                data = data.get("result", data)
            items = data if isinstance(data, list) else data.get("tools") or data.get("commands") or []
            for t in items:
                tid = isinstance(t, dict) and (t.get("tool") or t.get("id"))
                if tid:
                    tools.append({"id": tid, "title": t.get("title", ""), "summary": t.get("summary", "")})
        except ValueError:
            for line in out.stdout.splitlines():
                parts = line.strip().split(None, 1)
                if parts and parts[0].replace("_", "").replace("-", "").isalnum():
                    tools.append({"id": parts[0].replace("-", "_"), "title": parts[1] if len(parts) > 1 else "", "summary": ""})
        _tool_list_cache = tools
    return _tool_list_cache


def call(name: str, args: dict[str, Any], owner: str) -> Any:
    runs = craft.get_runs()
    if name == "craft_projects_list":
        return craft.list_projects(owner)
    if name == "craft_build_start":
        target = (args.get("target") or "").strip()
        if args.get("file_id"):
            target = str(storage.path_of(args["file_id"]))
        elif target and not target.startswith(("http://", "https://")):
            raise ValueError("target must be a web address (http/https); use file_id for an uploaded file")
        if not (args.get("request") or "").strip():
            raise ValueError("request is required")
        run = runs.start(owner, target, args["request"].strip(), max(0, min(int(args.get("rounds", 2)), 10)))
        return _summary(run)
    if name == "craft_build_followup":
        if args.get("run_id"):
            project = runs.get(args["run_id"], owner).project
        else:
            project = str(runs.owned_project(owner, args.get("project_path", "")))
        if not project:
            raise ValueError("That build has no project yet")
        return _summary(runs.start(owner, "-", args["request"].strip(), 2, continue_in=project))
    if name == "craft_build_status":
        events, _, run = runs.events(args["run_id"], owner)
        return {**_summary(run), "recent": _feed(events)}
    if name == "craft_build_answer":
        run = runs.get(args["run_id"], owner)
        if not run.pending:
            raise ValueError("Nothing is waiting for an answer")
        if run.pending.get("kind") == "approval":
            raise ValueError("This is an approval of an action; only the person can approve or refuse it, on the build screen.")
        answer = {k: args[k] for k in ("choice", "text") if args.get(k)}
        return _summary(runs.answer(args["run_id"], owner, answer))
    if name == "craft_build_stop":
        return _summary(runs.stop(args["run_id"], owner))
    if name == "craft_engine_tools_search":
        words = [w for w in (args.get("query") or "").lower().split() if w]
        hits = [t for t in engine_tools() if all(w in f"{t['id']} {t['title']} {t['summary']}".lower() for w in words)]
        return hits[:25]
    if name == "craft_engine_tool":
        tool = (args.get("tool") or "").strip()
        if not tool or not tool.replace("_", "").isalnum():
            raise ValueError("tool must be an engine tool id such as web_site_capture")
        out = subprocess.run(
            [str(craft.engine_bin()), tool.replace("_", "-"), "--json", json.dumps(args.get("params") or {})],
            capture_output=True, text=True, env=craft.engine_env(), timeout=900, stdin=subprocess.DEVNULL,
            cwd=str(runs.projects_dir(owner)),
        )
        text = (out.stdout or out.stderr).strip()
        if out.returncode != 0:
            raise ValueError(f"{tool} failed: {text[-2000:]}")
        return text[:20000]
    raise KeyError(name)
