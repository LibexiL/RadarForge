"""Used by the installers: exits 0 if this Python can run RadarForge, 1 if not."""
import struct
import sys

ok = sys.version_info >= (3, 10) and struct.calcsize("P") == 8
print(f"Python {sys.version.split()[0]} ({struct.calcsize('P') * 8}-bit) at {sys.executable}")
if not ok:
    print("RadarForge needs 64-bit Python 3.10 or newer.")
sys.exit(0 if ok else 1)
