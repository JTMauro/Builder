"""Command line entry point: `media-backend serve` or `media-backend mcp`."""

from __future__ import annotations

import argparse


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="media-backend", description="Audio/video backend with a Claude assistant")
    sub = parser.add_subparsers(dest="cmd", required=True)
    serve = sub.add_parser("serve", help="Run the HTTP API (with the MCP connector at /mcp)")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    sub.add_parser("mcp", help="Run the MCP connector over stdio (for `claude mcp add`)")
    chat = sub.add_parser("chat", help="Talk to the built-in assistant in the terminal")
    chat.add_argument("message", nargs="*")
    args = parser.parse_args(argv)

    if args.cmd == "serve":
        import uvicorn

        from .api import create_app

        uvicorn.run(create_app(), host=args.host, port=args.port)
    elif args.cmd == "mcp":
        from .mcp_server import run_stdio

        run_stdio()
    elif args.cmd == "chat":
        from .assistant import get_assistant
        from .config import get_settings

        bot, session = get_assistant(), None
        name = get_settings().assistant_name
        first = " ".join(args.message).strip()
        while True:
            msg = first or input("you> ").strip()
            first = ""
            if msg in ("exit", "quit", ""):
                return
            try:
                res = bot.chat(msg, session_id=session)
            except Exception as e:  # noqa: BLE001
                print(f"error: {e}")
                return
            session = res.session_id
            print(f"{name}> {res.reply}")
            for f in res.new_files:
                print(f"   new file {f.id}  {f.name}")


if __name__ == "__main__":
    main()
