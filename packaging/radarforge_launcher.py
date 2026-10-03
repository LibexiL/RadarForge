"""Entry point for the packaged app (PyInstaller): the Windows installer and the Linux AppImage."""
import multiprocessing
import sys

from radarforge.app import main

if __name__ == "__main__":
    multiprocessing.freeze_support()      # radar decoding uses worker processes
    sys.exit(main())
