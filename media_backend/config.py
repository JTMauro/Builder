"""Settings, all read from environment variables so other projects can reuse the backend as-is."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _env(name: str, default: str) -> str:
    value = os.environ.get(name)
    return value if value not in (None, "") else default


@dataclass(frozen=True)
class Settings:
    data_dir: Path = field(default_factory=lambda: Path(_env("MEDIA_BACKEND_DATA", "./media-data")).resolve())
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
    max_upload_mb: int = field(default_factory=lambda: int(_env("MEDIA_BACKEND_MAX_UPLOAD_MB", "2048")))
    workers: int = field(default_factory=lambda: int(_env("MEDIA_BACKEND_WORKERS", "2")))
    ffmpeg: str = field(default_factory=lambda: _env("FFMPEG_BIN", "ffmpeg"))
    ffprobe: str = field(default_factory=lambda: _env("FFPROBE_BIN", "ffprobe"))
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
