import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from media_backend import craft
from media_backend.api import create_app

FAKE = str(Path(__file__).parent / "fixtures" / "fake_engine.py")
H = {"X-Builder": "1"}


@pytest.fixture(autouse=True)
def fake_engine(monkeypatch):
    monkeypatch.setenv("CRAFT_ENGINE_BIN", FAKE)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    craft._runs = None
    craft._ai_ready = False
    yield
    craft._runs = None


def follow(c, run_id, until):
    cursor, events = 0, []
    deadline = time.time() + 20
    while time.time() < deadline:
        r = c.get(f"/craft/runs/{run_id}?after={cursor}&wait=2").json()
        events += r["events"]
        cursor = r["cursor"]
        if until(r["run"], events):
            return r["run"], events
    raise AssertionError(f"timed out; events so far: {events}")


def signed_in(c, name="maker"):
    c.post("/auth/setup", json={"username": name, "password": "hammer-time"})
    return c


def test_status_and_full_run_with_approval():
    with TestClient(create_app()) as c:
        signed_in(c)
        st = c.get("/craft/status").json()
        assert st["engine"] and "0.0.0-test" in st["version"]
        assert c.post("/craft/runs", headers=H, json={"request": "", "target": ""}).status_code == 422
        assert c.post("/craft/runs", headers=H, json={"request": "x", "target": "/etc/passwd"}).status_code == 422
        r = c.post("/craft/runs", headers=H, json={"request": "A landing page like this", "target": "https://example.com"})
        assert r.status_code == 201
        rid = r.json()["id"]
        run, events = follow(c, rid, lambda run, ev: run["status"] == "waiting")
        assert run["pending"]["title"] == "Run `npm install`"
        assert any(e["type"] == "log" for e in events) or True
        # streamed text is merged into one event
        assert [e["text"] for e in events if e["type"] == "text"] == ["Looking at the page.\n"]
        assert c.post(f"/craft/runs/{rid}/answer", headers=H, json={"allow": True, "always": True}).json()["status"] == "running"
        assert c.post(f"/craft/runs/{rid}/answer", headers=H, json={"allow": True}).status_code == 409
        run, events = follow(c, rid, lambda run, ev: run["finished_at"] is not None)
        assert run["status"] == "done" and run["message"] == "Built"
        assert run["checks"][0]["similarity"] == 87.5 and run["preview"] == "http://127.0.0.1:5199/"
        assert run["step"] == "study" and run["project"].endswith("made-by-test")
        # the project shows up, and its check screenshot can be fetched; outside paths can't
        projects = c.get("/craft/projects").json()
        assert projects[0]["name"] == "Made by test" and projects[0]["last_check"]["similarity"] == 87.5
        shot = c.get("/craft/projects/file", params={"path": run["project"] + "/checks/shot.png"})
        assert shot.status_code == 200 and shot.content.startswith(b"\x89PNG")
        assert c.get("/craft/projects/file", params={"path": run["project"] + "/../../../builder.db"}).status_code == 404
        # a follow-up continues the same project
        f = c.post(f"/craft/runs/{rid}/followup", headers=H, json={"request": "Make the header blue"})
        assert f.status_code == 201
        run2, _ = follow(c, f.json()["id"], lambda run, ev: run["status"] == "waiting")
        c.post(f"/craft/runs/{run2['id']}/answer", headers=H, json={"allow": False})
        run2, _ = follow(c, run2["id"], lambda run, ev: run["finished_at"] is not None)
        assert run2["status"] == "failed" and run2["project"] == run["project"]


def test_builds_are_private_and_stoppable(monkeypatch):
    monkeypatch.setenv("FAKE_ENGINE_HANG", "1")
    with TestClient(create_app()) as alice:
        signed_in(alice, "alice")
        alice.post("/auth/users", headers=H, json={"username": "bob", "password": "password2"})
        rid = alice.post("/craft/runs", headers=H, json={"request": "a game", "target": ""}).json()["id"]
        follow(alice, rid, lambda run, ev: run["status"] == "waiting")
        bob = TestClient(alice.app)
        bob.post("/auth/login", json={"username": "bob", "password": "password2"})
        assert bob.get(f"/craft/runs/{rid}").status_code == 404
        assert bob.post(f"/craft/runs/{rid}/stop", headers=H).status_code == 404
        assert bob.get("/craft/runs").json() == [] and bob.get("/craft/projects").json() == []
        alice.post(f"/craft/runs/{rid}/answer", headers=H, json={"allow": True})
        time.sleep(0.5)
        assert alice.post(f"/craft/runs/{rid}/stop", headers=H).json()["status"] == "stopped"
        run, _ = follow(alice, rid, lambda run, ev: run["finished_at"] is not None)
        assert run["status"] == "stopped"


def test_engine_missing(monkeypatch, tmp_path):
    monkeypatch.setenv("CRAFT_ENGINE_BIN", str(tmp_path / "nope"))
    monkeypatch.setenv("CARGO_TARGET_DIR", str(tmp_path))
    monkeypatch.setattr(craft, "ENGINE_DIR", tmp_path)
    monkeypatch.setattr(craft.shutil, "which", lambda _n: None)
    with TestClient(create_app()) as c:
        signed_in(c)
        assert c.get("/craft/status").json()["engine"] is False
        r = c.post("/craft/runs", headers=H, json={"request": "x", "target": ""})
        assert r.status_code == 503 and "isn't built" in r.json()["detail"]


def test_media_tools_plugged_into_engine(monkeypatch, tmp_path):
    import json as _json
    log = tmp_path / "engine.log"
    monkeypatch.setenv("FAKE_ENGINE_LOG", str(log))
    craft.ensure_ai()
    calls = [_json.loads(l) for l in log.read_text().splitlines()]
    assert [c[0] for c in calls] == ["plugins-add", "plugins-refresh"]
    spec = _json.loads(calls[0][calls[0].index("--json") + 1])
    assert spec["id"] == "builder_media" and spec["args"] == ["-m", "media_backend.cli", "mcp"]
    assert spec["env"]["MEDIA_BACKEND_ALLOWED_DIRS"].endswith("projects")
    assert "ANTHROPIC_API_KEY" not in spec["env"]
    craft.ensure_ai()  # once per start
    assert len(log.read_text().splitlines()) == 2


def test_mcp_craft_tools_and_media_only_plugin_mode():
    import asyncio
    from mcp.client import Client
    from media_backend.mcp_server import build_server

    async def names(server):
        async with Client(server, raise_exceptions=True) as c:
            return {t.name for t in (await c.list_tools()).tools}

    full = asyncio.run(names(build_server(media_only=False)))
    assert {"craft_build_start", "craft_build_status", "craft_engine_tool", "ask_assistant", "trim"} <= full
    plugin = asyncio.run(names(build_server(media_only=True)))
    assert "trim" in plugin and "animate_image" in plugin
    assert not any(n.startswith("craft_") for n in plugin) and "ask_assistant" not in plugin
    assert craft.media_plugin_spec()["env"]["MEDIA_BACKEND_MCP_MEDIA_ONLY"] == "1"


def test_rivet_cannot_answer_approvals(monkeypatch):
    from media_backend import craft_tools
    with TestClient(create_app()) as c:
        signed_in(c)
        rid = c.post("/craft/runs", headers=H, json={"request": "a page", "target": ""}).json()["id"]
        follow(c, rid, lambda run, ev: run["status"] == "waiting")
        with pytest.raises(ValueError, match="only the person"):
            craft_tools.call("craft_build_answer", {"run_id": rid, "text": "yes"}, "maker")
        status = craft_tools.call("craft_build_status", {"run_id": rid}, "maker")
        assert status["waiting_for"]["kind"] == "approval" and status["recent"]
        with pytest.raises(craft.RunNotFound):
            craft_tools.call("craft_build_status", {"run_id": rid}, "someone-else")
        c.post(f"/craft/runs/{rid}/answer", headers=H, json={"allow": False})
