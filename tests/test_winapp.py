"""The desktop server (Builder.exe on Windows): settings file, platform helpers, server start/stop."""
import json
import urllib.request

import os

import pytest

from media_backend import config, winapp


@pytest.fixture(autouse=True)
def _own_environment(monkeypatch):
    # load_settings() copies builder.env into the environment: keep that out of the other tests.
    monkeypatch.setattr(os, "environ", os.environ.copy())


def test_settings_file_created_and_loaded(tmp_path, monkeypatch):
    monkeypatch.setenv("MEDIA_BACKEND_DATA", str(tmp_path))
    monkeypatch.delenv("MEDIA_BACKEND_ALLOWED_DIRS", raising=False)
    monkeypatch.delenv("BUILDER_PORT", raising=False)
    values = winapp.load_settings()
    assert (tmp_path / "builder.env").exists()
    assert values["BUILDER_PORT"] == "8000"
    assert "ANTHROPIC_API_KEY" not in values  # commented out in the template
    assert os.environ["MEDIA_BACKEND_ALLOWED_DIRS"]  # defaults to the user folder


def test_real_environment_wins(tmp_path, monkeypatch):
    monkeypatch.setenv("MEDIA_BACKEND_DATA", str(tmp_path))
    (tmp_path / "builder.env").write_text('BUILDER_PORT=9001\nASSISTANT_NAME="Rivet Two"\n')
    monkeypatch.setenv("BUILDER_PORT", "9100")
    monkeypatch.delenv("ASSISTANT_NAME", raising=False)
    winapp.load_settings()
    assert os.environ["BUILDER_PORT"] == "9100"
    assert os.environ["ASSISTANT_NAME"] == "Rivet Two"


def test_set_env_value_keeps_comments(tmp_path):
    p = tmp_path / "builder.env"
    p.write_text("# keep me\n# ANTHROPIC_API_KEY=\nBUILDER_HOST=127.0.0.1\n")
    winapp.set_env_value(p, "BUILDER_HOST", "0.0.0.0")
    winapp.set_env_value(p, "ANTHROPIC_API_KEY", "sk-test")
    winapp.set_env_value(p, "NEW_ONE", "x")
    text = p.read_text()
    assert "# keep me" in text
    assert winapp.read_env_file(p) == {"ANTHROPIC_API_KEY": "sk-test", "BUILDER_HOST": "0.0.0.0", "NEW_ONE": "x"}


def test_platform_helpers(monkeypatch):
    monkeypatch.setattr(config, "IS_WINDOWS", True)
    assert config.exe_name("ffmpeg") == "ffmpeg.exe"
    assert config.exe_name("ffmpeg.exe") == "ffmpeg.exe"
    monkeypatch.setattr(config, "IS_WINDOWS", False)
    assert config.exe_name("ffmpeg") == "ffmpeg"
    cmd = config.builder_command("mcp")
    assert cmd[-3:] == ["-m", "media_backend.cli", "mcp"]
    monkeypatch.setattr(config, "FROZEN", True)
    assert config.builder_command("mcp")[0].endswith("builder-cli") and config.builder_command("mcp")[1:] == ["mcp"]


def test_server_starts_and_is_recognised(tmp_path, monkeypatch):
    monkeypatch.setenv("MEDIA_BACKEND_DATA", str(tmp_path))
    config.reset_settings()
    port = winapp._free_port()
    assert not winapp.builder_at(port)
    server = winapp.Server("127.0.0.1", port)
    assert server.start()
    try:
        assert winapp.builder_at(port)
        assert not winapp.port_free("127.0.0.1", port)
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health") as r:
            assert json.loads(r.read())["app"] == "builder"
    finally:
        server.stop()
    config.reset_settings()
