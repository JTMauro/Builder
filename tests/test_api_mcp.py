import asyncio
import copy
import json
from types import SimpleNamespace

from fastapi.testclient import TestClient
from mcp.client import Client

from media_backend import assistant as assistant_mod
from media_backend.api import create_app
from media_backend.mcp_server import build_server


def test_http_api_flow(media, monkeypatch):
    from media_backend import config
    monkeypatch.setenv("MEDIA_BACKEND_AUTH", "off")
    config.reset_settings()
    with TestClient(create_app()) as c:
        assert c.get("/health").json()["ok"]
        assert any(o["name"] == "animate_image" for o in c.get("/operations").json())
        up = c.post("/files", files={"file": ("my clip!.mp4", (media["dir"] / "clip.mp4").read_bytes(), "video/mp4")})
        assert up.status_code == 201 and up.json()["name"] == "my_clip_.mp4"
        fid = up.json()["id"]
        assert c.get(f"/files/{fid}/probe").json()["has_audio"]
        job = c.post("/jobs?wait=true", json={"operation": "trim", "params": {"file_id": fid, "start": 0, "duration": 1}}).json()
        assert job["status"] == "succeeded"
        out = job["outputs"][0]["id"]
        assert c.get(f"/files/{out}/content").content[:12].find(b"ftyp") >= 0
        assert c.post(f"/ops/thumbnail", json={"file_id": fid}).json()["status"] == "succeeded"
        assert c.post("/jobs", json={"operation": "trim", "params": {"file_id": fid, "start": 5, "end": 1}}).status_code == 422
        assert c.post("/jobs", json={"operation": "nope", "params": {}}).status_code == 422
        assert c.get("/files/000000000000").status_code == 404
        assert c.delete(f"/files/{out}").status_code == 204


H = {"X-Builder": "1"}


def test_server_token_still_works(media, monkeypatch):
    from media_backend import config
    monkeypatch.setenv("MEDIA_BACKEND_TOKEN", "s3cret")
    config.reset_settings()
    with TestClient(create_app()) as c:
        assert c.get("/health").status_code == 200
        assert c.get("/files").status_code == 401
        assert c.post("/mcp/", json={}).status_code == 401
        assert c.get("/files", headers={"Authorization": "Bearer s3cret"}).status_code == 200
        assert c.get("/files", headers={"Authorization": "Bearer wrong"}).status_code == 401


def test_accounts_and_login(media):
    with TestClient(create_app()) as c:
        st = c.get("/auth/status").json()
        assert st["needs_setup"] and st["user"] is None
        assert c.get("/files").status_code == 401
        # first account becomes admin and is signed in
        r = c.post("/auth/setup", json={"username": "maker", "password": "hammer-time", "display_name": "Maker"})
        assert r.status_code == 200 and r.json()["user"]["is_admin"]
        assert "httponly" in r.headers["set-cookie"].lower()
        assert c.post("/auth/setup", json={"username": "x2", "password": "whatever123"}).status_code == 409
        assert c.get("/files").status_code == 200
        # cookie writes need our header (blocks cross-site form posts)
        assert c.post("/auth/users", json={"username": "sam", "password": "sawdust99"}).status_code == 403
        r = c.post("/auth/users", headers=H, json={"username": "sam", "password": "sawdust99", "display_name": "Sam"})
        assert r.status_code == 201 and not r.json()["is_admin"]
        assert c.post("/auth/users", headers=H, json={"username": "sam", "password": "sawdust99"}).status_code == 422
        assert c.post("/auth/users", headers=H, json={"username": "al", "password": "short"}).status_code == 422
        # sign out, then wrong and right passwords
        assert c.post("/auth/logout", headers=H).status_code == 204
        assert c.get("/files").status_code == 401
        assert c.post("/auth/login", json={"username": "sam", "password": "nope-nope"}).status_code == 401
        r = c.post("/auth/login", json={"username": "SAM", "password": "sawdust99"})
        assert r.status_code == 200 and r.json()["user"]["username"] == "sam"
        # non-admins can't manage people
        assert c.get("/auth/users").status_code == 403
        # personal token for Claude Code: shown once, works as a bearer token (incl. MCP), can be revoked
        t = c.post("/auth/tokens", headers=H, json={"name": "laptop"}).json()
        assert t["token"].startswith("mbp_") and t["hint"] == t["token"][-4:]
        assert all("token" not in x for x in c.get("/auth/tokens").json())
        anon = TestClient(c.app)
        assert anon.get("/files").status_code == 401
        assert anon.get("/files", headers={"Authorization": f"Bearer {t['token']}"}).status_code == 200
        assert c.delete(f"/auth/tokens/{t['id']}", headers=H).status_code == 204
        assert anon.get("/files", headers={"Authorization": f"Bearer {t['token']}"}).status_code == 401
        # password change signs you out everywhere
        assert c.post("/auth/password", headers=H, json={"current_password": "bad", "new_password": "newpass123"}).status_code == 401
        assert c.post("/auth/password", headers=H, json={"current_password": "sawdust99", "new_password": "newpass123"}).status_code == 204
        assert c.get("/files").status_code == 401
        assert c.post("/auth/login", json={"username": "sam", "password": "newpass123"}).status_code == 200


def test_login_lockout(media):
    with TestClient(create_app()) as c:
        c.post("/auth/setup", json={"username": "maker", "password": "hammer-time"})
        c.post("/auth/logout", headers=H)
        for _ in range(5):
            assert c.post("/auth/login", json={"username": "maker", "password": "wrong-pass"}).status_code == 401
        assert c.post("/auth/login", json={"username": "maker", "password": "hammer-time"}).status_code == 429


def test_open_signup(media, monkeypatch):
    from media_backend import config
    with TestClient(create_app()) as c:
        c.post("/auth/setup", json={"username": "maker", "password": "hammer-time"})
        assert c.post("/auth/signup", json={"username": "kid", "password": "drawings1"}).status_code == 403
        monkeypatch.setenv("MEDIA_BACKEND_OPEN_SIGNUP", "on")
        config.reset_settings()
        r = c.post("/auth/signup", json={"username": "kid", "password": "drawings1"})
        assert r.status_code == 200 and not r.json()["user"]["is_admin"]


def test_chats_are_private_per_user(media):
    from media_backend import assistant as amod, users

    class Reply:
        def create(self, **kw):
            text = "Hello " + kw["messages"][0]["content"][:5]
            return SimpleNamespace(stop_reason="end_turn", content=[_block(type="text", text=text)])

    amod._assistant = amod.Assistant(client=SimpleNamespace(beta=SimpleNamespace(messages=Reply())), store=users.get_store())
    with TestClient(create_app()) as alice:
        alice.post("/auth/setup", json={"username": "alice", "password": "password1"})
        alice.post("/auth/users", headers=H, json={"username": "bob", "password": "password2"})
        r = alice.post("/assistant/chat", headers=H, json={"message": "alice secret plan"}).json()
        sid = r["session_id"]
        assert alice.post("/assistant/chat", headers=H, json={"message": "more", "session_id": sid}).status_code == 200
        chats = alice.get("/assistant/sessions").json()
        assert [x["id"] for x in chats] == [sid] and chats[0]["title"] == "alice secret plan"
        t = alice.get(f"/assistant/sessions/{sid}").json()["messages"]
        assert [m["role"] for m in t] == ["user", "assistant", "user", "assistant"]

        bob = TestClient(alice.app)
        bob.post("/auth/login", json={"username": "bob", "password": "password2"})
        assert bob.get("/assistant/sessions").json() == []
        assert bob.get(f"/assistant/sessions/{sid}").status_code == 404
        assert bob.post("/assistant/chat", headers=H, json={"message": "hi", "session_id": sid}).status_code == 404
        assert bob.delete(f"/assistant/sessions/{sid}", headers=H).status_code == 404

    # chats survive a restart (fresh assistant, same database)
    amod._assistant = amod.Assistant(client=SimpleNamespace(beta=SimpleNamespace(messages=Reply())), store=users.get_store())
    with TestClient(create_app()) as alice:
        alice.post("/auth/login", json={"username": "alice", "password": "password1"})
        assert len(alice.get(f"/assistant/sessions/{sid}").json()["messages"]) == 4
        assert alice.delete(f"/assistant/sessions/{sid}", headers=H).status_code == 204
        assert alice.get("/assistant/sessions").json() == []


def test_mcp_tools(media):
    async def go():
        async with Client(build_server(), raise_exceptions=True) as c:
            names = {t.name for t in (await c.list_tools()).tools}
            assert {"import_path", "export_file", "view_file", "ask_assistant", "concat", "animate_image"} <= names
            r = await c.call_tool("import_path", {"path": str(media["dir"] / "clip.mp4")})
            fid = json.loads(r.content[0].text)["id"]
            r = await c.call_tool("trim", {"params": {"file_id": fid, "start": 0, "duration": 1}})
            payload = json.loads(r.content[0].text)
            assert payload["status"] == "succeeded", payload
            out = payload["outputs"][0]["id"]
            (media["dir"] / "exported").mkdir()
            r = await c.call_tool("export_file", {"file_id": out, "dest_path": str(media["dir"] / "exported")})
            assert (media["dir"] / "exported" / "clip_trim.mp4").exists(), r
            r = await c.call_tool("export_file", {"file_id": out, "dest_path": str(media["dir"] / "exported")})
            assert r.is_error and "already exists" in r.content[0].text
            r = await c.call_tool("view_file", {"file_id": fid, "frames": 2})
            assert [b.type for b in r.content] == ["image", "image"]
            r = await c.call_tool("import_path", {"path": "/etc/passwd"})
            assert r.is_error and "outside the allowed" in r.content[0].text
            r = await c.call_tool("get_job", {"job_id": "nope"})
            assert r.is_error and "No job" in r.content[0].text
            r = await c.call_tool("trim", {"params": {"file_id": "000000000000", "start": 0}})
            assert "No file" in r.content[0].text
    asyncio.run(go())


# ------------------------------------------------------------------ assistant with a fake Claude client


def _block(**kw):
    return SimpleNamespace(**kw, to_dict=lambda kw=kw: dict(kw))


class FakeMessages:
    """Scripted model: probe the file, make a thumbnail, look at it, then reply."""

    def __init__(self, file_id):
        self.calls = []
        self.script = [
            [_block(type="tool_use", id="t1", name="probe", input={"file_id": file_id})],
            [_block(type="tool_use", id="t2", name="thumbnail", input={"file_id": file_id, "time": 1}),
             _block(type="tool_use", id="t3", name="look_at", input={"file_id": file_id, "frames": 2})],
            [_block(type="text", text="Done! Your thumbnail is ready.")],
        ]

    def create(self, **kwargs):
        self.calls.append(copy.deepcopy(kwargs))
        content = self.script[len(self.calls) - 1]
        stop = "tool_use" if any(b.type == "tool_use" for b in content) else "end_turn"
        return SimpleNamespace(content=content, stop_reason=stop)


def test_assistant_tool_loop(media, monkeypatch):
    fake = FakeMessages(media["video"])
    bot = assistant_mod.Assistant(client=SimpleNamespace(beta=SimpleNamespace(messages=fake)))
    res = bot.chat("make me a thumbnail", file_ids=[media["video"]])
    assert res.reply == "Done! Your thumbnail is ready."
    assert [c["tool"] for c in res.tool_calls] == ["probe", "thumbnail", "look_at"]
    assert all(c["ok"] for c in res.tool_calls)
    assert any(f.mime_type == "image/png" for f in res.new_files)
    # request shape
    req = fake.calls[0]
    assert req["model"] == "claude-opus-5-5" and req["fallbacks"] == "default"
    assert "Rivet" in req["system"]
    # both parallel tool results came back in ONE user message, images included for look_at
    results = fake.calls[2]["messages"][-1]["content"]
    assert [r["tool_use_id"] for r in results] == ["t2", "t3"]
    assert any(b.get("type") == "image" for b in results[1]["content"])
    # conversation continues in the same session
    assert bot._sessions[res.session_id].messages[0]["role"] == "user"


def test_assistant_reports_tool_errors(media):
    class Msgs:
        n = 0
        def create(self, **kw):
            Msgs.n += 1
            if Msgs.n == 1:
                return SimpleNamespace(stop_reason="tool_use", content=[_block(type="tool_use", id="x", name="trim", input={"file_id": "bad"})])
            self.last = copy.deepcopy(kw)
            return SimpleNamespace(stop_reason="end_turn", content=[_block(type="text", text="That file doesn't exist.")])
    m = Msgs()
    bot = assistant_mod.Assistant(client=SimpleNamespace(beta=SimpleNamespace(messages=m)))
    res = bot.chat("trim it")
    assert res.tool_calls[0]["ok"] is False
    assert m.last["messages"][-1]["content"][0]["is_error"] is True


def test_frontend_served_without_login():
    with TestClient(create_app()) as c:
        page = c.get("/")
        assert page.status_code == 200 and "Builder" in page.text and 'id="mascot"' in page.text
        assert c.get("/static/app.js").status_code == 200 and c.get("/static/app.css").status_code == 200
        assert c.get("/files").status_code == 401
