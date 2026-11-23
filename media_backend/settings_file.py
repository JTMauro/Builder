"""builder.env: Builder's settings file (in its data folder, %APPDATA%\\Builder on Windows).

One KEY=value per line, read into the environment when Builder starts. The web app's Settings screen
writes it too (the Claude API key, remote access), so people never have to edit it by hand.
"""

from __future__ import annotations

import os
from pathlib import Path

from .config import IS_WINDOWS, _default_data_dir

ENV_TEMPLATE = """\
# Builder settings: one KEY=value per line. Restart Builder (tray icon > Restart) after changing them.
# Lines starting with # are ignored.

# Claude API key for Rivet and Craft builds (https://console.anthropic.com/).
# ANTHROPIC_API_KEY=

# Port Builder listens on.
BUILDER_PORT=8000

# 127.0.0.1 = only this computer. 0.0.0.0 = other devices on your network too (they still sign in).
BUILDER_HOST=127.0.0.1

# Folders Claude Code may import media from and export results to (separate several with ;).
# Empty means your user folder.
MEDIA_BACKEND_ALLOWED_DIRS=

# Web apps on other sites that may call Builder from a browser, comma-separated (or *).
# MEDIA_BACKEND_CORS_ORIGINS=

# Let anyone who can reach Builder make an account from the sign-in screen.
# MEDIA_BACKEND_OPEN_SIGNUP=off

# Unreal Engine (renders with Movie Render Queue). Builder looks in the usual places if these are empty.
# UNREAL_EDITOR=C:\\Program Files\\Epic Games\\UE_5.6
# UNREAL_PROJECT=C:\\Users\\YOU\\Documents\\Unreal Projects\\Film\\Film.uproject
# Let connected Claude chats run Python inside Unreal on this computer (off by default).
# UNREAL_ALLOW_SCRIPTS=off
"""

def data_dir() -> Path:
    return Path(os.environ.get("MEDIA_BACKEND_DATA") or _default_data_dir()).expanduser()


def env_path() -> Path:
    return data_dir() / "builder.env"


def read_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError:
        return values
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key:
            values[key] = value
    return values


def set_env_value(path: Path, key: str, value: str) -> None:
    """Change one setting in builder.env, keeping the rest of the file (and its comments) as it is."""
    lines = path.read_text(encoding="utf-8-sig").splitlines() if path.exists() else []
    out, done = [], False
    for line in lines:
        bare = line.strip().lstrip("#").strip()
        if bare.split("=", 1)[0].strip() == key and "=" in bare and not done:
            out.append(f"{key}={value}")
            done = True
        else:
            out.append(line)
    if not done:
        out.append(f"{key}={value}")
    path.write_text("\n".join(out) + "\n", encoding="utf-8")


def load_settings() -> dict[str, str]:
    """Create builder.env on first run, then copy its values into the environment.
    Values already set in the real environment win, so a script can still override them."""
    path = env_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text(ENV_TEMPLATE, encoding="utf-8")
    values = read_env_file(path)
    for k, v in values.items():
        if v != "" and not os.environ.get(k):
            os.environ[k] = v
    # A tray app's working folder means nothing to the person, so default to their user folder.
    if not os.environ.get("MEDIA_BACKEND_ALLOWED_DIRS"):
        os.environ["MEDIA_BACKEND_ALLOWED_DIRS"] = str(Path.home())
    return values


def save_secret(key: str, value: str) -> None:
    """Write a setting that is a secret (API key, tunnel token): to builder.env, readable only by this
    account, and into the running server's environment."""
    path = env_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text(ENV_TEMPLATE, encoding="utf-8")
    set_env_value(path, key, value)
    if not IS_WINDOWS:  # %APPDATA% is already private to the account on Windows
        os.chmod(path, 0o600)
    if value:
        os.environ[key] = value
    else:
        os.environ.pop(key, None)
