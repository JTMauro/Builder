"""Standalone-app settings: the Claude API key, remote access (Cloudflare Tunnel) and connector links."""

import asyncio
import os
import sys
import textwrap

import urllib.error
import urllib.request
import pytest
from fastapi.testclient import TestClient
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from media_backend import api, remote, settings_file
from media_backend.api import create_app
from media_backend.users import get_store

from test_access import live_server  # noqa: F401  (fixture)


def status(url, method="POST"):
    req = urllib.request.Request(url, data=b"{}" if method == "POST" else None, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code


@pytest.fixture(autouse=True)
def _own_environment(monkeypatch):
    monkeypatch.setattr(os, "environ", os.environ.copy())
    os.environ.pop("ANTHROPIC_API_KEY", None)
    os.environ.pop("BUILDER_REMOTE", None)
    yield
    remote.get_tunnel().stop()


def admin_client():
    store = get_store()
    store.create_user("jamie", "hammer-time", "Jamie", is_admin=True)
    c = TestClient(create_app())
    c.post("/auth/login", json={"username": "jamie", "password": "hammer-time"})
    c.headers["X-Builder"] = "1"
    return c


def test_claude_key_checked_saved_and_hidden(monkeypatch):
    c = admin_client()
    assert c.get("/settings").json()["claude_key"] == {"set": False, "hint": ""}
    assert c.put("/settings/claude-key", json={"key": "not-a-key"}).status_code == 422

    checked = []

    class FakeModels:
        def list(self, limit):
            checked.append(limit)

    class FakeClient:
        def __init__(self, api_key, **kw):
            self.models = FakeModels()

    monkeypatch.setattr(api.anthropic, "Anthropic", FakeClient)
    key = "sk-ant-api03-" + "x" * 40 + "WXYZ"
    r = c.put("/settings/claude-key", json={"key": key})
    assert r.status_code == 200 and checked == [1]
    assert r.json()["hint"] == "sk-ant-…WXYZ"
    assert os.environ["ANTHROPIC_API_KEY"] == key
    assert settings_file.read_env_file(settings_file.env_path())["ANTHROPIC_API_KEY"] == key
    body = c.get("/settings").text
    assert key not in body and "WXYZ" in body  # never sent back in full

    assert c.delete("/settings/claude-key").status_code == 204
    assert "ANTHROPIC_API_KEY" not in os.environ
    assert settings_file.read_env_file(settings_file.env_path()).get("ANTHROPIC_API_KEY", "") == ""


def test_settings_are_for_admins_only():
    store = get_store()
    store.create_user("jamie", "hammer-time", is_admin=True)
    store.create_user("sam", "saw-dust-99")
    c = TestClient(create_app())
    c.post("/auth/login", json={"username": "sam", "password": "saw-dust-99"})
    c.headers["X-Builder"] = "1"
    assert c.get("/settings").status_code == 403
    assert c.put("/settings/remote", json={"mode": "quick"}).status_code == 403
    assert c.get("/connect/info").status_code == 200


def test_remote_quick_tunnel(tmp_path, monkeypatch):
    fake = tmp_path / "cloudflared"
    fake.write_text(textwrap.dedent(f"""\
        #!{sys.executable}
        import sys, time
        print("INF Requesting new quick Tunnel on trycloudflare.com...", flush=True)
        print("INF |  https://brave-hammer-nail.trycloudflare.com  |", flush=True)
        time.sleep(60)
        """))
    fake.chmod(0o755)
    monkeypatch.setattr(remote, "cloudflared_bin", lambda: str(fake))
    c = admin_client()
    r = c.put("/settings/remote", json={"mode": "quick"})
    assert r.status_code == 200, r.text
    assert r.json()["url"] == "https://brave-hammer-nail.trycloudflare.com"
    assert c.get("/connect/info").json()["public_url"] == "https://brave-hammer-nail.trycloudflare.com"
    assert settings_file.read_env_file(settings_file.env_path())["BUILDER_REMOTE"] == "quick"
    assert c.put("/settings/remote", json={"mode": "named", "public_url": "nope"}).status_code == 422
    r = c.put("/settings/remote", json={"mode": "off"})
    assert r.json()["running"] is False


def test_remote_needs_accounts(monkeypatch):
    monkeypatch.setenv("MEDIA_BACKEND_AUTH", "off")
    from media_backend import config

    config.reset_settings()
    c = TestClient(create_app())
    assert c.put("/settings/remote", json={"mode": "quick"}, headers={"X-Builder": "1"}).status_code == 400


def test_connector_link_runs_mcp_as_the_tokens_person(live_server):  # noqa: F811
    base, token = live_server
    assert status(f"{base}/c/mbp_wrong/mcp/") == 401
    assert status(f"{base}/c/{token}/files", "GET") == 401  # a connector link only opens /mcp

    async def go():
        async with streamable_http_client(f"{base}/c/{token}/mcp/") as (read, write, *_):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = await session.list_tools()
                return [t.name for t in tools.tools]

    names = asyncio.run(go())
    assert "trim" in names and "craft_build_start" in names

    # Revoking the token closes the link.
    store = get_store()
    user = store.list_users()[0]
    for t in store.list_api_tokens(user.id):
        store.delete_api_token(user.id, t["id"] if isinstance(t, dict) else t.id)
    assert status(f"{base}/c/{token}/mcp/") == 401


def test_desktop_show_hook_is_local_only():
    c = TestClient(create_app(), client=("127.0.0.1", 50000))
    assert c.post("/_desktop/show").status_code == 404  # no desktop window registered
    shown = []
    api.DESKTOP["show"] = lambda: shown.append(1)
    try:
        assert c.post("/_desktop/show").status_code == 204 and shown == [1]
        assert c.post("/_desktop/show", headers={"cf-connecting-ip": "203.0.113.9"}).status_code == 404
        far = TestClient(create_app(), client=("203.0.113.9", 50000))
        assert far.post("/_desktop/show").status_code == 404
    finally:
        api.DESKTOP.pop("show", None)
    assert shown == [1]
