# -*- coding: utf-8 -*-
"""pyMFT: lightweight Windows Media Foundation Transform wrapper for Python."""

import ctypes
import os
import sys
from pathlib import Path


def _find_dll() -> str:
    """Locate pyMFT.dll in development or PyInstaller packaged layouts."""
    candidates = []

    # 1) Same directory as this package (e.g. copied next to pyMFT python folder)
    here = Path(__file__).resolve().parent
    candidates.append(here / "pyMFT.dll")

    # 2) PyInstaller one-file extraction folder
    base = getattr(sys, "_MEIPASS", None)
    if base:
        candidates.append(Path(base) / "pyMFT.dll")
        candidates.append(Path(base) / "_internal" / "pyMFT.dll")

    # 3) Development build output
    repo_root = here.parent.parent
    candidates.append(repo_root / "build" / "bin" / "Release" / "pyMFT.dll")
    candidates.append(repo_root / "build" / "bin" / "Debug" / "pyMFT.dll")

    # 4) PyInstaller _internal next to executable
    exe_dir = Path(sys.executable).resolve().parent
    candidates.append(exe_dir / "pyMFT.dll")
    candidates.append(exe_dir / "_internal" / "pyMFT.dll")

    for cand in candidates:
        if cand.exists():
            return str(cand)

    raise FileNotFoundError("pyMFT.dll not found; searched: " + ", ".join(str(c) for c in candidates))


class _DecoderConfig(ctypes.Structure):
    _fields_ = [
        ("width", ctypes.c_int),
        ("height", ctypes.c_int),
        ("enable_hardware", ctypes.c_int),
        ("prefer_igpu", ctypes.c_int),
    ]


# Load the DLL once at import time.
_DLL_PATH = _find_dll()
_dll = ctypes.CDLL(_DLL_PATH, winmode=0)

# Global init/shutdown
_dll.pyMFT_init.restype = ctypes.c_int
_dll.pyMFT_init.argtypes = []
_dll.pyMFT_shutdown.restype = None
_dll.pyMFT_shutdown.argtypes = []
_dll.pyMFT_get_last_error.restype = ctypes.c_char_p
_dll.pyMFT_get_last_error.argtypes = []

# Decoder
_dll.pyMFT_decoder_create.restype = ctypes.c_void_p
_dll.pyMFT_decoder_create.argtypes = [ctypes.POINTER(_DecoderConfig)]
_dll.pyMFT_decoder_destroy.restype = None
_dll.pyMFT_decoder_destroy.argtypes = [ctypes.c_void_p]
_dll.pyMFT_decoder_feed.restype = ctypes.c_int
_dll.pyMFT_decoder_feed.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint8), ctypes.c_int]
_dll.pyMFT_decoder_read.restype = ctypes.c_int
_dll.pyMFT_decoder_read.argtypes = [
    ctypes.c_void_p,
    ctypes.POINTER(ctypes.c_uint8),
    ctypes.c_int,
    ctypes.POINTER(ctypes.c_int),
    ctypes.POINTER(ctypes.c_int),
    ctypes.c_int,
]
_dll.pyMFT_decoder_is_hardware.restype = ctypes.c_int
_dll.pyMFT_decoder_is_hardware.argtypes = [ctypes.c_void_p]


def _err() -> str:
    msg = _dll.pyMFT_get_last_error()
    return msg.decode("utf-8", "ignore") if msg else "unknown error"


_init_count = 0


def init() -> None:
    """Initialize COM and Media Foundation. Safe to call multiple times (ref-counted)."""
    global _init_count
    rc = _dll.pyMFT_init()
    if rc != 0:
        raise RuntimeError(f"pyMFT_init failed: {_err()}")
    _init_count += 1


def shutdown() -> None:
    """Shutdown COM and Media Foundation when the last caller finishes."""
    global _init_count
    if _init_count <= 0:
        return
    _dll.pyMFT_shutdown()
    _init_count -= 1
