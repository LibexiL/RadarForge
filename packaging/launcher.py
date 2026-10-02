"""The program a packaged (PyInstaller) build starts. `python -m radarforge` is the same thing for a source install."""
import multiprocessing
import sys

if __name__ == "__main__":
    multiprocessing.freeze_support()        # the decoding workers are started from this same program
    from radarforge.app import main
    sys.exit(main())
