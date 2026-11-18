"""Settings, all read from environment variables so other projects can reuse the backend as-is."""

from __future__ import annotations

import os
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path

IS_WINDOWS = os.name == "nt"
# True inside the packaged Windows app (PyInstaller sets sys.frozen).
FROZEN = bool(getattr(sys, "frozen", False))


def _env(name: str, default: str) -> str:
    value = os.environ.get(name)
    return value if value not in (None, "") else default


# Extra subprocess options: on Windows, console programs (ffmpeg, the engine) run without flashing a
# console window, and their output is read as UTF-8 rather than the system code page.
NO_WINDOW: dict = {"creationflags": 0x08000000, "encoding": "utf-8", "errors": "replace"} if IS_WINDOWS else {}


def exe_name(name: str) -> str:
    """A program's file name on this computer (adds .exe on Windows)."""
    return f"{name}.exe" if IS_WINDOWS and not name.lower().endswith(".exe") else name


def app_dir() -> Path:
    """The folder the packaged app is installed in (the one holding Builder.exe)."""
    return Path(sys.executable).resolve().parent


def _default_data_dir() -> str:
    # Windows keeps user data in %APPDATA%\Builder (never in the install folder, so upgrades keep it).
    if IS_WINDOWS and os.environ.get("APPDATA"):
        return str(Path(os.environ["APPDATA"]) / "Builder")
    return str(Path.home() / ".builder")


def _default_tool(name: str) -> str:
    """ffmpeg/ffprobe: the copy shipped with the Windows app first, then Builder's bin/, then PATH."""
    places = []
    if FROZEN:
        places += [app_dir() / "ffmpeg" / exe_name(name), app_dir() / exe_name(name)]
    places.append(Path(_env("MEDIA_BACKEND_DATA", _default_data_dir())).expanduser() / "bin" / exe_name(name))
    for p in places:
        if p.is_file():
            return str(p)
    return shutil.which(name) or name


def builder_command(*args: str) -> list[str]:
    """How to start Builder's own command line (used to plug Builder's tools into the engine)."""
    if FROZEN:
        return [str(app_dir() / exe_name("builder-cli")), *args]
    return [sys.executable, "-m", "media_backend.cli", *args]


@dataclass(frozen=True)
class Settings:
    # One home per computer, shared by the web app, other apps and every Claude Code chat:
    # files, accounts, chats, Craft projects and the engine program (bin/).
    data_dir: Path = field(default_factory=lambda: Path(_env("MEDIA_BACKEND_DATA", _default_data_dir())).expanduser().resolve())
    # Optional bearer token. When set, every HTTP route (including /mcp) requires it.
    api_token: str = field(default_factory=lambda: _env("MEDIA_BACKEND_TOKEN", ""))
    # Directories the MCP `import_path` / `export_file` tools may read from or write to.
    # Defaults to the current working directory of whoever launched the server.
    allowed_dirs: tuple[Path, ...] = field(
        default_factory=lambda: tuple(
            Path(p).expanduser().resolve()
            for p in _env("MEDIA_BACKEND_ALLOWED_DIRS", os.getcwd()).split(os.pathsep)
            if p
        )
    )
    # Web/API login. "off" leaves every route open (only for a machine nobody else can reach).
    auth_enabled: bool = field(default_factory=lambda: _env("MEDIA_BACKEND_AUTH", "on").lower() not in ("off", "0", "false", "no"))
    # Let anyone create an account from the sign-in screen (otherwise an admin adds people).
    open_signup: bool = field(default_factory=lambda: _env("MEDIA_BACKEND_OPEN_SIGNUP", "off").lower() in ("on", "1", "true", "yes"))
    # Web apps on other sites that may call the API from a browser (comma-separated origins, or "*").
    # They authenticate with a personal token; the sign-in cookie is never sent cross-site.
    cors_origins: tuple[str, ...] = field(
        default_factory=lambda: tuple(o.strip() for o in _env("MEDIA_BACKEND_CORS_ORIGINS", "").split(",") if o.strip())
    )
    max_upload_mb: int = field(default_factory=lambda: int(_env("MEDIA_BACKEND_MAX_UPLOAD_MB", "2048")))
    workers: int = field(default_factory=lambda: int(_env("MEDIA_BACKEND_WORKERS", "2")))
    ffmpeg: str = field(default_factory=lambda: _env("FFMPEG_BIN", "") or _default_tool("ffmpeg"))
    ffprobe: str = field(default_factory=lambda: _env("FFPROBE_BIN", "") or _default_tool("ffprobe"))
    # Built-in assistant
    assistant_name: str = field(default_factory=lambda: _env("ASSISTANT_NAME", "Rivet"))
    assistant_persona: str = field(
        default_factory=lambda: _env(
            "ASSISTANT_PERSONA",
            "a friendly, practical builder who loves making things and explains what they're doing in plain words",
        )
    )
    model: str = field(default_factory=lambda: _env("ASSISTANT_MODEL", "claude-opus-5-5"))
    effort: str = field(default_factory=lambda: _env("ASSISTANT_EFFORT", "medium"))
    max_tool_turns: int = field(default_factory=lambda: int(_env("ASSISTANT_MAX_TOOL_TURNS", "25")))

    @property
    def files_dir(self) -> Path:
        return self.data_dir / "files"

    @property
    def bin_dir(self) -> Path:
        return self.data_dir / "bin"


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings


def reset_settings() -> None:
    """Re-read the environment (used by tests)."""
    global _settings
    _settings = None
