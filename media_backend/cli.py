"""Command line: `builder serve | mcp | chat | setup | doctor | token | user`.

(`media-backend` is the same command under its old name.)
"""

from __future__ import annotations

import argparse
import getpass
import os
import shutil
import subprocess
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="builder", description="Builder: Craft builds, an audio/video workshop and Rivet")
    sub = parser.add_subparsers(dest="cmd", required=True)
    serve = sub.add_parser("serve", help="Run the web app and HTTP API (with the MCP connector at /mcp)")
    serve.add_argument("--host", default="127.0.0.1", help="127.0.0.1 for this computer only; 0.0.0.0 to let other devices in")
    serve.add_argument("--port", type=int, default=8000)
    sub.add_parser("mcp", help="Run the MCP connector over stdio (what `claude mcp add` starts)")
    chat = sub.add_parser("chat", help="Talk to Rivet in the terminal")
    chat.add_argument("message", nargs="*")
    setup = sub.add_parser("setup", help="Get this computer ready: build or install the Craft engine, check ffmpeg and the AI key")
    setup.add_argument("--engine-bin", type=Path, help="Use this craft-anything-cli program instead of building engine/")
    setup.add_argument("--skip-engine", action="store_true", help="Don't build or install the engine")
    setup.add_argument("--debug", action="store_true", help="Faster, unoptimised engine build")
    sub.add_parser("doctor", help="Show what's ready and what's missing")
    token = sub.add_parser("token", help="Make a personal access token for another app or Claude Code")
    token.add_argument("--user", required=True, help="The account the token acts as")
    token.add_argument("--name", default="Another app", help="What the token is for")
    user = sub.add_parser("user", help="Add an account without the web app")
    user.add_argument("action", choices=["add"])
    user.add_argument("username")
    user.add_argument("--display-name", default="")
    user.add_argument("--admin", action="store_true")
    args = parser.parse_args(argv)

    if args.cmd == "serve":
        import uvicorn

        from .api import create_app

        shown = "127.0.0.1" if args.host in ("0.0.0.0", "::") else args.host
        print(f"Builder is open at http://{shown}:{args.port}/", flush=True)
        uvicorn.run(create_app(), host=args.host, port=args.port)
    elif args.cmd == "mcp":
        from .mcp_server import run_stdio

        run_stdio()
    elif args.cmd == "chat":
        _chat(args)
    elif args.cmd == "setup":
        sys.exit(_setup(args))
    elif args.cmd == "doctor":
        sys.exit(_doctor())
    elif args.cmd == "token":
        sys.exit(_token(args))
    elif args.cmd == "user":
        sys.exit(_user_add(args))


def _chat(args) -> None:
    from .assistant import get_assistant
    from .config import get_settings

    bot, session = get_assistant(), None
    name = get_settings().assistant_name
    owner = os.environ.get("BUILDER_USER", "local")
    first = " ".join(args.message).strip()
    while True:
        msg = first or input("you> ").strip()
        first = ""
        if msg in ("exit", "quit", ""):
            return
        try:
            res = bot.chat(msg, session_id=session, owner=owner)
        except Exception as e:  # noqa: BLE001
            print(f"error: {e}")
            return
        session = res.session_id
        print(f"{name}> {res.reply}")
        for f in res.new_files:
            print(f"   new file {f.id}  {f.name}")


def _ok(flag: bool, text: str) -> bool:
    print(f"  {'✓' if flag else '✗'} {text}")
    return flag


def _setup(args) -> int:
    from . import craft
    from .config import get_settings

    s = get_settings()
    s.bin_dir.mkdir(parents=True, exist_ok=True)
    print(f"Builder's home: {s.data_dir}")
    ok = _ok(bool(shutil.which(s.ffmpeg) and shutil.which(s.ffprobe)), "ffmpeg and ffprobe")
    if not ok:
        print("    Install ffmpeg (macOS: brew install ffmpeg · Ubuntu: sudo apt install ffmpeg · Windows: winget install ffmpeg)")
    dest = s.bin_dir / "craft-anything-cli"
    if args.skip_engine:
        print("  - engine skipped")
    elif args.engine_bin:
        shutil.copy2(args.engine_bin, dest)
        dest.chmod(0o755)
        _ok(True, f"engine installed from {args.engine_bin}")
    elif not (craft.ENGINE_DIR / "Cargo.toml").exists():
        ok = _ok(False, "engine source (engine/) not found; pass --engine-bin")
    elif not shutil.which("cargo"):
        ok = _ok(False, "Rust isn't installed: get it from https://rustup.rs, then run `builder setup` again")
    else:
        profile = "debug" if args.debug else "release"
        print(f"  … building the Craft engine ({profile}); the first build takes several minutes")
        cmd = ["cargo", "build", "-p", "craft-anything-cli"] + ([] if args.debug else ["--release"])
        target = Path(os.environ.get("CARGO_TARGET_DIR") or craft.ENGINE_DIR / "target")
        built = subprocess.run(cmd, cwd=craft.ENGINE_DIR, env={**os.environ, "CARGO_TARGET_DIR": str(target)})
        if built.returncode != 0:
            ok = _ok(False, "the engine didn't build; see the messages above")
        else:
            shutil.copy2(target / profile / "craft-anything-cli", dest)
            dest.chmod(0o755)
            _ok(True, f"engine installed in {dest}")
    if not _ok(bool(os.environ.get("ANTHROPIC_API_KEY")), "ANTHROPIC_API_KEY (Rivet and Craft builds)"):
        print("    Set it where Builder runs, e.g. export ANTHROPIC_API_KEY=... (Craft can also use Claude Code if it's installed)")
    print("\nNext:\n  builder serve                       # then open http://127.0.0.1:8000/")
    print("  claude mcp add builder -s user -e BUILDER_USER=<your username> -- builder mcp   # every Claude Code project")
    return 0 if ok else 1


def _doctor() -> int:
    from . import craft
    from .config import get_settings
    from .users import get_store

    s = get_settings()
    print(f"Builder's home: {s.data_dir}")
    good = _ok(bool(shutil.which(s.ffmpeg)), "ffmpeg")
    try:
        exe = craft.engine_bin()
        _ok(True, f"Craft engine: {craft.engine_version() or exe}")
    except craft.EngineMissing:
        good = _ok(False, "Craft engine: not installed (run `builder setup`)") and good
    _ok(bool(os.environ.get("ANTHROPIC_API_KEY")), "ANTHROPIC_API_KEY set")
    users = get_store().count()
    _ok(users > 0 or not s.auth_enabled, f"accounts: {users}" + ("" if users else " (the web app asks for the first one)"))
    print(f"  · sign-in: {'on' if s.auth_enabled else 'off'} · Claude Code identity (BUILDER_USER): {os.environ.get('BUILDER_USER', 'claude-code')}")
    return 0 if good else 1


def _token(args) -> int:
    from .users import get_store

    store = get_store()
    user = next((u for u in store.list_users() if u.username.lower() == args.user.lower()), None)
    if user is None:
        print(f"No account called {args.user}. Make one in the web app or with `builder user add {args.user}`.")
        return 1
    _, token = store.new_api_token(user.id, args.name)
    print(token)
    print(f"(acts as {user.username}; shown once; revoke it in the web app under Connect Claude Code)", file=sys.stderr)
    return 0


def _user_add(args) -> int:
    from .users import AuthError, get_store

    store = get_store()
    password = getpass.getpass(f"Password for {args.username} (8+ characters): ")
    try:
        u = store.create_user(args.username, password, args.display_name, is_admin=args.admin or store.count() == 0)
    except AuthError as e:
        print(e)
        return 1
    print(f"Added {u.username}{' (admin)' if u.is_admin else ''}.")
    return 0


if __name__ == "__main__":
    main()
