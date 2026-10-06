"""How much memory RadarForge is using: this process plus its decoder processes (Settings → Performance)."""
from __future__ import annotations

import os
import sys


def rss_bytes(pid: int | None = None) -> int:
    """Resident memory of a process (this one by default), in bytes; 0 when it can't be read."""
    pid = pid or os.getpid()
    if sys.platform.startswith("linux"):
        try:
            with open(f"/proc/{pid}/status", encoding="ascii", errors="replace") as fh:
                for line in fh:
                    if line.startswith("VmRSS:"):
                        return int(line.split()[1]) * 1024
        except (OSError, ValueError):
            pass
        return 0
    if sys.platform == "win32":
        try:
            import ctypes
            from ctypes import wintypes

            class _Counters(ctypes.Structure):
                _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                            ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                            ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t)]
            k32 = ctypes.windll.kernel32
            k32.OpenProcess.restype = wintypes.HANDLE
            k32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
            k32.GetCurrentProcess.restype = wintypes.HANDLE
            k32.K32GetProcessMemoryInfo.argtypes = (wintypes.HANDLE, ctypes.POINTER(_Counters), wintypes.DWORD)
            k32.CloseHandle.argtypes = (wintypes.HANDLE,)
            own = pid == os.getpid()
            h = k32.GetCurrentProcess() if own else k32.OpenProcess(0x1000 | 0x0010, False, pid)
            if not h:
                return 0
            try:
                c = _Counters()
                c.cb = ctypes.sizeof(_Counters)
                if k32.K32GetProcessMemoryInfo(h, ctypes.byref(c), c.cb):
                    return int(c.WorkingSetSize)
            finally:
                if not own:
                    k32.CloseHandle(h)
        except Exception:
            pass
    return 0


def decoder_pids() -> list:
    """Process ids of the running decoder processes."""
    from .data import level2
    pids = []
    for pool in (level2._proc_pool, level2._l3_pool):
        procs = getattr(pool, "_processes", None) or {}
        pids.extend(p.pid for p in list(procs.values()) if p.pid)
    return pids


def usage() -> tuple:
    """(bytes used by this process, [bytes used by each decoder process])."""
    return rss_bytes(), [rss_bytes(p) for p in decoder_pids()]


def describe() -> str:
    main, workers = usage()
    if not main:
        return ""
    text = f"{main / 1e6:,.0f} MB"
    workers = [w for w in workers if w]
    if workers:
        text += f", plus {sum(workers) / 1e6:,.0f} MB in {len(workers)} decoder process" + \
            ("es" if len(workers) != 1 else "")
    return text
