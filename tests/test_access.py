"""Using Builder from other apps and other Claude Code chats."""

import socket
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
import uvicorn
from fastapi.testclient import TestClient

from media_backend import config, craft
from media_backend.api import create_app
from media_backend.client import Builder, BuilderError
from media_backend.mcp_server import caller
from media_backend.users import get_store

FAKE = str(Path(__file__).parent / "fixtures" / "fake_engine.py")


@pytest.fixture
def live_server(monkeypatch):
    monkeypatch.setenv("CRAFT_ENGINE_BIN", FAKE)
    craft._runs = None
    craft._ai_ready = False
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(create_app(), host="127.0.0.1", port=port, log_level="error"))
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    store = get_store()
    user = store.create_user("jamie", "hammer-time", "Jamie", is_admin=True)
    _, token = store.new_api_token(user.id, "test app")
    yield f"http://127.0.0.1:{port}", token
    server.should_exit = True
    t.join(timeout=5)
    craft._runs = None


def test_client_media_and_builds(live_server, media, tmp_path):
    url, token = live_server
    with pytest.raises(BuilderError) as e:
        Builder(url).files()  # no token
    assert e.value.status == 401
    b = Builder(url, token)
    assert b.health()["ok"]
    clip = b.upload(media["dir"] / "clip.mp4")
    short = b.run("trim", file_id=clip["id"], start=0, duration=1)["outputs"][0]
    assert b.download(short["id"], tmp_path / "short.mp4").stat().st_size > 0
    with pytest.raises(BuilderError):
        b.run("trim", file_id="000000000000", start=0)
    run = b.build("A tiny page", target="https://example.com")
    seen = []
    for ev in b.follow(run["id"], poll=2):
        seen.append(ev["type"])
        if ev["type"] == "ask":
            b.answer(run["id"], allow=True)
    assert {"project", "ask", "check", "finished"} <= set(seen)
    assert b.build_status(run["id"])["status"] == "done"
    assert b.projects()[0]["name"] == "Made by test"


def test_cors_and_public_docs(monkeypatch):
    monkeypatch.setenv("MEDIA_BACKEND_CORS_ORIGINS", "https://myapp.example")
    config.reset_settings()
    with TestClient(create_app()) as c:
        assert c.get("/openapi.json").json()["info"]["title"] == "Builder API"
        assert c.get("/docs").status_code == 200
        pre = c.options("/files", headers={"Origin": "https://myapp.example", "Access-Control-Request-Method": "GET",
                                           "Access-Control-Request-Headers": "authorization"})
        assert pre.status_code == 200 and pre.headers["access-control-allow-origin"] == "https://myapp.example"
        assert "access-control-allow-credentials" not in pre.headers  # tokens only, never the cookie
        r = c.get("/files", headers={"Origin": "https://myapp.example"})
        assert r.status_code == 401 and r.headers["access-control-allow-origin"] == "https://myapp.example"
        other = c.options("/files", headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "GET"})
        assert "access-control-allow-origin" not in other.headers


def test_no_cors_by_default():
    with TestClient(create_app()) as c:
        r = c.options("/files", headers={"Origin": "https://myapp.example", "Access-Control-Request-Method": "GET"})
        assert "access-control-allow-origin" not in r.headers


def test_connector_identity(monkeypatch):
    store = get_store()
    user = store.create_user("jamie", "hammer-time")
    _, token = store.new_api_token(user.id, "chat")
    ctx = lambda headers: SimpleNamespace(headers=headers)  # noqa: E731
    assert caller(ctx({"Authorization": f"Bearer {token}"})) == "jamie"  # over HTTP: the token's person
    assert caller(ctx({"authorization": "Bearer nonsense"})) == "service"
    monkeypatch.delenv("BUILDER_USER", raising=False)
    assert caller(None) == "claude-code"  # stdio default
    monkeypatch.setenv("BUILDER_USER", "jamie")
    assert caller(None) == "jamie"  # stdio on your own computer, as you

    class NoRequest:
        @property
        def headers(self):
            raise ValueError("no request")

    assert caller(NoRequest()) == "jamie"


def test_cli_setup_doctor_token_user(monkeypatch, capsys, tmp_path):
    from media_backend import cli

    fake_bin = tmp_path / "engine-bin"
    fake_bin.write_text(Path(FAKE).read_text())
    fake_bin.chmod(0o755)
    monkeypatch.delenv("CRAFT_ENGINE_BIN", raising=False)
    with pytest.raises(SystemExit) as e:
        cli.main(["setup", "--engine-bin", str(fake_bin)])
    assert e.value.code == 0
    assert (config.get_settings().bin_dir / "craft-anything-cli").exists()
    assert craft.engine_bin() == config.get_settings().bin_dir / "craft-anything-cli"
    with pytest.raises(SystemExit) as e:
        cli.main(["doctor"])
    assert "0.0.0-test" in capsys.readouterr().out
    monkeypatch.setattr(cli.getpass, "getpass", lambda _p: "hammer-time")
    with pytest.raises(SystemExit) as e:
        cli.main(["user", "add", "jamie"])
    assert e.value.code == 0 and get_store().list_users()[0].is_admin  # first account is the admin
    capsys.readouterr()
    with pytest.raises(SystemExit) as e:
        cli.main(["token", "--user", "JAMIE", "--name", "my app"])
    token = capsys.readouterr().out.strip()
    assert e.value.code == 0 and token.startswith("mbp_") and get_store().user_for_api_token(token).username == "jamie"
    with pytest.raises(SystemExit) as e:
        cli.main(["token", "--user", "nobody"])
    assert e.value.code == 1
