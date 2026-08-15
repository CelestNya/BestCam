"""verify_res_match.py — Check BestCam shared memory header + per-resolution camera read.

Usage: python verify_res_match.py [python-exe-with-cv2]
Reads the shared memory header (companion's current output), then opens the
virtual camera at each supported resolution and inspects the received frame
for color correctness (garbled NV12 shows as strongly color-shifted means).
"""

import ctypes
import struct
import sys
import time

import cv2
import numpy as np

SHARED_MEM_NAME = "Global\\BestCam_SharedMem"
HEADER_SIZE = 24


def read_header():
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    h = k32.OpenFileMappingW(0x0002 | 0x0004, False, SHARED_MEM_NAME)
    if not h:
        return None
    try:
        ptr = k32.MapViewOfFile(h, 0x0002 | 0x0004, 0, 0, HEADER_SIZE)
        if not ptr:
            return None
        try:
            raw = ctypes.string_at(ptr, HEADER_SIZE)
            w, hh, stride, fsize, fidx = struct.unpack("<4IQ", raw)
            return w, hh, stride, fsize, fidx
        finally:
            k32.UnmapViewOfFile(ctypes.c_void_p(ptr))
    finally:
        k32.CloseHandle(h)


def check_res(w, h, tag):
    cap = cv2.VideoCapture(0, cv2.CAP_DSHOW)
    if not cap.isOpened():
        print(f"[{tag}] request {w}x{h}: FAILED to open")
        cap.release()
        return
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, w)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, h)
    # Give the driver a moment to renegotiate the media type
    time.sleep(1.0)
    ok = False
    for _ in range(10):
        ret, frame = cap.read()
        if ret:
            ok = True
            break
    cap.release()
    if not ok:
        print(f"[{tag}] request {w}x{h}: NO FRAME")
        return
    hdr = read_header()
    hdr_str = f"header={hdr[0]}x{hdr[1]}" if hdr else "header=?"
    b = frame[:, :, 0].mean()
    g = frame[:, :, 1].mean()
    r = frame[:, :, 2].mean()
    std = frame.std()
    print(
        f"[{tag}] request {w}x{h} -> got {frame.shape[1]}x{frame.shape[0]} "
        f"| {hdr_str} | BGR=({b:.0f},{g:.0f},{r:.0f}) std={std:.1f}"
    )


def main():
    hdr = read_header()
    print("Shared memory header:", hdr)
    if hdr:
        print(f"  companion output = {hdr[0]}x{hdr[1]} frameSize={hdr[3]}")
    print()
    res = [(1920, 1080), (1280, 720), (800, 450), (640, 480)]
    for w, h in res:
        check_res(w, h, "MATCH " if hdr and hdr[0] == w and hdr[1] == h else "MISMATCH")


if __name__ == "__main__":
    main()
