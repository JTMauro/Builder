"""Builder.exe: the desktop server with a tray icon (windowed, no console)."""
import multiprocessing
import sys

from media_backend.winapp import main

if __name__ == "__main__":
    multiprocessing.freeze_support()
    sys.exit(main())
