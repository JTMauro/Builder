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
from .config import FROZEN, IS_WINDOWS, NO_WINDOW, app_dir, exe_name
from .settings_file import data_dir, env_path, load_settings, set_env_value

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_NAME = "Builder"

log = logging.getLogger("builder.app")


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
        if not IS_WINDOWS:
            # Like uvicorn: connections from a just-stopped Builder linger in TIME_WAIT and must not count as
            # "in use". (On Windows this option would let two programs share a port, so it's left off there.)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
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


def window_available() -> bool:
    """Builder's own window needs pywebview (Edge WebView2 on Windows, built into Windows 10 and 11)."""
    if os.environ.get("BUILDER_WINDOW", "on").lower() in ("off", "0", "no", "browser"):
        return False
    try:
        import webview  # noqa: F401

        return IS_WINDOWS or sys.platform == "darwin"
    except Exception:  # noqa: BLE001
        return False


def run_desktop(server: Server, argv: list[str], show: bool) -> None:
    """The tray icon, plus Builder's own window when available (otherwise pages open in the browser).
    Closing the window keeps Builder running in the tray; Quit in the tray menu stops it."""
    import pystray

    state = {"restart": False, "quitting": False}
    window = None
    if window_available():
        import webview

        window = webview.create_window("Builder", server.url, width=1320, height=880, min_size=(960, 640),
                                       hidden=not show, text_select=True)

        def on_closing():
            if state["quitting"]:
                return True
            window.hide()  # keep serving apps and Claude in the background
            return False

        window.events.closing += on_closing
    elif show:
        webbrowser.open(server.url)

    def open_page(fragment: str = "") -> None:
        if window is None:
            webbrowser.open(server.url + fragment)
            return
        if fragment:  # a query string makes the page reload, so it acts on the fragment
            window.load_url(f"{server.url}?open={fragment.strip('#')}{fragment}")
        window.show()
        try:
            window.restore()
        except Exception:  # noqa: BLE001
            pass

    from . import api

    api.DESKTOP["show"] = lambda: open_page()  # a second start of Builder.exe brings this window forward

    def copy_address(icon, item):
        copy_text(server.url.rstrip("/"))
        icon.notify(f"Copied {server.url.rstrip('/')}", "Builder")

    def edit_settings_file(icon, item):
        open_path(env_path())
        icon.notify("Change the settings, save, then choose Restart.", "Builder")

    def toggle_autostart(icon, item):
        set_autostart(not autostart_enabled())

    def network_on(item):
        return os.environ.get("BUILDER_HOST", "127.0.0.1") == "0.0.0.0"

    def toggle_network(icon, item):
        set_env_value(env_path(), "BUILDER_HOST", "127.0.0.1" if network_on(item) else "0.0.0.0")
        finish(icon, restart=True)

    def finish(icon, restart=False):
        state["restart"], state["quitting"] = restart, True
        icon.stop()
        if window is not None:
            window.destroy()

    menu = pystray.Menu(
        pystray.MenuItem("Open Builder", lambda i, it: open_page(), default=True),
        pystray.MenuItem("Connect apps && Claude…", lambda i, it: open_page("#connect")),
        pystray.MenuItem("Settings…", lambda i, it: open_page("#settings")),
        pystray.MenuItem("Copy address", copy_address),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Settings file (builder.env)", edit_settings_file),
        pystray.MenuItem("Open data folder", lambda i, it: open_path(data_dir())),
        pystray.MenuItem("Open log", lambda i, it: open_path(data_dir() / "logs" / "builder.log")),
        pystray.MenuItem("Start with Windows", toggle_autostart, checked=lambda it: autostart_enabled(),
                         visible=IS_WINDOWS),
        pystray.MenuItem("Let other devices on my network connect", toggle_network, checked=network_on),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Restart", lambda i, it: finish(i, restart=True)),
        pystray.MenuItem("Quit Builder", lambda i, it: finish(i)),
    )
    icon = pystray.Icon("Builder", icon_image(), f"Builder {__version__} · {server.url}", menu)
    if window is not None:
        import webview

        icon.run_detached()  # the window's event loop owns the main thread
        webview.start(private_mode=False, storage_path=str(data_dir() / "webview"))
        if not state["quitting"]:
            icon.stop()
    else:
        icon.run()
    api.DESKTOP.pop("show", None)
    server.stop()
    if state["restart"]:
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

    if FROZEN and IS_WINDOWS:
        from . import remote

        check(remote.cloudflared_bin() is not None, "cloudflared (remote access)")
        check(window_available(), "app window (pywebview)")

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
    os.environ["BUILDER_PORT"] = str(port)  # remote access tunnels to this port

    if builder_at(port):
        # Already running: bring its window forward (or open it in the browser).
        if not args.no_browser and not _show_running(port):
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
    show = not args.no_browser or first_run

    if args.headless:
        if show:
            webbrowser.open(server.url)
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
        if show:
            webbrowser.open(server.url)
        try:
            while server.thread.is_alive():
                server.thread.join(1)
        except KeyboardInterrupt:
            server.stop()
        return 0
    run_desktop(server, argv, show)
    return 0


def _show_running(port: int) -> bool:
    req = urllib.request.Request(f"http://127.0.0.1:{port}/_desktop/show", data=b"", method="POST")
    try:
        with urllib.request.urlopen(req, timeout=3) as r:
            return r.status == 204
    except Exception:  # noqa: BLE001
        return False


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
