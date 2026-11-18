"""Remote access: give this computer's Builder a public https address through Cloudflare Tunnel, so
Claude chats anywhere (claude.ai, Claude Code on the web, the desktop and mobile apps) and apps on other
networks can reach it. Sign-in and personal tokens still apply to every request.

Two modes (BUILDER_REMOTE in builder.env):
  quick   no account needed; Cloudflare gives a random https://<words>.trycloudflare.com address that
          changes each time Builder starts (fine for trying it, connectors must be updated after a restart)
  named   a tunnel made in the Cloudflare dashboard (Zero Trust > Networks > Tunnels): paste its token
          (BUILDER_TUNNEL_TOKEN) and the public hostname you gave it (BUILDER_PUBLIC_URL); the address
          never changes
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import threading
import time

from .config import FROZEN, NO_WINDOW, app_dir, exe_name, get_settings

MODES = ("off", "quick", "named")
_URL = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")


class RemoteError(RuntimeError):
    pass


def cloudflared_bin() -> str | None:
    name = exe_name("cloudflared")
    places = []
    if FROZEN:
        places += [app_dir() / "cloudflared" / name, app_dir() / name]
    places.append(get_settings().bin_dir / name)
    for p in places:
        if p.is_file():
            return str(p)
    return shutil.which("cloudflared")


class Tunnel:
    def __init__(self) -> None:
        self.proc: subprocess.Popen | None = None
        self.mode = "off"
        self.url = ""
        self.error = ""
        self.lock = threading.Lock()

    # ------------------------------------------------------------ status

    def status(self) -> dict:
        running = self.proc is not None and self.proc.poll() is None
        if self.mode != "off" and not running and not self.error and self.proc is not None:
            self.error = "The tunnel stopped. Check the log, or turn remote access off and on again."
        return {"mode": self.mode, "running": running, "url": self.url if running else "", "error": self.error,
                "available": cloudflared_bin() is not None}

    # ------------------------------------------------------------ start / stop

    def start(self, mode: str, port: int, token: str = "", public_url: str = "", wait: float = 30.0) -> dict:
        if mode not in MODES:
            raise RemoteError(f"Unknown mode {mode!r}")
        self.stop()
        if mode == "off":
            return self.status()
        exe = cloudflared_bin()
        if not exe:
            raise RemoteError("cloudflared isn't installed. The Windows app includes it; elsewhere install it from "
                              "https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/")
        if mode == "named":
            if not token or not public_url.startswith("https://"):
                raise RemoteError("A named tunnel needs its token and its public address (https://...).")
            # The token goes through the environment, never the command line (other programs can read that).
            args = [exe, "tunnel", "--no-autoupdate", "run"]
            env = {**os.environ, "TUNNEL_TOKEN": token}
        else:
            args = [exe, "tunnel", "--no-autoupdate", "--url", f"http://127.0.0.1:{port}"]
            env = dict(os.environ)
        with self.lock:
            self.mode, self.error = mode, ""
            self.url = public_url.rstrip("/") if mode == "named" else ""
            self.proc = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                         text=True, env=env, **NO_WINDOW)
        found = threading.Event()
        threading.Thread(target=self._read, args=(self.proc, found), daemon=True, name="cloudflared").start()
        if mode == "quick":
            if not found.wait(wait):
                self.stop()
                raise RemoteError("Cloudflare didn't give an address in time. Check the internet connection and try again.")
        else:
            time.sleep(min(wait, 3.0))
            if self.proc.poll() is not None:
                err = self.error or "the tunnel stopped"
                self.stop()
                raise RemoteError(f"The named tunnel didn't start: {err}")
        return self.status()

    def _read(self, proc: subprocess.Popen, found: threading.Event) -> None:
        log = get_settings().data_dir / "logs"
        log.mkdir(parents=True, exist_ok=True)
        with open(log / "tunnel.log", "a", encoding="utf-8") as f:
            assert proc.stdout is not None
            for line in proc.stdout:
                f.write(line)
                f.flush()
                if self.mode == "quick" and not self.url:
                    m = _URL.search(line)
                    if m:
                        self.url = m.group(0)
                        found.set()
                if " ERR " in line and not self.error:
                    self.error = line.split(" ERR ", 1)[1].strip()[:300]

    def stop(self) -> None:
        with self.lock:
            p, self.proc = self.proc, None
            self.mode, self.url = "off", ""
        if p and p.poll() is None:
            p.terminate()
            try:
                p.wait(10)
            except subprocess.TimeoutExpired:
                p.kill()


_tunnel: Tunnel | None = None


def get_tunnel() -> Tunnel:
    global _tunnel
    if _tunnel is None:
        _tunnel = Tunnel()
    return _tunnel


def public_url(request_base: str = "") -> str:
    """The address people elsewhere should use: the tunnel's, else BUILDER_PUBLIC_URL, else ''."""
    st = get_tunnel().status()
    return st["url"] or os.environ.get("BUILDER_PUBLIC_URL", "").rstrip("/")


def connector_path(token: str) -> str:
    """The MCP address for Claude connectors, which can't send headers: the token is part of the path."""
    return f"/c/{token}/mcp/"

