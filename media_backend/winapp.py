"""Builder as a desktop server: what Builder.exe runs on Windows (also `builder app` anywhere).

It runs the web app, the HTTP API and the MCP connector on this computer (http://127.0.0.1:8000 by
default) and puts a Builder icon in the system tray: open Builder, connect Claude Code, settings,
start with Windows, quit. Other apps send files to the API; Claude Code chats on this computer use
the connector at /mcp/.

Settings live in builder.env in Builder's data folder (%APPDATA%\\Builder on Windows), one KEY=value
per line, read before the server starts. The same folder holds accounts, files, chats and projects,
so reinstalling or upgrading never loses them.

    Builder.exe                   start the server with a tray icon (opens the browser the first time)
    Builder.exe --headless        server only, no tray icon (for a scheduled task or a service wrapper)
    Builder.exe --no-browser      don't open the browser (what "Start with Windows" uses)
    builder-cli.exe app --selftest   check the install without touching your data (prints SELFTEST PASSED)
"""

from __future__ import annotations

import argparse
import json
import logging
import logging.handlers
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

from . import __version__
from .config import FROZEN, IS_WINDOWS, NO_WINDOW, _default_data_dir, app_dir, exe_name

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_NAME = "Builder"

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
"""

log = logging.getLogger("builder.app")


# ---------------------------------------------------------------- settings file


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


def setup_logging() -> Path:
    logs = data_dir() / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    path = logs / "builder.log"
    handler = logging.handlers.RotatingFileHandler(path, maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    # A windowed .exe has no console: anything printed would otherwise crash or vanish.
    if sys.stdout is None or sys.stderr is None:
        stream = open(logs / "console.log", "a", encoding="utf-8", buffering=1)  # noqa: SIM115
        sys.stdout = sys.stdout or stream
        sys.stderr = sys.stderr or stream
    return path


# ---------------------------------------------------------------- server


def builder_at(port: int, timeout: float = 1.0) -> bool:
    """Is Builder already answering on this port?"""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=timeout) as r:
            return json.loads(r.read().decode()).get("app") == "builder"
    except Exception:  # noqa: BLE001
        return False


def port_free(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("" if host == "0.0.0.0" else host, port))
            return True
        except OSError:
            return False


class Server:
    """uvicorn in a background thread, so the tray icon can own the main thread."""

    def __init__(self, host: str, port: int):
        import uvicorn

        from .api import create_app

        self.host, self.port = host, port
        config = uvicorn.Config(create_app(), host=host, port=port, log_config=None, access_log=False)
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self._run, name="builder-server", daemon=True)
        self.error: BaseException | None = None

    def _run(self) -> None:
        try:
            self.server.run()
        except BaseException as e:  # noqa: BLE001  (SystemExit from uvicorn on a bind error included)
            self.error = e
            log.exception("server stopped")

    def start(self, wait: float = 30.0) -> bool:
        self.thread.start()
        end = time.time() + wait
        while time.time() < end:
            if self.server.started:
                return True
            if not self.thread.is_alive():
                return False
            time.sleep(0.05)
        return False

    def stop(self) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=15)

    @property
    def url(self) -> str:
        shown = "127.0.0.1" if self.host in ("0.0.0.0", "::") else self.host
        return f"http://{shown}:{self.port}/"


# ---------------------------------------------------------------- Start with Windows


def autostart_command() -> str:
    exe = app_dir() / "Builder.exe" if FROZEN else Path(sys.executable)
    return f'"{exe}" --no-browser' if FROZEN else f'"{exe}" -m media_backend.cli app --no-browser'


def autostart_enabled() -> bool:
    if not IS_WINDOWS:
        return False
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            winreg.QueryValueEx(k, RUN_NAME)
            return True
    except OSError:
        return False


def set_autostart(on: bool) -> None:
    if not IS_WINDOWS:
        return
    import winreg

    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
        if on:
            winreg.SetValueEx(k, RUN_NAME, 0, winreg.REG_SZ, autostart_command())
        else:
            try:
                winreg.DeleteValue(k, RUN_NAME)
            except OSError:
                pass


# ---------------------------------------------------------------- tray


def open_path(path: Path) -> None:
    if IS_WINDOWS:
        os.startfile(str(path))  # noqa: S606  (opens the folder or file in its usual app)
    else:
        subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", str(path)])


def copy_text(text: str) -> None:
    if IS_WINDOWS:
        subprocess.run(["clip"], input=text.encode("utf-16-le"), check=False, **{"creationflags": 0x08000000})


def icon_image():
    from PIL import Image, ImageDraw

    path = Path(__file__).with_name("static") / "icon.png"
    if path.exists():
        return Image.open(path)
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((2, 2, 62, 62), 14, fill=(240, 120, 40, 255))
    d.text((22, 18), "B", fill="white")
    return img


def relaunch(argv: list[str]) -> None:
    cmd = [sys.executable, *argv] if FROZEN else [sys.executable, "-m", "media_backend.cli", "app", *argv]
    subprocess.Popen(cmd, close_fds=True, **({"creationflags": 0x00000008} if IS_WINDOWS else {}))  # DETACHED


def run_tray(server: Server, argv: list[str]) -> None:
    import pystray

    restart = {"on": False}

    def open_builder(icon=None, item=None):
        webbrowser.open(server.url)

    def connect(icon, item):
        webbrowser.open(server.url + "#connect")
        icon.notify("Sign in, then choose Connect Claude Code to make a token and copy the command.", "Builder")

    def copy_address(icon, item):
        copy_text(server.url.rstrip("/"))
        icon.notify(f"Copied {server.url.rstrip('/')}", "Builder")

    def edit_settings(icon, item):
        open_path(env_path())
        icon.notify("Change the settings, save, then choose Restart.", "Builder")

    def toggle_autostart(icon, item):
        set_autostart(not autostart_enabled())

    def network_on(item):
        return os.environ.get("BUILDER_HOST", "127.0.0.1") == "0.0.0.0"

    def toggle_network(icon, item):
        set_env_value(env_path(), "BUILDER_HOST", "127.0.0.1" if network_on(item) else "0.0.0.0")
        do_restart(icon, item)

    def do_restart(icon, item):
        restart["on"] = True
        icon.stop()

    def quit_(icon, item):
        icon.stop()

    menu = pystray.Menu(
        pystray.MenuItem("Open Builder", open_builder, default=True),
        pystray.MenuItem("Connect Claude Code…", connect),
        pystray.MenuItem("Copy address", copy_address),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Settings (builder.env)", edit_settings),
        pystray.MenuItem("Open data folder", lambda i, it: open_path(data_dir())),
        pystray.MenuItem("Open log", lambda i, it: open_path(data_dir() / "logs" / "builder.log")),
        pystray.MenuItem("Start with Windows", toggle_autostart, checked=lambda it: autostart_enabled(),
                         visible=IS_WINDOWS),
        pystray.MenuItem("Let other devices connect", toggle_network, checked=network_on),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Restart", do_restart),
        pystray.MenuItem("Quit Builder", quit_),
    )
    icon = pystray.Icon("Builder", icon_image(), f"Builder {__version__} · {server.url}", menu)
    icon.run()
    server.stop()
    if restart["on"]:
        relaunch([a for a in argv if a != "--first-run"] + ["--no-browser"])


# ---------------------------------------------------------------- self-test


def selftest() -> int:
    """Check the install end to end in a throwaway data folder. Prints OK lines, then SELFTEST PASSED."""
    failures = 0

    def check(ok: bool, what: str, detail: str = "") -> None:
        nonlocal failures
        print(f"{'OK  ' if ok else 'FAIL'} {what}{(': ' + detail) if detail else ''}", flush=True)
        failures += 0 if ok else 1

    tmp = tempfile.mkdtemp(prefix="builder-selftest-")
    os.environ.update({"MEDIA_BACKEND_DATA": tmp, "MEDIA_BACKEND_AUTH": "off", "MEDIA_BACKEND_ALLOWED_DIRS": tmp})
    from . import config

    config.reset_settings()
    s = config.get_settings()
    check(True, f"Builder {__version__}", "packaged" if FROZEN else "from source")

    for tool in (s.ffmpeg, s.ffprobe):
        try:
            out = subprocess.run([tool, "-version"], capture_output=True, text=True, timeout=30, **NO_WINDOW)
            check(out.returncode == 0, Path(tool).name, out.stdout.splitlines()[0] if out.stdout else out.stderr[:200])
        except OSError as e:
            check(False, Path(tool).name, str(e))

    from . import craft

    try:
        exe = craft.engine_bin()
        check(True, "Craft engine", craft.engine_version() or str(exe))
    except craft.EngineMissing:
        check(False, "Craft engine", f"{exe_name('craft-anything-cli')} not found")

    from . import ops

    check(len(ops.REGISTRY) > 0, "audio/video operations", str(len(ops.REGISTRY)))

    port = _free_port()
    server = Server("127.0.0.1", port)
    check(server.start(), "web server starts", server.url)
    try:
        check(builder_at(port, 5), "/health answers")
        # A real round trip: make a one-second tone with ffmpeg, upload it, and probe it through the API.
        tone = Path(tmp) / "tone.wav"
        subprocess.run([s.ffmpeg, "-v", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=1", str(tone)],
                       check=False, timeout=60, **NO_WINDOW)
        check(tone.exists(), "ffmpeg makes a test tone")
        if tone.exists():
            fid = _upload(port, tone)
            check(bool(fid), "upload a file through the API")
            if fid:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/files/{fid}/probe", timeout=30) as r:
                    probe = json.loads(r.read().decode())
                dur = float(probe.get("duration_s") or 0)
                check(0.9 < dur < 1.2, "probe the upload", f"{dur:.2f} s")
        req = urllib.request.Request(f"http://127.0.0.1:{port}/mcp/", method="POST", data=b"{}",
                                     headers={"Content-Type": "application/json", "Accept": "application/json, text/event-stream"})
        try:
            urllib.request.urlopen(req, timeout=10)
            check(True, "MCP connector at /mcp/")
        except urllib.error.HTTPError as e:  # a bare {} is not a valid MCP message: any MCP-level reply proves it's mounted
            check(e.code in (400, 406, 422), "MCP connector at /mcp/", f"HTTP {e.code}")
    finally:
        server.stop()
    if IS_WINDOWS:
        check(bool(icon_image()), "tray icon")
    print("SELFTEST PASSED" if failures == 0 else f"SELFTEST FAILED ({failures})", flush=True)
    return 0 if failures == 0 else 1


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _upload(port: int, path: Path) -> str:
    boundary = "----builder-selftest"
    body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{path.name}\"\r\n"
            f"Content-Type: audio/wav\r\n\r\n").encode() + path.read_bytes() + f"\r\n--{boundary}--\r\n".encode()
    req = urllib.request.Request(f"http://127.0.0.1:{port}/files", data=body, method="POST",
                                 headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read().decode()).get("id", "")
    except Exception as e:  # noqa: BLE001
        print(f"     upload error: {e}", flush=True)
        return ""


# ---------------------------------------------------------------- entry point


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    p = argparse.ArgumentParser(prog="Builder", description="Builder desktop server")
    p.add_argument("--host", help="127.0.0.1 (this computer) or 0.0.0.0 (your network too)")
    p.add_argument("--port", type=int)
    p.add_argument("--headless", action="store_true", help="no tray icon")
    p.add_argument("--no-browser", action="store_true", help="don't open the browser")
    p.add_argument("--selftest", action="store_true", help="check the install and exit")
    p.add_argument("--version", action="version", version=f"Builder {__version__}")
    args = p.parse_args(argv)

    if args.selftest:
        return selftest()

    first_run = not env_path().exists()
    load_settings()
    from .config import reset_settings

    reset_settings()
    log_path = setup_logging()
    host = args.host or os.environ.get("BUILDER_HOST") or "127.0.0.1"
    port = args.port or int(os.environ.get("BUILDER_PORT") or 8000)

    if builder_at(port):
        # Already running: just bring it up.
        if not args.no_browser:
            webbrowser.open(f"http://127.0.0.1:{port}/")
        return 0
    if not port_free(host, port):
        msg = f"Port {port} is used by another program. Change BUILDER_PORT in {env_path()} and start Builder again."
        log.error(msg)
        _alert(msg)
        return 1

    server = Server(host, port)
    if not server.start():
        msg = f"Builder couldn't start. Details are in {log_path}."
        log.error(msg)
        _alert(msg)
        return 1
    log.info("Builder %s is open at %s (data: %s)", __version__, server.url, data_dir())
    print(f"Builder is open at {server.url}", flush=True)
    if not args.no_browser or first_run:
        webbrowser.open(server.url)

    if args.headless:
        try:
            while server.thread.is_alive():
                server.thread.join(1)
        except KeyboardInterrupt:
            server.stop()
        return 0
    try:
        import pystray  # noqa: F401
    except Exception:  # noqa: BLE001  (no tray available: run like --headless)
        log.warning("no system tray available; running without an icon")
        try:
            while server.thread.is_alive():
                server.thread.join(1)
        except KeyboardInterrupt:
            server.stop()
        return 0
    run_tray(server, argv)
    return 0


def _alert(msg: str) -> None:
    if IS_WINDOWS:
        try:
            import ctypes

            ctypes.windll.user32.MessageBoxW(None, msg, "Builder", 0x10)
            return
        except Exception:  # noqa: BLE001
            pass
    print(msg, file=sys.stderr)


if __name__ == "__main__":
    sys.exit(main())
