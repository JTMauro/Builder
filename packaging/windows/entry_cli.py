"""builder-cli.exe: Builder's command line (console): mcp, serve, token, user add, doctor, app --selftest."""
import multiprocessing

from media_backend.cli import main

if __name__ == "__main__":
    multiprocessing.freeze_support()
    main()
